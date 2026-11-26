"""Exercise client and server routing with real Xray and synthetic local data.

Set XRAY_BIN to a trusted Xray executable; no real nodes or credentials are used.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest


def varint(value):
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint((number << 3) | 2) + varint(len(value)) + value


def geosite(code, domains):
    entry = field(1, code.encode())
    for domain in domains:
        entry += field(2, field(1, 3) + field(2, domain.encode()))
    return field(1, entry)


def geoip(code, addr, prefix):
    entry = field(1, code.encode()) + field(2, field(1, socket.inet_aton(addr)) + field(2, prefix))
    return field(1, entry)


def readn(sock, count):
    output = b''
    while len(output) < count:
        data = sock.recv(count - len(output))
        if not data:
            raise EOFError
        output += data
    return output


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class DirectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        self.request.recv(8192)
        self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\nDIRECT')


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(2)
            hello = readn(self.request, 2)
            readn(self.request, hello[1])
            self.request.sendall(b'\x05\x00')
            req = readn(self.request, 4)
            if req[3] == 1:
                readn(self.request, 4)
            elif req[3] == 3:
                readn(self.request, readn(self.request, 1)[0])
            elif req[3] == 4:
                readn(self.request, 16)
            else:
                return
            readn(self.request, 2)
            self.request.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
            self.request.recv(8192)
            self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nPROXY')
        except (OSError, EOFError):
            pass


def query(port, host, destination_port, *, torrent=False):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
            sock.settimeout(3)
            sock.sendall(b'\x05\x01\x00')
            if readn(sock, 2) != b'\x05\x00':
                return 'BLOCKED'
            name = host.encode('ascii')
            sock.sendall(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + struct.pack('!H', destination_port))
            answer = readn(sock, 4)
            if answer[1] != 0:
                return 'BLOCKED'
            size = 4 if answer[3] == 1 else 16 if answer[3] == 4 else readn(sock, 1)[0]
            readn(sock, size + 2)
            payload = (b'\x13BitTorrent protocol' + bytes(48) if torrent else
                       b'GET / HTTP/1.1\r\nHost: ' + name + b'\r\nConnection: close\r\n\r\n')
            sock.sendall(payload)
            response = b''
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                response += chunk
            return response.split(b'\r\n\r\n')[-1].decode() if response else 'BLOCKED'
    except (OSError, EOFError, UnicodeError):
        return 'BLOCKED'


class XrayRoutingTests(unittest.TestCase):
    def test_client_and_server_priorities(self):
        executable = os.environ.get('XRAY_BIN') or shutil.which('xray')
        if not executable:
            if os.environ.get('REQUIRE_XRAY_ROUTING_TESTS'):
                self.fail('XRAY_BIN is required in CI')
            self.skipTest('XRAY_BIN not supplied')
        servers = [Server(('127.0.0.1', 0), DirectHandler), Server(('127.0.0.1', 0), ProxyHandler)]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix='routing-test-') as temp:
                path = Path(temp)
                (path / 'geosite.dat').write_bytes(
                    geosite('RU-BLOCKED', ['blocked.test', 'private-blocked.test']) +
                    geosite('CATEGORY-RU', ['blocked.test', 'private-blocked.test', 'russian.test']))
                (path / 'geoip.dat').write_bytes(geoip('PRIVATE', '198.18.0.0', 15) + geoip('RU', '203.0.113.0', 24)
                                                + geoip('RU-BLOCKED', '203.0.113.0', 25))
                with socket.socket() as s:
                    s.bind(('127.0.0.1', 0))
                    port = s.getsockname()[1]
                dns = {'hosts': {name: '127.0.0.1' for name in ('blocked.test', 'russian.test', 'other.test', '2ip.io')}}
                dns['hosts']['private-blocked.test'] = '198.18.0.9'
                dns['hosts']['ipblocked.test'] = '203.0.113.5'
                inbound = {'listen': '127.0.0.1', 'port': port, 'protocol': 'socks',
                           'settings': {'auth': 'noauth', 'udp': False},
                           'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True}}
                direct = {'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}}
                proxy = {'tag': 'proxy', 'protocol': 'socks', 'settings': {'servers': [{'address': '127.0.0.1', 'port': servers[1].server_address[1]}]}}
                block = {'tag': 'block', 'protocol': 'blackhole'}
                server_rules = [
                    {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'block'},
                    {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'block'},
                    {'type': 'field', 'domain': ['geosite:ru-blocked'], 'outboundTag': 'direct'},
                    {'type': 'field', 'ip': ['geoip:ru-blocked'], 'outboundTag': 'direct'},
                    {'type': 'field', 'domain': ['geosite:category-ru'], 'outboundTag': 'block'},
                    {'type': 'field', 'ip': ['geoip:ru'], 'outboundTag': 'block'},
                ]
                client_rules = [
                    {'type': 'field', 'domain': ['geosite:ru-blocked'], 'outboundTag': 'proxy'},
                    {'type': 'field', 'ip': ['geoip:ru-blocked'], 'outboundTag': 'proxy'},
                    {'type': 'field', 'domain': ['domain:2ip.io', 'geosite:category-ru'], 'outboundTag': 'direct'},
                ]
                guard_rules = [dict(rule) for rule in server_rules]
                guard_rules[2]['outboundTag'] = 'proxy'
                guard_rules[3]['outboundTag'] = 'proxy'
                for mode, rules, outs, expected in [
                    ('server', server_rules, [direct, block, proxy],
                     {'blocked.test': 'DIRECT', 'russian.test': 'BLOCKED', 'other.test': 'DIRECT', 'private-blocked.test': 'BLOCKED'}),
                    ('client', client_rules, [proxy, direct, block],
                     {'blocked.test': 'PROXY', 'russian.test': 'DIRECT', '2ip.io': 'DIRECT', 'other.test': 'PROXY', 'ipblocked.test': 'PROXY'}),
                    ('guard', guard_rules, [direct, block, proxy],
                     {'blocked.test': 'PROXY', 'private-blocked.test': 'BLOCKED', 'ipblocked.test': 'PROXY'}),
                    ('guard-unsafe', guard_rules, [direct, block, proxy],
                     {'private-blocked.test': 'PROXY'}),
                ]:
                    with self.subTest(mode=mode):
                        config = {'log': {'loglevel': 'none'}, 'dns': dns, 'inbounds': [inbound],
                                  'outbounds': outs, 'routing': {'domainStrategy': 'IPOnDemand' if mode in ('server', 'guard') else 'IPIfNonMatch', 'rules': rules}}
                        env = {k: v for k, v in os.environ.items() if 'proxy' not in k.lower()}
                        env['XRAY_LOCATION_ASSET'] = temp
                        proc = subprocess.Popen([executable, 'run', '-c', 'stdin:'], stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
                        try:
                            proc.stdin.write(json.dumps(config).encode())
                            proc.stdin.close()
                            time.sleep(.5)
                            if proc.poll() is not None:
                                self.fail('Xray did not start: ' + proc.stdout.read().decode(errors='replace'))
                            for host, result in expected.items():
                                self.assertEqual(query(port, host, servers[0].server_address[1]), result, host)
                            if mode == 'server':
                                self.assertEqual(query(port, 'blocked.test', servers[0].server_address[1], torrent=True), 'BLOCKED', 'BitTorrent must be blocked before allowed domains')
                        finally:
                            proc.terminate()
                            proc.wait(timeout=5)
                            proc.stdout.close()
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
