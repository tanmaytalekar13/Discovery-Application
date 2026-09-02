"""
Builds the "Source" preview tab and repository tree/file browsing.
Supports fetching repository tree and individual files from GitHub with caching.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx

from app.models import Item
from app.artifacts import disk_cache


class SourcePreviewResult:
    def __init__(
        self,
        available: bool,
        language: str | None = None,
        content: str | None = None,
        note: str | None = None,
    ) -> None:
        self.available = available
        self.language = language
        self.content = content
        self.note = note


class RepositoryTreeResult:
    def __init__(
        self,
        available: bool,
        tree: list[dict[str, Any]] | None = None,
        note: str | None = None,
    ) -> None:
        self.available = available
        self.tree = tree
        self.note = note


class SourceFileResult:
    def __init__(
        self,
        available: bool,
        content: str | None = None,
        language: str | None = None,
        note: str | None = None,
    ) -> None:
        self.available = available
        self.content = content
        self.language = language
        self.note = note


def _infer_language_from_path(file_path: str) -> str:
    """Infer language from file path extension."""
    for ext, lang in (
        (".py", "python"),
        (".ts", "typescript"),
        (".tsx", "typescript"),
        (".js", "javascript"),
        (".jsx", "javascript"),
        (".json", "json"),
        (".go", "go"),
        (".rs", "rust"),
        (".java", "java"),
        (".cpp", "cpp"),
        (".c", "c"),
        (".md", "markdown"),
        (".yaml", "yaml"),
        (".yml", "yaml"),
        (".toml", "toml"),
    ):
        if file_path.endswith(ext):
            return lang
    return "plaintext"


def _extract_github_repo_info(source_url: str) -> tuple[str, str, str] | None:
    """
    Extract owner, repo, and default branch from GitHub URL.

    Returns (owner, repo, branch) or None if not a GitHub URL.
    """
    if not source_url or "github.com" not in source_url:
        return None

    # Parse URLs like:
    # - https://github.com/owner/repo
    # - https://github.com/owner/repo/tree/branch
    parts = source_url.rstrip("/").split("github.com/")
    if len(parts) < 2:
        return None

    path_parts = parts[1].split("/")
    if len(path_parts) < 2:
        return None

    owner = path_parts[0]
    repo = path_parts[1]

    # Default to main branch
    branch = "main"
    if len(path_parts) >= 4 and path_parts[2] == "tree":
        branch = path_parts[3]

    return owner, repo, branch


async def fetch_github_tree(
    owner: str, repo: str, branch: str = "main"
) -> list[dict[str, Any]] | None:
    """
    Fetch repository tree from GitHub API.

    Returns simplified tree structure or None on error.
    """
    github_token = os.getenv("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "DiscoveryApplication/1.0",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                return None

            data = response.json()
            tree = data.get("tree", [])

            # Simplify tree structure for frontend
            simplified_tree = []
            for item in tree:
                if item.get("type") in ("blob", "tree"):
                    simplified_tree.append({
                        "path": item["path"],
                        "type": item["type"],  # "blob" = file, "tree" = directory
                        "size": item.get("size"),
                    })

            return simplified_tree
    except Exception:
        return None


async def fetch_github_file(
    owner: str, repo: str, file_path: str, branch: str = "main"
) -> str | None:
    """
    Fetch individual file content from GitHub API.

    Returns decoded file content or None on error.
    """
    github_token = os.getenv("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "DiscoveryApplication/1.0",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    # URL-encode the file path
    encoded_path = file_path.replace(" ", "%20")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}?ref={branch}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                return None

            data = response.json()

            # GitHub returns base64-encoded content
            if "content" in data and data.get("encoding") == "base64":
                content_b64 = data["content"].replace("\n", "")
                decoded = base64.b64decode(content_b64).decode("utf-8", errors="replace")
                return decoded

            return None
    except Exception:
        return None


async def get_repository_tree(item: Item) -> tuple[RepositoryTreeResult, str | None]:
    """
    Fetch and cache repository tree structure.

    Returns (RepositoryTreeResult, relative_cache_path or None).
    """
    # Check existing cache
    cache_path = item.artifacts.source_tree_cache_path
    if cache_path:
        cached_data = disk_cache.read_cached_json(cache_path)
        if cached_data:
            return RepositoryTreeResult(
                available=True,
                tree=cached_data.get("tree"),
            ), cache_path

    # Cache miss: fetch from GitHub
    source_url = str(item.artifacts.source_url) if item.artifacts.source_url else None
    if not source_url:
        return RepositoryTreeResult(
            available=False,
            note="No source URL available for this item.",
        ), None

    repo_info = _extract_github_repo_info(source_url)
    if not repo_info:
        return RepositoryTreeResult(
            available=False,
            note="Source URL is not a GitHub repository.",
        ), None

    owner, repo, branch = repo_info
    tree = await fetch_github_tree(owner, repo, branch)

    if not tree:
        return RepositoryTreeResult(
            available=False,
            note="Failed to fetch repository tree from GitHub.",
        ), None

    # Save to disk cache
    new_cache_path = disk_cache.generate_cache_path(item.item_id, "source_tree", "json")
    cache_data = {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "tree": tree,
    }

    if disk_cache.write_cached_json(new_cache_path, cache_data):
        return RepositoryTreeResult(available=True, tree=tree), new_cache_path

    return RepositoryTreeResult(available=True, tree=tree), None


async def get_source_file(item: Item, file_path: str) -> SourceFileResult:
    """
    Fetch and cache individual source file.

    Returns SourceFileResult with file content.
    """
    # Check cache
    cache_path = disk_cache.generate_source_file_cache_path(item.item_id, file_path)
    cached_content = disk_cache.read_cached_file(cache_path)
    if cached_content:
        return SourceFileResult(
            available=True,
            content=cached_content,
            language=_infer_language_from_path(file_path),
        )

    # Cache miss: fetch from GitHub
    source_url = str(item.artifacts.source_url) if item.artifacts.source_url else None
    if not source_url:
        return SourceFileResult(
            available=False,
            note="No source URL available for this item.",
        )

    repo_info = _extract_github_repo_info(source_url)
    if not repo_info:
        return SourceFileResult(
            available=False,
            note="Source URL is not a GitHub repository.",
        )

    owner, repo, branch = repo_info
    content = await fetch_github_file(owner, repo, file_path, branch)

    if not content:
        return SourceFileResult(
            available=False,
            note=f"Failed to fetch file '{file_path}' from GitHub.",
        )

    # Save to disk cache
    disk_cache.write_cached_file(cache_path, content)

    return SourceFileResult(
        available=True,
        content=content,
        language=_infer_language_from_path(file_path),
    )


def resolve_source(item: Item) -> SourcePreviewResult:
    """Legacy function for backward compatibility - returns README/doc text."""
    source_code = item.artifacts.source_code

    if not source_code:
        return SourcePreviewResult(
            available=False,
            note="Source code was not retrieved for this item — only registry "
                 "or configuration metadata is available.",
        )

    # Infer language from source URL
    url = str(item.artifacts.source_url) if item.artifacts.source_url else ""
    language = "markdown"
    for ext, lang in (
        (".py", "python"),
        (".ts", "typescript"),
        (".js", "javascript"),
        (".json", "json"),
        (".go", "go"),
    ):
        if url.endswith(ext):
            language = lang
            break

    return SourcePreviewResult(
        available=True,
        language=language,
        content=source_code,
        note="This is the README / extracted documentation text captured during "
             "discovery, not a full repository checkout.",
    )
