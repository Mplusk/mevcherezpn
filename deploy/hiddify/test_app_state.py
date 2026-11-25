"""Test an existing Hiddify 5 profile updating while the core is connected."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.request import ProxyHandler, Request, build_opener

from auto_profile import automatic_profile
from test_auto_profile import AGENT, fixture
from test_core_compatibility import MockNode, Trap, core_environment, free_ports, wait_for


def varint(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def string_field(number, value):
    raw = value.encode("utf-8")
    return varint((number << 3) | 2) + varint(len(raw)) + raw


class AppStateTests(unittest.TestCase):
    def test_profile_update_keeps_hiddify_connected(self):
        core = os.environ.get("HIDDIFY_CORE_V5")
        if not core or importlib.util.find_spec("grpc") is None:
            if os.environ.get("REQUIRE_HIDDIFY_CORE_TESTS"):
                self.fail("Hiddify 5 core and grpc are required in CI")
            self.skipTest("Hiddify 5 core and grpc not supplied")
        import grpc

        nodes = [MockNode("A", .02), MockNode("B", .08), MockNode("C", .15)]
        trap = Trap()
        for server in [*nodes, trap]:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        proc = None
        channel = None
        try:
            with tempfile.TemporaryDirectory(prefix="hiddify-app-test-") as folder:
                try:
                    api_port, mixed_port, clash_port = free_ports(3)
                    proc = subprocess.Popen(
                        [core, "grpcweb", "--listen", f"127.0.0.1:{api_port}",
                         "--mixed-port", str(mixed_port), "--workdir", folder],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        cwd=folder, env=core_environment(core),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    opener = build_opener(ProxyHandler({}))

                    def status():
                        try:
                            with opener.open(f"http://127.0.0.1:{api_port}/api/status", timeout=2) as response:
                                return json.load(response)
                        except Exception:
                            return {}

                    self.assertTrue(wait_for(status, 15), "isolated Hiddify core did not start")
                    channel = grpc.insecure_channel(f"127.0.0.1:{api_port}")
                    grpc.channel_ready_future(channel).result(timeout=10)

                    def call(method, data):
                        rpc = channel.unary_unary(
                            "/hcore.Core/" + method,
                            request_serializer=lambda value: value,
                            response_deserializer=lambda value: value,
                        )
                        return rpc(data, timeout=30)

                    def parse(data):
                        request = Request(
                            f"http://127.0.0.1:{api_port}/api/resolve",
                            data=json.dumps({"config": json.dumps(data)}).encode(),
                            headers={"Content-Type": "application/json"},
                        )
                        with opener.open(request, timeout=30) as response:
                            result = json.load(response)
                        self.assertFalse(result.get("parseError"), result.get("parseError"))
                        self.assertTrue(result.get("parsed"))
                        return result["parsed"]

                    settings = {"mixed-port": mixed_port, "clash-api-port": clash_port,
                                "enable-clash-api": True, "enable-tun": False,
                                "set-system-proxy": False, "balancer-strategy": "round-robin",
                                "connection-test-url": f"http://127.0.0.1:{trap.server_address[1]}/check",
                                "url-test-interval": 2, "log-level": "warn"}
                    call("ChangeHiddifySettings", string_field(1, json.dumps(settings)))

                    def mock_proxies(data):
                        for out in data["outbounds"]:
                            if out.get("type") != "vless":
                                continue
                            name = out["tag"].split(" ", 1)[0]
                            node = nodes[("NL", "DE", "PL").index(name)]
                            tag = out["tag"]
                            out.clear()
                            out.update({"type": "socks", "tag": tag, "server": "127.0.0.1",
                                        "server_port": node.server_address[1], "version": "5"})
                        return data

                    # The deployed profile hides the country endpoints and exposes
                    # an Auto (backup) alias, which an existing user may select.
                    original = json.loads(automatic_profile(json.dumps(fixture()), AGENT))
                    for out in original["outbounds"]:
                        if out.get("type") == "vless":
                            out["tag"] += " §hide§"
                        elif out.get("tag") == "Auto":
                            out["outbounds"] = [tag + " §hide§" for tag in out["outbounds"]]
                    original = mock_proxies(original)
                    call("Start", string_field(2, parse(original)))
                    self.assertTrue(wait_for(lambda: (status().get("core") or {}).get("coreState") == "STARTED"),
                                    "the original client profile did not connect")
                    call("SelectOutbound", string_field(1, "select") + string_field(2, "Auto (backup)"))
                    updated = mock_proxies(json.loads(automatic_profile(json.dumps(fixture()), AGENT)))
                    call("HotReload", string_field(2, parse(updated)))

                    def updated_select():
                        current = status()
                        if (current.get("core") or {}).get("coreState") != "STARTED":
                            return None
                        for group in (current.get("proxies") or {}).get("items", []):
                            if group.get("tag") == "select":
                                tags = {item.get("tag") for item in group.get("items", [])}
                                if {"Auto", "Auto (backup)", "lowest", "balance", "NL", "DE", "PL"} <= tags:
                                    return group
                        return None

                    selected = wait_for(updated_select, 25)
                    self.assertIsNotNone(selected, "Hiddify did not reconnect with the automatic profile")
                    # Some versions preserve the prior alias while others fall back
                    # to the configured default; either remains selectable.
                    self.assertIn(selected.get("selected"), {"Auto (backup)", "balance", "lowest", "Auto"})
                finally:
                    if channel:
                        channel.close()
                        channel = None
                    if proc and proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=5)
                    proc = None
        finally:
            if channel:
                channel.close()
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for server in [*nodes, trap]:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
