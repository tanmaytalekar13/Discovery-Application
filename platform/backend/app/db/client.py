from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


class ArcadeDBError(RuntimeError):
    """Raised when an ArcadeDB operation fails."""


class ArcadeDBClient:
    """Asynchronous client for interacting with ArcadeDB."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._auth = (
            self._settings.arcadedb_user,
            self._settings.arcadedb_password,
        )

    @property
    def base_url(self) -> str:
        return f"http://{self._settings.arcadedb_host}:{self._settings.arcadedb_port}"

    @property
    def ready_url(self) -> str:
        return f"{self.base_url}/api/v1/ready"

    @property
    def server_url(self) -> str:
        return f"{self.base_url}/api/v1/server"

    @property
    def command_url(self) -> str:
        database = quote(self._settings.arcadedb_database, safe="")
        return f"{self.base_url}/api/v1/command/{database}"

    async def _request(
        self, 
        method: str, 
        url: str, 
        timeout: float = 10.0, 
        **kwargs: Any
    ) -> httpx.Response:
        """Centralized request handler for HTTP calls."""
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, auth=self._auth, **kwargs)

    async def check_connection(self) -> bool:
        """Check if the ArcadeDB server is ready to accept requests."""
        try:
            response = await self._request("GET", self.ready_url, timeout=3.0)
            return response.is_success
        except httpx.RequestError:
            return False

    async def server_databases(self) -> list[str]:
        """Return the databases currently available on the ArcadeDB server."""
        response = await self._request("GET", f"{self.base_url}/api/v1/databases")

        if not response.is_success:
            raise ArcadeDBError(
                f"Failed to list ArcadeDB databases ({response.status_code}): {response.text}"
            )

        payload = response.json()
        result = payload.get("result", [])

        if not isinstance(result, list):
            raise ArcadeDBError("Unexpected ArcadeDB database response format.")

        return [str(database) for database in result]

    async def create_database(self, database: str) -> None:
        """Create a new ArcadeDB database."""
        payload = {"command": f"create database {database}"}
        response = await self._request("POST", self.server_url, json=payload)

        if not response.is_success:
            raise ArcadeDBError(
                f"Failed to create ArcadeDB database ({response.status_code}): {response.text}"
            )

    async def command(
        self,
        language: str,
        command: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a command against the configured ArcadeDB database."""
        payload: dict[str, Any] = {
            "language": language,
            "command": command,
        }

        if params is not None:
            payload["params"] = params

        response = await self._request("POST", self.command_url, json=payload)

        if not response.is_success:
            raise ArcadeDBError(
                f"ArcadeDB command failed ({response.status_code}): {response.text}"
            )

        return response.json()