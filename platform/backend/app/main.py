
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.config import get_settings
from app.db.client import ArcadeDBClient
from app.sandbox.routes import router as sandbox_router
from app.verification.api import router as verification_router
from app.verification.queue import build_worker_handles, start_background_loops, stop_background_loops

settings = get_settings()
arcadedb = ArcadeDBClient(settings)
app = FastAPI(title="Agentic Discovery Platform", version="0.1.0")
app.include_router(api_router)
app.include_router(sandbox_router)
app.include_router(verification_router)

# Verification background workers (spec v2 Sections 5, 7, 9.1, 11).
# These loops are the ONLY writers of verification status/score; search
# endpoints only read from the DB. Started on startup, cancelled on
# shutdown; a DB outage delays verification but never breaks the API.
_verification_tasks: list = []


@app.on_event("startup")
async def start_verification_workers() -> None:
    global _verification_tasks
    try:
        handles = build_worker_handles(get_settings(), ArcadeDBClient(get_settings()))
    except Exception:  # noqa: BLE001 - misconfigured workers must not block startup
        import logging

        logging.getLogger(__name__).exception(
            "Verification workers could not be built; continuing without them"
        )
        return
    _verification_tasks = start_background_loops(handles)


@app.on_event("shutdown")
async def stop_verification_workers() -> None:
    global _verification_tasks
    if _verification_tasks:
        await stop_background_loops(_verification_tasks)
        _verification_tasks = []


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
