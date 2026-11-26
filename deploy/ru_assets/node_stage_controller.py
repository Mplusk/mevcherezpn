"""Stage one node, with independent NL probe and rollback. No secrets on disk."""
import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent
KEY = Path(os.environ.get('LOCALAPPDATA', '/nonexistent')) / 'Temp/gamelife-vpn-deploy-efa7e9c4fc754433a3f1541b41f29813/deploy_ed25519'
HOSTS = {'de': '2.27.75.91', 'pl': '191.217.211.240', 'nl': '153.52.155.69'}
MAIN = '132.243.234.163'


class ExternalCheckRolledBack(RuntimeError):
    pass


class TimerCheckRolledBack(RuntimeError):
    pass


class RollbackIncomplete(RuntimeError):
    pass


def ssh(host, command, *, data=None, timeout=180):
    return subprocess.run(['ssh', '-T', '-i', str(KEY), '-o', 'BatchMode=yes',
                           '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                           '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15',
                           '-o', 'ServerAliveCountMax=6', 'root@' + host, command],
                          input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=timeout, check=True).stdout


def pycmd(source):
    encoded = base64.b64encode(source.encode()).decode()
    return 'python3 -c "exec(__import__(\'base64\').b64decode(\'' + encoded + '\'))"'


def profile(node, monitor):
    source = ("import json\nc=json.load(open('/etc/gamelife-vpn-monitor/credentials.json'))\n"
              "p=json.loads(c['MONITOR_PROFILE_JSON'])\n"
              "print(json.dumps(p[" + repr(node) + "]))\n")
    data = json.loads(ssh(monitor, pycmd(source)))
    if not isinstance(data, dict):
        raise ValueError('invalid profile')
    return data


def independent_probe(node, monitor):
    source = ("import json,sys\nsys.path.insert(0,'/opt/gamelife-vpn-monitor')\n"
              "import vpn_monitor as m\nc=json.load(open('/etc/gamelife-vpn-monitor/credentials.json'))\n"
              "p=m.validate_profiles(c['MONITOR_PROFILE_JSON'])\n"
              "ok=m.retry(lambda:m.probe_one('/opt/gamelife-vpn-monitor/xray',p[" + repr(node) + "],m.NODES[" + repr(node) + "][1]))\n"
              "sys.exit(0 if ok else 1)\n")
    ssh(monitor, pycmd(source), timeout=180)


def monitor_ok(monitor):
    # The last scheduled result can reflect the expected brief node restart.
    # Check scheduling separately; independent_probe checks actual connectivity.
    ssh(monitor, 'systemctl is-active --quiet gamelife-vpn-monitor.timer', timeout=30)


def main(node):
    if node not in HOSTS:
        raise ValueError('unknown node')
    monitor = MAIN if node == 'nl' else HOSTS['nl']
    outbound = profile(node, monitor)
    independent_probe(node, monitor)
    monitor_ok(monitor)
    target = HOSTS[node]
    remote_dir = ssh(target, 'umask 077; mktemp -d /root/services/gamelife-vpn/ru-assets/.node-installer.XXXXXXXX').decode().strip()
    if not re.fullmatch(r'/root/services/gamelife-vpn/ru-assets/\.node-installer\.[A-Za-z0-9]{8}', remote_dir):
        raise ValueError('invalid temporary directory')
    try:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for name in ('node_install.py', 'installer.py', 'sync.py', 'reduce.py', 'compose.ru-assets.yaml'):
                data = (HERE / name).read_bytes()
                item = tarfile.TarInfo(name)
                item.mode, item.size = 0o600, len(data)
                tar.addfile(item, io.BytesIO(data))
        ssh(target, 'tar -xf - -C ' + remote_dir, data=archive.getvalue())
        def call(action, tx=None):
            req = {'action': action, 'node': node, 'outbound': outbound}
            if tx is not None:
                req['transaction'] = tx
            return json.loads(ssh(target, 'python3 ' + remote_dir + '/node_install.py',
                                  data=json.dumps(req).encode(), timeout=1800))
        result = call('stage')
        if result.get('status') not in ('staged', 'unchanged'):
            raise RuntimeError('stage failed')
        tx = result.get('transaction')
        failure = None
        try:
            independent_probe(node, monitor)
        except Exception:
            failure = ExternalCheckRolledBack
        if failure is None:
            try:
                monitor_ok(monitor)
            except Exception:
                failure = TimerCheckRolledBack
        if failure is not None:
            if tx and re.fullmatch(r'[a-f0-9]{32}', tx):
                rolled_back = call('rollback', tx)
                if rolled_back.get('status') != 'rolled_back':
                    raise RollbackIncomplete('post-stage verification failed') from None
                independent_probe(node, monitor)
                raise failure('post-stage verification failed; mount rolled back') from None
            raise failure('post-stage verification failed; pre-existing mount unchanged') from None
        return result
    finally:
        try:
            ssh(target, 'rm -rf -- ' + remote_dir, timeout=30)
        except Exception:
            pass


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('node required')
        print(json.dumps(main(sys.argv[1]), separators=(',', ':')))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': type(exc).__name__}, separators=(',', ':')))
        raise SystemExit(1)
