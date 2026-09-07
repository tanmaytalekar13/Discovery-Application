"""Local STDIO MCP client for testing npm/pip MCP servers in Docker containers.

This module provides a client for connecting to local MCP tools that
run as STDIO servers inside Docker containers. It handles container
execution, environment variable injection, and JSON-RPC communication.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
            # Build docker exec command
            docker_cmd = self._build_docker_exec_command(command, env_vars)

            logger.info("Starting docker exec: %s", " ".join(docker_cmd))

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
            returncode = self._process.returncode if self._process else None
            user_msg, auth_reason, required_env_vars = self._analyze_error(stderr_text, returncode)
            await self._cleanup()
            return LocalConnectResult(
                connected=False,
                error=str(exc),
                user_message=user_msg or f"Failed to start MCP server: {exc}",
                auth_reason=auth_reason,
                required_env_vars=required_env_vars,
            )

    def _build_docker_exec_command(
        self,
        command: list[str],
        env_vars: dict[str, str],
    ) -> list[str]:
        """Build docker exec command with environment variables."""
        docker_cmd = ["docker", "exec", "-i", self._container_id]

        # Add environment variables
        for key, value in env_vars.items():
            docker_cmd.extend(["-e", f"{key}={value}"])

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
                return LocalInvokeResult(
                    status="error",
                    error=result["error"],
                    duration_ms=duration_ms,
                    user_message=f"Tool invocation failed: {result['error']}",
                )

            return LocalInvokeResult(
                status="success",
                result=result.get("result"),
                duration_ms=duration_ms,
            )

        except asyncio.TimeoutError:
            return LocalInvokeResult(
                status="error",
                error="Tool invocation timed out",
                user_message="The tool took too long to respond.",
            )
        except Exception as exc:
            return LocalInvokeResult(
                status="error",
                error=str(exc),
                user_message=f"Tool invocation failed: {exc}",
            )

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
                    # EOF reached - process exited
                    logger.info("Process stdout EOF reached")
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
        """Wait for the process to be ready for JSON-RPC communication.

        Returns True if ready, False if process exited.
        """
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < timeout:
            # Check if process is still running
            if self._process and self._process.returncode is not None:
                return False
            # Check if we got any stdout (process is talking)
            if self._buffer.strip():
                self._startup_done = True
                return True
            await asyncio.sleep(0.5)

        # Timeout - check if process is still running
        if self._process and self._process.returncode is not None:
            return False
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
