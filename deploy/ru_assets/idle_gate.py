"""Protect existing connections during a one-time, idle-only core apply.

The short gate rejects only new TCP SYN packets to the known local VLESS
listener. A detached in-container watchdog removes its unique rule even if the
operator crashes. Other VPN servers must be independently checked before use.
"""
from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
import time

CONTAINER = 'gamelife-hiddify-panel-1'


class Busy(RuntimeError):
    pass


def docker(args, *, runner=subprocess.run, capture=False, timeout=30):
    return runner(['docker', 'exec', CONTAINER, *args],
                  stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, timeout=timeout, check=True)


def inspect_inbounds(runner=subprocess.run, *, allow_empty_xray=False):
    configs = []
    for name in ('xray.json', 'hiddify-core.json'):
        response = docker(['cat', '/opt/hiddify-manager/generated/' + name], runner=runner, capture=True)
        if len(response.stdout) > 16 * 1024 * 1024:
            raise ValueError('configuration too large')
        configs.append(json.loads(response.stdout))
    xitems = configs[0].get('inbounds')
    citems = configs[1].get('inbounds')
    if xitems is None and allow_empty_xray:
        xitems = []
    if not isinstance(xitems, list) or not isinstance(citems, list) or (not xitems and not allow_empty_xray):
        raise ValueError('inbound lists unavailable')
    xports, coreports = [], []
    for item in xitems:
        stream = item.get('streamSettings') or {}
        port = item.get('port')
        if (item.get('listen') != '127.0.0.1' or item.get('protocol') != 'vless'
                or stream.get('network') not in ('ws', 'websocket')
                or type(port) is not int or not 1 <= port <= 65535):
            raise ValueError('unsupported user inbound; do not apply')
        xports.append(port)
    for item in citems:
        port = item.get('listen_port')
        if (item.get('listen') != '127.0.0.1' or item.get('type') not in ('socks', 'shadowsocks')
                or type(port) is not int or not 1 <= port <= 65535):
            raise ValueError('unsupported Hiddify inbound; do not apply')
        coreports.append(port)
    if len(set(xports + coreports)) != len(xports + coreports):
        raise ValueError('overlapping listener ports')
    return xports, coreports


def active_connections(ports, runner=subprocess.run):
    total = 0
    for port in ports:
        for state in ('established', 'syn-recv', 'syn-sent'):
            response = docker(['ss', '-Htn', 'state', state, 'sport', '=', ':' + str(port)],
                              runner=runner, capture=True)
            if len(response.stdout) > 1024 * 1024:
                raise ValueError('connection listing too large')
            total += len(response.stdout.splitlines())
    return total


def rule(port, token):
    if type(port) is not int or not 1 <= port <= 65535 or not re.fullmatch('GL-GEO-[a-f0-9]{16}', token):
        raise ValueError('invalid gate')
    # --syn is deliberate: conntrack NEW may incorrectly match established
    # sockets when connection tracking is first enabled in the namespace.
    return ['-p', 'tcp', '-d', '127.0.0.1', '--dport', str(port), '--syn',
            '-m', 'comment', '--comment', token, '-j', 'REJECT']


class IdleGate:
    def __init__(self, xports, coreports, *, runner=subprocess.run, token=None, watchdog_seconds=1200, sleep=time.sleep):
        self.xports = list(xports)
        self.all_ports = self.xports + list(coreports)
        self.runner = runner
        self.token = token or 'GL-GEO-' + secrets.token_hex(8)
        if not 60 <= watchdog_seconds <= 1800:
            raise ValueError('invalid watchdog interval')
        self.watchdog_seconds = watchdog_seconds
        self.sleep = sleep
        self.installed = []

    def __enter__(self):
        if active_connections(self.all_ports, self.runner):
            raise Busy('node has active connections')
        try:
            for port in self.all_ports:
                args = rule(port, self.token)
                # Start the watchdog before adding a rule. A container restart
                # destroys the network namespace and therefore also the rule.
                cleanup = shlex.join(['/usr/sbin/iptables', '-w', '5', '-D', 'OUTPUT', *args])
                script = 'sleep ' + str(self.watchdog_seconds) + '; ' + cleanup + ' >/dev/null 2>&1 || true'
                self.runner(['docker', 'exec', '-d', CONTAINER, 'sh', '-c', script],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
                self.installed.append(port)
                docker(['iptables', '-w', '5', '-I', 'OUTPUT', '1', *args], runner=self.runner)
            if active_connections(self.all_ports, self.runner):
                raise Busy('connection arrived while the gate was installed')
            self.sleep(2)
            if active_connections(self.all_ports, self.runner):
                raise Busy('connection arrived while the gate was settling')
            return self
        except Exception:
            self.close()
            raise

    def close(self):
        errors = []
        for port in reversed(self.installed):
            try:
                docker(['iptables', '-w', '5', '-D', 'OUTPUT', *rule(port, self.token)], runner=self.runner)
            except Exception:
                errors.append(port)
        self.installed.clear()
        if errors:
            raise RuntimeError('gate cleanup not confirmed; watchdog remains active')

    def confirm(self):
        if len(self.installed) != len(self.all_ports):
            raise RuntimeError('gate is incomplete')
        for port in self.installed:
            docker(['iptables', '-w', '5', '-C', 'OUTPUT', *rule(port, self.token)], runner=self.runner)
        if active_connections(self.all_ports, self.runner):
            raise Busy('connection appeared while gated')

    def __exit__(self, *_):
        self.close()
