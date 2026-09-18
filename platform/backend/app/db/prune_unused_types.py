"""One-shot cleanup: drop write-only Phase 10 vertex/edge types from ArcadeDB.

Rationale: the DiscoverySource / DiscoveryEvidence / ReliabilityEvaluation /
DiscoveryRejection vertex types (and their HAS_* edges) were written by the
normalization pipeline but never read back anywhere - search, the API, and the
frontend all use the data denormalized onto the Item vertex
(provenance / evidence_summary / reliability_*). They have been removed from
the schema; this script removes the leftover types, records, and indexes from
an already-initialized database.

Usage:
    python -m app.db.prune_unused_types            # drop types + data
    python -m app.db.prune_unused_types --dry-run  # report counts only
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.config import get_settings
from app.db.client import ArcadeDBClient

logger = logging.getLogger(__name__)

# Vertex types removed from the schema (with their write-only edges).
UNUSED_VERTEX_TYPES = (
    "DiscoverySource",
    "DiscoveryEvidence",
    "ReliabilityEvaluation",
    "DiscoveryRejection",
)

UNUSED_EDGE_TYPES = (
    "HAS_DISCOVERY_SOURCE",
    "HAS_DISCOVERY_EVIDENCE",
    "HAS_RELIABILITY_EVALUATION",
)


async def _count(db: ArcadeDBClient, type_name: str) -> int:
    try:
        response = await db.command("sql", f"SELECT count(*) FROM {type_name}", {})
        rows = response.get("result", [])
        if not rows:
            return 0
        for value in rows[0].values():
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0
    except Exception:  # noqa: BLE001 - type may not exist yet
        return 0


async def _run(db: ArcadeDBClient, sql: str) -> bool:
    """Run a DDL statement, tolerating "type does not exist" errors."""
    try:
        await db.command("sql", sql, {})
        return True
    except Exception as exc:  # noqa: BLE001 - missing types are expected
        message = str(exc).lower()
        if "does not exist" in message or "not found" in message:
            return False
        raise


async def prune(dry_run: bool = False) -> dict:
    settings = get_settings()
    db = ArcadeDBClient(settings)

    counts = {name: await _count(db, name) for name in UNUSED_VERTEX_TYPES}

    if not dry_run:
        # Records first (edges before vertices), then the schema types.
        # ArcadeDB SQL: DELETE FROM <type> removes the records;
        # DROP TYPE <type> removes the schema definition itself.
        for type_name in UNUSED_EDGE_TYPES:
            await _run(db, f"DELETE FROM {type_name}")
        for type_name in UNUSED_VERTEX_TYPES:
            await _run(db, f"DELETE FROM {type_name}")
        for type_name in UNUSED_VERTEX_TYPES:
            await _run(db, f"DROP TYPE {type_name}")
        for type_name in UNUSED_EDGE_TYPES:
            await _run(db, f"DROP TYPE {type_name}")

    return {
        "counts": counts,
        "total_records": sum(counts.values()),
        "dry_run": dry_run,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without dropping")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    report = await prune(dry_run=args.dry_run)
    for type_name, count in report["counts"].items():
        print(f"  {type_name}: {count} record(s)")
    mode = "would drop" if args.dry_run else "dropped"
    print(f"{mode} {len(UNUSED_VERTEX_TYPES)} unused vertex types "
          f"({report['total_records']} record(s) total)")


if __name__ == "__main__":
    asyncio.run(main())
