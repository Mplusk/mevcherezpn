"""PROTOTYPE ONLY: relay CI decision logic; not wired to the dispatcher.

The adapter and journal are intentionally unspecified. This code is not safe
to call on a production relay without two-phase delivery, authenticated node
preflight, durable recovery, and independent VPN health/rollback verification.
"""
from __future__ import annotations

import copy
import re

TARGET = '/opt/hiddify-manager/services/xray/bin/gamelife'
SOURCE = '/root/services/gamelife-vpn/ru-assets'
HOOK_GENERATION = 1
REQUIRED = ('head_verified', 'ci_verified', 'host_verified', 'assets_verified',
            'image_verified', 'compose_matches_runtime', 'networks_verified',
            'baseline_healthy', 'independent_vpn_healthy', 'rollback_ready',
            'templates_independent', 'journal_durable')


class HookError(RuntimeError):
    pass


class RollbackUnsafe(HookError):
    pass


def asset_mount(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('mounts'), list):
        raise HookError('invalid runtime snapshot')
    if any(not isinstance(m, dict) or not isinstance(m.get('Destination'), str) for m in snapshot['mounts']):
        raise HookError('invalid mount entry')
    mounts = [m for m in snapshot['mounts'] if m.get('Destination') == TARGET]
    if len(mounts) > 1:
        raise HookError('ambiguous asset mount')
    if not mounts:
        return False
    mount = mounts[0]
    if (mount.get('Type') != 'bind' or mount.get('Source') != SOURCE
            or mount.get('RW') is not False):
        raise HookError('unexpected asset mount')
    return True


def run(adapter, revision, *, staging=False):
    """Return unchanged, absent, or installed; never delete an existing mount.

    `adapter.dispatcher_generation` must be captured by the current process
    at startup; reading a rewritten dispatcher file is not proof that this
    process loaded the new allowlist. All probes must be implemented with
    actual checks, and rechecked by the adapter under a shared deployment lock.
    """
    if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise HookError('invalid revision')
    if type(staging) is not bool:
        raise HookError('invalid staging flag')
    before = copy.deepcopy(adapter.inspect())
    if asset_mount(before):
        # This path must never call install, recreate, rollback, or detach.
        if adapter.verify_existing(before, revision) is not True:
            raise HookError('existing mount is not verified')
        return 'unchanged'
    if not staging:
        return 'absent'
    if getattr(adapter, 'dispatcher_generation', None) != HOOK_GENERATION:
        raise HookError('new dispatcher must be loaded in a later invocation')
    proof = adapter.preflight(revision, before)
    if (not isinstance(proof, dict) or proof.get('revision') != revision
            or any(proof.get(key) is not True for key in REQUIRED)):
        raise HookError('staging preflight is incomplete')
    # Unknown or already-dependent templates make any mount rollback unsafe.
    if adapter.templates_depend_on_mount() is not False:
        raise HookError('template dependency prevents safe staging')
    try:
        # The production adapter must journal intent durably before recreation,
        # perform only the verified operation, and implement atomic recovery.
        adapter.install(before, revision)
        after = adapter.inspect()
        if not asset_mount(after) or adapter.verify_installed(before, after, revision) is not True:
            raise HookError('post-install verification failed')
        return 'installed'
    except Exception as error:
        try:
            current = adapter.inspect()
            installed = asset_mount(current)
            if installed:
                if adapter.templates_depend_on_mount() is not False:
                    raise RollbackUnsafe('templates depend on assets; retain mount')
                adapter.rollback(before, revision)
            restored = adapter.inspect()
            if asset_mount(restored) or adapter.verify_restored(before, restored, revision) is not True:
                raise RollbackUnsafe('rollback was not verified')
        except Exception as rollback:
            raise RollbackUnsafe('rollback uncertain; do not detach mount') from rollback
        raise HookError('staging failed; prior state was verified') from error
