"""Apply action monitoring: completion is not proof of service health.

Callers must independently validate generated configuration, node sync, core
health, real VPN data paths, and rollback. Never print the returned install log.
"""
import http.client
import time
from urllib.parse import urlencode


class ApplyError(RuntimeError):
    pass


def start_and_wait(panel, timeout=900, interval=5, clock=time.monotonic,
                   wall_clock=time.time, sleep=time.sleep):
    state = panel.request('GET', 'apply/')
    if not isinstance(state, dict) or not isinstance(state.get('running'), list):
        raise ApplyError('unexpected apply state')
    if state['running']:
        raise ApplyError('another action is running')
    since = wall_clock()
    try:
        result = panel.request('POST', 'apply/apply/', {})
    except Exception as exc:
        # The server could have started the operation before the response was
        # lost. Do not send another POST or infer rollback is safe.
        raise ApplyError('apply start ambiguous; inspect state before retry') from exc
    if (not isinstance(result, dict) or result.get('action') != 'apply'
            or not isinstance(result.get('started'), (int, float))
            or isinstance(result.get('started'), bool) or result['started'] < since - 5):
        raise ApplyError('apply acknowledgement invalid')
    started = result['started']
    deadline = clock() + timeout
    query = urlencode({'file': '0-install.log', 'since': str(started), 'tail': '1'})
    while clock() < deadline:
        try:
            status = panel.request('GET', 'apply/log/?' + query)
            if (isinstance(status, dict) and status.get('started') is True
                    and status.get('finished') is True):
                current = panel.request('GET', 'apply/')
                if (isinstance(current, dict) and isinstance(current.get('running'), list)
                        and 'apply' not in current['running']):
                    return started
        except (OSError, TimeoutError, http.client.HTTPException, RuntimeError):
            # The panel can briefly restart during apply.
            pass
        sleep(interval)
    raise ApplyError('apply completion not observed')
