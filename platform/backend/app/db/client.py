from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


class ArcadeDBError(RuntimeError):
    """Raised when an ArcadeDB operation fails."""


class ArcadeDBClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def ready_url(self) -> str:
        host = self._settings.arcadedb_host
        port = self._settings.arcadedb_port
        return f"http://{host}:{port}/api/v1/ready"

    async def check_connection(self) -> bool:
        auth = (
            self._settings.arcadedb_user,
            self._settings.arcadedb_password,
        )

        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                self.ready_url,
                auth=auth,
            )

        return response.is_success

    def database_url(self, path: str = "") -> str:
        host = self._settings.arcadedb_host
        port = self._settings.arcadedb_port
        database = quote(
            self._settings.arcadedb_database,
            safe="",
        )

        return f"http://{host}:{port}/api/v1/{database}{path}"

    async def command(
        self,
        language: str,
        command: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a command against the configured ArcadeDB database.
        """

        payload: dict[str, Any] = {
            "language": language,
            "command": command,
        }

        if params:
            payload["params"] = params

        auth = (
            self._settings.arcadedb_user,
            self._settings.arcadedb_password,
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self.database_url("/command"),
                json=payload,
                auth=auth,
            )

        if not response.is_success:
            raise ArcadeDBError(
                f"ArcadeDB command failed "
                f"({response.status_code}): {response.text}"
            )

        return response.json()