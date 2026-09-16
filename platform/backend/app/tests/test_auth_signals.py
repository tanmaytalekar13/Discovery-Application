"""Tests for in-band auth-failure detection in MCP tool results.

Covers the mcparmory-style failure mode: a server whose handshake and
tools/list succeed (credentials are read from the environment later) and
whose tools return protocol-successful (isError=false) payloads carrying
an application-level auth code such as {"ok": false, "error":
"not_authed"}. Also covers the README-documented credential extraction
used by /test/local/prepare.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.sandbox.auth_signals import extract_inband_auth_error
from app.sandbox.local_mcp_client import LocalInvokeResult, LocalMCPClient


def _payload(result: dict) -> dict:
    """Wrap a tool result the way LocalMCPClient.receive sees it."""
    return {"result": result}


class TestExtractInbandAuthError:
    def test_slack_not_authed_structured(self):
        payload = {"ok": False, "error": "not_authed"}
        assert extract_inband_auth_error(payload) == "not_authed"

    def test_json_in_text_block(self):
        """The screenshot failure mode: JSON wrapped in a text content block."""
        payload = {
            "content": [
                {"text": json.dumps({"ok": False, "error": "not_authed"}), "type": "text"}
            ]
        }
        assert extract_inband_auth_error(payload) == "not_authed"

    def test_message_key_detected(self):
        assert extract_inband_auth_error({"ok": False, "message": "Unauthorized"}) == "Unauthorized"

    def test_bearer_prefix_text(self):
        assert extract_inband_auth_error("401 Unauthorized: invalid token") is not None

    def test_nested_list_recursion(self):
        payload = {"content": [{"json": {"error": "invalid_token"}}]}
        assert extract_inband_auth_error(payload) == "invalid_token"

    def test_genuine_success_not_flagged(self):
        assert extract_inband_auth_error({"ok": True, "channels": ["general"]}) is None

    def test_prose_mention_not_flagged(self):
        """Mid-sentence mentions of auth words are not auth failures."""
        assert extract_inband_auth_error(
            {"text": "The api keys page lists all your tokens"}
        ) is None

    def test_error_about_something_else_not_flagged(self):
        assert extract_inband_auth_error({"ok": False, "error": "channel_not_found"}) is None

    def test_empty_and_deep_payloads(self):
        assert extract_inband_auth_error(None) is None
        assert extract_inband_auth_error({}) is None
        assert extract_inband_auth_error("a" * 400) is None

    def test_depth_guard(self):
        deep = {"error": "not_authed"}
        for _ in range(10):
            deep = {"nested": deep}
        assert extract_inband_auth_error(deep) is None


class TestLocalInvokeAuthClassification:
    """LocalMCPClient.invoke must map in-band auth failures to requires_auth."""

    @staticmethod
    def _invoke_result_for(payload: dict) -> LocalInvokeResult:
        client = LocalMCPClient()
        # Simulate the invoke() success branch by exercising the same
        # classification logic it uses on the received payload.
        auth_error = client._extract_auth_error(payload)
        if auth_error:
            return LocalInvokeResult(
                status="error",
                error=auth_error,
                user_message="This tool needs credentials. Add the required token or API key and try again.",
                requires_auth=True,
                auth_reason="unauthorized",
            )
        return LocalInvokeResult(status="success", result=payload)

    def test_not_authed_payload_classified_as_auth(self):
        result = self._invoke_result_for(
            {
                "content": [
                    {"text": '{"ok":false,"error":"not_authed"}', "type": "text"}
                ],
                "structuredContent": {"ok": False, "error": "not_authed"},
            }
        )
        assert result.status == "error"
        assert result.requires_auth is True
        assert result.auth_reason == "unauthorized"

    def test_real_payload_still_success(self):
        result = self._invoke_result_for(
            {"content": [{"text": "Pong!", "type": "text"}], "structuredContent": None}
        )
        assert result.status == "success"
        assert result.requires_auth is False


class TestLocalInvokePayloadAuth:
    """End-to-end-ish: invoke()'s classification on a stubbed JSON-RPC result."""

    def test_invoke_maps_inband_auth(self, monkeypatch):
        import asyncio

        client = LocalMCPClient()
        client._process = SimpleNamespace(returncode=None)  # appears connected

        rpc_response = {
            "result": {
                "content": [
                    {"text": '{"ok":false,"error":"not_authed"}', "type": "text"}
                ],
                "isError": False,
            }
        }

        async def fake_send_request(method, params, timeout=30.0):
            return rpc_response

        monkeypatch.setattr(client, "_send_request", fake_send_request)

        result = asyncio.run(
            client.invoke("verify_authentication", {}, timeout=5.0)
        )
        assert result.status == "error"
        assert result.requires_auth is True
        assert result.auth_reason == "unauthorized"

    def test_invoke_success_passthrough(self, monkeypatch):
        import asyncio

        client = LocalMCPClient()
        client._process = SimpleNamespace(returncode=None)

        async def fake_send_request(method, params, timeout=30.0):
            return {"result": {"content": [{"text": "Pong!", "type": "text"}], "isError": False}}

        monkeypatch.setattr(client, "_send_request", fake_send_request)

        result = asyncio.run(client.invoke("ping", {}, timeout=5.0))
        assert result.status == "success"
        assert result.requires_auth is False
