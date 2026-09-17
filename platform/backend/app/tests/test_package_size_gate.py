"""Tests for the package-size preflight (exit-137 prevention at the source).

All registry access goes through httpx.MockTransport - no network, no Docker.
The estimator is fail-open: unknown size never blocks a package.
"""

from __future__ import annotations

import asyncio

import httpx

from app.sandbox.package_size import (
    NPM_UNPACKED_SIZE_LIMIT_BYTES,
    PackageSizeEstimator,
)
from app.verification.models import McpServerRecord, Transport
from app.verification.prefilter import (
    local_package_from_install_cmd,
    prefilter_server_with_size,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _npm_payload(unpacked_size: int, version: str = "1.0.0") -> dict:
    return {
        "dist-tags": {"latest": version},
        "versions": {
            version: {
                "version": version,
                "dist": {"unpackedSize": unpacked_size},
            }
        },
    }


def _pypi_payload(dep_count: int, extra_count: int = 0) -> dict:
    requires = [f"dep{i}>=1.0" for i in range(dep_count)]
    # PEP 508 extras markers (what PyPI actually returns for optional deps).
    requires += [f"depX>=1.0 ; extra == 'cli{i}'" for i in range(extra_count)]
    return {"info": {"version": "2.0.0", "requires_dist": requires}}


def _pypi_router(payloads: dict[str, dict | None]):
    """MockTransport handler routing /pypi/<name>/json to per-package payloads.

    A name missing from `payloads` yields a metadata-less package (mirrors
    real PyPI packages without requires_dist)."""

    def handler(request: httpx.Request) -> httpx.Response:
        parts = [p for p in request.url.path.split("/") if p]
        # .../pypi/<name>/json
        name = parts[parts.index("pypi") + 1] if "pypi" in parts else ""
        payload = payloads.get(name)
        if payload is None:
            return httpx.Response(200, json={"info": {"version": "1.0.0", "requires_dist": None}})
        return httpx.Response(200, json=payload)

    return handler


def _npm_router(payloads: dict[str, dict]):
    """MockTransport handler routing /<name> and /<name>/<version> to packuments."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        name = path.split("/")[0]
        payload = payloads.get(name)
        if payload is None:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    return handler


def _estimator(handler) -> PackageSizeEstimator:
    return PackageSizeEstimator(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _local_record(install_cmd: str) -> McpServerRecord:
    return McpServerRecord(
        name="some-mcp",
        source_url="https://example.com/some-mcp",
        transport=Transport.LOCAL,
        install_cmd=install_cmd,
    )


# ---------------------------------------------------------------------------
# PackageSizeEstimator
# ---------------------------------------------------------------------------


class TestNpmEstimator:
    def test_unpacked_size_resolved_from_latest(self):
        payload = _npm_payload(unpacked_size=5 * 1024 * 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "registry.npmjs.org"
            return httpx.Response(200, json=payload)

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_npm("some-mcp")
            finally:
                await estimator.aclose()

        estimate = asyncio.run(run())
        assert estimate.known
        assert not estimate.over_limit
        assert estimate.bytes_estimate == 5 * 1024 * 1024
        assert estimate.version == "1.0.0"

    def test_scoped_package_with_version(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=_npm_payload(unpacked_size=1024, version="2.3.4"))

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_npm("@scope/pkg@2.3.4")
            finally:
                await estimator.aclose()

        estimate = asyncio.run(run())
        assert seen["url"].endswith("/@scope/pkg")
        assert estimate.bytes_estimate == 1024
        assert estimate.version == "2.3.4"

    def test_oversized_package_is_flagged(self):
        huge = NPM_UNPACKED_SIZE_LIMIT_BYTES + 1

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_npm_payload(unpacked_size=huge))

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_npm("huge-mcp")
            finally:
                await estimator.aclose()

        assert asyncio.run(run()).over_limit

    def test_registry_error_fails_open(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_npm("some-mcp")
            finally:
                await estimator.aclose()

        estimate = asyncio.run(run())
        assert not estimate.known
        assert not estimate.over_limit

    def test_closure_bytes_sum_root_and_dependencies(self):
        def packument(version: str, size: int, deps: dict | None = None) -> dict:
            doc = {"version": version, "dist": {"unpackedSize": size}}
            if deps is not None:
                doc["dependencies"] = deps
            return {
                "dist-tags": {"latest": version},
                "versions": {version: doc},
            }

        handler = _npm_router({
            "root-pkg": packument("1.0.0", 3 * 1024 * 1024, {"dep-a": "^2.0.0", "dep-b": "^1.0.0"}),
            "dep-a": packument("2.0.0", 1024),
            "dep-b": packument("1.5.0", 2048),
        })

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_npm("root-pkg")
            finally:
                await estimator.aclose()

        estimate = asyncio.run(run())
        # Root (3MB) + dep-a (1KB) + dep-b (2KB) - the full closure, each at
        # its own resolved version.
        assert estimate.bytes_estimate == 3 * 1024 * 1024 + 1024 + 2048


class TestPypiEstimator:
    def test_direct_dependencies_counted(self):
        handler = _pypi_router({"some-server": _pypi_payload(dep_count=3, extra_count=2)})

        def assert_root(request: httpx.Request) -> httpx.Response:
            assert str(request.url).startswith("https://pypi.org/pypi/some-server/json")
            return handler(request)

        async def run():
            estimator = _estimator(assert_root)
            try:
                return await estimator.estimate_pypi("some-server")
            finally:
                await estimator.aclose()

        estimate = asyncio.run(run())
        # Root + 3 children (children have no metadata of their own -> leaf 1 each).
        assert estimate.dependency_count == 4
        assert not estimate.over_limit

    def test_closure_width_counts_transitive_deps(self):
        handler = _pypi_router({
            "root": {"info": {"version": "1.0.0", "requires_dist": ["mid1>=1", "mid2>=1"]}},
            "mid1": {"info": {"version": "1.0.0", "requires_dist": ["leaf-a>=1", "leaf-b>=1"]}},
            "mid2": {"info": {"version": "1.0.0", "requires_dist": ["leaf-c>=1"]}},
        })

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_pypi("root")
            finally:
                await estimator.aclose()

        # root + 2 mids + 3 leaves = 6 nodes in the closure.
        assert asyncio.run(run()).dependency_count == 6

    def test_cyclic_closure_counts_each_node_once(self):
        handler = _pypi_router({
            "a": {"info": {"version": "1.0.0", "requires_dist": ["b>=1"]}},
            "b": {"info": {"version": "1.0.0", "requires_dist": ["a>=1"]}},
        })

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_pypi("a")
            finally:
                await estimator.aclose()

        assert asyncio.run(run()).dependency_count == 2

    def test_extras_are_stripped_from_identifier(self):
        requested: list[str] = []
        inner = _pypi_router({"some-server": _pypi_payload(dep_count=1)})

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return inner(request)

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_pypi("some-server[all]")
            finally:
                await estimator.aclose()

        asyncio.run(run())
        # The root fetch must use the stripped name (no [extras] in the URL).
        assert any("/some-server/json" in path for path in requested)
        assert not any("[all]" in path for path in requested)

    def test_too_many_dependencies_is_flagged(self):
        from app.sandbox.package_size import PYPI_DEPENDENCY_LIMIT

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=_pypi_payload(dep_count=PYPI_DEPENDENCY_LIMIT + 1)
            )

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_pypi("heavy-server")
            finally:
                await estimator.aclose()

        assert asyncio.run(run()).over_limit

    def test_root_without_metadata_is_unknown_not_zero(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"info": {"version": "1.0.0", "requires_dist": None}})

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_pypi("mystery-server")
            finally:
                await estimator.aclose()

        estimate = asyncio.run(run())
        assert not estimate.known
        assert not estimate.over_limit

    def test_network_error_fails_open(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        async def run():
            estimator = _estimator(handler)
            try:
                return await estimator.estimate_pypi("some-server")
            finally:
                await estimator.aclose()

        estimate = asyncio.run(run())
        assert not estimate.known


# ---------------------------------------------------------------------------
# local_package_from_install_cmd
# ---------------------------------------------------------------------------


class TestLocalPackageFromInstallCmd:
    def test_npx(self):
        assert local_package_from_install_cmd("npx -y @scope/pkg") == ("npm", "@scope/pkg")

    def test_uvx(self):
        assert local_package_from_install_cmd("uvx mcp-server-foo") == ("pypi", "mcp-server-foo")

    def test_pipx(self):
        assert local_package_from_install_cmd("pipx run mcp-server-foo") == ("pypi", "mcp-server-foo")

    def test_remote_or_unknown_is_none(self):
        assert local_package_from_install_cmd(None) is None
        assert local_package_from_install_cmd("") is None
        assert local_package_from_install_cmd("docker run foo") is None
        assert local_package_from_install_cmd("npx") is None


# ---------------------------------------------------------------------------
# prefilter_server_with_size (ingestion path)
# ---------------------------------------------------------------------------


class TestPrefilterServerWithSize:
    def test_oversized_npm_is_malformed(self):
        huge = NPM_UNPACKED_SIZE_LIMIT_BYTES + 1024

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_npm_payload(unpacked_size=huge))

        async def run():
            estimator = _estimator(handler)
            try:
                return await prefilter_server_with_size(
                    _local_record("npx -y huge-mcp"), estimator=estimator
                )
            finally:
                await estimator.aclose()

        result = asyncio.run(run())
        assert not result.passed
        assert result.status == "malformed"
        assert result.failures and "package_too_large" in result.failures[0]

    def test_small_package_passes(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_npm_payload(unpacked_size=1024))

        async def run():
            estimator = _estimator(handler)
            try:
                return await prefilter_server_with_size(
                    _local_record("npx -y tiny-mcp"), estimator=estimator
                )
            finally:
                await estimator.aclose()

        result = asyncio.run(run())
        assert result.passed

    def test_unknown_size_fails_open(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        async def run():
            estimator = _estimator(handler)
            try:
                return await prefilter_server_with_size(
                    _local_record("uvx mcp-server-x"), estimator=estimator
                )
            finally:
                await estimator.aclose()

        assert asyncio.run(run()).passed

    def test_remote_records_skip_size_check(self):
        """No npm/PyPI lookup may happen for remote endpoints."""

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("registry must not be called for remote records")

        async def run():
            estimator = _estimator(handler)
            try:
                return await prefilter_server_with_size(
                    McpServerRecord(
                        name="remote",
                        source_url="https://example.com/remote",
                        transport=Transport.REMOTE,
                        endpoint_url="https://mcp.example.com/mcp",
                    ),
                    estimator=estimator,
                )
            finally:
                await estimator.aclose()

        assert asyncio.run(run()).passed

    def test_structurally_invalid_records_still_fail_first(self):
        async def run():
            return await prefilter_server_with_size(_local_record(""))

        result = asyncio.run(run())
        assert not result.passed
        assert result.status == "malformed"
