from fastapi.testclient import TestClient

from app.main import app


class ReadyClient:
    async def check_connection(self) -> bool:
        return True


def test_health_reports_arcadedb_connection(monkeypatch):
    monkeypatch.setattr("app.main.arcadedb", ReadyClient())

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "arcadedb": "connected"}


def test_health_reports_disconnected_arcadedb(monkeypatch):
    class UnreadyClient:
        async def check_connection(self) -> bool:
            return False

    monkeypatch.setattr("app.main.arcadedb", UnreadyClient())

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "arcadedb": "disconnected"}
