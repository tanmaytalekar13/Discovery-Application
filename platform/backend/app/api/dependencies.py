from __future__ import annotations

from app.config import Settings, get_settings
from app.db.client import ArcadeDBClient
from app.db.repositories import ItemRepository, TestRunRepository
from app.query.service import Phase11SearchService


def get_arcadedb_client() -> ArcadeDBClient:
    return ArcadeDBClient(get_settings())


def get_item_repository() -> ItemRepository:
    return ItemRepository(get_arcadedb_client())


def get_test_run_repository() -> TestRunRepository:
    return TestRunRepository(get_arcadedb_client())


def get_search_service() -> Phase11SearchService:
    settings: Settings = get_settings()
    return Phase11SearchService(get_item_repository(), settings)
