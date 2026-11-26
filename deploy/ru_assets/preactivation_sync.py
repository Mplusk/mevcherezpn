"""Sequential pre-activation asset synchronisation, operator driven.

Do not use after a generated core config starts referencing gamelife/current.
No VPN credentials or generated configs are printed. This does not implement
post-activation reload, watchdog rollback or ongoing revision consistency.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

HOSTS = ('132.243.234.163', '2.27.75.91', '191.217.211.240', '153.52.155.69')
URL = 'https://nl.gamelife.shop/assets/geodata/manifest.json'
FILES = ('geoip.dat', 'geosite.dat', 'geosite-ru-blocked.srs', 'geoip-ru-blocked.srs')

REMOTE = r'''
import hashlib,json,os,subprocess,sys,urllib.request
expected=EXPECTED
paths=['/opt/hiddify-manager/generated/xray.json','/opt/hiddify-manager/generated/hiddify-core.json']
names=('geoip.dat','geosite.dat','geosite-ru-blocked.srs','geoip-ru-blocked.srs')
root='/root/services/gamelife-vpn/ru-assets'
def safe():
 for p in paths:
  r=subprocess.run(['docker','exec','gamelife-hiddify-panel-1','cat',p],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=20,check=True)
  if b'gamelife/current/' in r.stdout: raise RuntimeError('active configuration uses assets')
def public():
 with urllib.request.urlopen('https://nl.gamelife.shop/assets/geodata/manifest.json',timeout=20) as f:
  if json.load(f)!=expected: raise RuntimeError('public manifest changed')
def compare():
 if os.path.basename(os.path.realpath(root+'/current'))!=expected['revision']: return False
 return all(hashlib.sha256(open(root+'/current/'+n,'rb').read()).hexdigest()==expected['sha256'][n] for n in names)
try:
 safe();public()
 if ACTION=='sync':
  if subprocess.run(['systemctl','is-active','--quiet','gamelife-ru-assets.service']).returncode==0: raise RuntimeError('sync already running')
  subprocess.run(['systemctl','start','gamelife-ru-assets.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=420,check=True)
  safe();public()
 if ACTION in ('sync','verify') and not compare(): raise RuntimeError('current files differ')
 print('OK')
except Exception:
 print('FAILED')
 sys.exit(1)
'''


def manifest():
    with urllib.request.urlopen(URL, timeout=20) as response:
        raw = response.read(8193)
    data = json.loads(raw)
    if len(raw) > 8192 or not isinstance(data, dict) or set(data) != {'version', 'revision', 'sha256'}:
        raise ValueError('invalid manifest')
    import hashlib
    import re
    hashes = data['sha256']
    if (type(data['version']) is not int or data['version'] != 1 or not isinstance(hashes, dict)
            or set(hashes) != set(FILES) or any(not isinstance(x, str) or not re.fullmatch('[0-9a-f]{64}', x) for x in hashes.values())):
        raise ValueError('invalid manifest fields')
    revision = hashlib.sha256(b'gamelife-geo-manifest-v1\x00' + json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if revision != data['revision']:
        raise ValueError('invalid manifest revision')
    return data


def run_node(host, key, expected, action):
    program = REMOTE.replace('EXPECTED', repr(expected)).replace('ACTION', repr(action))
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes', '-i', str(key),
               'root@' + host, 'python3 -']
    result = subprocess.run(command, input=program.encode(), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=450)
    if result.returncode or result.stdout.strip() != b'OK':
        raise RuntimeError('node operation failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true', help='actually start sync sequentially')
    mode.add_argument('--verify-only', action='store_true', help='check all active hashes without syncing')
    args = parser.parse_args()
    if not args.key.is_file():
        raise ValueError('SSH key unavailable')
    expected = manifest()
    for host in HOSTS:
        run_node(host, args.key, expected, 'check')
        print('PREFLIGHT_OK ' + host, flush=True)
    if args.verify_only:
        for host in HOSTS:
            run_node(host, args.key, expected, 'verify')
        if manifest() != expected:
            raise RuntimeError('public manifest changed')
        print('ALL_NODES_MATCH', flush=True)
        return
    if not args.execute:
        print('DRY_RUN_OK')
        return
    for host in HOSTS:
        if manifest() != expected:
            raise RuntimeError('public manifest changed')
        run_node(host, args.key, expected, 'sync')
        print('SYNC_OK ' + host, flush=True)
    if manifest() != expected:
        raise RuntimeError('public manifest changed')
    for host in HOSTS:
        run_node(host, args.key, expected, 'verify')
    if manifest() != expected:
        raise RuntimeError('public manifest changed')
    print('ALL_NODES_MATCH', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('PREACTIVATION_SYNC_FAILED ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
