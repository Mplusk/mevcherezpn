"""Install reviewed template overrides through Hiddify's management API.

This changes panel templates but does not apply generated configurations.
The operator must provision and validate DAT assets on every node first.
"""
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import sys
from urllib.parse import urlsplit

from patch import transform_singbox, transform_xray

TRANSFORMS = {'xray/server/base/route': transform_xray,
              'hiddify-core/server/base/route': transform_singbox}


def prepare(rows):
    result = []
    for slug, transform in TRANSFORMS.items():
        found = [row for row in rows if row.get('slug') == slug]
        if len(found) != 1:
            raise ValueError('missing or duplicate expected template')
        row = found[0]
        if not row.get('is_builtin') or not isinstance(row.get('id'), int):
            raise ValueError('unexpected template identity')
        content = row.get('content')
        if not isinstance(content, str):
            raise ValueError('unexpected template content')
        updated = transform(content)
        if row.get('builtin_override') and updated != content:
            raise ValueError('existing custom override requires manual review')
        result.append({'id': row['id'], 'slug': slug, 'content': content,
                       'builtin_override': bool(row.get('builtin_override')),
                       'updated': updated})
    return result


def install(panel, backup_path):
    changes = prepare(panel.get_templates())
    todo = [row for row in changes if row['content'] != row['updated']]
    if not todo:
        return False
    backup_path = Path(backup_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(changes, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    touched = []
    try:
        for row in todo:
            # Include the row before calling PATCH: network errors can occur
            # after the server commits but before the response reaches us.
            touched.append(row)
            panel.patch_template(row['id'], {'content': row['updated'], 'builtin_override': True})
        current = prepare(panel.get_templates())
        if any(row['content'] != row['updated'] or not row['builtin_override'] for row in current):
            raise RuntimeError('template readback mismatch')
    except Exception as error:
        errors = []
        for row in reversed(touched):
            try:
                restore = ({'content': row['content'], 'builtin_override': True}
                           if row['builtin_override'] else {'builtin_override': False})
                panel.patch_template(row['id'], restore)
            except Exception:
                errors.append(row['slug'])
        if errors:
            raise RuntimeError('template rollback incomplete: ' + ', '.join(errors)) from error
        raise RuntimeError('template update failed and was rolled back') from error
    return True


class LocalPanel:
    def __init__(self, state_path):
        info = os.stat(state_path, follow_symlinks=False)
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('unsafe access state permissions')
        state = json.loads(Path(state_path).read_text(encoding='utf-8'))
        parsed = urlsplit(state['owner_url'])
        parts = parsed.path.strip('/').split('/')
        if parsed.scheme != 'https' or len(parts) != 2 or any(not x or '/' in x for x in parts):
            raise ValueError('unexpected local panel state')
        self.host = state['bootstrap_hostname']
        self.admin, self.key = parts

    def request(self, method, suffix, data=None):
        host = self.host
        class LocalConnection(http.client.HTTPSConnection):
            def connect(inner):
                sock = socket.create_connection(('127.0.0.1', 28443), inner.timeout)
                inner.sock = inner._context.wrap_socket(sock, server_hostname=host)
        connection = LocalConnection(host, timeout=30, context=ssl._create_unverified_context())
        try:
            body = json.dumps(data).encode() if data is not None else None
            connection.request(method, f'/{self.admin}/api/v2/admin/{suffix}', body,
                               {'Host': host, 'Hiddify-API-Key': self.key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status not in (200, 201, 204) or len(raw) > 4 * 1024 * 1024:
                raise RuntimeError('panel API request failed')
            if response.status == 204 and method == 'DELETE' and not raw:
                return {}
            return json.loads(raw)
        finally:
            connection.close()

    def get_templates(self):
        rows = self.request('GET', 'proxy-templates/')
        if not isinstance(rows, list):
            raise ValueError('invalid template list')
        return rows

    def patch_template(self, template_id, data):
        if not isinstance(template_id, int) or template_id <= 0:
            raise ValueError('invalid template id')
        return self.request('PATCH', f'proxy-templates/{template_id}/', data)


if __name__ == '__main__':
    if len(sys.argv) != 3 or os.geteuid() != 0:
        raise SystemExit('Usage: template_install.py STATE_FILE BACKUP_FILE (root only)')
    try:
        changed = install(LocalPanel(sys.argv[1]), sys.argv[2])
        print('TEMPLATE_STATUS=' + ('UPDATED' if changed else 'UNCHANGED'))
    except Exception as exc:
        print('TEMPLATE_ERROR=' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
