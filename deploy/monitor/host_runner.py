"""Run the existing probes and notifier from a root-only JSON credential file."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

SECRETS = Path('/etc/gamelife-vpn-monitor/credentials.json')
ROOT = Path('/opt/gamelife-vpn-monitor')
STATE = '/var/lib/gamelife-vpn-monitor/state.json'
REQUIRED = {'MONITOR_PROFILE_JSON', 'MONITOR_SUBSCRIPTION_URL', 'MONITOR_SMTP_USERNAME',
            'MONITOR_SMTP_PASSWORD', 'MONITOR_ALERT_TO', 'MONITOR_SOURCE'}


def load_credentials(path=SECRETS):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r', encoding='utf-8') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or stat.S_IMODE(s.st_mode) != 0o600 or s.st_size > 1024 * 1024:
            raise ValueError('invalid credentials file permissions')
        values = json.load(f)
    if not isinstance(values, dict) or set(values) != REQUIRED or not all(isinstance(v, str) and v for v in values.values()):
        raise ValueError('invalid credentials')
    if values['MONITOR_SOURCE'] not in ('control', 'nl'):
        raise ValueError('invalid source')
    return values


def main():
    try:
        credentials = load_credentials()
    except (OSError, ValueError, TypeError):
        print('MONITOR_CREDENTIALS_ERROR', flush=True)
        return 1
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    with tempfile.TemporaryDirectory(prefix='probe-', dir='/run/gamelife-vpn-monitor') as tmp:
        result = str(Path(tmp) / 'result.json')
        probe_env = {**env, 'MONITOR_PROFILE_JSON': credentials['MONITOR_PROFILE_JSON'],
                     'MONITOR_SUBSCRIPTION_URL': credentials['MONITOR_SUBSCRIPTION_URL'],
                     'MONITOR_RESULT_FILE': result, 'XRAY_BIN': str(ROOT / 'xray')}
        status = 1
        try:
            check = subprocess.run([sys.executable, str(ROOT / 'vpn_monitor.py')], env=probe_env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=410)
            status = check.returncode
        except (OSError, subprocess.TimeoutExpired):
            pass
        alert_env = {**env, **{key: credentials[key] for key in ('MONITOR_SMTP_USERNAME',
                      'MONITOR_SMTP_PASSWORD', 'MONITOR_ALERT_TO', 'MONITOR_SOURCE')},
                     'MONITOR_RESULT_FILE': result, 'MONITOR_STATE_FILE': STATE}
        try:
            alert = subprocess.run([sys.executable, str(ROOT / 'alerts.py')], env=alert_env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
            if alert.returncode:
                print('MONITOR_ALERT_FAILED', flush=True)
                return 1
        except (OSError, subprocess.TimeoutExpired):
            print('MONITOR_ALERT_FAILED', flush=True)
            return 1
    print('MONITOR_CHECK_OK' if status == 0 else 'MONITOR_CHECK_FAILED', flush=True)
    return 0 if status == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
