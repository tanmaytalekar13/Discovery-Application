"""
Real, runnable local A2A agent used as a test fixture for Phase 03.

Per CODEX_EXECUTION_PLAN.md, Phase 03's Definition of Done requires
"real local A2A agent works" - this file is that agent. It is a
genuine A2A server that speaks the real protocol over HTTP(S) - a
real Agent Card served at the well-known location (A2A specification
Section 5.3) and a real JSON-RPC 2.0 `message/send` endpoint (Section
7.1) - it is not a mock of A2A responses. Tests spawn this file as a
subprocess (uvicorn) and talk to it through
`app.discovery.a2a.client.A2AClient`, exercising the actual Agent
Card fetch and `message/send` exchange.

Run directly for manual testing:
    python -m app.discovery.a2a.testing.sample_agent --port 9999
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

AGENT_NAME = "sample-a2a-agent"
AGENT_VERSION = "1.0.0"
PROTOCOL_VERSION = "0.3.0"
ENDPOINT_PATH = "/a2a"

app = FastAPI(title=AGENT_NAME)


def _agent_card(base_url: str) -> dict[str, Any]:
    endpoint_url = f"{base_url}{ENDPOINT_PATH}"
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": AGENT_NAME,
        "description": "Sample local A2A agent used for Phase 03 protocol-core tests.",
        "url": endpoint_url,
        "preferredTransport": "JSONRPC",
        "version": AGENT_VERSION,
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "echo",
                "name": "Echo",
                "description": "Echoes back the text it receives, prefixed with 'Echo: '.",
                "tags": ["echo", "test"],
                "examples": ["hello"],
            },
            {
                "id": "reverse_text",
                "name": "Reverse Text",
                "description": "Reverses text sent with a 'reverse:' prefix.",
                "tags": ["text", "test"],
                "examples": ["reverse:hello"],
            },
        ],
        "supportsAuthenticatedExtendedCard": False,
    }


@app.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> JSONResponse:
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(_agent_card(base_url))


def _extract_text(message: dict[str, Any]) -> str:
    parts = message.get("parts") or []
    texts = [part.get("text", "") for part in parts if part.get("kind") == "text"]
    return " ".join(text for text in texts if text)


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@app.post(ENDPOINT_PATH)
async def a2a_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    request_id = body.get("id")
    method = body.get("method")

    if method != "message/send":
        return JSONResponse(
            _jsonrpc_error(request_id, -32601, f"Method '{method}' not found.")
        )

    params = body.get("params") or {}
    message = params.get("message") or {}
    text = _extract_text(message).strip()

    if not text:
        return JSONResponse(
            _jsonrpc_error(request_id, -32602, "Message must contain non-empty text.")
        )

    if text.lower().startswith("reverse:"):
        reply_text = text[len("reverse:") :][::-1]
    else:
        reply_text = f"Echo: {text}"

    reply_message = {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": reply_text}],
    }

    return JSONResponse(_jsonrpc_result(request_id, reply_message))


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
