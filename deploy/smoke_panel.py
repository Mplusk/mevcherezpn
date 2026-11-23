"""Exercise the Hiddify user API against the disposable CI container."""

import http.client
import json
import re
import socket
import ssl
import subprocess
import time
import uuid
from urllib.parse import urlsplit


class LocalPanelConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection(("127.0.0.1", 28443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def main():
    result = subprocess.run(
        ["docker", "exec", "gamelife-hiddify-panel-1", "hiddify", "admin"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    admin_url = None
    for candidate in re.findall(r"https?://[^\s<>\"']+", output):
        parsed = urlsplit(candidate.rstrip(".,);"))
        if not parsed.hostname:
            continue
        segments = parsed.path.strip("/").split("/")
        for index, segment in enumerate(segments):
            try:
                admin_uuid = str(uuid.UUID(segment))
            except ValueError:
                continue
            if index:
                admin_url = (parsed.hostname, segments[index - 1], admin_uuid)
                break
        if admin_url:
            break
    if not admin_url:
        raise RuntimeError("No usable admin link returned by the disposable panel")

    hostname, admin_path, admin_key = admin_url
    base = f"/{admin_path}/api/v2/admin/user/"
    origin_ip = subprocess.run(
        [
            "docker", "inspect", "gamelife-hiddify-panel-1",
            "--format", '{{with index .NetworkSettings.Networks "vpn-ci-proxy"}}{{.IPAddress}}{{end}}',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    if not origin_ip:
        raise RuntimeError("Disposable panel is not connected to the proxy network")
    deadline = time.monotonic() + 120
    while True:
        http_origin = http.client.HTTPConnection(origin_ip, 80, timeout=10)
        try:
            http_origin.request(
                "GET",
                f"/{admin_path}/api/v2/panel/info/",
                headers={
                    "Host": hostname,
                    "Hiddify-API-Key": admin_key,
                    "X-Forwarded-Proto": "https",
                },
            )
            origin_response = http_origin.getresponse()
            origin_response.read()
            status = origin_response.status
        except (OSError, http.client.HTTPException):
            status = None
        finally:
            http_origin.close()
        if status == 200:
            break
        if status not in (None, 502, 503, 504) or time.monotonic() >= deadline:
            raise RuntimeError(f"Panel HTTP origin did not become ready (HTTP {status})")
        time.sleep(3)
    connection = LocalPanelConnection(
        hostname,
        timeout=30,
        context=ssl._create_unverified_context(),
    )

    def request(method, path, body=None, key=admin_key):
        headers = {"Hiddify-API-Key": key, "Host": hostname}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        data = response.read()
        if response.status >= 400:
            raise RuntimeError(f"Panel {method} returned HTTP {response.status}")
        return json.loads(data)

    settings = request("GET", f"/{admin_path}/api/v2/admin/settings/")
    client_paths = [
        field["value"]
        for category in settings.get("categories", [])
        for field in category.get("fields", [])
        if field.get("key") == "proxy_path_client"
    ]
    if len(client_paths) != 1 or not client_paths[0]:
        raise RuntimeError("Panel did not return a client proxy path")
    domain = request(
        "POST",
        f"/{admin_path}/api/v2/admin/domains/",
        {"domain": "vpn.example.invalid", "mode": "cdn"},
    )
    if domain.get("domain") != "vpn.example.invalid":
        raise RuntimeError("Panel returned unexpected domain state")
    protocols = request(
        "PUT", f"/{admin_path}/api/v2/admin/protocols/",
        {"values": {"vless_enable": True, "ws_enable": True}},
    )
    enabled = {item["key"]: item["enabled"] for item in protocols.get("items", [])}
    if not enabled.get("vless_enable") or not enabled.get("ws_enable"):
        raise RuntimeError("Panel did not enable the required VPN protocol switches")
    agent_id = str(uuid.uuid4())
    agent = request(
        "POST",
        f"/{admin_path}/api/v2/admin/admin_user/",
        {
            "uuid": agent_id,
            "name": "ci-agent",
            "mode": "agent",
            "can_add_admin": False,
            "lang": "ru",
            "capabilities": {
                "add_sub_admins": False,
                "override_preferred_outbound": False,
                "add_extra_sublinks": False,
                "modify_user_extra": False,
                "modify_domains": False,
                "modify_outbounds": False,
            },
        },
    )
    if agent.get("uuid") != agent_id or agent.get("mode") != "agent":
        raise RuntimeError("Panel returned unexpected API administrator state")
    user_id = str(uuid.uuid4())
    user = request(
        "POST",
        base,
        {
            "uuid": user_id,
            "name": "ci-smoke",
            "usage_limit_GB": 1,
            "package_days": 2,
            "mode": "no_reset",
            "enable": True,
        },
        key=agent_id,
    )
    if user.get("uuid") != user_id or user.get("enable") is not True:
        raise RuntimeError("Panel returned unexpected user creation state")
    http_origin = http.client.HTTPConnection(origin_ip, 80, timeout=30)
    http_origin.request(
        "GET",
        f"/{client_paths[0]}/{user_id}/",
        headers={"Host": hostname, "X-Forwarded-Proto": "https"},
    )
    profile = http_origin.getresponse()
    profile.read()
    if profile.status not in (200, 302):
        raise RuntimeError(f"Panel profile returned HTTP {profile.status}")
    http_origin.close()
    http_origin = http.client.HTTPConnection(origin_ip, 80, timeout=30)
    http_origin.request(
        "GET",
        f"/{client_paths[0]}/{user_id}/xray/",
        headers={"Host": "vpn.example.invalid", "X-Forwarded-Proto": "https"},
    )
    config_response = http_origin.getresponse()
    config_data = config_response.read()
    if config_response.status != 200:
        raise RuntimeError(f"Panel Xray profile returned HTTP {config_response.status}")
    configs = json.loads(config_data)
    if isinstance(configs, dict):
        configs = [configs]
    matching = []
    for config in configs:
        for outbound in config.get("outbounds", []):
            stream = outbound.get("streamSettings", {})
            servers = outbound.get("settings", {}).get("vnext", [])
            if (outbound.get("protocol") == "vless" and stream.get("network") == "ws"
                    and stream.get("security") == "tls" and servers
                    and (servers[0].get("address") == "vpn.example.invalid"
                         or stream.get("tlsSettings", {}).get("serverName") == "vpn.example.invalid")):
                matching.append(outbound)
    if not matching:
        raise RuntimeError("Panel did not generate a VLESS WebSocket/TLS profile for the domain")
    http_origin.close()
    user = request("PATCH", base + user_id + "/", {"enable": False}, key=agent_id)
    if user.get("uuid") != user_id or user.get("enable") is not False:
        raise RuntimeError("Panel returned unexpected user update state")
    request("DELETE", base + user_id + "/", key=agent_id)
    request("DELETE", f"/{admin_path}/api/v2/admin/admin_user/{agent_id}/")
    connection.close()
    print("A limited Hiddify administrator created, disabled and deleted a user.")


if __name__ == "__main__":
    main()
