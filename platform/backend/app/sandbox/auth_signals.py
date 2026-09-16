"""Shared detection of application-level auth failures in MCP tool results.

MCP lets a server return a protocol-successful (``isError=false``) result
whose payload is an application-level auth failure — e.g. Slack's
``{"ok": false, "error": "not_authed"}``, optionally wrapped in a text
content block. The HTTP transport never sees a 401, so this class of
failure must be detected from the payload itself. Both the local stdio
client and the remote HTTP client use :func:`extract_inband_auth_error`
so the UI can offer the credential form instead of rendering the error
as a "Success" result.
"""

from __future__ import annotations

import json
from typing import Any

# Exact application-level auth error codes servers return inside a
# result payload's `error` field.
AUTH_ERROR_CODES = frozenset((
    "not_authed",
    "not_authenticated",
    "unauthenticated",
    "unauthorized",
    "authentication_required",
    "auth_required",
    "invalid_auth",
    "invalid_token",
    "token_expired",
    "account_inactive",
    "missing_credentials",
    "no_credentials",
    "missing_api_key",
    "missing_token",
))

# Strong textual prefixes for free-text auth failures. Deliberately
# prefix-based (not substring) so a legitimate result that merely mentions
# "api keys" mid-sentence is never misclassified.
AUTH_TEXT_PREFIXES = (
    "unauthorized",
    "authentication",
    "not authenticated",
    "invalid token",
    "invalid api key",
    "missing token",
    "missing api key",
    "api key required",
    "token required",
    "credentials required",
    "auth required",
    "not_authed",
    "401",
    "403",
)


def _auth_error_string(value: str) -> bool:
    """True when `value` reads as an auth failure, not ordinary content."""
    lowered = value.strip().lower()
    if not lowered or len(lowered) > 300:
        return False
    return lowered in AUTH_ERROR_CODES or any(
        lowered.startswith(prefix) for prefix in AUTH_TEXT_PREFIXES
    )


def extract_inband_auth_error(payload: Any, _depth: int = 0) -> str | None:
    """Find an application-level auth failure inside a tool result payload.

    Recurses (bounded) through dicts/lists and JSON-in-text blocks — many
    servers wrap their JSON result in a text content block
    (``content[0].text = '{"ok":false,"error":"not_authed"}'``). Returns
    the auth error text, or None when the payload looks like a genuine
    successful result.
    """
    if _depth > 6 or payload is None:
        return None
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith(("{", "[")):
            try:
                return extract_inband_auth_error(json.loads(text), _depth + 1)
            except (ValueError, TypeError):
                pass
        if _auth_error_string(text):
            return text[:300]
        return None
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and _auth_error_string(value):
                return value[:300]
        for value in payload.values():
            found = extract_inband_auth_error(value, _depth + 1)
            if found:
                return found
        return None
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = extract_inband_auth_error(item, _depth + 1)
            if found:
                return found
    return None
