"""Exercise Caddy with local fake users and a local fake origin."""

import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renderer import render
from test_renderer import fixture


IMAGE = ('caddy:2.11.9-alpine@sha256:'
         '003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0')
VALID_USER = '22222222-3333-4444-8888-555555555555'
INVALID_USER = '11111111-2222-4333-8444-555555555555'


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        if VALID_USER not in self.path:
            self.send_response(403)
            self.end_headers()
            return
        payload = json.dumps({'path': self.path, 'host': self.headers.get('Host'),
                              'agent': self.headers.get('User-Agent')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class LocalHTTPS(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection(('127.0.0.1', self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


@unittest.skipUnless(os.environ.get('RELAY_DOCKER_TESTS') == '1', 'Docker integration not requested')
class CaddyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='relay-ci-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        directory.chmod(0o755)
        key, cert = directory / 'test.key', directory / 'test.crt'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=gateway.example.test',
                        '-addext', 'subjectAltName=DNS:gateway.example.test',
                        '-addext', 'basicConstraints=critical,CA:TRUE'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o644)
        cls.context = ssl.create_default_context(cafile=str(cert))
        cls.origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        cls.addClassCleanup(cls.origin.server_close)
        cls.addClassCleanup(cls.origin.shutdown)
        threading.Thread(target=cls.origin.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        config = render(fixture(), listen_port=cls.port, certificate='/fixtures/test.crt',
                        key='/fixtures/test.key',
                        origin='http://127.0.0.1:' + str(cls.origin.server_port),
                        bind='127.0.0.1', admin='off')
        (directory / 'Caddyfile').write_text(config, encoding='utf-8')
        cls.name = 'relay-ci-test-' + secrets.token_hex(6)
        run = subprocess.run(['docker', 'run', '-d', '--rm', '--name', cls.name,
                              '--network', 'host', '--read-only', '--tmpfs', '/tmp',
                              '--cap-drop', 'ALL', '-v', str(directory) + ':/fixtures:ro',
                              '-e', 'XDG_DATA_HOME=/tmp/caddy-data',
                              '-e', 'XDG_CONFIG_HOME=/tmp/caddy-config',
                              IMAGE, 'caddy', 'run', '--config', '/fixtures/Caddyfile',
                              '--adapter', 'caddyfile'], capture_output=True, timeout=120)
        if run.returncode:
            raise RuntimeError('isolated Caddy container did not start')
        cls.addClassCleanup(lambda: subprocess.run(['docker', 'stop', cls.name],
                                                   stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL, timeout=30))
        for _ in range(40):
            try:
                status, _ = cls.fetch('/')
                if status == 404:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.5)
        else:
            raise RuntimeError('isolated Caddy did not become ready')

    @classmethod
    def fetch(cls, path):
        conn = LocalHTTPS('gateway.example.test', cls.port, timeout=5, context=cls.context)
        try:
            conn.request('GET', path, headers={'User-Agent': 'relay-test-agent'})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_all_aliases_are_rewritten_to_the_parent_path(self):
        for alias in ('parent_path', 'legacy_path', 'node_path'):
            with self.subTest(alias=alias):
                status, body = self.fetch('/' + alias + '/' + VALID_USER + '/auto/?fixture=1')
                self.assertEqual(status, 200)
                decoded = json.loads(body)
                self.assertEqual(decoded['path'], '/parent_path/' + VALID_USER + '/auto/?fixture=1')
                self.assertEqual(decoded['host'], 'parent.example.test')
                self.assertEqual(decoded['agent'], 'relay-test-agent')

    def test_invalid_paths_and_unknown_users_do_not_return_profiles(self):
        for path in ('/', '/admin/', '/node_path/' + '-' * 36 + '/',
                     '/other_path/' + VALID_USER + '/',
                     '/node_path/' + VALID_USER + '/../admin/',
                     '/node_path/' + VALID_USER + 'extra/'):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 404)
        self.assertEqual(self.fetch('/node_path/' + INVALID_USER + '/')[0], 403)

    def test_z_upstream_failure_does_not_log_subscription_tokens(self):
        self.origin.shutdown()
        self.origin.server_close()
        status, _ = self.fetch('/node_path/' + VALID_USER + '/auto/?fixture=private')
        self.assertEqual(status, 502)
        logs = subprocess.run(['docker', 'logs', self.name], capture_output=True,
                              timeout=30, check=True)
        output = logs.stdout + logs.stderr
        for value in (VALID_USER, 'node_path', 'parent_path', 'fixture=private',
                      'relay-test-agent'):
            with self.subTest(value=value):
                self.assertNotIn(value.encode(), output)


if __name__ == '__main__':
    unittest.main()
