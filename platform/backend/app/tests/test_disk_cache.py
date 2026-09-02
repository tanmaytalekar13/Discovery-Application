"""
Tests for disk cache module.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from app.artifacts import disk_cache


def test_validate_relative_path():
    """Test path validation prevents traversal."""
    # Valid paths
    disk_cache._validate_relative_path("items/123/integration.json")
    disk_cache._validate_relative_path("files/src/main.py")

    # Invalid paths
    with pytest.raises(ValueError):
        disk_cache._validate_relative_path("")

    with pytest.raises(ValueError):
        disk_cache._validate_relative_path("/etc/passwd")

    with pytest.raises(ValueError):
        disk_cache._validate_relative_path("../secret.txt")

    with pytest.raises(ValueError):
        disk_cache._validate_relative_path("items/../../../etc/passwd")


def test_write_and_read_cached_file(tmp_path, monkeypatch):
    """Test writing and reading cached files."""
    monkeypatch.setattr(disk_cache, "CACHE_BASE_DIR", tmp_path)

    test_path = "items/test/test.txt"
    test_content = "Hello, World!"

    # Write
    assert disk_cache.write_cached_file(test_path, test_content) is True

    # Check exists
    assert disk_cache.path_exists(test_path) is True

    # Read
    content = disk_cache.read_cached_file(test_path)
    assert content == test_content


def test_write_and_read_cached_json(tmp_path, monkeypatch):
    """Test writing and reading cached JSON."""
    monkeypatch.setattr(disk_cache, "CACHE_BASE_DIR", tmp_path)

    test_path = "items/test/test.json"
    test_data = {"key": "value", "count": 42}

    # Write
    assert disk_cache.write_cached_json(test_path, test_data) is True

    # Read
    data = disk_cache.read_cached_json(test_path)
    assert data == test_data


def test_cache_miss_returns_none(tmp_path, monkeypatch):
    """Test reading non-existent file returns None."""
    monkeypatch.setattr(disk_cache, "CACHE_BASE_DIR", tmp_path)

    assert disk_cache.read_cached_file("nonexistent.txt") is None
    assert disk_cache.read_cached_json("nonexistent.json") is None


def test_generate_cache_path():
    """Test cache path generation."""
    item_id = uuid4()

    path = disk_cache.generate_cache_path(item_id, "integration", "json")
    assert path == f"items/{item_id}/integration.json"

    file_path = disk_cache.generate_source_file_cache_path(item_id, "src/main.py")
    assert file_path == f"items/{item_id}/files/src/main.py"
