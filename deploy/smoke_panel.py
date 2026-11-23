"""Exercise the Hiddify user API against the disposable CI container."""

import http.client
import json
import re
import socket
import ssl
import subprocess
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
    connection = LocalPanelConnection(
        hostname,
        timeout=30,
        context=ssl._create_unverified_context(),
    )

    def request(method, path, body=None):
        headers = {"Hiddify-API-Key": admin_key, "Host": hostname}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        data = response.read()
        if response.status >= 400:
            raise RuntimeError(f"Panel {method} returned HTTP {response.status}")
        return json.loads(data)

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
    )
    if user.get("uuid") != user_id or user.get("enable") is not True:
        raise RuntimeError("Panel returned unexpected user creation state")
    user = request("PATCH", base + user_id + "/", {"enable": False})
    if user.get("uuid") != user_id or user.get("enable") is not False:
        raise RuntimeError("Panel returned unexpected user update state")
    request("DELETE", base + user_id + "/")
    connection.close()
    print("Disposable Hiddify API created, disabled and deleted a user.")


if __name__ == "__main__":
    main()
