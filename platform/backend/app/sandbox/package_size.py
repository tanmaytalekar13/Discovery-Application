"""Package-size estimation for local MCP sandbox testing.

The sandbox container runs with a hard memory cap. Heavyweight packages -
long dependency closures pulled by an ``uvx``/``npx`` install - can
transiently exceed it and get OOM-killed (exit 137) mid-handshake. Rather
than letting users discover that at connect time, this module estimates
the install footprint from registry metadata *before* anything is
started, so oversized packages can be filtered and the UI can warn.

What is measured (and why):
- npm:  ``registry.npmjs.org/<pkg>`` gives ``dist.unpackedSize`` per
  version. The OOM risk comes from the whole extracted closure, so we
  sum the root package's unpacked size with its (capped) dependency
  closure, using each dep's declared ``dist.unpackedSize``.
- PyPI: the JSON API publishes ``info.requires_dist`` (declarations like
  ``mcp>=1.0 ; python_version>="3.10"``) but no unpacked sizes. The
  closure width (BFS over declared runtime dependencies, capped) is the
  best cheap proxy for how many wheels ``uvx`` must download+extract in
  parallel - which is exactly the transient spike that gets SIGKILLed.

The estimator is deliberately fail-open: any registry hiccup returns
``known=False``, which never blocks a test. A wrong "too big" verdict is
worse than no verdict because the OOM retry path can still recover
transient kills.

Closure traversals are capped (depth and node count) and memoized per
process; a cap hit only ever *undercounts*, so the static gate stays a
fast filter - the hard runtime backstop lives in connect().
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (env-tunable so a beefier host can raise them without code)
# ---------------------------------------------------------------------------

# npm: max total unpacked bytes of the install closure (default 60 MB).
NPM_UNPACKED_SIZE_LIMIT_BYTES = int(os.getenv("SANDBOX_NPM_SIZE_LIMIT_MB", "60")) * 1024 * 1024
# PyPI: max transitive dependency closure (default 40 packages).
PYPI_DEPENDENCY_LIMIT = int(os.getenv("SANDBOX_PYPI_DEP_LIMIT", "40"))

_NPM_REGISTRY = "https://registry.npmjs.org"
_PYPI_REGISTRY = "https://pypi.org"
_SIZE_TIMEOUT_S = 6.0

# Traversal caps: a hit only undercounts (fail-open direction).
_CLOSURE_MAX_NODES = 60
_CLOSURE_MAX_DEPTH = 4

# npm version fields that mean "not a concrete version".
_FLOATING_TAGS = frozenset({"latest", "next", "beta", "alpha", "rc", ""})


@dataclass(frozen=True)
class PackageSizeEstimate:
    """Best-effort size evidence for one package."""

    identifier: str
    registry: str  # "npm" | "pypi"
    bytes_estimate: int | None = None  # npm closure bytes
    dependency_count: int | None = None  # pypi closure width
    version: str | None = None  # resolved concrete version, when known

    @property
    def known(self) -> bool:
        return self.bytes_estimate is not None or self.dependency_count is not None

    @property
    def over_limit(self) -> bool:
        if self.bytes_estimate is not None:
            return self.bytes_estimate > NPM_UNPACKED_SIZE_LIMIT_BYTES
        if self.dependency_count is not None:
            return self.dependency_count > PYPI_DEPENDENCY_LIMIT
        return False


def describe_size(estimate: PackageSizeEstimate) -> str:
    """One-line human-readable measurement for rejection evidence/logs."""
    if estimate.bytes_estimate is not None:
        return f"install closure {estimate.bytes_estimate // (1024 * 1024)}MB"
    if estimate.dependency_count is not None:
        return f"dependency closure {estimate.dependency_count} packages"
    return "size unknown"


@dataclass
class _ClosureStats:
    """Mutable accumulator shared across the (bounded) closure walk."""

    nodes: set[str] = field(default_factory=set)
    total_bytes: int = 0


def _strip_version(identifier: str) -> tuple[str, str]:
    """Split an npm identifier into (name, version-or-tag).

    Handles plain names and @scope/name, with or without @version. Only the
    LAST @ counts when it is not the leading scope marker:
    @scope/pkg@1.2.3 -> ("@scope/pkg", "1.2.3"); @scope/pkg -> ("@scope/pkg", "").
    """
    at = identifier.rfind("@")
    if at > 0:
        return identifier[:at], identifier[at + 1:]
    return identifier, ""


def _clean_npm_spec(spec: str) -> str:
    """Reduce a dependency spec like ``^1.2.3`` / ``github:a/b`` to "" (unknown).

    Only concrete semver specs (or empty = latest) are followed; ranges and
    remote specs are treated as unresolvable so the walker skips them.
    """
    spec = (spec or "").strip()
    if not spec:
        return ""
    if re.fullmatch(r"v?\d+(\.\d+){0,2}", spec):
        return spec.lstrip("v")
    return ""


def _pyproject_like_extras(name: str) -> str:
    """Strip PEP 508 extras/environment markers: pkg[extra]>=1.0 ; python_version<"3"."""
    return re.split(r"[<>=!~;\[\s]", name.strip(), maxsplit=1)[0].strip()


def _is_optional_pypi_dep(requires_line: str) -> bool:
    """True when a requires_dist entry is an optional extra (base install skips it)."""
    return "extra ==" in (requires_line or "")


class PackageSizeEstimator:
    """Queries registry metadata for install-footprint estimates.

    Constructed with its own ``httpx.AsyncClient`` so tests can inject a
    MockTransport. Never raises: registry/network errors resolve to
    ``PackageSizeEstimate.known == False``.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=_SIZE_TIMEOUT_S)
        self._owns_client = client is None
        # Process-level memoization: (registry, name, version-or-"") -> payload
        # doc (or None on failure). Shared across walks so the closure BFS is
        # one fetch per unique node even across many estimates.
        self._doc_cache: dict[tuple[str, str, str], dict | None] = {}
        self._cache_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- transport ----------------------------------------------------------

    async def _get_json(self, url: str) -> dict | None:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:  # noqa: BLE001 - fail open, never block a test
            logger.debug("Registry lookup failed for %s: %s", url, exc)
            return None

    # -- npm ---------------------------------------------------------------

    async def _npm_doc(self, name: str, version_or_tag: str) -> tuple[dict | None, str | None]:
        """(packument, resolved-version) for an npm package name.

        ``version_or_tag`` may be a concrete version, a dist-tag, or empty
        (-> latest). Concrete-version fast path fetches the versioned doc
        directly; otherwise the packument is fetched and dist-tags resolve.
        """
        key = ("npm", name, version_or_tag)
        if key in self._doc_cache:
            cached = self._doc_cache[key]
            if isinstance(cached, tuple):
                return cached
            return None, None

        if version_or_tag and re.fullmatch(r"v?\d+(\.\d+){0,2}", version_or_tag):
            doc = await self._get_json(f"{_NPM_REGISTRY}/{name}/{version_or_tag.lstrip('v')}")
            # A version-shaped doc carries its own dist/version fields; some
            # registries (and test doubles) serve packument shapes instead,
            # so only accept the fast path when it really is a version doc.
            if isinstance(doc, dict) and isinstance(doc.get("dist"), dict):
                result = (doc, str(doc.get("version") or version_or_tag))
                self._doc_cache[key] = result
                return result
            # Fall through to packument resolution.

        packument = await self._get_json(f"{_NPM_REGISTRY}/{name}")
        if not isinstance(packument, dict):
            self._doc_cache[key] = None
            return None, None

        versions = packument.get("versions")
        version_key: str | None = None
        if isinstance(versions, dict):
            if version_or_tag and version_or_tag in versions:
                version_key = version_or_tag
            elif isinstance(packument.get("dist-tags"), dict):
                version_key = packument["dist-tags"].get(version_or_tag or "latest")
            if version_key is None:
                version_key = packument["dist-tags"].get("latest") if isinstance(
                    packument.get("dist-tags"), dict
                ) else None
        doc = versions.get(version_key) if isinstance(versions, dict) and version_key else None
        if not isinstance(doc, dict):
            self._doc_cache[key] = None
            return None, None
        result = (doc, str(version_key))
        self._doc_cache[key] = result
        return result

    async def _npm_closure(self, root_name: str, version_or_tag: str, stats: _ClosureStats, depth: int) -> int:
        """Recursively accumulate unpacked bytes; returns bytes added this subtree.

        Bounded by _CLOSURE_MAX_NODES / _CLOSURE_MAX_DEPTH; a cap hit only
        undercounts, which keeps the gate fail-open in the right direction.
        """
        if depth > _CLOSURE_MAX_DEPTH or len(stats.nodes) >= _CLOSURE_MAX_NODES:
            return 0
        if root_name in stats.nodes:
            return 0
        stats.nodes.add(root_name)

        doc, version = await self._npm_doc(root_name, version_or_tag)
        if not isinstance(doc, dict):
            return 0

        dist = doc.get("dist") if isinstance(doc.get("dist"), dict) else {}
        unpacked = dist.get("unpackedSize")
        added = int(unpacked) if isinstance(unpacked, (int, float)) and unpacked > 0 else 0
        stats.total_bytes += added

        deps = doc.get("dependencies")
        if isinstance(deps, dict) and len(stats.nodes) < _CLOSURE_MAX_NODES:
            dep_names = list(deps)[: max(0, _CLOSURE_MAX_NODES - len(stats.nodes))]
            results = await asyncio.gather(
                *(
                    self._npm_closure(name, _clean_npm_spec(deps[name]), stats, depth + 1)
                    for name in dep_names
                )
            )
            added += sum(results)
        return added

    async def estimate_npm(self, identifier: str) -> PackageSizeEstimate:
        """Unpacked bytes of the root package plus its (capped) dependency closure."""
        name, requested = _strip_version((identifier or "").strip())
        if not name or name.startswith(("./", "../", "/")):
            return PackageSizeEstimate(identifier=identifier, registry="npm")

        stats = _ClosureStats()
        total = await self._npm_closure(name, requested, stats, depth=0)

        # Cache hit from the walk: when the ROOT metadata itself is missing we
        # cannot judge at all - return unknown (fail-open), not bytes_estimate=0.
        root_doc, root_version = await self._npm_doc(name, requested)
        if not isinstance(root_doc, dict):
            return PackageSizeEstimate(identifier=identifier, registry="npm")

        return PackageSizeEstimate(
            identifier=identifier,
            registry="npm",
            bytes_estimate=total,
            version=root_version,
        )

    # -- PyPI --------------------------------------------------------------

    async def _pypi_requires(self, name: str) -> list[str] | None:
        """Runtime requires_dist for a PyPI package (None when unknown)."""
        key = ("pypi", name, "")
        if key in self._doc_cache:
            cached = self._doc_cache[key]
            return cached if isinstance(cached, list) else None
        payload = await self._get_json(f"{_PYPI_REGISTRY}/pypi/{name}/json")
        info = payload.get("info") if isinstance(payload, dict) else None
        requires = info.get("requires_dist") if isinstance(info, dict) else None
        if not isinstance(requires, list):
            self._doc_cache[key] = None
            return None
        deps = [r for r in requires if isinstance(r, str) and not _is_optional_pypi_dep(r)]
        self._doc_cache[key] = deps
        return deps

    async def _pypi_closure_width(self, root_name: str, stats: _ClosureStats, depth: int) -> int:
        """BFS over declared runtime dependencies (bounded); returns nodes added."""
        if depth > _CLOSURE_MAX_DEPTH or len(stats.nodes) >= _CLOSURE_MAX_NODES:
            return 0
        if root_name in stats.nodes:
            return 0
        stats.nodes.add(root_name)

        requires = await self._pypi_requires(root_name)
        if requires is None:
            # No metadata (or registry miss): count the node itself only.
            return 1

        added = 1  # this node
        child_names = [_pyproject_like_extras(r) for r in requires]
        child_names = [c for c in child_names if c][: max(0, _CLOSURE_MAX_NODES - len(stats.nodes))]
        results = await asyncio.gather(
            *(
                self._pypi_closure_width(child, stats, depth + 1)
                for child in child_names
            )
        )
        return added + sum(results)

    async def estimate_pypi(self, identifier: str) -> PackageSizeEstimate:
        """Dependency-closure width for the target package.

        PyPI's JSON API publishes ``info.requires_dist`` but no unpacked
        size of the wheel closure, so closure width (not just direct deps)
        is the proxy for how heavy the uvx extract will be. Root metadata
        missing -> unknown (fail-open); a child missing metadata still
        counts as 1 node (deliberate undercount, never overcount).
        """
        name = _pyproject_like_extras(identifier)
        if not name:
            return PackageSizeEstimate(identifier=identifier, registry="pypi")

        if await self._pypi_requires(name) is None:
            return PackageSizeEstimate(identifier=identifier, registry="pypi")

        stats = _ClosureStats()
        width = await self._pypi_closure_width(name, stats, depth=0)
        if width <= 0:
            return PackageSizeEstimate(identifier=identifier, registry="pypi")

        return PackageSizeEstimate(
            identifier=identifier,
            registry="pypi",
            dependency_count=width,
        )

    # -- dispatch ----------------------------------------------------------

    async def estimate(self, registry_type: str, identifier: str | None) -> PackageSizeEstimate:
        """Dispatch on the registry hint used across the sandbox pipeline."""
        if not identifier:
            return PackageSizeEstimate(identifier="", registry=registry_type or "npm")
        if registry_type in ("pypi", "pip", "uvx"):
            return await self.estimate_pypi(identifier)
        return await self.estimate_npm(identifier)
