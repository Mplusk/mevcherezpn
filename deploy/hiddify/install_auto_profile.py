"""Install the compatible automatic profile hook in the pinned panel image."""

from __future__ import annotations

import ast
import sysconfig
from pathlib import Path


OLD = '''        data = self._render_core_config(core, c, pretty=True)
        return add_headers(data, c, "application/json")
'''
NEW = '''        data = self._render_core_config(core, c, pretty=True)
        if core == "hiddify-core":
            from hiddifypanel.panel.user.gamelife_auto_profile import automatic_profile
            data = automatic_profile(data, request.user_agent.string)
        return add_headers(data, c, "application/json")
'''

HEADERS_OLD = '''    resp.headers["profile-title"] = "base64:" + hutils.encode.do_base_64(c["profile_title"])

    return resp
'''
HEADERS_NEW = '''    resp.headers["profile-title"] = "base64:" + hutils.encode.do_base_64(c["profile_title"])

    from hiddifypanel.panel.user.gamelife_happ_routing import add_routing_header
    return add_routing_header(resp, request.user_agent.string)
'''


def transform_source(source: str) -> str:
    updated = source
    for old, new in ((OLD, NEW), (HEADERS_OLD, HEADERS_NEW)):
        if updated.count(old) == 0 and updated.count(new) == 1:
            continue
        if updated.count(old) != 1 or updated.count(new) != 0:
            raise ValueError("unexpected Hiddify subscription implementation")
        updated = updated.replace(old, new, 1)
    ast.parse(updated)
    return updated


def main() -> None:
    directory = Path(sysconfig.get_paths()["purelib"]) / "hiddifypanel/panel/user"
    target = directory / "user.py"
    source = target.read_text(encoding="utf-8")
    updated = transform_source(source)
    for target_name, source_name in (("gamelife_auto_profile.py", "auto_profile.py"),
                                     ("gamelife_happ_routing.py", "happ_routing.py")):
        module = directory / target_name
        content = Path(__file__).with_name(source_name).read_bytes()
        if not module.is_file() or module.read_bytes() != content:
            module.write_bytes(content)
            module.chmod(0o644)
    if updated != source:
        target.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
