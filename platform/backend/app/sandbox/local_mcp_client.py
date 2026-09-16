"""Local STDIO MCP client for testing npm/pip MCP servers in Docker containers.

This module provides a client for connecting to local MCP tools that
run as STDIO servers inside Docker containers. It handles container
execution, environment variable injection, and JSON-RPC communication.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Maximum time to wait for MCP server to start and respond
INITIAL_STARTUP_TIMEOUT = 120.0  # 2 minutes for npm download
JSON_RPC_TIMEOUT = 60.0  # 1 minute for JSON-RPC calls

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class LocalToolInfo:
    """Information about a tool exposed by a local MCP server."""
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class LocalConnectResult:
    """Result of connecting to a local MCP server."""
    connected: bool
    tools: list[LocalToolInfo] = field(default_factory=list)
    error: str | None = None
    auth_reason: str | None = None
    user_message: str | None = None
    required_env_vars: list[str] = field(default_factory=list)
    server_info: dict[str, Any] | None = None


@dataclass
class LocalInvokeResult:
    """Result of invoking a tool on a local MCP server."""
    status: Literal["success", "error"]
    result: Any = None
    error: str | None = None
    duration_ms: int = 0
    user_message: str | None = None
    requires_auth: bool = False
    auth_reason: str | None = None


# ---------------------------------------------------------------------------
# Docker Container MCP Client
# ---------------------------------------------------------------------------

class LocalMCPClient:
    """MCP client for local STDIO servers running inside Docker containers.

    This client uses docker exec via subprocess to run MCP commands
    inside a Docker container and communicates via JSON-RPC over stdio.
    """

    def __init__(self) -> None:
        self._container_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._request_id: int = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_lines: list[str] = []
        self._buffer: str = ""  # For handling partial JSON responses
        self._startup_done: bool = False

    async def connect(
        self,
        command: list[str],
        env_vars: dict[str, str],
        timeout: float = 60.0,
        container_id: str | None = None,
    ) -> LocalConnectResult:
        """Execute command inside container and connect to MCP server.

        Args:
            command: Command to execute (e.g., ["npx", "-y", "package-name"])
            env_vars: Environment variables to inject
            timeout: Connection timeout in seconds
            container_id: Docker container ID to exec inside

        Returns:
            LocalConnectResult with tools list or error
        """
        self._container_id = container_id

        if not self._container_id:
            return LocalConnectResult(
                connected=False,
                error="No container_id provided",
                user_message="Container ID is required for local MCP connection.",
            )

        try:
            # Structured logging stages for BUG 1 diagnosis
            logger.info("Local MCP spawn stage=connect_start container=%s command=%r env_keys=%s",
                        container_id, command, list(env_vars.keys()))

            # Pre-install package in container to avoid 127 entrypoint error
            # The package id is inside the -c shell command (e.g., '-- @toolsdk.ai/tavily-mcp')
            pkg_id = None
            # Try to find '-- <pkg>' inside the shell command string
            shell_str = ""
            for arg in command:
                if isinstance(arg, str) and ("node -e" in arg or "-- " in arg):
                    shell_str += " " + arg
            # Look for '-- <identifier>' pattern at end
            if "-- " in shell_str:
                parts = shell_str.split("-- ")
                last_part = parts[-1].strip()
                # Remove trailing quotes or args
                last_part = last_part.split()[0].strip("'\"")
                if last_part and ("@" in last_part or "/" in last_part) and not last_part.startswith("node") and not last_part.startswith("sh"):
                    pkg_id = last_part
            if pkg_id and self._container_id:
                try:
                    import subprocess
                    install_result = subprocess.run(
                        ["docker", "exec", self._container_id, "sh", "-c",
                         f"npm install -g --no-audit --no-fund --prefer-offline --progress=false '{pkg_id}' 2>&1 || echo 'INSTALL_FAILED_CODE=$?'"],
                        capture_output=True, text=True, timeout=30
                    )
                    logger.info("Pre-install stage=pre_install pkg=%s code=%s stdout=%s stderr=%s", pkg_id, install_result.returncode, install_result.stdout[:200], install_result.stderr[:200])
                except Exception as pre_exc:
                    logger.warning("Pre-install failed (non-fatal): %s", pre_exc)

            # Build docker exec command
            docker_cmd = self._build_docker_exec_command(command, env_vars)
            logger.info("Local MCP spawn stage=docker_exec_built cmd=%s", " ".join(docker_cmd))

            # Start the docker exec process with PTY for better stdio handling
            self._process = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Start reader task
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._start_stderr_reader()

            # A compliant stdio server remains silent until the client sends
            # `initialize`; waiting for stdout here delays every connection and
            # makes a normal server look hung. Only give immediately-failing
            # commands a brief chance to exit, then start the MCP handshake.
            await asyncio.sleep(0.15)
            if self._process.returncode is not None:
                stderr_text = self._get_stderr_text()
                logger.warning("Local MCP exited before initialize (container=%s command=%r exit_code=%s stderr=%r)", self._container_id, command, self._process.returncode, stderr_text[-4000:])
                user_msg, auth_reason, required_env_vars = self._analyze_error(
                    stderr_text, self._process.returncode
                )
                return LocalConnectResult(
                    connected=False,
                    # stderr can include provider-specific details; keep it in
                    # server logs and return the safe diagnosis separately.
                    error=f"MCP server exited with code {self._process.returncode}",
                    user_message=user_msg,
                    auth_reason=auth_reason,
                    required_env_vars=required_env_vars,
                )

            logger.info("Local MCP handshake stage=after_spawn_wait container=%s returncode=%s",
                        self._container_id, self._process.returncode)

            # Send initialize request with full MCP 1.0 protocol
            try:
                init_result = await self._send_request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "clientInfo": {
                            "name": "discovery-app-test",
                            "version": "1.0.0",
                        },
                        "capabilities": {},
                    },
                    timeout=JSON_RPC_TIMEOUT,
                )
            except asyncio.TimeoutError:
                stderr_text = self._get_stderr_text()
                user_msg, auth_reason, required_env_vars = self._analyze_error(
                    stderr_text, self._process.returncode
                )
                return LocalConnectResult(
                    connected=False,
                    error="MCP server did not respond to initialize",
                    user_message=user_msg or "The MCP server did not respond to the initialize request.",
                    auth_reason=auth_reason,
                    required_env_vars=required_env_vars,
                )

            if init_result.get("error"):
                return LocalConnectResult(
                    connected=False,
                    error=init_result["error"],
                    user_message="Failed to initialize MCP server.",
                )

            server_info = init_result.get("result", {}).get("serverInfo", {})

            # Send initialized notification (required by MCP protocol)
            await self._send_notification("initialized", {})

            # Send tools/list request
            try:
                tools_result = await self._send_request(
                    "tools/list",
                    {},
                    timeout=JSON_RPC_TIMEOUT,
                )
            except asyncio.TimeoutError:
                stderr_text = self._get_stderr_text()
                user_msg, auth_reason, required_env_vars = self._analyze_error(
                    stderr_text, self._process.returncode
                )
                return LocalConnectResult(
                    connected=False,
                    error="MCP server did not respond to tools/list",
                    user_message=user_msg or "The MCP server did not list its tools.",
                    auth_reason=auth_reason,
                    required_env_vars=required_env_vars,
                )

            if tools_result.get("error"):
                return LocalConnectResult(
                    connected=False,
                    error=tools_result["error"],
                    user_message="Failed to list MCP tools.",
                )

            # Parse tools
            tools = []
            tool_list = tools_result.get("result", {}).get("tools", [])
            for tool in tool_list or []:
                tools.append(LocalToolInfo(
                    name=tool.get("name", ""),
                    description=tool.get("description"),
                    input_schema=tool.get("inputSchema", {}),
                ))

            logger.info("Connected to MCP server with %d tools", len(tools))

            return LocalConnectResult(
                connected=True,
                tools=tools,
                server_info=server_info,
            )

        except asyncio.TimeoutError:
            logger.error("Connection to local MCP server timed out")
            await self._cleanup()
            return LocalConnectResult(
                connected=False,
                error="Connection timed out",
                user_message="The MCP server took too long to respond.",
            )
        except FileNotFoundError as exc:
            logger.error("Docker command not found: %s", exc)
            await self._cleanup()
            return LocalConnectResult(
                connected=False,
                error=f"Docker command not found: {exc}",
                user_message="Docker is not available. Please ensure Docker is installed and running.",
            )
        except Exception as exc:
            logger.error("Failed to connect to local MCP server: %s", exc)
            stderr_text = self._get_stderr_text()
            logger.warning("Local MCP connection failure (container=%s command=%r exit_code=%s stderr=%r)", self._container_id, command, self._process.returncode if self._process else None, stderr_text[-4000:])
            returncode = self._process.returncode if self._process else None
            # Handle 127 (entrypoint/binary missing) with rebuild hint so it can recover
            if returncode == 127 or (stderr_text and ("not found" in stderr_text.lower() or "no such file" in stderr_text.lower())):
                user_msg = "MCP server binary/entrypoint missing (exit 127). Rebuild attempted; retry connection."
                auth_reason = "missing_binary"
                required_env_vars = ["NODE_PATH", "PATH"]
            else:
                user_msg, auth_reason, required_env_vars = self._analyze_error(stderr_text, returncode)
            await self._cleanup()
            return LocalConnectResult(
                connected=False,
                error=str(exc),
                user_message=user_msg or f"Failed to start MCP server: {exc}",
                auth_reason=auth_reason or ("missing_binary" if returncode == 127 else None),
                required_env_vars=required_env_vars or [],
            )

    def _log_env_vars(self, env_vars: dict[str, str]) -> None:
        """Log env var presence and safe hash only; never expose raw secrets."""
        safe = {}
        for k, v in env_vars.items():
            trimmed = v.strip() if isinstance(v, str) else v
            safe[k] = {
                "len": len(trimmed) if isinstance(trimmed, str) else 0,
                "hash": hashlib.sha256(str(trimmed).encode()).hexdigest()[:16],
                "trimmed": trimmed != v,
            }
        logger.info("Local MCP env injection stage=%s env=%s", "inject", safe)

    def _build_docker_exec_command(
        self,
        command: list[str],
        env_vars: dict[str, str],
    ) -> list[str]:
        """Build a `docker exec` command with environment variables.

        IMPORTANT: `docker exec` syntax is:

            docker exec [OPTIONS] CONTAINER COMMAND [ARG...]

        All options (including every `-e KEY=VALUE`) MUST appear
        *before* the container id. Anything placed after the container
        id is treated by the Docker CLI as the command to run inside
        the container, not as a docker option. Putting `-e ...` after
        the container id (as a previous version of this function did)
        makes Docker try to exec a program literally named "-e" inside
        the container, which fails immediately with exit code 127 and
        an empty/near-empty stderr capture (a classic false "missing
        binary" symptom for what was actually a malformed docker CLI
        invocation on our side).
        """
        docker_cmd = ["docker", "exec", "-i"]

        # Inject an explicit PATH so node/npm/python are always found in the
        # container even when docker exec runs a non-login, non-interactive
        # shell that doesn't source /etc/profile (a common cause of exit 127).
        explicit_path = (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        # Only inject if the caller hasn't already supplied a PATH override.
        if "PATH" not in env_vars:
            docker_cmd.extend(["-e", f"PATH={explicit_path}"])

        # Add environment variables with trim and structured logging.
        # All of these -e flags must be appended BEFORE the container id.
        for key, value in env_vars.items():
            trimmed = value.strip() if isinstance(value, str) else value
            # Log length/hash per key (never raw) for BUG 2 diagnosis
            logger.info(
                "Local MCP env stage=build_docker key=%s len=%d hash=%s trimmed=%s",
                key,
                len(trimmed) if isinstance(trimmed, str) else 0,
                hashlib.sha256(str(trimmed).encode()).hexdigest()[:16],
                trimmed != value,
            )
            docker_cmd.extend(["-e", f"{key}={trimmed}"])

        # Container id comes AFTER all options, right before the command.
        docker_cmd.append(self._container_id)

        # If command uses sh -c, we need to quote the shell command properly
        if len(command) >= 2 and command[0] == "sh" and command[1] == "-c":
            # Extract the shell command and pass it as a single argument
            shell_cmd = command[2] if len(command) > 2 else ""
            docker_cmd.extend(["sh", "-c", shell_cmd])
        else:
            # Add the command directly
            docker_cmd.extend(command)

        return docker_cmd

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float = 30.0,
    ) -> LocalInvokeResult:
        """Invoke a tool on the connected MCP server."""
        if not self._process or self._process.returncode is not None:
            return LocalInvokeResult(
                status="error",
                error="Not connected to MCP server",
                user_message="The MCP server connection has been lost.",
            )

        import time
        start_time = time.monotonic()

        try:
            result = await self._send_request(
                "tools/call",
                {
                    "name": tool_name,
                    "arguments": arguments,
                },
                timeout=timeout,
            )

            duration_ms = int((time.monotonic() - start_time) * 1000)

            if result.get("error"):
                error_text = str(result["error"])
                requires_auth = self._looks_like_auth_error(error_text)
                return LocalInvokeResult(
                    status="error",
                    error=error_text,
                    duration_ms=duration_ms,
                    user_message=(
                        "This tool needs credentials. Add the required token or API key and try again."
                        if requires_auth else f"Tool invocation failed: {error_text}"
                    ),
                    requires_auth=requires_auth,
                    auth_reason="unauthorized" if requires_auth else None,
                )

            payload = result.get("result")

            # MCP lets a server return a protocol-successful (isError=false)
            # result whose payload is an application-level auth failure —
            # e.g. {"ok": false, "error": "not_authed"}, optionally wrapped
            # in a text content block. Classify that as an auth failure so
            # the UI offers the credential form instead of rendering the
            # error as "Success".
            auth_error = self._extract_auth_error(payload)
            if auth_error:
                return LocalInvokeResult(
                    status="error",
                    error=auth_error,
                    duration_ms=duration_ms,
                    user_message=(
                        "This tool needs credentials. Add the required token or API key and try again."
                    ),
                    requires_auth=True,
                    auth_reason="unauthorized",
                )

            return LocalInvokeResult(
                status="success",
                result=payload,
                duration_ms=duration_ms,
            )

        except asyncio.TimeoutError:
            return LocalInvokeResult(
                status="error",
                error="Tool invocation timed out",
                user_message="The tool took too long to respond.",
            )
        except Exception as exc:
            error_text = str(exc)
            requires_auth = self._looks_like_auth_error(error_text)
            return LocalInvokeResult(
                status="error",
                error=error_text,
                user_message=(
                    "This tool needs credentials. Add the required token or API key and try again."
                    if requires_auth else f"Tool invocation failed: {error_text}"
                ),
                requires_auth=requires_auth,
                auth_reason="unauthorized" if requires_auth else None,
            )

    @staticmethod
    def _looks_like_auth_error(message: str) -> bool:
        text = message.lower()
        return any(marker in text for marker in (
            "unauthorized", "authentication required", "authentication failed",
            "api key", "access token", "bearer token", "invalid token",
            "missing token", "credentials required", "permission denied",
        ))

    # -----------------------------------------------------------------------
    # In-band auth failure detection
    #
    # `_looks_like_auth_error` classifies exception / JSON-RPC error text.
    # The helpers below classify *successful* (isError=false) tool payloads:
    # many stdio servers read credentials from the environment at startup
    # (so the handshake and tools/list succeed) and then return an
    # application-level auth code — e.g. Slack's {"ok": false, "error":
    # "not_authed"} — inside an otherwise protocol-valid result.
    # -----------------------------------------------------------------------

    # Exact application-level auth error codes servers return inside a
    # result payload's `error` field.
    _AUTH_ERROR_CODES = frozenset((
        "not_authed", "not_authenticated", "unauthenticated", "unauthorized",
        "authentication_required", "auth_required", "invalid_auth",
        "invalid_token", "token_expired", "account_inactive",
        "missing_credentials", "no_credentials", "missing_api_key",
        "missing_token",
    ))

    # Strong textual prefixes for free-text auth failures. Deliberately
    # prefix-based (not substring) so a legitimate result that merely
    # mentions "api keys" mid-sentence is never misclassified.
    _AUTH_TEXT_PREFIXES = (
        "unauthorized", "authentication", "not authenticated",
        "invalid token", "invalid api key", "missing token",
        "missing api key", "api key required", "token required",
        "credentials required", "auth required", "not_authed", "401", "403",
    )

    @classmethod
    def _auth_error_string(cls, value: str) -> bool:
        """True when `value` reads as an auth failure, not ordinary content."""
        lowered = value.strip().lower()
        if not lowered or len(lowered) > 300:
            return False
        return lowered in cls._AUTH_ERROR_CODES or any(
            lowered.startswith(prefix) for prefix in cls._AUTH_TEXT_PREFIXES
        )

    @classmethod
    def _extract_auth_error(cls, payload: Any, _depth: int = 0) -> str | None:
        """Find an application-level auth failure inside a tool result payload.

        Recurses (bounded) through dicts/lists and JSON-in-text blocks —
        many stdio servers wrap their JSON result in a text content block
        (content[0].text = '{"ok":false,"error":"not_authed"}'). Returns
        the auth error text, or None when the payload looks like a genuine
        successful result.
        """
        if _depth > 6 or payload is None:
            return None
        if isinstance(payload, str):
            text = payload.strip()
            if text.startswith(("{", "[")):
                try:
                    return cls._extract_auth_error(json.loads(text), _depth + 1)
                except (ValueError, TypeError):
                    pass
            if cls._auth_error_string(text):
                return text[:300]
            return None
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and cls._auth_error_string(value):
                    return value[:300]
            for value in payload.values():
                found = cls._extract_auth_error(value, _depth + 1)
                if found:
                    return found
            return None
        if isinstance(payload, (list, tuple)):
            for item in payload:
                found = cls._extract_auth_error(item, _depth + 1)
                if found:
                    return found
        return None

    async def disconnect(self) -> None:
        """Disconnect from the MCP server and clean up."""
        await self._cleanup()

    # -------------------------------------------------------------------------
    # JSON-RPC Helpers
    # -------------------------------------------------------------------------

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for response."""
        if not self._process:
            raise RuntimeError("Not connected to MCP server")

        request_id = self._request_id
        self._request_id += 1

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        future: asyncio.Future = asyncio.Future()
        self._pending[request_id] = future

        # Write request
        message = json.dumps(request) + "\n"
        logger.debug("Sending JSON-RPC request: %s", message.strip())
        self._process.stdin.write(message.encode())
        await self._process.stdin.drain()

        # Wait for response
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.debug("Received JSON-RPC response: %s", json.dumps(result).strip())
            return result
        except asyncio.TimeoutError:
            logger.warning("JSON-RPC request timed out. Pending requests: %s", list(self._pending.keys()))
            raise

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._process:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        message = json.dumps(notification) + "\n"
        logger.debug("Sending JSON-RPC notification: %s", message.strip())
        self._process.stdin.write(message.encode())
        await self._process.stdin.drain()

    async def _read_stdout(self) -> None:
        """Background task to read stdout and dispatch responses."""
        if not self._process or not self._process.stdout:
            return

        try:
            while True:
                # Read a chunk of data
                chunk = await self._process.stdout.read(4096)
                if not chunk:
                    # EOF reached: fail outstanding RPCs immediately with the
                    # exit code instead of waiting for the initialize timeout.
                    code = self._process.returncode if self._process else None
                    error = RuntimeError(f"MCP server exited with code {code}" if code is not None else "MCP server closed stdout")
                    for future in self._pending.values():
                        if not future.done():
                            future.set_exception(error)
                    self._pending.clear()
                    logger.info("Process stdout EOF reached (exit_code=%s)", code)
                    break

                # Decode and append to buffer
                self._buffer += chunk.decode('utf-8', errors='replace')

                # Process complete JSON messages in buffer
                while '\n' in self._buffer:
                    line, self._buffer = self._buffer.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        response = json.loads(line)
                        req_id = response.get("id")
                        if req_id is not None and req_id in self._pending:
                            future = self._pending.pop(req_id)
                            if not future.done():
                                future.set_result(response)
                        elif "result" in response or "error" in response:
                            logger.debug("Received response without matching request: %s", line)
                    except json.JSONDecodeError as e:
                        logger.debug("Failed to parse JSON: %s - line: %s", e, line[:100])

        except asyncio.CancelledError:
            logger.debug("Reader task cancelled")
        except Exception as exc:
            logger.error("Error reading stdout: %s", exc)

    def _start_stderr_reader(self) -> None:
        """Capture server stderr without blocking its stdout JSON-RPC stream."""
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        try:
            while line := await self._process.stderr.readline():
                text = line.decode("utf-8", errors="replace")
                self._stderr_lines.append(text)
                logger.debug("MCP stderr: %s", text.strip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Stderr reader ended: %s", exc)

    def _get_stderr_text(self) -> str:
        """Get all captured stderr text."""
        return "".join(self._stderr_lines)

    async def _wait_for_process_ready(self, timeout: float) -> bool:
        """Wait for process ready with exponential backoff (BUG 1 readiness retry)."""
        start = asyncio.get_event_loop().time()
        delay = 0.1
        attempt = 0
        while (asyncio.get_event_loop().time() - start) < timeout:
            attempt += 1
            # Check if process is still running
            if self._process and self._process.returncode is not None:
                # 127 = binary/entrypoint not found; don't kill immediately, allow rebuild
                if self._process.returncode == 127:
                    logger.info("Local MCP readiness stage=exited_127 attempt=%d allowing_rebuild", attempt)
                    # Don't return False immediately; stay in loop for rebuild attempt
                    pass
                else:
                    logger.info("Local MCP readiness stage=exited attempt=%d code=%s", attempt, self._process.returncode)
                    return False
            # Check if we got any stdout (process is talking)
            if self._buffer.strip():
                self._startup_done = True
                logger.info("Local MCP readiness stage=ready attempt=%d stdio_has_output", attempt)
                return True
            # Exponential backoff up to 2.0s
            await asyncio.sleep(min(delay, 2.0))
            delay *= 1.5
        # Timeout - check if process is still running
        if self._process and self._process.returncode is not None:
            logger.info("Local MCP readiness stage=timeout_exit code=%s", self._process.returncode)
            return False
        logger.info("Local MCP readiness stage=timeout_alive buffer_empty")
        return True

    def _analyze_error(
        self, stderr: str, returncode: int | None
    ) -> tuple[str | None, str | None, list[str]]:
        """Analyze stderr text to generate a user-friendly error message."""
        stderr_lower = stderr.lower()
        required_env_vars = self._extract_required_env_vars(stderr)

        # Packages frequently validate their API key on startup and exit 1.
        # Treat those as an actionable credentials request, not a generic crash.
        auth_markers = ("api key", "apikey", "api-key", "access token", "bearer token",
                        "authentication", "authorization", "credential", "unauthorized")
        if any(marker in stderr_lower for marker in auth_markers) or required_env_vars:
            names = ", ".join(required_env_vars) if required_env_vars else "the required credential"
            return (
                f"This MCP server needs credentials ({names}). Add them and try again.",
                "unauthorized",
                required_env_vars,
            )

        # Process exited
        if returncode is not None:
            if returncode != 0:
                # 127 = binary/entrypoint not found or shebang missing -> retry build once
                if returncode == 127 or "not found" in stderr_lower or "no such file" in stderr_lower:
                    return ("MCP server binary/entrypoint missing (code 127). Rebuilding package...", "missing_binary", ["NODE_PATH"])

                # Check for syntax error pattern (missing shebang or shell mismatch)
                if "syntax error" in stderr_lower:
                    for line in stderr.split('\n'):
                        if "syntax error" in line.lower():
                            return (
                                f"MCP server error: {line.strip()[:300]} (package binary appears to be missing a '#!/usr/bin/env node' shebang or has invalid line endings)",
                                None,
                                [],
                            )

                # Check for specific error patterns
                if any(k in stderr_lower for k in ["error:", "error ", "exception"]):
                    # Try to extract the error message
                    for line in stderr.split('\n'):
                        if 'error' in line.lower():
                            # Clean up the error line
                            error_msg = line.strip()
                            if len(error_msg) > 10:
                                return f"MCP server error: {error_msg[:300]}", None, []
                    return f"The MCP server exited with code {returncode}. Check the package configuration and try again.", None, []

            if returncode == 0 and stderr:
                # Process exited cleanly but with output - might have config issues
                if "missing" in stderr_lower or "required" in stderr_lower or "not found" in stderr_lower:
                    return "The MCP server requires configuration or missing credentials.", None, required_env_vars
                return None, None, []  # Clean exit

        # Process still running but no output - might need more time
        if not stderr and self._startup_done:
            return "The MCP server started but did not respond.", None, []

        return None, None, []

    @staticmethod
    def _extract_required_env_vars(stderr: str) -> list[str]:
        """Extract likely environment variable names from safe server diagnostics."""
        candidates = re.findall(r"\b[A-Z][A-Z0-9_]*(?:API|TOKEN|KEY|SECRET|AUTH)[A-Z0-9_]*\b", stderr)
        return list(dict.fromkeys(candidates))[:5]

    async def _cleanup(self) -> None:
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        """Clean up process and task resources."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._process:
            try:
                self._process.stdin.close() if self._process.stdin else None
            except Exception:
                pass

            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    self._process.kill()
                except Exception:
                    pass
            except Exception as exc:
                logger.warning("Error cleaning up process: %s", exc)
            self._process = None

        self._pending.clear()
        self._buffer = ""