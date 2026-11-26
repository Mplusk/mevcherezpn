"""Execute one audited node operation over pinned SSH.

Transport failure after a write is an unknown outcome. Inspect the node's
private journal and panel/apply status before choosing a recovery action.
Use outbound_controller for coordinated outbound activation and rollback.
"""
import re
import os
import subprocess
import sys
import preflight_controller as wire


def main():
    if len(sys.argv) != 4:
        raise ValueError('usage: node_controller.py ACTION NODE TX')
    action, node, tx = sys.argv[1:]
    if (action not in ('preflight', 'stage', 'verify-staged', 'rollback',
                       'verify-active', 'activate', 'disable', 'inspect-shape')
            or node not in wire.HOSTS or not re.fullmatch('[a-f0-9]{32}', tx)):
        raise ValueError('invalid arguments')
    sources = wire.read_sources()
    expected = os.environ.get('GAMELIFE_ROUTE_BUNDLE_SHA256')
    if expected is not None and wire.source_digest(sources) != expected:
        raise ValueError('source bundle changed')
    payload = wire.program(node, action, tx, sources=sources)
    try:
        process = subprocess.run(['ssh', '-T', '-i', str(wire.KEY), '-o', 'BatchMode=yes',
                                  '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                                  '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                                  '-o', 'IdentityAgent=none',
                                  '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
                                  '-o', 'ServerAliveCountMax=6', 'root@' + wire.HOSTS[node], 'python3 -'],
                                 input=payload, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=1100, check=False)
    except (OSError, subprocess.SubprocessError):
        print('NODE_OUTCOME_UNKNOWN_RECONCILE')
        return 1
    output = process.stdout.decode('ascii', 'replace').strip()
    if process.returncode != 0 or output != 'PREFLIGHT_OK':
        if (action == 'inspect-shape' and re.fullmatch(r'PREFLIGHT_FAILED_[A-Za-z0-9_]{1,90}', output)):
            print(output)
            return 1
        print('NODE_NOT_CONFIRMED_RECONCILE')
        return 1
    print('NODE_OK')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('NODE_ERROR_RECONCILE')
        raise SystemExit(1)
