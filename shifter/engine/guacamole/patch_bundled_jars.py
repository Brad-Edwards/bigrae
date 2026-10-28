#!/usr/bin/env python3
"""Replace bundled dependency jars inside Guacamole archives (#2610).

Guacamole 1.6.0 bundles its dependencies as nested ``.jar`` entries: the web
application under ``WEB-INF/lib/`` and each extension at the root of its jar.
Some extensions nest a second jar that bundles the same dependencies again
(for example ``guacamole-auth-jdbc-base`` inside the PostgreSQL extension).
No Guacamole release bundles a fixed guava yet, so this script swaps the
vulnerable nested jars for fixed, hash-verified copies.

Usage::

    patch_bundled_jars.py --replace 'guava-[0-9.]+-jre\\.jar=/patch/guava-33.7.2-jre.jar' \\
        --replace 'failureaccess-[0-9.]+\\.jar=/patch/failureaccess-1.0.3.jar' ARCHIVE...

Each ``--replace`` pairs a regular expression for the nested jar file name with
the replacement jar. The script rewrites each archive in place, recurses into
nested jars that contain a match, and fails when an archive has no match, so a
changed upstream layout can not pass silently.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Replacement:
    """One nested jar name pattern and the jar that replaces each match."""

    pattern: re.Pattern[str]
    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    def data(self) -> bytes:
        return self.path.read_bytes()


def _matching(name: str, replacements: list[Replacement]) -> Replacement | None:
    """Return the replacement whose pattern matches the base name of ``name``."""
    base = name.rsplit("/", 1)[-1]
    return next((r for r in replacements if r.pattern.fullmatch(base)), None)


def patch_archive(data: bytes, replacements: list[Replacement]) -> tuple[bytes, int]:
    """Return the patched archive bytes and the number of replaced entries."""
    replaced = 0
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            payload = source.read(info)
            match = _matching(info.filename, replacements)
            if match is not None:
                directory = info.filename[: -len(info.filename.rsplit("/", 1)[-1])]
                info = zipfile.ZipInfo(directory + match.name, date_time=info.date_time)
                info.compress_type = zipfile.ZIP_STORED
                payload = match.data()
                replaced += 1
            elif info.filename.endswith(".jar"):
                nested, count = patch_archive(payload, replacements)
                if count:
                    payload = nested
                    replaced += count
            target.writestr(info, payload)
    return out.getvalue(), replaced


def _parse_replacement(value: str) -> Replacement:
    pattern, sep, path = value.partition("=")
    if not sep or not pattern or not path:
        raise argparse.ArgumentTypeError("use PATTERN=PATH")
    return Replacement(re.compile(pattern), Path(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replace", action="append", type=_parse_replacement, required=True)
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(argv)
    for archive in args.archives:
        patched, count = patch_archive(archive.read_bytes(), args.replace)
        if not count:
            print(f"{archive}: no bundled jar matched; the upstream layout changed", file=sys.stderr)
            return 1
        archive.write_bytes(patched)
        print(f"{archive}: replaced {count} bundled jar(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
