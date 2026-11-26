"""Hot-update the RU entries in Xray's full built-in geodata databases.

The panel's server safeguard also uses unqualified ``geoip:ru`` and
``geosite:category-ru``.  Keep every other built-in entry byte-for-byte,
including the private-address safeguards and any categories added by an admin.
Never print generated configuration or account data.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile

import sync
from reduce import MAX_BYTES, reduce_dat

CONTAINER = 'gamelife-hiddify-panel-1'
BASE = '/opt/hiddify-manager/services/xray/bin/'
CONFIG = '/opt/hiddify-manager/generated/xray.json'
CODES = {'geoip.dat': 'RU', 'geosite.dat': 'CATEGORY-RU'}


def merge(original, managed, code):
    """Replace only one named protobuf entry, retaining all other records."""
    wanted = frozenset((code,))
    previous = reduce_dat(original, wanted)
    replacement = reduce_dat(managed, wanted)
    if original.count(previous) != 1:
        raise ValueError('ambiguous original geodata entry')
    candidate = original.replace(previous, replacement, 1)
    if len(candidate) > MAX_BYTES or reduce_dat(candidate, wanted) != replacement:
        raise ValueError('merged geodata is invalid')
    return candidate


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for name, content in files.items():
            item = tarfile.TarInfo(name)
            item.mode, item.size = 0o600, len(content)
            tar.addfile(item, io.BytesIO(content))
    return stream.getvalue()


def validate_candidate(config, originals, managed, runner=subprocess.run):
    """Test the real server configuration in a disposable, networkless core."""
    files = {'config.json': config}
    files.update(originals)
    files.update({'gamelife/current/' + name: data for name, data in managed.items()})
    script = ('set -eu; d=$(mktemp -d); tar -xf - -C "$d"; '
              'export XRAY_LOCATION_ASSET="$d"; '
              '/opt/hiddify-manager/services/xray/bin/xray run -test -c "$d/config.json" >/dev/null 2>&1')
    result = runner(['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                     '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                     '--memory', '256m', '--pids-limit', '64', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec',
                     '--entrypoint', '/bin/sh', sync.IMAGE, '-c', script],
                    input=archive(files), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=90, check=False)
    if result.returncode:
        raise ValueError('Xray rejected merged built-in geodata')


# The inputs are public DAT files only.  Compare the old hashes before
# replacing, and atomically rename each validated file on the same filesystem.
# A failure between renames leaves valid old/new files; the next run repairs it.
WRITE = r'''import hashlib,io,os,stat,sys,tarfile,tempfile
base='/opt/hiddify-manager/services/xray/bin'
names=('geoip.dat','geosite.dat')
old=dict(zip(names,sys.argv[1:3])); expected=dict(zip(names,sys.argv[3:5]))
if len(sys.argv)!=5: raise ValueError('invalid arguments')
contents={}
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as tar:
 for member in tar:
  if member.name not in names or member.name in contents or not member.isfile() or member.size<=0 or member.size>134217728: raise ValueError('invalid input')
  contents[member.name]=tar.extractfile(member).read()
if set(contents)!=set(names): raise ValueError('missing input')
temps={}
try:
 for name in names:
  path=os.path.join(base,name); info=os.lstat(path)
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022: raise ValueError('unsafe default asset')
  if hashlib.sha256(open(path,'rb').read()).hexdigest()!=old[name]: raise ValueError('default asset changed')
  if hashlib.sha256(contents[name]).hexdigest()!=expected[name]: raise ValueError('candidate digest mismatch')
  fd,tmp=tempfile.mkstemp(prefix='.gamelife-geo-',dir=base); temps[name]=tmp
  with os.fdopen(fd,'wb') as stream:
   os.fchmod(stream.fileno(),stat.S_IMODE(info.st_mode)); os.fchown(stream.fileno(),info.st_uid,info.st_gid)
   stream.write(contents[name]); stream.flush(); os.fsync(stream.fileno())
 for name in names:
  path=os.path.join(base,name)
  if hashlib.sha256(open(path,'rb').read()).hexdigest()!=old[name]: raise ValueError('default asset changed')
  os.replace(temps.pop(name),path)
 fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
finally:
 for path in temps.values():
  try:os.unlink(path)
  except FileNotFoundError:pass
'''


def update(root, *, inspect, runner=subprocess.run):
    """Merge the current RU rules while retaining every other Xray category."""
    root = Path(root)
    fd = os.open(root / '.sync.lock', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('unsafe sync lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = inspect()
        selector = os.readlink(root / 'current')
        if not re.fullmatch(r'revisions/[0-9a-f]{64}', selector):
            raise ValueError('unsafe current revision')
        if (root / 'current').resolve(strict=True).parent != (root / 'revisions').resolve(strict=True):
            raise ValueError('current revision escapes asset root')
        result = runner(['docker', 'inspect', '--format', '{{.Id}}', CONTAINER],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
        container_id = result.stdout.decode('ascii').strip()
        if not re.fullmatch('[0-9a-f]{64}', container_id):
            raise ValueError('invalid panel identity')

        def get(path, maximum):
            result = runner(['docker', 'exec', container_id, 'cat', path],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=True)
            if not result.stdout or len(result.stdout) > maximum:
                raise ValueError('invalid panel data size')
            return result.stdout

        config = get(CONFIG, 16 * 1024 * 1024)
        parsed = json.loads(config)
        if not isinstance(parsed, dict):
            raise ValueError('invalid Xray configuration')
        current = {}
        candidates = {}
        managed = {}
        for name, code in CODES.items():
            path = root / 'current' / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError('unsafe managed asset')
            data = path.read_bytes()
            sync.validate(name, data)
            managed[name] = data
            current[name] = get(BASE + name, MAX_BYTES)
            candidates[name] = merge(current[name], data, code)
        if candidates == current:
            return False
        validate_candidate(config, candidates, managed, runner)
        if os.readlink(root / 'current') != selector or inspect() != before:
            raise RuntimeError('running configuration changed during validation')
        old = [hashlib.sha256(current[n]).hexdigest() for n in CODES]
        new = [hashlib.sha256(candidates[n]).hexdigest() for n in CODES]
        result = runner(['docker', 'exec', '-i', container_id, 'python3', '-c', WRITE, *old, *new],
                        input=archive(candidates), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=90, check=False)
        if result.returncode:
            raise RuntimeError('default geodata update failed')
        if os.readlink(root / 'current') != selector or inspect() != before:
            raise RuntimeError('running configuration changed during publication')
        return True
    finally:
        os.close(fd)
