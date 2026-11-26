"""Perform private, real VLESS probes without logging profiles or responses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

import node_install

PROFILES = Path('/etc/gamelife-ru-assets/profiles.json')


class ProbeFailed(RuntimeError):
    def __init__(self, node):
        super().__init__('VPN probe failed')
        self.node = node


def read_profiles(path=PROFILES):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 256 * 1024:
            raise ValueError('unsafe monitoring profiles')
        profiles = json.load(stream)
    if not isinstance(profiles, dict) or set(profiles) != set(node_install.NODES):
        raise ValueError('unexpected monitoring profile list')
    for name, outbound in profiles.items():
        node_install.validate_request({'action': 'stage', 'node': name, 'outbound': outbound})
    return profiles


def probe(names, *, profiles=None, attempts=2, sleep=time.sleep):
    if not isinstance(names, (list, tuple)) or not names or any(name not in node_install.NODES for name in names):
        raise ValueError('unexpected probe targets')
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError('invalid probe attempts')
    profiles = read_profiles() if profiles is None else profiles
    with tempfile.TemporaryDirectory(prefix='gamelife-geo-probe-') as tmp:
        binary = Path(tmp) / 'xray'
        subprocess.run(['docker', 'cp', node_install.installer.CONTAINER + ':/opt/hiddify-manager/services/xray/bin/xray', str(binary)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
        binary.chmod(0o700)
        for name in names:
            for attempt in range(attempts):
                if node_install.probe(binary, profiles[name], node_install.NODES[name][1]):
                    break
                if attempt + 1 < attempts:
                    sleep(3)
            else:
                raise ProbeFailed(name)
