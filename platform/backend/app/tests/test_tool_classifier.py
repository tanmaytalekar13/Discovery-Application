"""Tests for the tool classification logic.

Covers:
  - Pure remote (mcp_registry_remotes with non-localhost URL)
  - Remote via package (streamable-http / sse transport, non-localhost)
  - Local stdio package
  - Localhost http package (treated as local)
  - Mixed packages (remote + stdio in same item)
  - Missing config_files, empty packages, malformed entries
  - Order preservation: streamable-http preferred over sse
  - Invalid URLs treated as local (defensive)
"""
from __future__ import annotations

import pytest

from app.sandbox.classifier import classify_tool
from app.sandbox.schemas import (
    ClassificationResult,
    LocalPackageHint,
    RemoteCandidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(config_files: list[dict] | None) -> dict:
    return {"artifacts": {"config_files": config_files or []}}


# ---------------------------------------------------------------------------
# Pure remote (mcp_registry_remotes)
# ---------------------------------------------------------------------------

def test_remote_with_streamable_http_is_testable():
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "streamable-http", "url": "https://mcp.example.com"}
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is True
    assert result.mode == "remote"
    assert isinstance(result.detail, list)
    assert result.detail[0].type == "streamable-http"
    assert result.detail[0].url == "https://mcp.example.com"


def test_remote_with_sse_is_testable():
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "sse", "url": "https://mcp.example.com/sse"}
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is True
    assert result.mode == "remote"
    assert result.detail[0].type == "sse"


def test_remote_localhost_url_is_filtered_out():
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "streamable-http", "url": "http://localhost:3000/mcp"}
                ],
            }
        ]
    )
    result = classify_tool(item)
    # Only entry was a localhost URL, so it gets filtered -> falls through
    # to the "not_testable" branch (no packages to fall back to).
    assert result.testable is False
    assert result.mode == "not_testable"


def test_remote_preserves_input_order():
    """streamable-http then sse in the source should come out in that order."""
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "streamable-http", "url": "https://a.example.com"},
                    {"type": "sse", "url": "https://b.example.com/sse"},
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert [c.url for c in result.detail] == [
        "https://a.example.com",
        "https://b.example.com/sse",
    ]


def test_remote_mixed_localhost_and_remote_keeps_only_remote():
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "streamable-http", "url": "http://localhost:3000/mcp"},
                    {"type": "sse", "url": "https://remote.example.com/sse"},
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is True
    assert result.mode == "remote"
    assert [c.url for c in result.detail] == ["https://remote.example.com/sse"]


def test_remote_skips_unknown_transport_types():
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "stdio", "url": "ignored"},
                    {"type": "streamable-http", "url": "https://ok.example.com"},
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is True
    assert [c.url for c in result.detail] == ["https://ok.example.com"]


# ---------------------------------------------------------------------------
# Remote via package (mcp_registry_packages)
# ---------------------------------------------------------------------------

def test_remote_via_package_streamable_http():
    item = _item(
        [
            {
                "kind": "mcp_registry_packages",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "some-remote-pkg",
                        "runtimeHint": "npx",
                        "transport": {
                            "type": "streamable-http",
                            "url": "https://remote.example.com/mcp",
                        },
                    }
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is True
    assert result.mode == "remote_via_package"
    assert result.detail[0].url == "https://remote.example.com/mcp"


def test_remote_via_package_sse():
    item = _item(
        [
            {
                "kind": "mcp_registry_packages",
                "packages": [
                    {
                        "transport": {
                            "type": "sse",
                            "url": "https://remote.example.com/sse",
                        }
                    }
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is True
    assert result.mode == "remote_via_package"
    assert result.detail[0].type == "sse"


def test_remote_via_package_localhost_url_is_local():
    """An http transport pointing at localhost is functionally local."""
    item = _item(
        [
            {
                "kind": "mcp_registry_packages",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "local-http-pkg",
                        "runtimeHint": "npx",
                        "transport": {
                            "type": "streamable-http",
                            "url": "http://127.0.0.1:3000/mcp",
                        },
                    }
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is False
    assert result.mode == "local_stdio"
    assert isinstance(result.detail[0], LocalPackageHint)


def test_mixed_remote_and_stdio_packages_prefers_remote():
    """When a tool has both stdio and a remote package, the remote wins."""
    item = _item(
        [
            {
                "kind": "mcp_registry_packages",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "gmail-workspace-mcp-server",
                        "runtimeHint": "npx",
                        "transport": {"type": "stdio"},
                        "environmentVariables": [
                            {"name": "GMAIL_OAUTH_CLIENT_ID", "isSecret": True}
                        ],
                    },
                    {
                        "registryType": "npm",
                        "identifier": "gmail-workspace-mcp-server-remote",
                        "runtimeHint": "npx",
                        "transport": {
                            "type": "streamable-http",
                            "url": "https://gmail.example.com/mcp",
                        },
                    },
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is True
    assert result.mode == "remote_via_package"
    assert [c.url for c in result.detail] == ["https://gmail.example.com/mcp"]


# ---------------------------------------------------------------------------
# Local stdio packages
# ---------------------------------------------------------------------------

def test_local_stdio_package():
    item = _item(
        [
            {
                "kind": "mcp_registry_packages",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "gmail-workspace-mcp-server",
                        "runtimeHint": "npx",
                        "transport": {"type": "stdio"},
                        "environmentVariables": [
                            {
                                "name": "GMAIL_OAUTH_CLIENT_ID",
                                "isSecret": True,
                                "description": "OAuth client id",
                            }
                        ],
                    }
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.testable is False
    assert result.mode == "local_stdio"
    assert isinstance(result.detail[0], LocalPackageHint)
    assert result.detail[0].install_command == "npx -y gmail-workspace-mcp-server"
    assert result.detail[0].environment_variables[0]["name"] == "GMAIL_OAUTH_CLIENT_ID"


def test_local_pypi_package_uses_uvx():
    item = _item(
        [
            {
                "kind": "mcp_registry_packages",
                "packages": [
                    {
                        "registryType": "pypi",
                        "identifier": "some-mcp-server",
                        "runtimeHint": "uvx",
                        "transport": {"type": "stdio"},
                    }
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.mode == "local_stdio"
    assert result.detail[0].install_command == "uvx some-mcp-server"


def test_local_oci_package_uses_docker():
    item = _item(
        [
            {
                "kind": "mcp_registry_packages",
                "packages": [
                    {
                        "registryType": "oci",
                        "identifier": "ghcr.io/example/mcp:latest",
                        "transport": {"type": "stdio"},
                    }
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert result.mode == "local_stdio"
    assert result.detail[0].install_command == "docker run -i --rm ghcr.io/example/mcp:latest"


# ---------------------------------------------------------------------------
# Not testable
# ---------------------------------------------------------------------------

def test_no_config_files_is_not_testable():
    result = classify_tool(_item(None))
    assert result.testable is False
    assert result.mode == "not_testable"
    assert result.reason is not None


def test_web_search_only_is_not_testable():
    item = {
        "artifacts": {
            "config_files": [
                {
                    "kind": "web_search_result",
                    "url": "https://blog.example.com/best-mcp-servers",
                }
            ]
        }
    }
    result = classify_tool(item)
    assert result.testable is False
    assert result.mode == "not_testable"


def test_empty_packages_array_is_not_testable():
    item = _item([{"kind": "mcp_registry_packages", "packages": []}])
    result = classify_tool(item)
    assert result.testable is False
    assert result.mode == "not_testable"


def test_empty_remotes_array_falls_through():
    item = _item([{"kind": "mcp_registry_remotes", "remotes": []}])
    result = classify_tool(item)
    assert result.testable is False
    assert result.mode == "not_testable"


# ---------------------------------------------------------------------------
# Robustness / malformed input
# ---------------------------------------------------------------------------

def test_malformed_url_in_remotes_is_treated_as_local():
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "streamable-http", "url": "not-a-valid-url"}
                ],
            }
        ]
    )
    result = classify_tool(item)
    # Invalid URL -> treated as local -> filtered out -> not_testable
    assert result.mode == "not_testable"


def test_non_dict_entries_are_ignored():
    item = _item(
        [
            "not-a-dict",
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "streamable-http", "url": "https://ok.example.com"}
                ],
            },
        ]
    )
    result = classify_tool(item)
    assert result.testable is True
    assert result.mode == "remote"


def test_missing_artifacts_is_not_testable():
    result = classify_tool({})
    assert result.testable is False
    assert result.mode == "not_testable"


def test_config_files_not_a_list_is_not_testable():
    result = classify_tool({"artifacts": {"config_files": "oops"}})
    assert result.testable is False
    assert result.mode == "not_testable"


def test_result_is_classification_result_instance():
    result = classify_tool(_item(None))
    assert isinstance(result, ClassificationResult)


def test_remote_candidates_have_correct_shape():
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "streamable-http", "url": "https://mcp.example.com"}
                ],
            }
        ]
    )
    result = classify_tool(item)
    assert isinstance(result.detail[0], RemoteCandidate)


# ---------------------------------------------------------------------------
# Multiple kinds: remotes present but only localhost
# ---------------------------------------------------------------------------

def test_remotes_only_localhost_falls_through_to_packages_local():
    item = _item(
        [
            {
                "kind": "mcp_registry_remotes",
                "remotes": [
                    {"type": "streamable-http", "url": "http://localhost:9000/mcp"}
                ],
            },
            {
                "kind": "mcp_registry_packages",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "local-tool",
                        "runtimeHint": "npx",
                        "transport": {"type": "stdio"},
                    }
                ],
            },
        ]
    )
    result = classify_tool(item)
    # No remote candidates -> falls into package branch -> stdio -> local_stdio
    assert result.testable is False
    assert result.mode == "local_stdio"
