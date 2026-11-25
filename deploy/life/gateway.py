"""Install a public Life site without disclosing or replacing private routes."""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parent
BEGIN = '\t# public-life-begin\n'
END = '\t# public-life-end\n'
BACKUP_BEGIN = '\t# subscription-backup-begin\n'
BACKUP_END = '\t# subscription-backup-end\n'
UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'


def public_html(source=SOURCE):
    html = (source / 'index.html').read_text(encoding='utf-8')
    javascript = (source / 'life.js').read_text(encoding='utf-8')
    if html.count('/*__LIFE_SCRIPT__*/') != 1 or '</script' in javascript.lower():
        raise ValueError('invalid site source')
    return html.replace('/*__LIFE_SCRIPT__*/', javascript)


def site_block(html):
    if re.search(r'(?m)^\s*LIFE_PAGE(?:\s|$)', html):
        raise ValueError('reserved heredoc delimiter')
    # Braces in CSS and JavaScript must not be interpreted as Caddy placeholders.
    escaped = html.replace('{', r'\{')
    indented = '\n'.join('\t\t\t' + line for line in escaped.splitlines())
    return (BEGIN + ''.join(
        '\thandle /assets/geodata/' + name + ' {\n'
        '\t\troot * /data/public-geodata/current\n'
        '\t\trewrite * /' + name + '\n'
        '\t\theader Content-Type application/octet-stream\n'
        '\t\theader X-Content-Type-Options nosniff\n'
        '\t\theader Cache-Control "public, max-age=3600"\n'
        '\t\tfile_server\n\t}\n'
        for name in ('geoip-ru-blocked.srs', 'geosite-ru-blocked.srs')
    ) + '\thandle /assets/geodata/geoip.dat {\n'
            '\t\troot * /data/public-geodata/current\n'
            '\t\trewrite * /geoip.dat\n'
            '\t\theader Content-Type application/octet-stream\n'
            '\t\theader X-Content-Type-Options nosniff\n'
            '\t\theader Cache-Control "public, max-age=3600"\n'
            '\t\tfile_server\n'
            '\t}\n'
            '\thandle /assets/geodata/geosite.dat {\n'
            '\t\troot * /data/public-geodata/current\n'
            '\t\trewrite * /geosite.dat\n'
            '\t\theader Content-Type application/octet-stream\n'
            '\t\theader X-Content-Type-Options nosniff\n'
            '\t\theader Cache-Control "public, max-age=3600"\n'
            '\t\tfile_server\n'
            '\t}\n'
            '\t@life_pages path / /rules /rules/ /patterns /patterns/ /about /about/\n'
            '\thandle @life_pages {\n'
            '\t\theader Content-Type "text/html; charset=utf-8"\n'
            '\t\theader X-Content-Type-Options nosniff\n'
            '\t\theader Referrer-Policy no-referrer\n'
            '\t\theader Content-Security-Policy "default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'; frame-ancestors \'none\'"\n'
            '\t\trespond <<LIFE_PAGE\n' + indented + '\n'
            '\t\t\tLIFE_PAGE 200\n\t}\n' + END)


def backup_block(routes):
    if not isinstance(routes, dict) or set(routes) != {'version', 'host', 'parent_host', 'parent_path', 'aliases'} or routes['version'] != 1:
        raise ValueError('invalid backup state')
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    host = routes['parent_host']
    if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(label + r'(?:\.' + label + r')+', host):
        raise ValueError('invalid upstream host')
    parent, aliases = routes['parent_path'], routes['aliases']
    if (not isinstance(parent, str) or not re.fullmatch(r'[A-Za-z0-9_-]{5,80}', parent)
            or not isinstance(aliases, list) or not 1 <= len(aliases) <= 3
            or any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9_-]{5,80}', p) for p in aliases)
            or len(set(aliases)) != len(aliases) or parent not in aliases):
        raise ValueError('invalid backup routes')
    paths = '|'.join(re.escape(path) for path in aliases)
    rewrites = ''.join('\t\turi replace /' + p + '/ /' + parent + '/ 1\n' for p in aliases if p != parent)
    return (BACKUP_BEGIN + '\t@subscription_backup path_regexp ^/(' + paths + ')/' + UUID + '(/.*)?$\n'
            '\thandle @subscription_backup {\n' + rewrites + '\t\treverse_proxy https://' + host + ' {\n'
            '\t\t\theader_up Host ' + host + '\n'
            '\t\t\theader_up X-Forwarded-Host ' + host + '\n'
            '\t\t\theader_up -X-GameLife-Decoy-Loop\n\t\t}\n\t}\n' + BACKUP_END)


def replace_marked(config, begin, end, replacement):
    if config.count(begin) != 1 or config.count(end) != 1:
        raise ValueError('ambiguous managed configuration')
    left, remaining = config.split(begin, 1)
    _, right = remaining.split(end, 1)
    return left + replacement + right


def transform(config, html, routes=None):
    block = site_block(html)
    if BEGIN in config or END in config:
        config = replace_marked(config, BEGIN, END, block)
    else:
        # The existing root handler is deliberately the only unmanaged section
        # replaced. Everything else, including credential-bearing paths, is kept.
        pattern = r'(?ms)^\thandle / \{\n.*?^\t\}\n'
        matches = list(re.finditer(pattern, config))
        if len(matches) != 1:
            raise ValueError('cannot identify the existing public page')
        match = matches[0]
        config = config[:match.start()] + block + config[match.end():]
    if routes is not None:
        backup = backup_block(routes)
        if BACKUP_BEGIN in config or BACKUP_END in config:
            config = replace_marked(config, BACKUP_BEGIN, BACKUP_END, backup)
        else:
            config = config.replace(BEGIN, backup + BEGIN, 1)
    return config


def docker(command, *, data=None):
    result = subprocess.run(['docker', 'exec', '-i', 'gamelife-gateway-proxy-1'] + command,
                            input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    if result.returncode:
        raise RuntimeError('gateway validation or reload failed')


def write_in_place(path, data):
    # Caddy's file is a bind mount: replacement by rename would leave the
    # container pointing to the old inode.
    with path.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def install(root, source=SOURCE, verify=None):
    gateway = root / 'gateway' / 'Caddyfile'
    info = gateway.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError('unsafe gateway configuration')
    original = gateway.read_bytes()
    routes_file = root / 'subscription-relay-backup' / 'routes.json'
    routes = None
    if routes_file.exists():
        info = routes_file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError('unsafe private route state')
        routes = json.loads(routes_file.read_text(encoding='utf-8'))
    candidate = transform(original.decode('utf-8'), public_html(source), routes).encode('utf-8')
    if candidate == original:
        if verify:
            verify()
        return False
    docker(['caddy', 'validate', '--config', '/dev/stdin', '--adapter', 'caddyfile'], data=candidate)
    backup = gateway.parent / ('Caddyfile.before-life-' + str(time.time_ns()))
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(original)
    try:
        write_in_place(gateway, candidate)
        docker(['caddy', 'reload', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'])
        if verify:
            verify()
    except Exception:
        write_in_place(gateway, original)
        docker(['caddy', 'reload', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'])
        raise
    return True


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('invalid invocation')
        changed = install(Path(sys.argv[1]))
        print('SITE_STATUS ' + ('updated' if changed else 'unchanged'))
    except Exception:
        print('SITE_ERROR installation_failed', file=sys.stderr)
        raise SystemExit(1)
