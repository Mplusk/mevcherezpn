"""Probe server-side routing through the dedicated monitoring account.

The private profile is read only into memory and sent to Xray over stdin. The
script never prints URLs, profile contents, subprocess output, or credentials.
The test does not change panel settings or send email. It does not exercise
Android/Hiddify client-side routing or measure access from a Russian operator.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time

MONITOR = Path(__file__).resolve().parent.parent / "monitor"
if not (MONITOR / "vpn_monitor.py").is_file():
    MONITOR = Path("/opt/gamelife-vpn-monitor")
sys.path.insert(0, str(MONITOR))
import vpn_monitor  # noqa: E402

BLOCKED_SITE = "https://agentura.ru/"
RU_SITE = "https://ya.ru/"
IP_SITES = ("https://api.ipify.org/", "https://icanhazip.com/")
CREDENTIALS = Path("/etc/gamelife-vpn-monitor/credentials.json")
XRAY = "/opt/gamelife-vpn-monitor/xray"


def read_profiles(path=CREDENTIALS):
    """Do not follow symlinks or accept a world/group-readable credential file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("unsafe monitoring credentials")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            credentials = json.load(stream)
        return vpn_monitor.validate_profiles(credentials["MONITOR_PROFILE_JSON"])
    finally:
        if fd >= 0:
            os.close(fd)


def clean_environment():
    # Do not pass monitoring credentials or configured system proxies to curl
    # or Xray; an explicit SOCKS address is used for every tunnel request.
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C", "HOME": "/nonexistent"}


def curl(url, *, port=None, ip=False):
    if url not in (BLOCKED_SITE, RU_SITE, *IP_SITES):
        raise ValueError("unexpected probe target")
    command = ["curl", "--silent", "--show-error", "--noproxy", "", "--proto", "=https",
               "--connect-timeout", "8", "--max-time", "15", "--max-redirs", "0"]
    if port is None:
        command += ["--proxy", ""]
    else:
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("invalid SOCKS port")
        command += ["--socks5-hostname", "127.0.0.1:" + str(port)]
    if ip:
        command += ["--max-filesize", "128"]
    else:
        command += ["--output", os.devnull, "--write-out", "%{http_code}"]
    command += ["--", url]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=20, env=clean_environment(), check=False)
        output = result.stdout.decode("ascii").strip()
        if ip:
            return str(ipaddress.ip_address(output)) if result.returncode == 0 else None
        return int(output) if result.returncode == 0 and len(output) == 3 and output.isdigit() else None
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return None


def reachable(status):
    return isinstance(status, int) and 200 <= status < 400


def site_status(status):
    if status is None:
        return "connection_failed"
    return "reachable" if reachable(status) else "http_error"


def probe(outbound, expected_ip, xray=XRAY):
    """A missing site response is inconclusive until checked on the exit VPS."""
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    config = {"log": {"loglevel": "none"},
              "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks",
                            "settings": {"auth": "noauth", "udp": False}}],
              "outbounds": [outbound]}
    process = None
    try:
        process = subprocess.Popen([xray, "run", "-c", "stdin:"], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=clean_environment())
        process.stdin.write(json.dumps(config).encode("utf-8"))
        process.stdin.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return {"exit": "failed", "blocked_site": "not_checked", "ru_site": "not_checked"}
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.3):
                    break
            except OSError:
                time.sleep(.1)
        else:
            return {"exit": "failed", "blocked_site": "not_checked", "ru_site": "not_checked"}
        valid_exit = any(curl(url, port=port, ip=True) == expected_ip for url in IP_SITES)
        if not valid_exit:
            return {"exit": "failed", "blocked_site": "not_checked", "ru_site": "not_checked"}
        blocked = curl(BLOCKED_SITE, port=port)
        russian = curl(RU_SITE, port=port)
        return {"exit": "ok", "blocked_site": site_status(blocked), "ru_site": site_status(russian)}
    except (OSError, ValueError, TypeError):
        return {"exit": "failed", "blocked_site": "not_checked", "ru_site": "not_checked"}
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def run(profiles, xray=XRAY):
    # Direct tests are from this monitoring machine, not from each exit VPS.
    direct = {"blocked_site": "reachable" if reachable(curl(BLOCKED_SITE)) else "unreachable",
              "ru_site": "reachable" if reachable(curl(RU_SITE)) else "unreachable"}
    nodes = {name: probe(profiles[name], expected, xray=xray)
             for name, (_, expected) in vpn_monitor.NODES.items()}
    return {"direct_from_monitor": direct, "nodes": nodes}


def main():
    try:
        result = run(read_profiles())
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        # Each exit must work; both reference sites must first respond directly
        # from the monitor. A denied RU site is observational, not proof of the
        # guard, because an upstream issue can also cause a failed connection.
        ok = (all(item == "reachable" for item in result["direct_from_monitor"].values())
              and all(item["exit"] == "ok" and item["blocked_site"] == "reachable"
                      and item["ru_site"] == "connection_failed" for item in result["nodes"].values()))
        return 0 if ok else 1
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        print("ROUTING_PROBE_ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())
