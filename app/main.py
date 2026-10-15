"""FastAPI app — exposes :class:`ConsoleCore` over HTTP.

Three data channels:

* ``GET /api/state``  — one full snapshot (first paint and fallback).
* ``GET /api/events`` — SSE stream of snapshots. SSE rather than WebSocket because
  browsers have ``EventSource`` built in and the server side is a plain
  ``StreamingResponse`` — which means no ``websockets`` dependency (this tool only
  pulls in fastapi + uvicorn).
* ``GET /api/video``  — MJPEG (``multipart/x-mixed-replace``) straight into an
  ``<img src>``. Far smoother than base64 frames over a socket, and nearly free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .worker import ConsoleCore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: SSE push rate. 20 Hz is plenty for debugging.
SSE_HZ = 20.0

core = ConsoleCore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the worker on boot; guarantee torque release and port cleanup on shutdown."""
    core.start()
    logger.info("SO-101 debug console v%s up", __version__)
    try:
        yield
    finally:
        logger.info("shutting down, releasing devices…")
        core.shutdown()


app = FastAPI(title="SO-101 Debug Console", version=__version__, lifespan=lifespan)


#: Assets whose content feeds the cache-busting fingerprint.
_VERSIONED_ASSETS = ("app.js", "style.css")


def asset_version() -> str:
    """Short fingerprint of the frontend assets.

    Static files are served from disk on every request, but the browser still
    caches them. Editing the JS while a tab stays open then shows a stale UI and
    looks like a bug that was already fixed. Stamping a content hash onto the
    script/style URLs makes a reload pick up changes automatically.
    """
    digest = hashlib.sha1()
    for name in _VERSIONED_ASSETS:
        path = STATIC_DIR / name
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _page(name: str) -> HTMLResponse:
    """Serve a templated HTML page with asset URLs fingerprinted."""
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    return HTMLResponse(html.replace("{{v}}", asset_version()))


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the console."""
    return _page("index.html")


@app.get("/operator", response_class=HTMLResponse)
async def operator() -> HTMLResponse:
    """Serve the stripped-down operator view.

    Meant to be popped out into its own window and dragged to a second monitor
    while teleoperating: large camera feeds, a compact joint readout, nothing else.
    The same state stream feeds it, so it stays in step with the console.
    """
    return _page("operator.html")


@app.get("/api/state")
async def get_state() -> JSONResponse:
    """Return one full state snapshot."""
    return JSONResponse(core.snapshot())


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    """SSE: push snapshots, skipping any whose ``seq`` has not changed."""

    async def gen():
        last_seq = -1
        period = 1.0 / SSE_HZ
        while True:
            if await request.is_disconnected():
                break
            snap = core.snapshot()
            if snap["seq"] != last_seq:
                last_seq = snap["seq"]
                yield f"data: {json.dumps(snap, ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(period)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/command")
async def command(payload: dict) -> JSONResponse:
    """Queue a command and return at once — the result arrives over SSE."""
    action = payload.get("action")
    if not action:
        raise HTTPException(status_code=400, detail="missing 'action'")
    args = {k: v for k, v in payload.items() if k != "action"}
    result = core.submit(action, **args)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "command rejected"))
    return JSONResponse(result)


@app.post("/api/estop")
async def estop() -> JSONResponse:
    """Emergency stop. Sets the flag directly — never queued behind a bus call."""
    core.estop()
    return JSONResponse({"ok": True})


@app.get("/api/video/{name}")
async def video(name: str, fps: float = 15.0) -> StreamingResponse:
    """MJPEG stream for ``<img src="/api/video/<name>">``."""
    if core._cameras.get(name) is None:  # noqa: SLF001 - collaborator in the same module
        raise HTTPException(status_code=404, detail=f"camera '{name}' is not open")
    return StreamingResponse(
        core._cameras.mjpeg(name, fps=fps),  # noqa: SLF001
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the server."""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
