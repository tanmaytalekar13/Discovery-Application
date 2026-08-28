from urllib.parse import quote

import httpx

from app.config import Settings


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
            response = await client.get(self.ready_url, auth=auth)
        return response.is_success

    def database_url(self, path: str = "") -> str:
        host = self._settings.arcadedb_host
        port = self._settings.arcadedb_port
        database = quote(self._settings.arcadedb_database, safe="")
        return f"http://{host}:{port}/api/v1/{database}{path}"
