"""Tests for npm registry discovery adapter."""
from __future__ import annotations

import pytest

from app.discovery.npm.client import NpmDiscoveryAdapter


class TestNpmAdapterClassify:
    """Unit tests for npm package classification logic."""

    def _classify(self, adapter, entry: dict) -> bool:
        """Return True if _classify_package returns a candidate."""
        return adapter._classify_package(entry) is not None

    @pytest.fixture
    def adapter(self):
        return NpmDiscoveryAdapter()

    def test_mcp_keyword_package_is_classified(self, adapter):
        entry = {
            "package": {
                "name": "@modelcontextprotocol/server-filesystem",
                "version": "0.1.0",
                "description": "MCP server for filesystem access",
                "links": {"repository": "https://github.com/modelcontextprotocol/server-filesystem"},
            },
            "score": {"final": 0.9},
            "downloads": {"monthly": 10000},
        }
        assert self._classify(adapter, entry) is True

    def test_mcp_prefix_name_is_classified(self, adapter):
        entry = {
            "package": {
                "name": "mcp-server-example",
                "version": "1.0.0",
                "description": "An example MCP server",
                "links": {},
            },
            "score": {"final": 0.8},
            "downloads": {"monthly": 500},
        }
        assert self._classify(adapter, entry) is True

    def test_unrelated_package_is_not_classified(self, adapter):
        entry = {
            "package": {
                "name": "lodash",
                "version": "4.17.21",
                "description": "A utility library",
                "links": {},
            },
            "score": {"final": 0.95},
            "downloads": {"monthly": 50000000},
        }
        assert self._classify(adapter, entry) is False

    def test_package_without_description_or_keywords_not_classified(self, adapter):
        entry = {
            "package": {
                "name": "some-random-package",
                "version": "1.0.0",
                "description": "",
                "keywords": [],
                "links": {},
            },
            "score": {"final": 0.5},
            "downloads": {"monthly": 100},
        }
        assert self._classify(adapter, entry) is False

    def test_model_context_protocol_in_description_classified(self, adapter):
        entry = {
            "package": {
                "name": "some-pkg",
                "version": "1.0.0",
                "description": "Provides Model Context Protocol server capabilities",
                "links": {},
            },
            "score": {"final": 0.7},
            "downloads": {"monthly": 1000},
        }
        assert self._classify(adapter, entry) is True

    def test_repository_url_with_mcp_classified(self, adapter):
        entry = {
            "package": {
                "name": "not-mcp-named",
                "version": "1.0.0",
                "description": "A server",
                "links": {"repository": "https://github.com/example/mcp-tool"},
            },
            "score": {"final": 0.6},
            "downloads": {"monthly": 200},
        }
        assert self._classify(adapter, entry) is True
