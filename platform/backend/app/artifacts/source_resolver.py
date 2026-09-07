"""
Builds the "Source" preview tab and repository tree/file browsing.
Supports fetching repository tree and individual files from GitHub with caching.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any
from urllib.parse import quote

import httpx

from app.models import Item
from app.artifacts import disk_cache


# Extensions that are almost never useful to preview/inspect and are
# frequently large (binaries, images, archives, lockfiles). Retained for a
# possible future prefetch policy; source files are currently cached only when
# the user opens them, which avoids exhausting GitHub's public API quota.
_SKIP_DOWNLOAD_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".mov", ".avi",
    ".so", ".dll", ".dylib", ".exe", ".bin", ".wasm",
    ".lock",
)
_SKIP_DOWNLOAD_FILENAMES = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock",
)
# Files larger than this are skipped during the eager download pass.
_MAX_EAGER_DOWNLOAD_BYTES = 500_000


def _should_skip_eager_download(file_path: str, size: int | None) -> bool:
    """Decide whether a repo file should be skipped during bulk download."""
    lower_path = file_path.lower()
    if lower_path.endswith(_SKIP_DOWNLOAD_EXTENSIONS):
        return True
    filename = file_path.rsplit("/", 1)[-1]
    if filename in _SKIP_DOWNLOAD_FILENAMES:
        return True
    if size is not None and size > _MAX_EAGER_DOWNLOAD_BYTES:
        return True
    return False


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
    repo = path_parts[1].removesuffix(".git")  # Strip .git suffix if present

    # Default to main branch
    branch = "main"
    if len(path_parts) >= 4 and path_parts[2] == "tree":
        branch = path_parts[3]

    return owner, repo, branch


async def _download_all_files(
    item: Item, owner: str, repo: str, branch: str, tree: list[dict[str, Any]]
) -> None:
    """
    Download all files from the repository tree in parallel and cache them to disk.
    Only downloads blob (file) types, skips tree (directory) entries.
    """
    # Filter only files (blobs), skip directories (trees) and files we've
    # decided aren't worth eagerly caching (binaries, lockfiles, oversized
    # files - see _should_skip_eager_download).
    file_nodes = [
        node
        for node in tree
        if node.get("type") == "blob"
        and node.get("path")
        and not _should_skip_eager_download(node["path"], node.get("size"))
    ]

    # Limit concurrent downloads to avoid overwhelming GitHub API
    semaphore = asyncio.Semaphore(10)  # Max 10 concurrent downloads

    async def download_one_file(file_node: dict[str, Any]) -> None:
        """Download and cache a single file."""
        async with semaphore:
            file_path = file_node.get("path")
            if not file_path:
                return

            # Check if already cached (path_exists is the correct check here,
            # not a truthy read, so empty-but-valid cached files aren't
            # mistaken for "not cached" and re-downloaded).
            cache_path = disk_cache.generate_source_file_cache_path(item.item_id, file_path)
            if disk_cache.path_exists(cache_path):
                return  # Already cached, skip

            # Fetch from GitHub
            content = await fetch_github_file(owner, repo, file_path, branch)
            if content is not None:
                # Save to disk
                disk_cache.write_cached_file(cache_path, content)

    # Download all files in parallel
    await asyncio.gather(*[download_one_file(node) for node in file_nodes], return_exceptions=True)


async def fetch_github_tree(
    owner: str, repo: str, branch: str = "main"
) -> list[dict[str, Any]] | None:
    """Fetch a repository tree, retaining the legacy list-only API."""
    result = await fetch_github_tree_with_branch(owner, repo, branch)
    return result[0] if result else None


async def fetch_github_tree_with_branch(
    owner: str, repo: str, branch: str = "main"
) -> tuple[list[dict[str, Any]], str] | None:
    """Fetch a tree and return the ref that GitHub actually accepted."""
    github_token = os.getenv("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "DiscoveryApplication/1.0",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    branches_to_try = [branch]
    if branch == "main":
        branches_to_try.extend(["master", "HEAD"])
    elif branch == "master":
        branches_to_try.extend(["main", "HEAD"])
    else:
        branches_to_try.extend(["main", "master", "HEAD"])

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for try_branch in branches_to_try:
            try:
                response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/git/trees/{try_branch}",
                    headers=headers,
                    params={"recursive": "1"},
                )
                if response.status_code != 200:
                    continue

                tree = response.json().get("tree", [])
                return [
                    {
                        "path": item["path"],
                        "type": item["type"],
                        "size": item.get("size"),
                    }
                    for item in tree
                    if item.get("type") in ("blob", "tree")
                ], try_branch
            except Exception:
                continue

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

    # Repositories still commonly use ``master`` as their default branch.
    # ``fetch_github_tree`` already falls back across these refs, but this
    # endpoint used to try only ``main``. That left a visible tree whose every
    # file failed to load. Keep the same fallback behaviour for individual
    # files (including old tree caches written before the resolved ref was
    # known).
    refs_to_try = [branch]
    if branch == "main":
        refs_to_try.extend(["master", "HEAD"])
    elif branch == "master":
        refs_to_try.extend(["main", "HEAD"])
    else:
        refs_to_try.extend(["main", "master", "HEAD"])

    # Preserve directory separators while escaping all special characters in
    # a filename (for example '#', '?', Unicode, and spaces).
    encoded_path = quote(file_path, safe="/")

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for ref in refs_to_try:
                response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}",
                    headers=headers,
                    params={"ref": ref},
                )

                if response.status_code != 200:
                    continue

                data = response.json()

                # GitHub returns base64-encoded content.
                if "content" in data and data.get("encoding") == "base64":
                    content_b64 = data["content"].replace("\n", "")
                    return base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        return None

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
        # Use `is not None`, not truthy: an empty-but-valid cached JSON
        # object (e.g. `{}`) must still count as a cache hit, not a miss.
        if cached_data is not None:
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
    fetched_tree = await fetch_github_tree_with_branch(owner, repo, branch)

    if not fetched_tree:
        return RepositoryTreeResult(
            available=False,
            note="Failed to fetch repository tree from GitHub.",
        ), None

    tree, resolved_branch = fetched_tree

    # Save tree to disk cache
    new_cache_path = disk_cache.generate_cache_path(item.item_id, "source_tree", "json")
    cache_data = {
        "owner": owner,
        "repo": repo,
        "branch": resolved_branch,
        "tree": tree,
    }

    disk_cache.write_cached_json(new_cache_path, cache_data)

    # Files are fetched and cached on demand. Downloading an entire repository
    # here easily exceeds GitHub's unauthenticated API limit before the user
    # has opened even one file.

    return RepositoryTreeResult(available=True, tree=tree), new_cache_path


async def get_source_file(item: Item, file_path: str) -> SourceFileResult:
    """
    Fetch and cache individual source file.

    Returns SourceFileResult with file content.
    """
    # Check cache
    cache_path = disk_cache.generate_source_file_cache_path(item.item_id, file_path)
    cached_content = disk_cache.read_cached_file(cache_path)
    # `is not None`, not truthy: a cached-but-empty file is still a cache
    # hit and must not trigger a redundant GitHub fetch.
    if cached_content is not None:
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
    # A tree cache records the ref that worked (e.g. master instead of the
    # assumed main). Reuse it so each selected file needs only one API call.
    tree_cache_path = item.artifacts.source_tree_cache_path
    if tree_cache_path:
        tree_cache = disk_cache.read_cached_json(tree_cache_path)
        cached_branch = tree_cache.get("branch") if tree_cache else None
        if isinstance(cached_branch, str) and cached_branch:
            branch = cached_branch
    content = await fetch_github_file(owner, repo, file_path, branch)

    if content is None:
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


async def resolve_source(item: Item) -> tuple[SourcePreviewResult, str | None]:
    """
    Returns README/doc text for source preview, with disk caching.

    Resolution order:
      1. item.artifacts.source_code (captured inline during discovery -
         already persisted in the DB, so no disk cache needed).
      2. Disk cache at item.artifacts.source_readme_cache_path, if set.
      3. Fetch README.md from GitHub, then write it to disk and return the
         new relative cache path so the caller can persist it as a pointer
         in the DB (mirrors the integration/source-tree caching pattern).

    Returns (SourcePreviewResult, relative_cache_path or None). The cache
    path is only non-None when a *new* cache entry was written; callers
    should compare against the existing DB value before writing.
    """
    source_code = item.artifacts.source_code
    if source_code:
        return _source_preview_from_inline_code(item, source_code), None

    # Cache hit: serve the previously downloaded README straight from disk,
    # no network call at all.
    existing_cache_path = item.artifacts.source_readme_cache_path
    if existing_cache_path:
        cached_content = disk_cache.read_cached_file(existing_cache_path)
        if cached_content is not None:
            return SourcePreviewResult(
                available=True,
                language="markdown",
                content=cached_content,
                note="README.md served from disk cache.",
            ), existing_cache_path

    # Cache miss: fetch README from GitHub, then persist it to disk.
    source_url = str(item.artifacts.source_url) if item.artifacts.source_url else None
    if source_url:
        repo_info = _extract_github_repo_info(source_url)
        if repo_info:
            owner, repo, branch = repo_info
            readme_content = await fetch_github_file(owner, repo, "README.md", branch)
            if readme_content is not None:
                new_cache_path = disk_cache.generate_cache_path(item.item_id, "readme", "md")
                disk_cache.write_cached_file(new_cache_path, readme_content)
                return SourcePreviewResult(
                    available=True,
                    language="markdown",
                    content=readme_content,
                    note="README.md fetched from GitHub repository and cached to disk.",
                ), new_cache_path

    return SourcePreviewResult(
        available=False,
        note="Source code was not retrieved for this item — only registry "
             "or configuration metadata is available.",
    ), None


def _source_preview_from_inline_code(item: Item, source_code: str) -> SourcePreviewResult:
    """Build a SourcePreviewResult from source_code already stored on the item."""
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
