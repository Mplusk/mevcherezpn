"""Read-only API preflight over pinned existing OpenSSH known_hosts entries.

Each remote Python module is loaded from stdin in memory. No panel response,
private key, or generated configuration is printed or written to a file.
Asset validation runs a short isolated pinned-image validation container.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT.parent / 'ru_assets'
KEY = Path(os.environ['LOCALAPPDATA']) / 'Temp/gamelife-vpn-deploy-efa7e9c4fc754433a3f1541b41f29813/deploy_ed25519'
HOSTS = {'main': '132.243.234.163', 'de': '2.27.75.91',
         'pl': '191.217.211.240', 'nl': '153.52.155.69'}
MODULES = [('reduce', ASSETS), ('sync', ASSETS), ('installer', ASSETS), ('node_install', ASSETS),
           ('patch', ROOT), ('template_install', ROOT), ('outbound_install', ROOT), ('journal', ROOT),
           ('transaction', ROOT), ('apply_monitor', ROOT), ('verify_generated', ROOT),
           ('remote_agent', ROOT), ('node_operator', ROOT), ('outbound_agent', ROOT)]


def read_sources():
    return {name: base64.b64encode((directory / (name + '.py')).read_bytes()).decode('ascii')
            for name, directory in MODULES}


def source_digest(sources):
    return hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def program(node, action='preflight', tx='0' * 32, *, sources=None):
    import re
    if (node not in HOSTS or action not in ('preflight', 'stage', 'verify-staged', 'rollback',
                                            'verify-active', 'activate', 'disable', 'inspect-shape')
            or not re.fullmatch('[a-f0-9]{32}', tx)):
        raise ValueError('invalid operation')
    module = 'outbound_agent' if action in ('verify-active', 'activate', 'disable') else 'node_operator'
    call = (" panel=sys.modules['node_operator'].panel_for(" + repr(node) + ")\n"
            " raw=panel.request('GET','outbounds/')\n"
            " info=sys.modules['remote_agent'].inspect(panel)\n"
            " assert isinstance(raw,dict) and raw.get('managed_by_parent') is "
            + ('False' if node == 'main' else 'True') + "\n"
            " assert all(info.get(k) is True for k in ('block_ready','direct_default'))\n"
            " assert info.get('exception_present') is False\n") if action == 'inspect-shape' else (
                " sys.modules[" + repr(module) + "].execute(" + repr(action) + ',' + repr(node) + ',' + repr(tx) + ")\n")
    if sources is None:
        sources = read_sources()
    if not isinstance(sources, dict) or set(sources) != {name for name, _ in MODULES}:
        raise ValueError('invalid source bundle')
    encoded = base64.b64encode(json.dumps(sources, separators=(',', ':')).encode()).decode()
    return ("import base64,json,sys,types\n"
            "sources=json.loads(base64.b64decode('" + encoded + "'))\n"
            "for name,encoded in sources.items():\n"
            " mod=types.ModuleType(name);mod.__file__='/opt/in-memory/'+name+'.py';sys.modules[name]=mod\n"
            " exec(compile(base64.b64decode(encoded),mod.__file__,'exec'),mod.__dict__)\n"
            "try:\n"
            + call +
            " print('PREFLIGHT_OK')\n"
            "except Exception as exc:\n"
            " detail=''\n"
            " if isinstance(exc,TypeError):\n"
            "  import traceback\n"
            "  frame=traceback.extract_tb(exc.__traceback__)[-1]\n"
            "  detail='_LINE'+str(frame.lineno)+'_'+frame.name\n"
            " if isinstance(exc,FileNotFoundError):\n"
            "  import os\n"
            "  root='/root/services/gamelife-vpn/'\n"
            "  detail='_'+''.join('1' if os.path.exists(root+p) else '0' for p in ('admin-access.json','node-access.json','relay-ci','ru-assets/current'))\n"
            " if isinstance(exc,ValueError):\n"
            "  try:\n"
            "   snap=sys.modules['installer'].inspect_container()\n"
            "   target='/opt/hiddify-manager/services/xray/bin/gamelife'\n"
            "   if not any(m.get('Destination')==target for m in snap.get('mounts',[])):detail='_MOUNT_ABSENT'\n"
            "  except Exception:pass\n"
            " print('PREFLIGHT_FAILED_'+type(exc).__name__+detail)\n").encode()


def main():
    if len(sys.argv) < 2 or any(node not in HOSTS for node in sys.argv[1:]):
        raise ValueError('choose known nodes')
    for node in sys.argv[1:]:
        process = subprocess.run(['ssh', '-T', '-i', str(KEY), '-o', 'BatchMode=yes',
                                  '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                                  '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                                  '-o', 'IdentityAgent=none',
                                  '-o', 'ConnectTimeout=15', 'root@' + HOSTS[node], 'python3 -'],
                                 input=program(node), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=180, check=False)
        line = process.stdout.decode('ascii', 'replace').strip()
        if process.returncode or not line.startswith('PREFLIGHT_') or '\n' in line or len(line) > 120:
            line = 'PREFLIGHT_TRANSPORT_OR_BOOTSTRAP_FAILED'
        print(node + ' ' + line, flush=True)


if __name__ == '__main__':
    main()
