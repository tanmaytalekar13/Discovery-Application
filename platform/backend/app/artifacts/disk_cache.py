"""
Disk cache module for artifact storage.

Handles reading/writing integration code, source trees, and individual
source files to disk. All paths are relative and stored in ArcadeDB.
Prevents path traversal attacks and uses atomic writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID


# Base directory for all cached artifacts.
#
# Defaults to a path meant to live on a mounted, persistent Docker volume
# (see docker-compose.yml -> backend.volumes) so that downloaded source
# code/trees survive container restarts and rebuilds. Override with the
# SOURCE_CACHE_DIR env var for local (non-container) development.
CACHE_BASE_DIR = Path(os.getenv("SOURCE_CACHE_DIR", "/data/discovery_cache"))


def _get_cache_root() -> Path:
    """Get or create the cache root directory."""
    CACHE_BASE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_BASE_DIR


def _validate_relative_path(relative_path: str) -> None:
    """
    Ensure the path is safe and doesn't contain traversal patterns.

    Raises ValueError if the path is unsafe.
    """
    if not relative_path:
        raise ValueError("Path cannot be empty")

    # Normalize and check for path traversal
    normalized = Path(relative_path).as_posix()
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise ValueError(f"Unsafe path: {relative_path}")


def get_absolute_path(relative_path: str) -> Path:
    """
    Convert a relative cache path to an absolute filesystem path.

    Args:
        relative_path: Relative path like "items/{item_id}/integration.json"

    Returns:
        Absolute Path object

    Raises:
        ValueError: If the path is unsafe
    """
    _validate_relative_path(relative_path)
    return _get_cache_root() / relative_path


def path_exists(relative_path: str) -> bool:
    """
    Check if a cached file exists on disk.

    Args:
        relative_path: Relative path from cache root

    Returns:
        True if file exists, False otherwise
    """
    try:
        _validate_relative_path(relative_path)
        abs_path = get_absolute_path(relative_path)
        return abs_path.exists() and abs_path.is_file()
    except (ValueError, OSError):
        return False


def read_cached_file(relative_path: str) -> str | None:
    """
    Read a cached file from disk.

    Args:
        relative_path: Relative path from cache root

    Returns:
        File contents as string, or None if not found/error
    """
    try:
        _validate_relative_path(relative_path)
        abs_path = get_absolute_path(relative_path)

        if not abs_path.exists():
            return None

        return abs_path.read_text(encoding="utf-8")
    except (ValueError, OSError, UnicodeDecodeError):
        return None


def read_cached_json(relative_path: str) -> dict[str, Any] | None:
    """
    Read a cached JSON file from disk.

    Args:
        relative_path: Relative path from cache root

    Returns:
        Parsed JSON dict, or None if not found/error
    """
    content = read_cached_file(relative_path)
    if content is None:
        return None

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def write_cached_file(relative_path: str, content: str) -> bool:
    """
    Write content to a cached file with atomic write.

    Args:
        relative_path: Relative path from cache root
        content: String content to write

    Returns:
        True if successful, False otherwise
    """
    try:
        _validate_relative_path(relative_path)
        abs_path = get_absolute_path(relative_path)

        # Create parent directories
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp file, then rename
        temp_path = abs_path.with_suffix(abs_path.suffix + ".tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(abs_path)

        return True
    except (ValueError, OSError):
        return False


def write_cached_json(relative_path: str, data: dict[str, Any]) -> bool:
    """
    Write JSON data to a cached file with atomic write.

    Args:
        relative_path: Relative path from cache root
        data: JSON-serializable dict to write

    Returns:
        True if successful, False otherwise
    """
    try:
        content = json.dumps(data, indent=2, ensure_ascii=False)
        return write_cached_file(relative_path, content)
    except (TypeError, ValueError):
        return False


def delete_cached_file(relative_path: str) -> bool:
    """
    Delete a cached file from disk.

    Args:
        relative_path: Relative path from cache root

    Returns:
        True if deleted or didn't exist, False on error
    """
    try:
        _validate_relative_path(relative_path)
        abs_path = get_absolute_path(relative_path)

        if abs_path.exists():
            abs_path.unlink()

        return True
    except (ValueError, OSError):
        return False


def generate_cache_path(item_id: UUID, artifact_type: str, extension: str = "json") -> str:
    """
    Generate a relative cache path for an artifact.

    Args:
        item_id: Item UUID
        artifact_type: Type of artifact ("integration", "source_tree", "source_file")
        extension: File extension (default "json")

    Returns:
        Relative path string like "items/{item_id}/integration.json"
    """
    return f"items/{item_id}/{artifact_type}.{extension}"


def generate_source_file_cache_path(item_id: UUID, file_path: str) -> str:
    """
    Generate a relative cache path for a source file.

    Args:
        item_id: Item UUID
        file_path: Repository file path (e.g., "src/main.py")

    Returns:
        Relative path string like "items/{item_id}/files/src/main.py"
    """
    # Sanitize the file path to prevent traversal
    safe_file_path = file_path.lstrip("/").replace("..", "")
    return f"items/{item_id}/files/{safe_file_path}"