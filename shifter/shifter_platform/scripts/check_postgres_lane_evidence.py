"""Apply the PostgreSQL-lane marker guard across all pytest-split shards (#2582).

Each sharded run writes ``postgres-lane-evidence.json`` (see the root
``conftest.py``). One shard can run zero ``postgres``-marked tests, so the
"at least one ran" rule (#1524) is applied here, across every shard.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def evidence_failures(evidence: list[dict[str, Any]]) -> list[str]:
    """Return the fail-closed reasons for a set of shard evidence records."""
    if not evidence:
        return ["no shard evidence found"]
    failures: list[str] = []
    splits = {record["splits"] for record in evidence}
    groups = sorted(record["group"] for record in evidence)
    if len(splits) != 1:
        failures.append(f"shards disagree on the split count: {sorted(splits)}")
    elif groups != list(range(1, splits.pop() + 1)):
        failures.append(f"shard groups are incomplete or duplicated: {groups}")
    if not any(record["ran"] for record in evidence):
        failures.append("no postgres-marked tests ran in any shard")
    skipped = sorted({nodeid for record in evidence for nodeid in record["skipped"]})
    if skipped:
        failures.append(f"{len(skipped)} postgres-marked test(s) skipped: {', '.join(skipped)}")
    return failures


def main(argv: list[str] | None = None) -> int:
    """Read the shard evidence files and fail when the lane contract is broken."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    evidence = [json.loads(path.read_text(encoding="utf-8")) for path in args.files]
    failures = evidence_failures(evidence)
    for failure in failures:
        sys.stderr.write(f"ERROR: PostgreSQL lane fail-closed guard: {failure} (#1524)\n")
    if failures:
        return 1
    ran = sum(len(record["ran"]) for record in evidence)
    sys.stdout.write(f"PostgreSQL lane: {ran} postgres-marked tests ran across {len(evidence)} shards\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
