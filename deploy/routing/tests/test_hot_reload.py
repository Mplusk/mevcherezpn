"""Exercise in-place geodata refresh with the same cores used by the VPN.

Only synthetic connections and public test rule sets are used.  Set XRAY_BIN,
HIDDIFY_PANEL_CORE and TEST_SRS_DIR to require the integration tests.
"""
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

import test_xray_routing as xr
import test_sing_guard as sb


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(100)
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    return
                self.request.sendall(data)
        except OSError:
            return


def held_connection(port, destination_port):
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    sock.settimeout(5)
    sock.sendall(b'\x05\x01\x00')
    if xr.readn(sock, 2) != b'\x05\x00':
        raise AssertionError('SOCKS negotiation failed')
    host = b'keep.test'
    sock.sendall(b'\x05\x01\x00\x03' + bytes([len(host)]) + host + destination_port.to_bytes(2, 'big'))
    answer = xr.readn(sock, 4)
    if answer[1] != 0:
        raise AssertionError('SOCKS connection failed')
    length = 4 if answer[3] == 1 else 16 if answer[3] == 4 else xr.readn(sock, 1)[0]
    xr.readn(sock, length + 2)
    return sock


def replace_link(root, revision):
    link = root / 'next'
    link.symlink_to(revision)
    os.replace(link, root / 'current')


class HotReloadTests(unittest.TestCase):
    def test_xray_reloads_symlinked_geosite_without_dropping_connection(self):
        if not os.environ.get('REQUIRE_HOT_RELOAD_TESTS'):
            self.skipTest('hot-reload integration tests not enabled')
        binary = os.environ.get('XRAY_BIN')
        if not binary:
            if os.environ.get('REQUIRE_HOT_RELOAD_TESTS'):
                self.fail('XRAY_BIN is required')
            self.skipTest('XRAY_BIN not supplied')
        direct = xr.Server(('127.0.0.1', 0), xr.DirectHandler)
        proxy = xr.Server(('127.0.0.1', 0), xr.ProxyHandler)
        echo = xr.Server(('127.0.0.1', 0), Echo)
        for server in (direct, proxy, echo):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix='xray-hot-') as tmp:
                root = Path(tmp)
                (root / 'old').mkdir()
                (root / 'new').mkdir()
                (root / 'old' / 'geosite.dat').write_bytes(xr.geosite('RU-BLOCKED', ['old.test']))
                (root / 'new' / 'geosite.dat').write_bytes(xr.geosite('RU-BLOCKED', ['reload.test']))
                (root / 'current').symlink_to('old')
                with socket.socket() as reserve:
                    reserve.bind(('127.0.0.1', 0))
                    port = reserve.getsockname()[1]
                config = {
                    'log': {'loglevel': 'error'},
                    'geodata': {'cron': '* * * * *'},
                    'dns': {'hosts': {'reload.test': '127.0.0.1', 'keep.test': '127.0.0.1'}},
                    'inbounds': [{'listen': '127.0.0.1', 'port': port, 'protocol': 'socks',
                                  'settings': {'auth': 'noauth', 'udp': False}}],
                    'outbounds': [{'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
                                  {'tag': 'proxy', 'protocol': 'socks', 'settings': {'servers': [
                                      {'address': '127.0.0.1', 'port': proxy.server_address[1]}]}}],
                    'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [
                        {'type': 'field', 'domain': ['ext:current/geosite.dat:ru-blocked'], 'outboundTag': 'proxy'}]},
                }
                env = {key: value for key, value in os.environ.items() if 'proxy' not in key.lower()}
                env['XRAY_LOCATION_ASSET'] = tmp
                proc = subprocess.Popen([binary, 'run', '-c', 'stdin:'], stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                held = None
                try:
                    proc.stdin.write(json.dumps(config).encode())
                    proc.stdin.close()
                    time.sleep(1)
                    self.assertIsNone(proc.poll(), 'Xray failed to start')
                    self.assertEqual(xr.query(port, 'reload.test', direct.server_address[1]), 'DIRECT')
                    held = held_connection(port, echo.server_address[1])
                    held.sendall(b'before')
                    self.assertEqual(xr.readn(held, 6), b'before')
                    replace_link(root, 'new')
                    deadline = time.monotonic() + 80
                    while time.monotonic() < deadline:
                        if xr.query(port, 'reload.test', direct.server_address[1]) == 'PROXY':
                            break
                        time.sleep(2)
                    else:
                        self.fail('Xray did not reload changed geodata on schedule')
                    self.assertIsNone(proc.poll(), 'Xray exited during reload')
                    held.sendall(b'after')
                    self.assertEqual(xr.readn(held, 5), b'after', 'existing connection was dropped')
                finally:
                    if held:
                        held.close()
                    proc.terminate()
                    proc.wait(timeout=5)
        finally:
            for server in (direct, proxy, echo):
                server.shutdown()
                server.server_close()

    def test_hiddify_reloads_atomically_replaced_binary_srs(self):
        if not os.environ.get('REQUIRE_HOT_RELOAD_TESTS'):
            self.skipTest('hot-reload integration tests not enabled')
        core, assets = os.environ.get('HIDDIFY_PANEL_CORE'), os.environ.get('TEST_SRS_DIR')
        if not core or not assets:
            if os.environ.get('REQUIRE_HOT_RELOAD_TESTS'):
                self.fail('Hiddify core and SRS fixtures are required')
            self.skipTest('Hiddify core or SRS fixtures not supplied')
        site = (Path(assets) / 'geosite-ru-blocked.srs').read_bytes()
        ip = (Path(assets) / 'geoip-ru-blocked.srs').read_bytes()
        dns, direct, proxy, echo = (sb.UDPServer(('127.0.0.1', 0), sb.DNS),
                                    sb.TCPServer(('127.0.0.1', 0), sb.Trap),
                                    sb.TCPServer(('127.0.0.1', 0), sb.MockProxy),
                                    sb.TCPServer(('127.0.0.1', 0), Echo))
        for server in (dns, direct, proxy, echo):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix='sing-hot-') as tmp:
                root = Path(tmp)
                (root / 'old').mkdir()
                (root / 'new').mkdir()
                (root / 'old' / 'rule.srs').write_bytes(site)
                (root / 'new' / 'rule.srs').write_bytes(ip)
                (root / 'current').symlink_to('old')
                rule = root / 'current' / 'rule.srs'
                with socket.socket() as reserve:
                    reserve.bind(('127.0.0.1', 0))
                    port = reserve.getsockname()[1]
                config = {'log': {'level': 'error'},
                          'dns': {'servers': [{'type': 'udp', 'tag': 'dns', 'server': '127.0.0.1', 'server_port': dns.server_address[1]}], 'final': 'dns'},
                          'inbounds': [{'type': 'socks', 'tag': 'in', 'listen': '127.0.0.1', 'listen_port': port}],
                          'outbounds': [{'type': 'socks', 'tag': 'proxy', 'server': '127.0.0.1', 'server_port': proxy.server_address[1], 'version': '5'},
                                        {'type': 'direct', 'tag': 'direct'}],
                          'route': {'default_domain_resolver': 'dns',
                                    'rules': [{'rule_set': ['test'], 'outbound': 'proxy'}],
                                    'rule_set': [{'tag': 'test', 'type': 'local', 'format': 'binary', 'path': str(rule)}],
                                    'final': 'direct'}}
                path = root / 'config.json'
                path.write_text(json.dumps(config), encoding='utf-8')
                proc = subprocess.Popen([core, 'srun', '-c', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                held = None
                try:
                    time.sleep(2)
                    self.assertIsNone(proc.poll(), 'Hiddify core failed to start')
                    self.assertEqual(sb.query(port, direct.server_address[1], 'agentura.ru'), 'PROXY')
                    held = held_connection(port, echo.server_address[1])
                    held.sendall(b'before')
                    self.assertEqual(xr.readn(held, 6), b'before')
                    replace_link(root, 'new')
                    signal = root / 'old' / 'same.srs'
                    signal.write_bytes(site)
                    os.replace(signal, root / 'old' / 'rule.srs')
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if sb.query(port, direct.server_address[1], 'agentura.ru') == 'DIRECT':
                            break
                        time.sleep(1)
                    else:
                        self.fail('Hiddify core did not reload the replaced binary SRS')
                    self.assertIsNone(proc.poll(), 'Hiddify core exited during reload')
                    held.sendall(b'after')
                    self.assertEqual(xr.readn(held, 5), b'after', 'existing connection was dropped')
                    # The watcher stays on the directory active at process start.
                    replace_link(root, 'old')
                    signal.write_bytes(site)
                    os.replace(signal, root / 'old' / 'rule.srs')
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if sb.query(port, direct.server_address[1], 'agentura.ru') == 'PROXY':
                            break
                        time.sleep(1)
                    else:
                        self.fail('Hiddify core did not reload a second revision')
                    held.sendall(b'again')
                    self.assertEqual(xr.readn(held, 5), b'again')
                finally:
                    if held:
                        held.close()
                    proc.terminate()
                    proc.wait(timeout=5)
        finally:
            for server in (dns, direct, proxy, echo):
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
