"""Exercise real Hiddify cores with synthetic local nodes when supplied by CI."""

import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from auto_profile import AUTO_ALIAS_TAG, AUTO_TAG, automatic_profile
from test_auto_profile import AGENT, fixture


def receive(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError("connection closed")
        data += chunk
    return data


class MockNode(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, name, delay):
        self.name, self.delay, self.failed, self.requests = name, delay, False, 0
        super().__init__(("127.0.0.1", 0), SocksHandler)


class SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(2)
            if self.server.failed:
                return
            hello = receive(self.request, 2)
            receive(self.request, hello[1])
            self.request.sendall(b"\x05\x00")
            req = receive(self.request, 4)
            if req[3] == 1:
                receive(self.request, 4)
            elif req[3] == 3:
                receive(self.request, receive(self.request, 1)[0])
            elif req[3] == 4:
                receive(self.request, 16)
            else:
                return
            receive(self.request, 2)
            self.request.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            buffer = b""
            while not self.server.failed:
                while b"\r\n\r\n" not in buffer and len(buffer) < 65536:
                    data = self.request.recv(4096)
                    if not data:
                        return
                    buffer += data
                request, buffer = buffer.split(b"\r\n\r\n", 1)
                time.sleep(self.server.delay)
                if self.server.failed:
                    return
                self.server.requests += 1
                close = b"connection: close" in request.lower()
                connection = b"close" if close else b"keep-alive"
                if request.startswith(b"HEAD "):
                    self.request.sendall(b"HTTP/1.1 204 No Content\r\nConnection: "
                                         + connection + b"\r\n\r\n")
                else:
                    body = json.dumps({"node": self.server.name, "ip": "192.0.2.1"}).encode()
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                         b"Connection: " + connection + b"\r\nContent-Length: "
                                         + str(len(body)).encode() + b"\r\n\r\n" + body)
                if close:
                    return
        except (OSError, EOFError):
            return


class Trap(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.requests = 0
        super().__init__(("127.0.0.1", 0), TrapHandler)


class TrapHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.requests += 1
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nDIRECT")


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def core_environment(core):
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(Path(core).resolve().parent) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def via_local_socks(port, destination_port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.settimeout(3)
            sock.sendall(b"\x05\x01\x00")
            if receive(sock, 2) != b"\x05\x00":
                return None
            sock.sendall(b"\x05\x01\x00\x01\x7f\x00\x00\x01"
                         + destination_port.to_bytes(2, "big"))
            reply = receive(sock, 4)
            if reply[1] != 0:
                return None
            if reply[3] == 1:
                receive(sock, 4)
            elif reply[3] == 4:
                receive(sock, 16)
            elif reply[3] == 3:
                receive(sock, receive(sock, 1)[0])
            else:
                return None
            receive(sock, 2)
            sock.sendall(b"GET /check HTTP/1.1\r\nHost: test.local\r\nConnection: close\r\n\r\n")
            result = b""
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                result += part
            return result.split(b"\r\n\r\n", 1)[-1]
    except (OSError, EOFError):
        return None


def wait_for(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.3)
    return None


class RealCoreCompatibilityTests(unittest.TestCase):
    def test_import_and_failover_without_direct_leak(self):
        cores = [os.environ.get("HIDDIFY_CORE_V4"), os.environ.get("HIDDIFY_CORE_V5")]
        if not all(cores):
            if os.environ.get("REQUIRE_HIDDIFY_CORE_TESTS"):
                self.fail("both Hiddify cores are required in CI")
            self.skipTest("Hiddify core binaries not supplied")
        for core in cores:
            with self.subTest(core=Path(core).name):
                self.run_core(core)

    def run_core(self, core):
        nodes = [MockNode("A", .02), MockNode("B", .15), MockNode("C", .25)]
        trap = Trap()
        for server in [*nodes, trap]:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        stop = threading.Event()
        proc = None
        try:
            with tempfile.TemporaryDirectory(prefix="hiddify-auto-test-") as folder:
                root = Path(folder)
                profile = root / "profile.json"
                profile.write_text(automatic_profile(json.dumps(fixture()), AGENT), encoding="utf-8")
                parsed = root / "parsed.json"
                subprocess.run([core, "parse", str(profile), "-o", str(parsed)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=40, check=True, cwd=folder, env=core_environment(core))
                built = root / "built.json"
                subprocess.run([core, "build", "-c", str(parsed), "-o", str(built)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=40, check=True, cwd=folder, env=core_environment(core))
                generated = json.loads(built.read_text(encoding="utf-8-sig"))
                by_tag = {out.get("tag"): out for out in generated.get("outbounds", [])}
                self.assertEqual(set(by_tag["select"]["outbounds"]),
                                 {"balance", "lowest", AUTO_TAG, AUTO_ALIAS_TAG, "NL", "DE", "PL"})
                self.assertEqual(by_tag[AUTO_ALIAS_TAG]["outbounds"], [AUTO_TAG])
                self.assertEqual(by_tag[AUTO_TAG]["strategy"], "lowest-delay")
                members = by_tag[AUTO_TAG]["outbounds"]
                self.assertEqual(len(members), 3)
                self.assertTrue(all("§hide§" not in tag and by_tag[tag]["type"] == "vless" for tag in members))
                self.assertNotIn("direct", members)

                # The app supplies this default; the standalone builder omits it.
                by_tag["balance"]["strategy"] = "round-robin"
                proxy_port, api_port = free_ports(2)
                test_outs = [by_tag[tag] for tag in ("select", "balance", "lowest", AUTO_TAG, AUTO_ALIAS_TAG)]
                for tag, node in zip(members, nodes):
                    test_outs.append({"type": "socks", "tag": tag, "version": "5",
                                      "server": "127.0.0.1", "server_port": node.server_address[1]})
                runtime = {"log": {"disabled": True},
                           "inbounds": [{"type": "socks", "tag": "in", "listen": "127.0.0.1",
                                         "listen_port": proxy_port}],
                           "outbounds": test_outs, "route": {"final": "select"},
                           "experimental": {"clash_api": {"external_controller": f"127.0.0.1:{api_port}"},
                                            "monitoring": {"interval": "2s", "url_test_timeout": "1s",
                                                           "debounce_window": "10ms", "idle_timeout": "60s",
                                                           "urls": [f"http://127.0.0.1:{trap.server_address[1]}/check"]}}}
                config = root / "runtime.json"
                config.write_text(json.dumps(runtime, ensure_ascii=False), encoding="utf-8")
                proc = subprocess.Popen([core, "-D", folder, "srun", "-c", str(config)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        cwd=folder, env=core_environment(core),
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                opener = build_opener(ProxyHandler({}))

                def group(tag):
                    try:
                        with opener.open(f"http://127.0.0.1:{api_port}/proxies/{quote(tag)}", timeout=1) as r:
                            return json.load(r).get("now")
                    except Exception:
                        return None

                def traffic():
                    while not stop.is_set():
                        via_local_socks(proxy_port, trap.server_address[1])
                        stop.wait(.3)

                thread = threading.Thread(target=traffic, daemon=True)
                thread.start()
                try:
                    self.assertTrue(wait_for(lambda: all(node.requests for node in nodes)),
                                    "client did not probe every independent node")
                    for tag, node in zip(members, nodes):
                        request = Request(f"http://127.0.0.1:{api_port}/proxies/select",
                                          data=json.dumps({"name": tag}).encode(), method="PUT",
                                          headers={"Content-Type": "application/json"})
                        with opener.open(request, timeout=5):
                            pass
                        self.assertTrue(wait_for(lambda: group("select") == tag), "manual selection failed")
                        expected = json.dumps({"node": node.name, "ip": "192.0.2.1"}).encode()
                        self.assertEqual(wait_for(lambda: via_local_socks(proxy_port, trap.server_address[1])),
                                         expected, "manual selection used the wrong node")
                    request = Request(f"http://127.0.0.1:{api_port}/proxies/select",
                                      data=json.dumps({"name": AUTO_TAG}).encode(), method="PUT",
                                      headers={"Content-Type": "application/json"})
                    with opener.open(request, timeout=5):
                        pass
                    self.assertTrue(wait_for(lambda: group("select") == AUTO_TAG))
                    selected = wait_for(lambda: group(AUTO_TAG) if group(AUTO_TAG) in members else None)
                    self.assertIn(selected, members)
                    before = wait_for(lambda: via_local_socks(proxy_port, trap.server_address[1]))
                    self.assertIn(before, (b'{"node": "A", "ip": "192.0.2.1"}',
                                           b'{"node": "B", "ip": "192.0.2.1"}',
                                           b'{"node": "C", "ip": "192.0.2.1"}'))
                    nodes[members.index(selected)].failed = True
                    self.assertTrue(wait_for(lambda: group(AUTO_TAG) in set(members) - {selected}),
                                    "the automatic group did not fail over")
                    self.assertTrue(wait_for(lambda: via_local_socks(proxy_port, trap.server_address[1])),
                                    "the backup node did not carry traffic")
                    for node in nodes:
                        node.failed = True
                    time.sleep(3)
                    for _ in range(3):
                        result = via_local_socks(proxy_port, trap.server_address[1])
                        self.assertNotEqual(result, b"DIRECT")
                    self.assertEqual(trap.requests, 0, "failed nodes must never bypass the VPN")
                finally:
                    stop.set()
                    thread.join(timeout=4)
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=5)
                    proc = None
        finally:
            stop.set()
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for server in [*nodes, trap]:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
