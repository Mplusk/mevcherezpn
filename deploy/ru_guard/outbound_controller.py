"""Coordinated four-node outbound activation, readback and safe deactivation.

Node checks prove generated configuration, not user connectivity. The caller
must separately run independent VPN and client-routing checks. If an SSH
operation fails, its result may be unknown: stop and reconcile before retry.
"""
import re
import os
import subprocess
import sys
import time
from pathlib import Path
import preflight_controller as wire

NODES = ('de', 'pl', 'nl', 'main')
NODE_CONTROLLER = Path(__file__).with_name('node_controller.py')


class Incomplete(RuntimeError):
    pass


def run_node(action, node, tx, *, expected_digest=None):
    env = os.environ.copy()
    if expected_digest is not None:
        env['GAMELIFE_ROUTE_BUNDLE_SHA256'] = expected_digest
    try:
        result = subprocess.run([sys.executable, str(NODE_CONTROLLER), action, node, tx],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=1150, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == b'NODE_OK'


def check_nodes(action, tx, call, *, attempts=1, sleep=time.sleep):
    pending = list(NODES)
    for attempt in range(attempts):
        pending = [node for node in pending if not call(action, node, tx)]
        if not pending:
            return
        if attempt + 1 < attempts:
            sleep(10)
    raise Incomplete('node verification incomplete')


def run(action, tx, call=run_node, sleep=time.sleep):
    if action not in ('activate', 'verify', 'rollback') or not re.fullmatch('[a-f0-9]{32}', tx):
        raise ValueError('unsupported operation')
    if action == 'verify':
        check_nodes('verify-active', tx, call)
        return
    if action == 'activate':
        # Four positive staged observations are required immediately before the
        # only write. Main rechecks itself under the shared deployment lock.
        check_nodes('verify-staged', tx, call)
        if not call('activate', 'main', tx):
            raise Incomplete('activation result unknown; reconcile before rollback')
        check_nodes('verify-active', tx, call, attempts=6, sleep=sleep)
        return
    # `disable` does not restore templates or unmount data; it is allowed only
    # for the original main transaction after pending apply has stopped.
    if not call('disable', 'main', tx):
        raise Incomplete('deactivation result unknown; reconcile before retry')
    check_nodes('verify-staged', tx, call, attempts=6, sleep=sleep)


def main():
    if len(sys.argv) != 3:
        raise ValueError('usage: outbound_controller.py activate|verify|rollback TX')
    digest = wire.source_digest(wire.read_sources())
    run(sys.argv[1], sys.argv[2], call=lambda action, node, tx: run_node(
        action, node, tx, expected_digest=digest))
    print('ALL_NODE_CONFIGURATIONS_OK')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('OUTBOUND_NOT_CONFIRMED_RECONCILE', file=sys.stderr)
        raise SystemExit(1)
