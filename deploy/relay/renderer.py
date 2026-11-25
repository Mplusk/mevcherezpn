"""Render a subscription-only Caddy configuration from private node state."""

import re


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_PATH = re.compile(r"[A-Za-z0-9_-]{5,80}\Z")
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _hostname(value):
    if not isinstance(value, str) or len(value) > 253 or value != value.lower():
        raise ValueError("invalid hostname")
    labels = value.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        raise ValueError("invalid hostname")
    return value


def validate_routes(routes):
    if not isinstance(routes, dict) or set(routes) != {
        "version", "host", "parent_host", "parent_path", "aliases"
    } or routes["version"] != 1:
        raise ValueError("invalid route state")
    host = _hostname(routes["host"])
    parent_host = _hostname(routes["parent_host"])
    parent_path = routes["parent_path"]
    aliases = routes["aliases"]
    if not isinstance(parent_path, str) or not _PATH.fullmatch(parent_path):
        raise ValueError("invalid parent path")
    if not isinstance(aliases, list) or not 1 <= len(aliases) <= 3:
        raise ValueError("invalid aliases")
    if any(not isinstance(path, str) or not _PATH.fullmatch(path) for path in aliases):
        raise ValueError("invalid aliases")
    if len(set(aliases)) != len(aliases) or parent_path not in aliases:
        raise ValueError("invalid aliases")
    return host, parent_host, parent_path, aliases


def render(routes, *, listen_port=8443, certificate=None, key=None,
           origin=None, bind=None, admin='127.0.0.1:2019', privacy_logs=True):
    """Render; optional arguments are used only by isolated integration tests."""
    host, parent_host, parent_path, aliases = validate_routes(routes)
    if not isinstance(listen_port, int) or not 1024 <= listen_port <= 65535:
        raise ValueError("invalid port")
    cert_root = "/shared/caddy/certificates/acme-v02.api.letsencrypt.org-directory/"
    certificate = certificate or f"{cert_root}{host}/{host}.crt"
    key = key or f"{cert_root}{host}/{host}.key"
    for path in (certificate, key):
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or ".." in path:
            raise ValueError("invalid certificate path")
    origin = origin or f"https://{parent_host}"
    if not re.fullmatch(r"https?://[a-z0-9.-]+(?::[0-9]{1,5})?", origin):
        raise ValueError("invalid origin")
    if bind not in (None, "127.0.0.1"):
        raise ValueError("invalid bind address")
    if admin not in ('127.0.0.1:2019', 'off'):
        raise ValueError("invalid admin address")
    if not isinstance(privacy_logs, bool):
        raise ValueError("invalid logging option")
    matcher = "|".join(re.escape(path) for path in aliases)
    rewrites = "\n".join(
        f"\t\turi replace /{path}/ /{parent_path}/ 1"
        for path in aliases if path != parent_path
    )
    bind_line = f" bind {bind}\n" if bind else ""
    logging = ''' log default {
  exclude http.log.error
  format filter {
   request delete
   error delete
  }
 }
''' if privacy_logs else ''
    return f'''{{
 auto_https off
 admin {admin}
{logging}}}
https://{host}:{listen_port} {{
{bind_line} tls {certificate} {key}
 @subscription path_regexp ^/({matcher})/{_UUID}(/.*)?$
 handle @subscription {{
{rewrites}
  reverse_proxy {origin} {{
   header_up Host {parent_host}
   header_up X-Forwarded-Host {parent_host}
   header_up -X-GameLife-Decoy-Loop
  }}
 }}
 handle {{
  respond 404
 }}
}}
'''
