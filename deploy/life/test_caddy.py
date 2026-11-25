"""Exercise Caddy itself using a synthetic subscription and no real secrets."""
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

from gateway import backup_block, public_html, site_block

IMAGE = 'caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0'
USER = '11111111-2222-4333-8444-555555555555'


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        data = json.dumps({'path': self.path, 'agent': self.headers.get('User-Agent')}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(os.environ.get('LIFE_DOCKER_TESTS') == '1', 'Docker integration not requested')
class CaddyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin = HTTPServer(('127.0.0.1', 0), Origin)
        cls.thread = threading.Thread(target=cls.origin.serve_forever, daemon=True)
        cls.thread.start()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.temp = tempfile.TemporaryDirectory(prefix='life-caddy-test-')
        cls.name = 'life-caddy-' + uuid.uuid4().hex[:12]
        routes = {'version': 1, 'host': 'node.example.test', 'parent_host': 'parent.example.test',
                  'parent_path': 'parent_path', 'aliases': ['parent_path', 'legacy_path']}
        backup = backup_block(routes).replace('https://parent.example.test',
                                               'http://127.0.0.1:' + str(cls.origin.server_port))
        config = ('{\n admin off\n auto_https off\n}\n:' + str(cls.port) + ' {\n'
                  + backup + site_block(public_html()) + '\thandle {\n\t\trespond 404\n\t}\n}\n')
        config_path = Path(cls.temp.name) / 'Caddyfile'
        config_path.write_text(config, encoding='utf-8')
        config_path.chmod(0o644)
        subprocess.run(['docker', 'run', '-d', '--network', 'host', '--name', cls.name,
                        '-v', str(config_path) + ':/etc/caddy/Caddyfile:ro', IMAGE], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                cls.get('/')
                break
            except OSError:
                time.sleep(1)
        else:
            cls.tearDownClass()
            raise RuntimeError('isolated gateway did not start')

    @classmethod
    def tearDownClass(cls):
        subprocess.run(['docker', 'rm', '-f', cls.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.origin.shutdown()
        cls.origin.server_close()
        cls.temp.cleanup()

    @classmethod
    def get(cls, path):
        connection = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=5)
        try:
            connection.request('GET', path, headers={'User-Agent': 'life-ci'})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_site_round_trips_through_caddy(self):
        for path in ('/', '/rules', '/patterns', '/about'):
            status, body, headers = self.get(path)
            self.assertEqual(status, 200)
            self.assertEqual(body.decode().rstrip('\n'), public_html().rstrip('\n'))
            self.assertIn("default-src 'none'", headers['Content-Security-Policy'])

    def test_backup_rewrites_only_a_valid_alias_and_uuid(self):
        for alias in ('parent_path', 'legacy_path'):
            status, body, _ = self.get('/' + alias + '/' + USER + '/auto/')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {'path': '/parent_path/' + USER + '/auto/',
                                                'agent': 'life-ci'})
        for path in ('/legacy_path/not-a-uuid/', '/admin_path/' + USER + '/auto/'):
            self.assertEqual(self.get(path)[0], 404)


if __name__ == '__main__':
    unittest.main()
