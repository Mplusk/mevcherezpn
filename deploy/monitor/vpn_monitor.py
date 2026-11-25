"""External VPN probes. Never print credentials, URLs, client output or responses."""
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NODES = {
    "de": ("de.gamelife.shop", "2.27.75.91"),
    "pl": ("pl.gamelife.shop", "191.217.211.240"),
    "nl": ("nl1.gamelife.shop", "153.52.155.69"),
}
SUBSCRIPTION_HOSTS = {"gamelife.shop", "de.gamelife.shop", "pl.gamelife.shop", "nl1.gamelife.shop", "nl.gamelife.shop"}
IP_CHECK_URLS = ("https://api.ipify.org/", "https://icanhazip.com/")


def validate_profiles(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(NODES):
        raise ValueError("expected three monitoring profiles")
    for name, (hostname, _) in NODES.items():
        out = data[name]
        if not isinstance(out, dict) or out.get("protocol") != "vless":
            raise ValueError("invalid profile")
        targets = out.get("settings", {}).get("vnext", [])
        stream = out.get("streamSettings", {})
        if (len(targets) != 1 or targets[0].get("address") != hostname
                or stream.get("network") not in ("ws", "websocket")
                or stream.get("security") != "tls"):
            raise ValueError("unexpected profile endpoint")
    return data


def validate_subscription_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in SUBSCRIPTION_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment or not parsed.path.endswith("/xray/")):
        raise ValueError("unexpected subscription URL")
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def subscription_ok(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(validate_subscription_url(url), headers={"User-Agent": "Xray"})
    try:
        with opener.open(req, timeout=15) as response:
            if response.status != 200:
                return False
            data = json.loads(response.read(1024 * 1024 + 1))
        configs = data if isinstance(data, list) else [data]
        hosts = set()
        for config in configs:
            for out in config.get("outbounds", []):
                if out.get("protocol") == "vless":
                    for target in out.get("settings", {}).get("vnext", []):
                        hosts.add(target.get("address"))
        return {item[0] for item in NODES.values()} <= hosts
    except (OSError, ValueError, TypeError, AttributeError, urllib.error.URLError):
        return False


def probe_one(xray, outbound, expected):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = {"log": {"loglevel": "none"}, "inbounds": [{"listen": "127.0.0.1", "port": port,
              "protocol": "socks", "settings": {"auth": "noauth", "udp": False}}],
              "outbounds": [outbound]}
    env = {k: v for k, v in os.environ.items() if not k.lower().startswith("monitor_") and k.lower() not in
           ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    proc = None
    try:
        proc = subprocess.Popen([xray, "run", "-c", "stdin:"], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        proc.stdin.write(json.dumps(config).encode())
        proc.stdin.close()
        time.sleep(1)
        if proc.poll() is not None:
            return False
        for url in IP_CHECK_URLS:
            try:
                result = subprocess.run(["curl", "-fsS", "--noproxy", "", "--max-time", "15",
                                         "--socks5-hostname", f"127.0.0.1:{port}", url],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, env=env)
                if not result.returncode and ipaddress.ip_address(result.stdout.decode().strip()) == ipaddress.ip_address(expected):
                    return True
            except (ValueError, UnicodeError, subprocess.TimeoutExpired):
                pass
        return False
    except OSError:
        return False
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def retry(func, attempts=3):
    for index in range(attempts):
        if func():
            return True
        if index + 1 < attempts:
            time.sleep(3)
    return False


def main():
    statuses = {"de": "unknown", "pl": "unknown", "nl": "unknown", "subscription": "unknown"}

    def save_result():
        path = os.environ.get("MONITOR_RESULT_FILE")
        if path:
            Path(path).write_text(json.dumps(statuses, sort_keys=True), encoding="utf-8")

    try:
        profiles = validate_profiles(os.environ.pop("MONITOR_PROFILE_JSON"))
    except (KeyError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        print("MONITOR_CONFIGURATION_ERROR", flush=True)
        save_result()
        return 1
    try:
        url = validate_subscription_url(os.environ.pop("MONITOR_SUBSCRIPTION_URL"))
    except (KeyError, ValueError, TypeError):
        url = None
    ok = True
    for name, (_, expected) in NODES.items():
        started = time.monotonic()
        result = retry(lambda: probe_one(os.environ.get("XRAY_BIN", "xray"), profiles[name], expected))
        statuses[name] = "ok" if result else "failed"
        print(f"NODE_{name.upper()}={'OK' if result else 'FAILED'} check_seconds={time.monotonic() - started:.1f}", flush=True)
        ok &= result
    result = retry(lambda: subscription_ok(url)) if url else False
    statuses["subscription"] = "ok" if result else ("failed" if url else "configuration_error")
    print(f"SUBSCRIPTION={'OK' if result else ('FAILED' if url else 'CONFIGURATION_ERROR')}", flush=True)
    save_result()
    return 0 if ok and result else 1


if __name__ == "__main__":
    sys.exit(main())
