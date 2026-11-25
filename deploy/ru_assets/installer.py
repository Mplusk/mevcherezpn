"""Proof of concept: add a read-only geo asset bind to a node Compose file.

This module is not wired into the relay dispatcher; do not use for production
until bootstrap, health, rollback and two-phase CI delivery are integrated.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

ANCHOR = '      - panel:/opt/hiddify-manager/data\n'
MARKER = '      # gamelife-ru-assets-v1\n'
TARGET = '/opt/hiddify-manager/services/xray/bin/gamelife'
CONTAINER = 'gamelife-hiddify-panel-1'
IMAGE = 'ghcr.io/hiddify/hiddify-manager:v14.2.0@sha256:399c248e20d139a7dab9cf05566751bcbbce30b3a03ae832525e95dfb78333c9'
NETWORKS = {'gamelife-hiddify_ingress': {'gamelife-hiddify-panel-1', 'panel'},
            'gamelife-hiddify_storage': {'gamelife-hiddify-panel-1', 'panel'},
            'gamelife-vpn-gateway': {'gamelife-vpn-origin'}}


def inspect_container():
    """Collect only non-secret Docker fields; never inspect the container env."""
    fmt = ('{{json (index .Config.Labels "com.docker.compose.project")}}\n'
           '{{json (index .Config.Labels "com.docker.compose.service")}}\n'
           '{{json (index .Config.Labels "com.docker.compose.project.config_files")}}\n'
           '{{json (index .Config.Labels "com.docker.compose.project.working_dir")}}\n'
           '{{json .Config.Image}}\n{{json .State.Running}}\n'
           '{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}\n'
           '{{json .NetworkSettings.Networks}}\n{{json .HostConfig.PortBindings}}\n{{json .Mounts}}')
    result = subprocess.run(['docker', 'inspect', '--format', fmt, CONTAINER],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=True)
    values = [json.loads(line) for line in result.stdout.decode('utf-8').splitlines()]
    if len(values) != 10:
        raise ValueError('invalid Docker inspection')
    return dict(zip(('project', 'service', 'config_files', 'working_dir', 'image',
                     'running', 'health', 'networks', 'ports', 'mounts'), values))


def validate_snapshot(snapshot, compose, expected_assets=None):
    if snapshot.get('project') != 'gamelife-hiddify' or snapshot.get('service') != 'panel':
        raise ValueError('unexpected Compose labels')
    files = snapshot.get('config_files')
    if not isinstance(files, str) or ',' in files or Path(files).resolve() != compose.resolve():
        raise ValueError('container does not use exactly this Compose file')
    if Path(snapshot.get('working_dir') or '/').resolve() != compose.parent.resolve():
        raise ValueError('unexpected Compose working directory')
    if snapshot.get('image') != IMAGE or snapshot.get('running') is not True or snapshot.get('health') != 'healthy':
        raise ValueError('unexpected image or panel is not healthy')
    networks = snapshot.get('networks')
    if not isinstance(networks, dict) or set(networks) != set(NETWORKS):
        raise ValueError('unexpected container networks')
    for name, aliases in NETWORKS.items():
        entry = networks[name]
        if set(entry.get('Aliases') or []) != aliases or entry.get('IPAMConfig') not in ({}, None):
            raise ValueError('unexpected network aliases or static addressing')
    mounts = snapshot.get('mounts')
    if not isinstance(mounts, list):
        raise ValueError('container mounts are unavailable')
    asset_mounts = [mount for mount in mounts if mount.get('Destination') == TARGET]
    if expected_assets is None:
        if asset_mounts:
            raise ValueError('unexpected existing asset mount')
    elif (len(asset_mounts) != 1 or asset_mounts[0].get('Type') != 'bind'
          or asset_mounts[0].get('RW') is not False
          or Path(asset_mounts[0].get('Source') or '/').resolve() != expected_assets.resolve()):
        raise ValueError('asset bind differs or is writable')


def restore_gateway(snapshot, runner):
    networks = snapshot.get('networks') or {}
    if 'gamelife-vpn-gateway' not in networks:
        runner(['docker', 'network', 'connect', '--alias', 'gamelife-vpn-origin',
                'gamelife-vpn-gateway', CONTAINER])


def transform(source: str, assets: Path) -> str:
    if not assets.is_absolute() or any(c in str(assets) for c in '\n\r:$'):
        raise ValueError('unsafe asset path')
    line = f'      - {assets}:{TARGET}:ro\n'
    if source.count(MARKER) == 1 and source.count(line) == 1:
        return source
    if MARKER in source or TARGET in source or source.count(ANCHOR) != 1:
        raise ValueError('unexpected Compose source')
    return source.replace(ANCHOR, ANCHOR + MARKER + line, 1)


def atomic(path: Path, data: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix='.ru-assets-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check_assets(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError('asset root missing or unsafe')
    current = root / 'current'
    if not current.is_symlink():
        raise ValueError('asset revision missing')
    resolved = current.resolve(strict=True)
    if resolved.parent != (root / 'revisions').resolve(strict=True):
        raise ValueError('asset revision escapes its directory')
    for name in ('geoip.dat', 'geosite.dat', 'geoip-ru-blocked.srs', 'geosite-ru-blocked.srs'):
        file = resolved / name
        if file.is_symlink() or not file.is_file() or file.stat().st_size == 0:
            raise ValueError('asset file missing or unsafe')
        if not file.stat().st_mode & stat.S_IROTH:
            raise ValueError('asset file not readable in container')


def install(compose: Path, assets: Path, envfile: Path, runner, verify, inspect,
            templates_depend_on_mount) -> bool:
    """Patch a single validated Compose file; restore it and recreate on failure.

    The supplied dependency probe must query each affected node's templates and
    return False only after proving no active template uses this mount. `verify`
    must wait for panel health and perform an independent end-to-end VPN check.
    """
    check_assets(assets)
    if compose.is_symlink() or not compose.is_file() or envfile.is_symlink() or not envfile.is_file():
        raise ValueError('unsafe Compose or environment file')
    original = compose.read_bytes()
    updated = transform(original.decode('utf-8'), assets).encode('utf-8')
    before = inspect()
    validate_snapshot(before, compose, assets if original == updated else None)
    if original == updated:
        verify()
        return False
    if templates_depend_on_mount() is not False:
        raise ValueError('templates already depend on assets or dependency is unknown')
    mode = stat.S_IMODE(compose.stat().st_mode)
    backup_dir = assets / 'compose-backups'
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    info = backup_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('unsafe Compose backup directory')
    backup = backup_dir / (str(time.time_ns()) + '.yaml')
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(original)
        out.flush()
        os.fsync(out.fileno())
    # Validate a candidate next to the original so relative Compose paths stay
    # unchanged; no configuration (which can contain secrets) is printed.
    fd, tmp = tempfile.mkstemp(prefix='.ru-compose-', suffix='.yaml', dir=compose.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(updated)
        runner(['docker', 'compose', '--env-file', str(envfile), '-f', tmp, 'config', '--quiet'])
        atomic(compose, updated, mode)
        try:
            runner(['docker', 'compose', '--env-file', str(envfile), '-f', str(compose), 'up', '-d', '--no-deps', 'panel'])
            restore_gateway(inspect(), runner)
            verify()
            after = inspect()
            validate_snapshot(after, compose, assets)
            if after.get('ports') != before.get('ports'):
                raise ValueError('container port bindings changed')
        except Exception as error:
            # Removing the bind before reverting dependent templates would make
            # Xray fail on its next start. Unknown is deliberately unsafe too.
            try:
                safe_to_remove = templates_depend_on_mount() is False
            except Exception:
                safe_to_remove = False
            if not safe_to_remove:
                raise RuntimeError('rollback unsafe: templates may depend on the asset mount') from error
            atomic(compose, original, mode)
            try:
                runner(['docker', 'compose', '--env-file', str(envfile), '-f', str(compose), 'up', '-d', '--no-deps', 'panel'])
                restore_gateway(inspect(), runner)
                verify()
                restored = inspect()
                validate_snapshot(restored, compose)
                if restored.get('ports') != before.get('ports'):
                    raise ValueError('rollback port bindings differ')
            except Exception as rollback:
                raise RuntimeError('rollback failed; manual recovery required') from rollback
            raise RuntimeError('installation failed; rollback completed') from error
    finally:
        os.unlink(tmp)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('compose', type=Path)
    parser.add_argument('assets', type=Path)
    args = parser.parse_args()
    # The CLI is intentionally read-only; callers need a real node-specific
    # health check and release orchestration before invoking install().
    check_assets(args.assets)
    transform(args.compose.read_text(encoding='utf-8'), args.assets)
    print('RU_ASSETS_PREFLIGHT_OK')


if __name__ == '__main__':
    main()
