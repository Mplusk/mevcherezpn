"""Correct UTC timestamps emitted without an offset by Hiddify v14.2.0.

The container runs in UTC. Browsers interpret a timestamp without an offset as
local time, so the node view can incorrectly show hours of missed contacts.
"""

from __future__ import annotations

import ast
import sysconfig
from pathlib import Path


OLD_IMPORT = "from datetime import date, datetime, timedelta\n"
NEW_IMPORT = "from datetime import date, datetime, timedelta, timezone\n"
OLD_ISO = '''def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value and value.year > 2000 else None
'''
NEW_ISO = '''def _iso(value: datetime | None) -> str | None:
    if not value or value.year <= 2000:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _usage_report_utc(report: dict | None) -> dict | None:
    if not isinstance(report, dict) or not isinstance(report.get("time"), str):
        return report
    try:
        value = datetime.fromisoformat(report["time"])
    except ValueError:
        return report
    return {**report, "time": _iso(value)}
'''
REPLACEMENTS = (
    (OLD_IMPORT, NEW_IMPORT),
    (OLD_ISO, NEW_ISO),
    ('"last_seen": last_seen.isoformat() if last_seen else None,', '"last_seen": _iso(last_seen),'),
    ('"last_usage_report": last_usage_report(child.id),', '"last_usage_report": _usage_report_utc(last_usage_report(child.id)),'),
)


def transform(source: str) -> str:
    """Patch the known upstream source once; reject incompatible versions."""
    original = all(source.count(old) == 1 and source.count(new) == 0 for old, new in REPLACEMENTS)
    already_patched = all(source.count(old) == 0 and source.count(new) == 1 for old, new in REPLACEMENTS)
    if already_patched:
        return source
    if not original:
        raise ValueError("unexpected Hiddify node timestamp implementation")
    for old, new in REPLACEMENTS:
        source = source.replace(old, new, 1)
    ast.parse(source)
    return source


def main() -> None:
    target = Path(sysconfig.get_paths()["purelib"]) / (
        "hiddifypanel/panel/commercial/restapi/v2/admin/nodes_api.py"
    )
    source = target.read_text(encoding="utf-8")
    updated = transform(source)
    if updated != source:
        target.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
