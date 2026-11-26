import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from opt_in import FILES, MARKER, SALT

SOURCE = Path(__file__).resolve().parent
TARGET = '/opt/hiddify-manager/services/xray/bin/gamelife'


@unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0, 'root-owned Linux files required')
class ParentOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.release = self.root / 'release'
        directory = self.release / 'deploy/ru_assets'
        directory.mkdir(parents=True)
        for name in ('parent_overlay.sh', 'opt_in.py', 'compose.ru-assets.yaml'):
            (directory / name).write_bytes((SOURCE / name).read_bytes())
        self.bash_env = Path(self.temp.name) / 'bash-env'
        # Bash sources this test-only stub; it also works when /tmp is noexec.
        self.bash_env.write_text('''docker() {
case "$1:$FAKE_DOCKER" in
  inspect:mounted) echo /opt/hiddify-manager/services/xray/bin/gamelife; return 0;;
  inspect:plain) echo /opt/hiddify-manager/data; return 0;;
  inspect:*) return 1;;
  ps:error) return 1;;
  ps:exists) echo known-container; return 0;;
  ps:*) return 0;;
esac
return 1
}
''')

    def tearDown(self):
        self.temp.cleanup()

    def make_valid_assets(self):
        root = self.root / 'ru-assets'
        root.mkdir(mode=0o755)
        revs = root / 'revisions'
        revs.mkdir(mode=0o755)
        data = {name: ('public-' + name).encode() for name in FILES}
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in data.items()}
        revision = hashlib.sha256(SALT + json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        dest = revs / revision
        dest.mkdir(mode=0o755)
        for name, content in data.items():
            (dest / name).write_bytes(content)
            (dest / name).chmod(0o644)
        (root / 'current').symlink_to('revisions/' + revision)
        return root

    def run_hook(self, state='plain', gateway='safe-network'):
        source = r'''set -euo pipefail
root="$1"; release="$2"; gateway_network="$3"; panel_files=(-f compose.panel.yaml)
source "$release/deploy/ru_assets/parent_overlay.sh"
configure_parent_ru_assets
printf 'COUNT=%s\n' "${#panel_files[@]}"
printf 'ASSET=%s\n' "${VPN_RU_ASSET_ROOT-unset}"
printf 'GATEWAY=%s\n' "${CADDY_NETWORK-unset}"
'''
        env = {'PATH': '/usr/bin:/bin', 'BASH_ENV': str(self.bash_env),
               'FAKE_DOCKER': state, 'LANG': 'C.UTF-8'}
        return subprocess.run(['bash', '-c', source, '--', str(self.root), str(self.release), gateway],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, timeout=15)

    def test_absent_marker_keeps_existing_compose(self):
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('COUNT=2', result.stdout)
        self.assertIn('ASSET=unset', result.stdout)

    def test_valid_marker_adds_overlay_and_validated_env(self):
        root = self.make_valid_assets()
        marker = root / MARKER
        marker.write_bytes(b'v1\n'); marker.chmod(0o600)
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('COUNT=4', result.stdout)
        self.assertIn('ASSET=' + str(root), result.stdout)
        self.assertIn('GATEWAY=safe-network', result.stdout)

    def test_missing_marker_with_active_mount_fails_closed(self):
        result = self.run_hook(state='mounted')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('marker missing', result.stderr)

    def test_inspection_failure_is_not_assumed_safe(self):
        for state in ('error', 'exists'):
            with self.subTest(state=state):
                self.assertNotEqual(self.run_hook(state=state).returncode, 0)
        self.assertEqual(self.run_hook(state='missing').returncode, 0)

    def test_active_marker_with_missing_assets_or_gateway_fails(self):
        root = self.make_valid_assets()
        marker = root / MARKER
        marker.write_bytes(b'v1\n'); marker.chmod(0o600)
        self.assertNotEqual(self.run_hook(gateway='').returncode, 0)
        (root / 'current' / 'geoip.dat').unlink()
        self.assertNotEqual(self.run_hook().returncode, 0)


if __name__ == '__main__':
    unittest.main()
