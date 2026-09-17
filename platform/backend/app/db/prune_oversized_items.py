"""One-shot cleanup: remove existing catalog Items whose package exceeds the
sandbox install-footprint limit.

Rationale: the Phase 10 size gate (normalization.pipeline._reject_oversized_package)
rejects oversized packages at ingestion, so they never become Items. Items
created BEFORE that gate existed are still in the catalog and still surface in
search results (with a dead "Run Locally" affordance). This script applies the
same size estimator to every existing local-stdio Item and deletes the ones
that can never run in the sandbox.

Usage:
    python -m app.db.prune_oversized_items            # delete + report
    python -m app.db.prune_oversized_items --dry-run  # report only
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.config import get_settings
from app.db.client import ArcadeDBClient
from app.db.repositories import ItemRepository
from app.sandbox.package_size import PackageSizeEstimator, describe_size

logger = logging.getLogger(__name__)


def _local_package(item) -> tuple[str, str] | None:
    """(registry_type, identifier) for a local-stdio item, else None."""
    artifacts = getattr(item, "artifacts", None)
    config_files = getattr(artifacts, "config_files", None) or []
    for entry in config_files:
        if not isinstance(entry, dict) or entry.get("kind") != "mcp_registry_packages":
            continue
        for package in entry.get("packages", []) or []:
            if not isinstance(package, dict) or not package.get("identifier"):
                continue
            registry_type = str(package.get("registry_type") or package.get("registryType") or "npm")
            identifier = str(package.get("identifier"))
            runtime_hint = str(package.get("runtime_hint") or package.get("runtimeHint") or "")
            if registry_type == "pypi" or "uvx" in runtime_hint or registry_type == "npm":
                size_registry = "pypi" if (registry_type == "pypi" or "uvx" in runtime_hint) else "npm"
                return size_registry, identifier
    return None


async def prune(dry_run: bool = False) -> dict:
    settings = get_settings()
    repository = ItemRepository(ArcadeDBClient(settings))
    estimator = PackageSizeEstimator()

    response = await repository._db.command("sql", "SELECT FROM Item", {})
    rows = response.get("result", [])
    items = [repository._to_item(row) for row in rows]

    removed: list[tuple[str, str, str]] = []
    checked = 0
    try:
        for item in items:
            package = _local_package(item)
            if package is None:
                continue
            checked += 1
            registry_type, identifier = package
            try:
                estimate = await estimator.estimate(registry_type, identifier)
            except Exception as exc:  # noqa: BLE001 - fail open per item
                logger.debug("Size estimation failed for %s: %s", identifier, exc)
                continue
            if estimate.known and estimate.over_limit:
                removed.append((str(item.item_id), item.name, describe_size(estimate)))
    finally:
        await estimator.aclose()

    if not dry_run:
        for item_id, _name, _size in removed:
            await repository._db.command(
                "sql", "DELETE FROM Item WHERE item_id = :item_id", {"item_id": item_id}
            )

    report = {
        "items_total": len(items),
        "local_packages_checked": checked,
        "removed": len(removed) if not dry_run else 0,
        "would_remove": len(removed) if dry_run else 0,
        "detail": removed,
        "dry_run": dry_run,
    }
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without deleting")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    report = await prune(dry_run=args.dry_run)
    mode = "would remove" if args.dry_run else "removed"
    print(f"Items scanned: {report['items_total']} (local packages: {report['local_packages_checked']})")
    print(f"{mode}: {report['would_remove'] if args.dry_run else report['removed']}")
    for _item_id, name, size in report["detail"]:
        print(f"  - {name} ({size})")


if __name__ == "__main__":
    asyncio.run(main())
