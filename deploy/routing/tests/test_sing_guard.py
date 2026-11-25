"""Isolated sing-box DNS rebinding check against the pinned panel core."""
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest


class UDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class DNS(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        if len(data) < 16:
            return
        pos = 12
        while pos < len(data) and data[pos]:
            pos += data[pos] + 1
        end = pos + 5
        if end > len(data):
            return
        qtype = struct.unpack('!H', data[end - 4:end - 2])[0]
        question = data[12:end]
        if b'\x05mixed\x04test' in question:
            ips = (b'\xcb\x00\x71\x05', b'\x7f\x00\x00\x01')
        elif b'\x06public\x04test' in question:
            ips = (b'\xcb\x00\x71\x05',)
        else:
            ips = (b'\x7f\x00\x00\x01',)
        answer = b''.join(b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04' + ip for ip in ips) if qtype == 1 else b''
        count = len(ips) if answer else 0
        header = data[:2] + b'\x81\x80\x00\x01' + struct.pack('!H', count) + b'\x00\x00\x00\x00'
        sock.sendto(header + question + answer, self.client_address)


class Trap(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        self.request.recv(8192)
        self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\nDIRECT')


class MockProxy(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(3)
            hello = readn(self.request, 2)
            readn(self.request, hello[1])
            self.request.sendall(b'\x05\x00')
            req = readn(self.request, 4)
            n = 4 if req[3] == 1 else 16 if req[3] == 4 else readn(self.request, 1)[0]
            readn(self.request, n + 2)
            self.request.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
            self.request.recv(8192)
            self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nPROXY')
        except (OSError, EOFError):
            pass


def readn(sock, n):
    result = b''
    while len(result) < n:
        chunk = sock.recv(n - len(result))
        if not chunk:
            raise EOFError
        result += chunk
    return result


def query(port, dest, hostname='blocked.test'):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=4) as sock:
            sock.settimeout(4)
            sock.sendall(b'\x05\x01\x00')
            if readn(sock, 2) != b'\x05\x00':
                return 'BLOCKED'
            name = hostname.encode('ascii')
            sock.sendall(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + struct.pack('!H', dest))
            reply = readn(sock, 4)
            if reply[1]:
                return 'BLOCKED'
            n = 4 if reply[3] == 1 else 16 if reply[3] == 4 else readn(sock, 1)[0]
            readn(sock, n + 2)
            sock.sendall(b'GET / HTTP/1.1\r\nHost: ' + name + b'\r\nConnection: close\r\n\r\n')
            data = sock.recv(8192)
            return 'DIRECT' if b'DIRECT' in data else 'PROXY' if b'PROXY' in data else 'BLOCKED'
    except (OSError, EOFError):
        return 'BLOCKED'


class SingGuardTests(unittest.TestCase):
    def test_resolve_before_private_guard(self):
        core = os.environ.get('HIDDIFY_PANEL_CORE')
        if not core:
            if os.environ.get('REQUIRE_SING_GUARD_TESTS'):
                self.fail('HIDDIFY_PANEL_CORE is required')
            self.skipTest('panel core not supplied')
        dns = UDPServer(('127.0.0.1', 0), DNS)
        trap = TCPServer(('127.0.0.1', 0), Trap)
        proxy = TCPServer(('127.0.0.1', 0), MockProxy)
        for server in (dns, trap, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix='sing-guard-') as tmp:
                for resolve in (False, True):
                    with self.subTest(resolve=resolve):
                        with socket.socket() as s:
                            s.bind(('127.0.0.1', 0))
                            port = s.getsockname()[1]
                        rules = ([{'action': 'resolve'}] if resolve else [])
                        rules += [{'ip_is_private': True, 'action': 'reject'},
                                  {'domain': ['blocked.test'], 'outbound': 'direct'},
                                  {'domain': ['public.test', 'mixed.test'], 'outbound': 'proxy'}]
                        config = {'log': {'level': 'error'},
                                  'dns': {'servers': [{'type': 'udp', 'tag': 'testdns', 'server': '127.0.0.1',
                                                       'server_port': dns.server_address[1]}], 'final': 'testdns'},
                                  'inbounds': [{'type': 'socks', 'tag': 'in', 'listen': '127.0.0.1', 'listen_port': port}],
                                  'outbounds': [{'type': 'direct', 'tag': 'direct'},
                                                {'type': 'socks', 'tag': 'proxy', 'server': '127.0.0.1',
                                                 'server_port': proxy.server_address[1], 'version': '5'}],
                                  'route': {'rules': rules, 'final': 'direct', 'default_domain_resolver': 'testdns'}}
                        path = Path(tmp) / 'config.json'
                        path.write_text(json.dumps(config), encoding='utf-8')
                        proc = subprocess.Popen([core, 'srun', '-c', str(path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                        try:
                            time.sleep(1)
                            if proc.poll() is not None:
                                self.fail('core did not start: ' + proc.stdout.read().decode(errors='replace'))
                            self.assertEqual(query(port, trap.server_address[1]), 'BLOCKED' if resolve else 'DIRECT')
                            self.assertEqual(query(port, trap.server_address[1], 'public.test'), 'PROXY',
                                             'domain identity must survive resolve')
                            self.assertEqual(query(port, trap.server_address[1], 'mixed.test'), 'BLOCKED' if resolve else 'PROXY',
                                             'a private address in a mixed DNS answer must be blocked')
                        finally:
                            proc.terminate()
                            proc.wait(timeout=5)
                            proc.stdout.close()
        finally:
            for server in (dns, trap, proxy):
                server.shutdown()
                server.server_close()


class OfficialRuleDNS(socketserver.BaseRequestHandler):
    """Respond locally with fixed A records; no DNS or user traffic leaves the test."""
    def handle(self):
        data, sock = self.request
        pos = 12
        while pos < len(data) and data[pos]:
            pos += data[pos] + 1
        end = pos + 5
        if end > len(data):
            return
        question = data[12:end]
        qtype = struct.unpack('!H', data[end - 4:end - 2])[0]
        if b'\x07private' in question:
            ip = b'\x7f\x00\x00\x01'
        elif b'\x09blockedip' in question:
            ip = b'\x01\x01\x01\x01'
        else:
            ip = b'\xcb\x00\x71\x05'
        answer = b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04' + ip if qtype == 1 else b''
        header = data[:2] + b'\x81\x80\x00\x01' + (b'\x00\x01' if answer else b'\x00\x00') + b'\x00\x00\x00\x00'
        sock.sendto(header + question + answer, self.client_address)


class LocalRuleSetTests(unittest.TestCase):
    def test_cold_start_with_local_official_rule_sets(self):
        core = os.environ.get('HIDDIFY_PANEL_CORE')
        assets = os.environ.get('TEST_SRS_DIR')
        if not core or not assets:
            if os.environ.get('REQUIRE_LOCAL_SRS_TESTS'):
                self.fail('HIDDIFY_PANEL_CORE and TEST_SRS_DIR are required')
            self.skipTest('panel core or SRS fixtures not supplied')
        dns = UDPServer(('127.0.0.1', 0), OfficialRuleDNS)
        trap = TCPServer(('127.0.0.1', 0), Trap)
        proxy = TCPServer(('127.0.0.1', 0), MockProxy)
        for server in (dns, trap, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix='sing-srs-') as tmp:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                config = {
                    'log': {'level': 'error'},
                    'dns': {'servers': [{'type': 'udp', 'tag': 'dns', 'server': '127.0.0.1',
                                         'server_port': dns.server_address[1]}], 'final': 'dns'},
                    'inbounds': [{'type': 'socks', 'tag': 'in', 'listen': '127.0.0.1', 'listen_port': port}],
                    'outbounds': [{'type': 'socks', 'tag': 'proxy', 'server': '127.0.0.1',
                                   'server_port': proxy.server_address[1], 'version': '5'},
                                  {'type': 'direct', 'tag': 'direct'}],
                    'route': {'default_domain_resolver': 'dns',
                              'rules': [{'action': 'resolve'}, {'ip_is_private': True, 'action': 'reject'},
                                        {'rule_set': ['site', 'ip'], 'outbound': 'proxy'}],
                              'rule_set': [{'tag': 'site', 'type': 'local', 'format': 'binary',
                                            'path': str(Path(assets) / 'geosite-ru-blocked.srs')},
                                           {'tag': 'ip', 'type': 'local', 'format': 'binary',
                                            'path': str(Path(assets) / 'geoip-ru-blocked.srs')}],
                              'final': 'direct'},
                }
                path = Path(tmp) / 'config.json'
                path.write_text(json.dumps(config), encoding='utf-8')
                proc = subprocess.Popen([core, 'srun', '-c', str(path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                try:
                    time.sleep(1)
                    if proc.poll() is not None:
                        self.fail('core did not start: ' + proc.stdout.read().decode(errors='replace'))
                    self.assertEqual(query(port, trap.server_address[1], 'agentura.ru'), 'PROXY', 'domain in SRS')
                    self.assertEqual(query(port, trap.server_address[1], 'private.agentura.ru'), 'BLOCKED',
                                     'private DNS result must be blocked before domain allow')
                    self.assertEqual(query(port, trap.server_address[1], 'blockedip.test'), 'PROXY', 'IP in SRS')
                finally:
                    proc.terminate()
                    proc.wait(timeout=5)
                    proc.stdout.close()
        finally:
            for server in (dns, trap, proxy):
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
