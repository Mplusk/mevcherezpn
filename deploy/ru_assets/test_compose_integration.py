"""Exercise the overlay with real Docker, using only a disposable project."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


IMAGE = 'caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0'
TARGET = '/opt/hiddify-manager/services/xray/bin/gamelife'
OVERLAY = Path(__file__).with_name('compose.ru-assets.yaml').resolve()


@unittest.skipUnless(os.environ.get('RU_ASSETS_DOCKER_TESTS') == '1', 'Docker integration not requested')
class ComposeIntegrationTests(unittest.TestCase):
    def test_overlay_survives_recreate_and_connects_gateway(self):
        suffix = uuid.uuid4().hex[:12]
        project = 'ruassetstest' + suffix
        gateway = 'ruassetstest-gateway-' + suffix
        container = project + '-panel-1'
        with tempfile.TemporaryDirectory(prefix='ru-asset-compose-') as directory:
            root = Path(directory)
            assets = root / 'assets'
            assets.mkdir(mode=0o755)
            (assets / 'sample').write_text('public')
            (assets / 'sample').chmod(0o644)
            base = root / 'compose.panel.yaml'
            base.write_text('services:\n  panel:\n    image: ' + IMAGE + '\n'
                            '    entrypoint: ["/bin/sh", "-c", "sleep 180"]\n'
                            '    volumes:\n      - panel:/opt/hiddify-manager/data\n'
                            '    networks:\n      ingress: {}\n      storage: {}\n'
                            'networks:\n  ingress:\n  storage:\n    internal: true\n'
                            'volumes:\n  panel:\n')
            env = {**os.environ, 'CADDY_NETWORK': gateway, 'VPN_RU_ASSET_ROOT': str(assets)}
            command = ['docker', 'compose', '-p', project, '-f', str(base), '-f', str(OVERLAY)]

            def run(args, *, check=True):
                return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=env, timeout=100, check=check)

            def inspect():
                raw = run(['docker', 'inspect', '--format',
                           '{{json .NetworkSettings.Networks}}\n{{json .Mounts}}', container]).stdout.splitlines()
                return json.loads(raw[0]), json.loads(raw[1])

            try:
                run(['docker', 'network', 'create', gateway])
                merged = json.loads(run(command + ['config', '--format', 'json']).stdout)
                self.assertEqual(merged['networks']['gateway']['name'], gateway)
                self.assertEqual(merged['services']['panel']['networks']['gateway']['aliases'], ['gamelife-vpn-origin'])
                binds = [v for v in merged['services']['panel']['volumes'] if v['target'] == TARGET]
                self.assertEqual(len(binds), 1)
                self.assertTrue(binds[0]['read_only'])
                self.assertFalse(binds[0]['bind']['create_host_path'])
                run(command + ['up', '-d'])
                for extra in ([], ['--force-recreate']):
                    if extra:
                        run(command + ['up', '-d'] + extra)
                    networks, mounts = inspect()
                    self.assertEqual(set(networks[gateway]['Aliases']), {'gamelife-vpn-origin', 'panel', container})
                    found = [m for m in mounts if m['Destination'] == TARGET]
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found[0]['Source'], str(assets))
                    self.assertFalse(found[0]['RW'])
                    self.assertEqual(run(['docker', 'exec', container, 'cat', TARGET + '/sample']).stdout, b'public')
            finally:
                run(command + ['down', '-v', '--remove-orphans'], check=False)
                run(['docker', 'network', 'rm', gateway], check=False)


if __name__ == '__main__':
    unittest.main()
