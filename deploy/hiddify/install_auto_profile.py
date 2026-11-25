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


def transform_source(source: str) -> str:
    if source.count(OLD) == 0 and source.count(NEW) == 1:
        return source
    if source.count(OLD) != 1 or source.count(NEW) != 0:
        raise ValueError("unexpected Hiddify subscription implementation")
    updated = source.replace(OLD, NEW, 1)
    ast.parse(updated)
    return updated


def main() -> None:
    directory = Path(sysconfig.get_paths()["purelib"]) / "hiddifypanel/panel/user"
    target = directory / "user.py"
    source = target.read_text(encoding="utf-8")
    updated = transform_source(source)
    module = directory / "gamelife_auto_profile.py"
    content = Path(__file__).with_name("auto_profile.py").read_bytes()
    if not module.is_file() or module.read_bytes() != content:
        module.write_bytes(content)
        module.chmod(0o644)
    if updated != source:
        target.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
