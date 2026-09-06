"""Tests for GitHub Topics MCP server discovery adapter."""
from __future__ import annotations

import pytest

from app.discovery.github_topics.client import GitHubTopicsAdapter


class TestGitHubTopicsAdapterClassify:
    """Unit tests for GitHub Topics adapter classification."""

    def _classify(self, adapter, repo: dict) -> bool:
        """Return True if _classify_repository returns a candidate."""
        return adapter._classify_repository(repo) is not None

    @pytest.fixture
    def adapter(self):
        return GitHubTopicsAdapter()

    def test_repo_with_mcp_topic_is_classified(self, adapter):
        repo = {
            "full_name": "example/mcp-server",
            "name": "mcp-server",
            "html_url": "https://github.com/example/mcp-server",
            "clone_url": "https://github.com/example/mcp-server.git",
            "description": "An MCP server for AI assistants",
            "stargazers_count": 100,
            "topics": ["mcp-server", "ai"],
        }
        assert self._classify(adapter, repo) is True

    def test_repo_with_mcp_in_description_classified(self, adapter):
        repo = {
            "full_name": "example/server",
            "name": "server",
            "html_url": "https://github.com/example/server",
            "clone_url": "https://github.com/example/server.git",
            "description": "A Model Context Protocol server",
            "stargazers_count": 50,
            "topics": ["python"],
        }
        assert self._classify(adapter, repo) is True

    def test_repo_without_mcp_evidence_not_classified(self, adapter):
        repo = {
            "full_name": "example/random-lib",
            "name": "random-lib",
            "html_url": "https://github.com/example/random-lib",
            "clone_url": "https://github.com/example/random-lib.git",
            "description": "A random library",
            "stargazers_count": 10,
            "topics": ["python", "utility"],
        }
        assert self._classify(adapter, repo) is False

    def test_invalid_full_name_not_classified(self, adapter):
        repo = {
            "full_name": "not-a-repo",
            "name": "not-a-repo",
            "html_url": "https://github.com/not-a-repo",
            "topics": ["mcp-server"],
        }
        assert self._classify(adapter, repo) is False
