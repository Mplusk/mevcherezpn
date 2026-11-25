"""Build explicit Compose arguments for both parent and node deployments.

This is a proposal, not connected to any production deployment. The caller
must supply the existing release environment, validate data and health, and
coordinate rollback before running the resulting commands.
"""
from __future__ import annotations

from pathlib import Path


def compose_args(release: Path, envfile: Path):
    if not release.is_absolute() or not envfile.is_absolute():
        raise ValueError('release and envfile must be absolute')
    base = release / 'compose.panel.yaml'
    extension = release / 'deploy/ru_assets/compose.ru-assets.yaml'
    for file in (base, extension, envfile):
        if file.is_symlink() or not file.is_file():
            raise ValueError('missing or unsafe Compose input')
    return ['docker', 'compose', '--env-file', str(envfile), '-f', str(base), '-f', str(extension)]


def validate_command(release: Path, envfile: Path):
    return compose_args(release, envfile) + ['config', '--quiet']


def recreate_panel_command(release: Path, envfile: Path):
    return compose_args(release, envfile) + ['up', '-d', '--no-deps', 'panel']
