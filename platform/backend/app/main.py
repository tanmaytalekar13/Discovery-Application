from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.client import ArcadeDBClient

settings = get_settings()
arcadedb = ArcadeDBClient(settings)
app = FastAPI(title="Agentic Discovery Platform", version="0.1.0")


@app.get("/health")
async def health() -> JSONResponse:
    try:
        connected = await arcadedb.check_connection()
    except Exception:
        connected = False

    if connected:
        return JSONResponse({"status": "ok", "arcadedb": "connected"})
    return JSONResponse(
        {"status": "error", "arcadedb": "disconnected"}, status_code=503
    )
