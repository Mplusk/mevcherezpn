"""Refresh validated routing data without restarting either VPN core.

This entry point refuses to publish until the running Xray configuration has
the tested geodata schedule.  Hiddify watches the directory that `current`
resolved to at startup, so its original directory must be retained and its
unchanged SRS files atomically replaced to trigger a read of the new target.
No subscription data or generated configuration is printed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

import sync

CONTAINER = 'gamelife-hiddify-panel-1'
CRON = '*/10 * * * *'
SRS = ('geosite-ru-blocked.srs', 'geoip-ru-blocked.srs')

# Executed only inside the existing panel container; generated config contents
# and process command lines remain in memory and never cross the boundary.
INSPECT = r'''import json,os,re,subprocess
def pid(service):
 r=subprocess.run(['systemctl','show',service,'-p','MainPID'],capture_output=True,text=True,check=True,timeout=15)
 value=r.stdout.strip().split('=')[-1]
 if not value.isdigit() or int(value)<2: raise ValueError('service pid unavailable')
 subprocess.run(['systemctl','is-active','--quiet',service],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=True)
 return int(value)
def start(pid):
 # The parenthesised process name may contain spaces: fields after ')' begin
 # with field 3 and starttime is field 22.
 with open('/proc/%d/stat' % pid) as stream: value=stream.read()
 return int(value[value.rfind(')')+2:].split()[19])
xp=pid('hiddify-xray.service');sp=pid('hiddify-core.service')
config='/opt/hiddify-manager/generated/xray.json'
with open(config,'rb') as stream: data=json.load(stream)
if data.get('geodata',{}).get('cron') != '*/10 * * * *': raise ValueError('Xray hot reload not configured')
rules=data.get('routing',{}).get('rules',[])
if not any('ext:gamelife/current/geosite.dat:ru-blocked' in r.get('domain',[]) for r in rules if isinstance(r,dict)): raise ValueError('Xray site rule missing')
if not any('ext:gamelife/current/geoip.dat:ru-blocked' in r.get('ip',[]) for r in rules if isinstance(r,dict)): raise ValueError('Xray IP rule missing')
with open('/opt/hiddify-manager/generated/hiddify-core.json','rb') as stream: core=json.load(stream)
sets=core.get('route',{}).get('rule_set',[])
paths={r.get('path') for r in sets if isinstance(r,dict) and r.get('type')=='local' and r.get('format')=='binary'}
base='/opt/hiddify-manager/services/xray/bin/gamelife/current/'
if not {base+'geosite-ru-blocked.srs',base+'geoip-ru-blocked.srs'}.issubset(paths): raise ValueError('Hiddify local sets missing')
cmd=open('/proc/%d/cmdline' % xp,'rb').read()
if b'/opt/hiddify-manager/generated/xray.json' not in cmd: raise ValueError('unexpected Xray config')
boot=next(int(line.split()[1]) for line in open('/proc/stat') if line.startswith('btime '))
started=boot+start(xp)/os.sysconf('SC_CLK_TCK')
if os.stat(config).st_mtime > started+1: raise ValueError('Xray configuration may not be loaded')
inodes=set()
for name in os.listdir('/proc/%d/fdinfo' % sp):
 try:
  with open('/proc/%d/fdinfo/%s' % (sp,name)) as stream: text=stream.read()
  inodes.update(int(x,16) for x in re.findall(r'inotify wd:[0-9a-f]+ ino:([0-9a-f]+)',text))
 except OSError: pass
root='/opt/hiddify-manager/services/xray/bin/gamelife/revisions'
matches=[]
for name in os.listdir(root):
 if re.fullmatch('[0-9a-f]{64}',name):
  info=os.lstat(root+'/'+name)
  if os.path.isdir(root+'/'+name) and not os.path.islink(root+'/'+name) and info.st_ino in inodes: matches.append(name)
if len(matches)!=1: raise ValueError('Hiddify watcher revision unavailable')
print(json.dumps({'xray_pid':xp,'xray_start':start(xp),'core_pid':sp,'core_start':start(sp),'watched':matches[0]},separators=(',',':')))
'''


def inspect_runtime(runner=subprocess.run):
    result = runner(['docker', 'exec', CONTAINER, 'python3', '-c', INSPECT],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=40, check=False)
    if result.returncode or len(result.stdout) > 1024:
        raise ValueError('running cores are not ready for hot updates')
    data = json.loads(result.stdout)
    if (not isinstance(data, dict) or set(data) != {'xray_pid', 'xray_start', 'core_pid', 'core_start', 'watched'}
            or any(type(data[key]) is not int or data[key] <= 0 for key in ('xray_pid', 'xray_start', 'core_pid', 'core_start'))
            or not isinstance(data['watched'], str) or not re.fullmatch('[0-9a-f]{64}', data['watched'])):
        raise ValueError('invalid core identity')
    return data


def notify_watcher(root, revision):
    """Re-arm a running local rule-set watcher without changing asset bytes."""
    if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{64}', revision):
        raise ValueError('invalid watcher revision')
    revisions = Path(root) / 'revisions'
    directory = revisions / revision
    if directory.is_symlink() or directory.resolve(strict=True).parent != revisions.resolve(strict=True):
        raise ValueError('unsafe watcher directory')
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('unsafe watcher directory permissions')
    for name in SRS:
        path = directory / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('unsafe watched asset')
        data = path.read_bytes()
        sync.validate(name, data)
        expected = hashlib.sha256(data).digest()
        fd, temp_name = tempfile.mkstemp(prefix='.watch-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_name, 0o644)
            os.replace(temp_name, path)
            if hashlib.sha256(path.read_bytes()).digest() != expected:
                raise ValueError('watcher notification changed asset bytes')
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def run(root=sync.ROOT, fetcher=sync.fetch, validator=sync.validate_xray,
        inspect=inspect_runtime, notify=notify_watcher):
    before = inspect()
    watched = before['watched']
    def commit(_revision):
        if inspect() != before:
            raise RuntimeError('VPN core changed during asset update')
        notify(root, watched)
        if inspect() != before:
            raise RuntimeError('VPN core changed during notification')
    def rollback():
        # If a core restarted while publishing, notify its new watcher instead.
        now = inspect()
        notify(root, now['watched'])
    return sync.sync(root, fetcher, validator, protected=(watched,), on_commit=commit, on_rollback=rollback)


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('root required')
    try:
        run()
        print('RU_ASSETS_HOT_SYNC_OK')
    except Exception as error:
        print('RU_ASSETS_HOT_SYNC_FAILED ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
