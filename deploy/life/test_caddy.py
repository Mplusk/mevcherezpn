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
        config = ('{\n admin off\n auto_https off\n}\n'
                  + 'http://node.example.test:' + str(cls.port)
                  + ', http://old.example.test:' + str(cls.port) + ' {\n'
                  + backup + site_block(public_html()) + '\thandle {\n\t\trespond 404\n\t}\n}\n')
        config_path = Path(cls.temp.name) / 'Caddyfile'
        config_path.write_text(config, encoding='utf-8')
        config_path.chmod(0o644)
        cls.geodata = Path(cls.temp.name) / 'public-geodata'
        cls.geodata.mkdir(mode=0o755)
        release = cls.geodata / 'revision'
        release.mkdir(mode=0o755)
        (cls.geodata / 'current').symlink_to('revision', target_is_directory=True)
        for name, data in [('geoip.dat', b'test geoip'), ('geosite.dat', b'test geosite'),
                           ('geoip-ru-blocked.srs', b'test ip srs'), ('geosite-ru-blocked.srs', b'test site srs'),
                           ('manifest.json', b'{"version":1}'),
                           ('other.dat', b'private other file')]:
            (release / name).write_bytes(data)
            (release / name).chmod(0o644)
        (Path(cls.temp.name) / 'secret.txt').write_text('not public', encoding='utf-8')
        subprocess.run(['docker', 'run', '-d', '--network', 'host', '--name', cls.name,
                        '-v', str(config_path) + ':/etc/caddy/Caddyfile:ro',
                        '-v', str(cls.geodata) + ':/data/public-geodata:ro', IMAGE], check=True,
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
    def get(cls, path, host='node.example.test'):
        connection = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=5)
        try:
            connection.request('GET', path, headers={'User-Agent': 'life-ci', 'Host': host})
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
        for host in ('node.example.test', 'old.example.test'):
            for alias in ('parent_path', 'legacy_path'):
                with self.subTest(host=host, alias=alias):
                    status, body, _ = self.get('/' + alias + '/' + USER + '/auto/', host)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body), {'path': '/parent_path/' + USER + '/auto/',
                                                        'agent': 'life-ci'})
        for path in ('/legacy_path/not-a-uuid/', '/admin_path/' + USER + '/auto/'):
            self.assertEqual(self.get(path)[0], 404)

    def test_only_exact_geodata_files_are_public(self):
        for name, expected in [('geoip.dat', b'test geoip'), ('geosite.dat', b'test geosite'),
                               ('geoip-ru-blocked.srs', b'test ip srs'), ('geosite-ru-blocked.srs', b'test site srs')]:
            with self.subTest(name=name):
                status, body, headers = self.get('/assets/geodata/' + name)
                self.assertEqual((status, body), (200, expected))
                self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        status, body, headers = self.get('/assets/geodata/manifest.json')
        self.assertEqual((status, json.loads(body)), (200, {'version': 1}))
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        for path in ('/assets/geodata/', '/assets/geodata/other.dat',
                     '/assets/geodata/geoip.dat/extra', '/assets/geodata/../secret.txt',
                     '/assets/geodata/%2e%2e/secret.txt', '/secret.txt'):
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'not public', body)
                self.assertNotIn(b'private other file', body)


if __name__ == '__main__':
    unittest.main()
