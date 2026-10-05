"""The Camoufox browser's launcher API (:9000), one browser per pod.

Public (the edge forwards "/" on the browser's automation host here, behind
its token):
  GET  /                                 the browser and its stable Playwright path
  GET  /health                           ok | paused | restarting (<= 90 s), else 503
  GET  /browsers                         the one browser
  POST /browsers/default/restart
  GET  /browsers/default/cookies
  POST /browsers/default/cookies         {"added", "dropped"}: Firefox refuses some
                                         cookies Chromium takes, and counts them
Loopback only (the control sidecar): GET /version; with X-Livellm-Keeper: 1
also POST /browsers/default/pause and /resume.

Not served: POST /browsers (one browser per pod) and the extension routes
(Camoufox browsers take no extensions).
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from core import const
from core.automation_proxy import AutomationProxy
from core.manager import MAX_PAUSE_SECONDS, CamoufoxManager
from core.server import ServeError

logger = logging.getLogger("launch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

proxy = AutomationProxy(const.automation_port(), const.STABLE_WS_PATH)
manager = CamoufoxManager(proxy)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await proxy.start()
    boot = asyncio.create_task(manager.start())
    watchdog = asyncio.create_task(manager.watchdog())
    yield
    watchdog.cancel()
    boot.cancel()
    logger.info("Shutting down the browser...")
    try:
        await asyncio.wait_for(manager.shutdown(), timeout=25.0)
    except Exception as e:
        logger.error(f"Error during browser shutdown: {e}")
    await proxy.stop()
    logger.info("Shutdown complete")


app = FastAPI(title="Camoufox Browser Launcher API", lifespan=lifespan)


def _entry() -> dict:
    return {
        "browser_id": const.DEFAULT_BROWSER_ID,
        "engine": "camoufox",
        "automation_port": proxy.bind_port,
        "ws_stable_endpoint": const.STABLE_WS_PATH,
        "playwright": const.PLAYWRIGHT_VERSION,
    }


@app.get("/")
async def root():
    if manager.state in ("stopped", "error") and manager.error in ("profile_engine", "profile_newer"):
        return Response(status_code=503, content="No browser")
    return _entry()


@app.get("/health")
async def health():
    status, body = manager.health()
    return JSONResponse(status_code=status, content=body)


@app.get("/browsers")
async def list_browsers() -> list:
    return [{**_entry(), "profile_path": str(const.PROFILE_DIR)}]


@app.post("/browsers")
async def create_browser():
    raise HTTPException(status_code=404, detail="Not Found")


@app.post("/browsers/default/restart")
async def restart_default():
    try:
        ok = await manager.restart()
    except RuntimeError:
        raise HTTPException(status_code=409, detail="The browser is paused")
    if not ok:
        raise HTTPException(status_code=503, detail=manager.error_message or "The browser did not start")
    return {**_entry(), "profile_path": str(const.PROFILE_DIR)}


@app.get("/browsers/default/cookies")
async def get_cookies() -> list:
    try:
        return await manager.get_cookies()
    except (ServeError, asyncio.TimeoutError) as e:
        raise HTTPException(status_code=503, detail=str(e) or "The browser did not answer")


@app.post("/browsers/default/cookies")
async def set_cookies(cookies: list[dict]) -> dict:
    if len(cookies) > 5000:
        raise HTTPException(status_code=400, detail="At most 5000 cookies at once")
    try:
        r = await manager.add_cookies(cookies)
    except (ServeError, asyncio.TimeoutError) as e:
        raise HTTPException(status_code=503, detail=str(e) or "The browser did not answer")
    return {"status": "success", "added": int(r.get("added") or 0), "dropped": int(r.get("dropped") or 0)}


# Every extension route of the Chrome launcher: Camoufox browsers take none.
@app.api_route("/browsers/{browser_id}/extensions", methods=["GET", "POST"])
@app.api_route("/browsers/{browser_id}/extensions/{extension_id}", methods=["DELETE", "PATCH"])
async def no_extensions(browser_id: str, extension_id: Optional[str] = None):
    raise HTTPException(status_code=404, detail="Not Found")


# ── Pod-local control (the control sidecar only) ──
#
# Loopback only: the public automation host reaches this port from Traefik's
# pod IP. The header forces a CORS preflight, which this app never answers,
# so a page in the browser (also loopback) can't send one either.

LOCAL_HEADER = "x-livellm-keeper"


def _require_local(request: Request, header: bool = True) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="forbidden")
    if header and request.headers.get(LOCAL_HEADER) != "1":
        raise HTTPException(status_code=403, detail="forbidden")


class PauseRequest(BaseModel):
    maxSeconds: float = Field(..., gt=0, le=MAX_PAUSE_SECONDS)


@app.post("/browsers/default/pause")
async def pause_default(body: PauseRequest, request: Request) -> dict:
    _require_local(request)
    await manager.pause(body.maxSeconds)
    return {"status": "paused", "maxSeconds": body.maxSeconds}


@app.post("/browsers/default/resume")
async def resume_default(request: Request) -> dict:
    _require_local(request)
    try:
        await manager.resume()
    except Exception as e:
        logger.error(f"Resume failed: {e}")
        raise HTTPException(status_code=503, detail="The browser did not start")
    return {"status": "ok"}


@app.get("/version")
async def version(request: Request) -> dict:
    # Read-only and answered without CORS headers (a page can't read it), so
    # loopback alone is enough here; pause/resume also need the header.
    _require_local(request, header=False)
    return manager.version()


if __name__ == "__main__":
    # A stop (SIGTERM, the pod's 30 s grace) waits at most 3 s for requests
    # in flight, then the lifespan closes the browser (its 25 s budget).
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info", timeout_graceful_shutdown=3)
