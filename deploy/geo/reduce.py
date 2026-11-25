"""Reduce Xray geo databases and atomically publish a pair via current.json.

Only local files are read; obtaining updates and serving them are separate.
current.json names an immutable revision directory, so a failed publish never
exposes a mixed pair. Consumers must resolve both paths from the same manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

MAX_BYTES = 128 * 1024 * 1024
MAX_CODE = 128
RULES = {
    "geoip.dat": frozenset(("RU", "PRIVATE", "RU-BLOCKED")),
    "geosite.dat": frozenset(("CATEGORY-RU", "RU-AVAILABLE-ONLY-INSIDE", "PRIVATE", "RU-BLOCKED")),
}


def varint(data: bytes, pos: int, end: int):
    value = 0
    for index in range(10):
        if pos >= end:
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        if index == 9 and byte > 1:
            raise ValueError("varint overflow")
        value |= (byte & 127) << (7 * index)
        if not byte & 128:
            if index and byte == 0:
                raise ValueError("noncanonical varint")
            return value, pos
    raise ValueError("varint overflow")


def validate_fields(data: bytes, pos: int, end: int, nested: bool):
    """Validate wire boundaries, including each length-delimited entry message."""
    while pos < end:
        tag, pos = varint(data, pos, end)
        if tag >> 3 == 0:
            raise ValueError("invalid protobuf field")
        if nested and tag >> 3 == 1:
            raise ValueError("duplicate country code field")
        wire = tag & 7
        if wire == 0:
            _, pos = varint(data, pos, end)
        elif wire == 2:
            length, pos = varint(data, pos, end)
            stop = pos + length
            if stop > end:
                raise ValueError("truncated protobuf field")
            if nested and tag >> 3 == 2:
                validate_fields(data, pos, stop, False)
            pos = stop
        elif wire in (1, 5):
            pos += 8 if wire == 1 else 4
            if pos > end:
                raise ValueError("truncated fixed-width field")
        else:
            raise ValueError("unsupported protobuf wire type")


def reduce_dat(data: bytes, wanted: frozenset[str]):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("invalid database size")
    pos = 0
    found = {}
    while pos < len(data):
        start = pos
        tag, pos = varint(data, pos, len(data))
        if tag != 10:
            raise ValueError("unexpected root field")
        size, pos = varint(data, pos, len(data))
        end = pos + size
        if end > len(data):
            raise ValueError("truncated entry")
        field, inner = varint(data, pos, end)
        if field != 10:
            raise ValueError("missing leading country code")
        length, inner = varint(data, inner, end)
        if not 0 < length <= MAX_CODE or inner + length > end:
            raise ValueError("invalid country code length")
        try:
            code = data[inner:inner + length].decode("ascii").upper()
        except UnicodeDecodeError as exc:
            raise ValueError("invalid country code") from exc
        if not re.fullmatch(r"[\x21-\x7e]+", code):
            raise ValueError("invalid country code")
        validate_fields(data, inner + length, end, True)
        if code in wanted:
            if code in found:
                raise ValueError("duplicate selected country code")
            found[code] = data[start:end]
        pos = end
    if set(found) != wanted:
        raise ValueError("missing selected country codes: " + ", ".join(sorted(wanted - set(found))))
    return b"".join(found[code] for code in sorted(found))


def bounded_read(path: Path):
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("database exceeds size limit")
    return data


def publish(source: Path, destination: Path):
    reduced = {name: reduce_dat(bounded_read(source / name), wanted)
               for name, wanted in RULES.items()}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in reduced.items()}
    revision = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    destination.mkdir(parents=True, exist_ok=True)
    revisions = destination / "revisions"
    revisions.mkdir(exist_ok=True)
    target = revisions / revision
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix=".geo-", dir=revisions) as tmp:
            staged = Path(tmp) / "data"
            staged.mkdir()
            for name, content in reduced.items():
                with (staged / name).open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            try:
                os.rename(staged, target)
            except FileExistsError:
                pass  # Concurrent publisher of the same content.
    # Verify an existing directory too; never point at an incomplete pair.
    for name, content in reduced.items():
        if bounded_read(target / name) != content:
            raise ValueError("existing revision does not match its content")
    manifest = json.dumps({"version": 1, "revision": revision, "sha256": hashes},
                          sort_keys=True, separators=(",", ":")).encode() + b"\n"
    fd, temp_name = tempfile.mkstemp(prefix=".current-", dir=destination)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(manifest)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination / "current.json")
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="directory containing both upstream .dat files")
    parser.add_argument("destination", type=Path, help="directory for immutable revisions and current.json")
    args = parser.parse_args()
    print(publish(args.source, args.destination))


if __name__ == "__main__":
    main()
