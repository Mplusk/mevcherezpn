"""Install a host firewall with an automatic rollback until verified."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parent
BACKUP = Path('/root/services/gamelife-vpn/firewall-rollback')
FILES = {'default-ufw': Path('/etc/default/ufw'),
         'user.rules': Path('/etc/ufw/user.rules'),
         'user6.rules': Path('/etc/ufw/user6.rules')}
GUARD = Path('/usr/local/sbin/gamelife-docker-guard')
ROLLBACK = Path('/usr/local/sbin/gamelife-firewall-rollback')
UNIT = Path('/etc/systemd/system/gamelife-docker-guard.service')
TIMER = 'gamelife-firewall-rollback-v1.timer'


def run(args, *, output=False):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90)
    if result.returncode:
        raise RuntimeError('firewall operation failed')
    return result.stdout.decode('utf-8', errors='replace') if output else ''


def portal_subnet():
    """Preserve the gateway's access to the unrelated host-local portal."""
    gateway = json.loads(run(['docker', 'inspect', 'gamelife-gateway-proxy-1'], output=True))[0]
    networks = gateway['NetworkSettings']['Networks']
    if len(networks) != 1:
        raise RuntimeError('unexpected gateway network')
    name, data = next(iter(networks.items()))
    network = json.loads(run(['docker', 'network', 'inspect', name], output=True))[0]
    address = ipaddress.ip_address(data['IPAddress'])
    candidates = [ipaddress.ip_network(item['Subnet']) for item in network['IPAM']['Config']
                  if item.get('Subnet') and ipaddress.ip_network(item['Subnet']).version == 4]
    matches = [n for n in candidates if address in n and n.is_private]
    if len(matches) != 1 or matches[0].prefixlen < 16:
        raise RuntimeError('unexpected portal subnet')
    return str(matches[0])


def copy_new(source, destination, mode):
    data = source.read_bytes()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def install(main):
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    status = run(['ufw', 'status'], output=True)
    if 'Status: inactive' not in status:
        raise RuntimeError('existing firewall requires manual review')
    existing = run(['ufw', 'show', 'added'], output=True)
    if any(line.strip().startswith(('ufw allow', 'ufw deny', 'ufw reject', 'ufw limit', 'ufw route'))
           for line in existing.splitlines()):
        raise RuntimeError('existing UFW rules require manual review')
    if any(path.exists() for path in (BACKUP, GUARD, ROLLBACK, UNIT)):
        raise RuntimeError('firewall guard already exists')
    for tool in ('iptables', 'ip6tables'):
        run([tool, '-w', '-S', 'DOCKER-USER'])
        probe = subprocess.run([tool, '-w', '-S', 'GAMELIFE-DOCKER-GUARD'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if probe.returncode == 0:
            raise RuntimeError('firewall guard chain already exists')
    subnet = portal_subnet() if main else None
    BACKUP.mkdir(mode=0o700)
    for name, path in FILES.items():
        if path.is_file():
            shutil.copy2(path, BACKUP / name)
    copy_new(SOURCE / 'docker-guard.sh', GUARD, 0o700)
    copy_new(SOURCE / 'rollback.sh', ROLLBACK, 0o700)
    copy_new(SOURCE / 'docker-guard.service', UNIT, 0o644)
    run(['systemctl', 'daemon-reload'])
    # If SSH or the site fails after enabling the rules, systemd reverts them.
    run(['systemd-run', '--quiet', '--on-active=240s', '--unit=gamelife-firewall-rollback-v1', str(ROLLBACK)])
    try:
        run(['ufw', 'default', 'deny', 'incoming'])
        run(['ufw', 'default', 'allow', 'outgoing'])
        for port in (22, 80, 443):
            run(['ufw', 'allow', str(port) + '/tcp'])
        if subnet:
            run(['ufw', 'allow', 'from', subnet, 'to', 'any', 'port', '18731', 'proto', 'tcp'])
        run(['systemctl', 'enable', '--now', 'gamelife-docker-guard.service'])
        run(['ufw', '--force', 'enable'])
        if 'Status: active' not in run(['ufw', 'status'], output=True):
            raise RuntimeError('firewall did not become active')
        run(['systemctl', 'is-active', '--quiet', 'gamelife-docker-guard.service'])
        print('FIREWALL_APPLIED_PENDING_CONFIRM', flush=True)
    except BaseException:
        subprocess.run([str(ROLLBACK)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        raise


def confirm():
    if os.geteuid() != 0 or 'Status: active' not in run(['ufw', 'status'], output=True):
        raise RuntimeError('firewall is not active')
    run(['systemctl', 'is-active', '--quiet', 'gamelife-docker-guard.service'])
    for tool in ('iptables', 'ip6tables'):
        run([tool, '-w', '-C', 'DOCKER-USER', '-j', 'GAMELIFE-DOCKER-GUARD'])
    run(['systemctl', 'stop', TIMER])
    print('FIREWALL_CONFIRMED', flush=True)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ('node', 'main', 'confirm'):
            raise ValueError('invalid invocation')
        confirm() if sys.argv[1] == 'confirm' else install(sys.argv[1] == 'main')
    except Exception:
        print('FIREWALL_ERROR', file=sys.stderr, flush=True)
        raise SystemExit(1)
