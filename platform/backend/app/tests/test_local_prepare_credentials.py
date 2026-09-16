"""Tests for /test/local/prepare credential extraction from README evidence.

Registry packages that declare no environment_variables metadata (like the
mcparmory servers) still document credentials in their README's mcpServers
JSON config block. The prepare endpoint must surface those names so the
credential form matches what the server actually expects (e.g.
SLACK_BOT_TOKEN, not a generic API_KEY).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.models import (
    ArtifactMetadata,
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemType,
    Reliability,
    SourceType,
)
from app.sandbox.routes import (
    _collect_credential_env_names,
    _env_name_is_secret,
    _extract_env_names_from_artifacts,
)


def _item_with_source(readme: str) -> Item:
    now = datetime.now(timezone.utc)
    source = DiscoverySource(type=SourceType.GITHUB, id="x/y", url="https://github.com/x/y")
    return Item(
        item_id=uuid4(),
        type=ItemType.TOOL,
        name="slack-mcp",
        description="Slack MCP server",
        source=source,
        provenance=[source],
        discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
        reliability=Reliability(score=0.5, confidence=0.5),
        artifacts=ArtifactMetadata(
            source_available=True,
            source_url="https://github.com/x/y",
            source_code=readme,
        ),
    )


_MCPSERVERS_README = """# Slack MCP Server

Configure your client:

```json
{
  "mcpServers": {
    "slack": {
      "command": "uvx",
      "args": ["mcparmory-slack"],
      "env": {
        "SLACK_BOT_TOKEN": "xoxb-your-token-here"
      }
    }
  }
}
```
"""


class TestEnvNameIsSecret:
    def test_token_names_are_secret(self):
        assert _env_name_is_secret("BEARER_TOKEN")
        assert _env_name_is_secret("SLACK_BOT_TOKEN")
        assert _env_name_is_secret("OPENAI_API_KEY")

    def test_plain_names_are_not_secret(self):
        assert not _env_name_is_secret("PORT")
        assert not _env_name_is_secret("LOG_LEVEL")


class TestExtractEnvNamesFromArtifacts:
    def test_extracts_from_mcpServers_env_block(self):
        item = _item_with_source(_MCPSERVERS_README)
        assert _extract_env_names_from_artifacts(item) == ["SLACK_BOT_TOKEN"]

    def test_extracts_from_bare_config_block(self):
        readme = """```json
{
  "command": "uvx",
  "args": ["mcparmory-github"],
  "env": {"BEARER_TOKEN": "ghp_x"}
}
```"""
        item = _item_with_source(readme)
        assert _extract_env_names_from_artifacts(item) == ["BEARER_TOKEN"]

    def test_deduplicates_names(self):
        readme = """```json
{"mcpServers": {"a": {"env": {"TOKEN_A": "1"}}}}
```
```json
{"mcpServers": {"b": {"env": {"TOKEN_A": "1", "TOKEN_B": "2"}}}}
```"""
        item = _item_with_source(readme)
        assert _extract_env_names_from_artifacts(item) == ["TOKEN_A", "TOKEN_B"]

    def test_no_readme_means_no_names(self):
        item = _item_with_source("Just prose, no config blocks.")
        assert _extract_env_names_from_artifacts(item) == []

    def test_missing_artifacts_is_safe(self):
        now = datetime.now(timezone.utc)
        source = DiscoverySource(type=SourceType.GITHUB, id="x/y", url=None)
        item = Item(
            item_id=uuid4(),
            type=ItemType.TOOL,
            name="x",
            description="",
            source=source,
            provenance=[source],
            discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
            reliability=Reliability(score=0.5, confidence=0.5),
        )
        assert _extract_env_names_from_artifacts(item) == []

    def test_invalid_json_blocks_ignored(self):
        readme = "```json\n{not valid json\n```"
        item = _item_with_source(readme)
        assert _extract_env_names_from_artifacts(item) == []

    def test_shell_example_fallback(self):
        """READMEs often document credentials in a shell example, not JSON."""
        readme = """# Setup

Run the server with your token:

```bash
SLACK_BOT_TOKEN=xoxb-your-token uvx mcparmory-slack
```
"""
        item = _item_with_source(readme)
        assert _extract_env_names_from_artifacts(item) == ["SLACK_BOT_TOKEN"]

    def test_generic_placeholder_names_not_suggested(self):
        """The prose fallback must not suggest the generic API_KEY placeholder."""
        readme = "Set API_KEY to your key from the dashboard."
        item = _item_with_source(readme)
        assert _extract_env_names_from_artifacts(item) == []

    def test_json_block_names_win_over_prose(self):
        readme = """Export SLACK_TEAM_ID first.

```json
{"mcpServers": {"s": {"env": {"SLACK_BOT_TOKEN": "xoxb-"}}}}
```"""
        item = _item_with_source(readme)
        assert _extract_env_names_from_artifacts(item) == ["SLACK_BOT_TOKEN"]


class TestCollectCredentialEnvNames:
    """The async path falls back to the Source-preview resolution chain."""

    def test_inline_source_code_takes_priority(self):
        import asyncio

        item = _item_with_source(_MCPSERVERS_README)

        async def fail_resolve(_item):
            raise AssertionError("resolve_source must not be called when inline source exists")

        assert asyncio.run(_collect_credential_env_names(item)) == ["SLACK_BOT_TOKEN"]

    def test_falls_back_to_resolve_source(self, monkeypatch):
        import asyncio

        from app.artifacts.source_resolver import SourcePreviewResult

        item = _item_with_source("")  # no inline source
        readme = '```json\n{"mcpServers": {"s": {"env": {"GITHUB_TOKEN": "x"}}}}\n```'

        async def fake_resolve(_item):
            return SourcePreviewResult(available=True, language="markdown", content=readme), None

        monkeypatch.setattr("app.sandbox.routes.resolve_source", fake_resolve)
        assert asyncio.run(_collect_credential_env_names(item)) == ["GITHUB_TOKEN"]

    def test_resolve_source_failure_is_swallowed(self, monkeypatch):
        import asyncio

        item = _item_with_source("")

        async def boom(_item):
            raise RuntimeError("github down")

        monkeypatch.setattr("app.sandbox.routes.resolve_source", boom)
        assert asyncio.run(_collect_credential_env_names(item)) == []
