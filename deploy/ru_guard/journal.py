"""Durable intent journal for the prototype transaction (no secrets)."""
import json
import os
from pathlib import Path
import tempfile
import stat


class JournalError(RuntimeError):
    pass


TERMINAL = {'complete', 'rolled_back', 'aborted'}


class RootLock:
    """Single prototype transaction per root; OS releases lock after a crash."""
    def __init__(self, root):
        self.path = Path(root) / '.transaction.lock'
        self.fd = None

    def __enter__(self):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        self.fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(self.fd)
            if (not stat.S_ISREG(info.st_mode)
                    or (os.name != 'nt' and (info.st_uid != 0 or info.st_mode & 0o077))):
                raise JournalError('unsafe transaction lock')
            if os.name == 'nt':
                import msvcrt
                if info.st_size == 0:
                    os.write(self.fd, b'0')
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def sync_directory(directory):
    # Windows cannot open a directory for fsync through Python's os.open;
    # production is Linux, where both file and directory must be flushed.
    if os.name == 'nt':
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def unfinished(root):
    """Fail closed for incomplete, unreadable, malformed, or missing journals."""
    root = Path(root)
    problems = []
    for item in root.glob('route-*'):
        try:
            if item.is_symlink() or not item.is_dir():
                raise ValueError('unsafe transaction directory')
            path = item / 'journal.json'
            if path.is_symlink() or not path.is_file():
                raise ValueError('missing journal')
            data = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or set(data) != {'version', 'status', 'events', 'nodes'}
                    or data.get('version') != 1 or data.get('status') not in TERMINAL):
                raise ValueError('nonterminal journal')
            nodes = data['nodes']
            if (not isinstance(nodes, list) or len(nodes) != 4 or len(set(nodes)) != 4
                    or any(not isinstance(node, str) or not node for node in nodes)):
                raise ValueError('invalid node mapping')
            events = data['events']
            if not isinstance(events, list) or len(events) % 2:
                raise ValueError('incomplete journal events')
            for index in range(0, len(events), 2):
                start, end = events[index:index + 2]
                if (not isinstance(start, dict) or not isinstance(end, dict)
                        or set(start) != {'phase', 'event'} or set(end) != {'phase', 'event'}
                        or start['event'] != 'intent' or end['event'] not in ('done', 'failed')
                        or start['phase'] != end['phase'] or not isinstance(start['phase'], str)):
                    raise ValueError('invalid journal events')
            if data['status'] == 'complete' and (not events
                    or events[-1] != {'phase': 'verify-subscription', 'event': 'done'}
                    or any(event['event'] == 'failed' for event in events)):
                raise ValueError('invalid complete journal')
            if data['status'] == 'rolled_back' and (not events
                    or events[-1] != {'phase': 'rollback-final', 'event': 'done'}):
                raise ValueError('invalid rollback journal')
            if data['status'] == 'aborted' and events:
                raise ValueError('invalid aborted journal')
        except (OSError, ValueError, TypeError):
            problems.append(item.name)
    return problems


class Journal:
    def __init__(self, directory, nodes):
        self.directory = Path(directory)
        self.path = self.directory / 'journal.json'
        self.state = {'version': 1, 'status': 'running', 'nodes': list(nodes), 'events': []}
        self.write()

    def write(self):
        fd, temporary = tempfile.mkstemp(prefix='.journal-', dir=self.directory)
        try:
            if os.name != 'nt':
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.state, stream, sort_keys=True, separators=(',', ':'))
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            sync_directory(self.directory)
        except Exception as exc:
            raise JournalError('journal persistence failed') from exc
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def event(self, phase, event):
        if (not isinstance(phase, str) or not phase or len(phase) > 80
                or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in phase)
                or event not in ('intent', 'done', 'failed')):
            raise JournalError('invalid journal event')
        self.state['events'].append({'phase': phase, 'event': event})
        self.write()

    def perform(self, phase, action):
        self.event(phase, 'intent')
        try:
            result = action()
        except JournalError:
            raise
        except Exception:
            self.event(phase, 'failed')
            raise
        self.event(phase, 'done')
        return result

    def finish(self, status):
        if status not in TERMINAL:
            raise JournalError('invalid terminal status')
        self.state['status'] = status
        self.write()
