"""Tools mode — browser-use as a library, with the reasoning removed.

The classic runtime IS an agent: its own loop, its own model calls, its own
per-provider client wiring. Tools mode keeps everything browser-use is actually
good at — DOM distillation into indexed interactive elements, robust action
execution over CDP — and exposes it as a plain HTTP tool surface for an
ENGINE (opencode / Claude Code) to drive. No model, no key, no provider
branches: the pod that reasons about the page never touches a provider API
from here, so every provider an engine supports — including a Claude
subscription — can browse.

Surface (JSON in/out, engine drives it with curl):

  GET  /healthz            liveness
  POST /state              {} | {"screenshot": true}
                           -> {url, title, elements, tabs[, screenshot_b64]}
  POST /act                {"action": "<name>", "params": {...}}
                           -> {ok, result, error, state: {url, title, elements}}
  GET  /screenshot         -> image/png of the current viewport
  GET  /actions            -> the action names + param schemas (self-describing)

Actions are browser-use's own registry (navigate, click by index, input text,
scroll, send_keys, go_back, tabs…) minus the ones that need an LLM inside
(extract_structured_data) — extraction is the engine's job; /state hands it
the distilled page.

The CDP target is AGENT_CDP_WS_URL, same env the loop mode uses; an http://
URL works too (browser-use resolves /json/version itself), which is what the
chart renders — the browser Service exposes CDP on :9222 and the ws GUID
changes per boot, so resolution must happen at connect time. The session is
built lazily and rebuilt on failure: the browser restarting under us must
read as one failed action, not a dead tool server.
"""

import asyncio
import base64
import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from browser_use import BrowserProfile, BrowserSession, Tools

from .config import settings

logger = logging.getLogger(__name__)

# LLM-dependent actions have no place in a tool server. The engine extracts
# from /state (or asks for a screenshot) instead.
EXCLUDED_ACTIONS = ("extract_structured_data",)


async def _resolve_ws(cdp_url: str) -> str:
    """Resolve an http(s) CDP base to the browser's ws endpoint — OURSELVES.

    Chrome's /json/version reports webSocketDebuggerUrl on the address Chrome
    believes it has, which inside a pod is a loopback port nothing else can
    reach. browser-use's own http resolution trusts that URL verbatim and
    dies. So: take only the /devtools/browser/<guid> PATH from the answer and
    keep the host:port we were given — the reachable one. Resolved per
    connect, because the GUID changes every time the browser restarts.
    """
    if cdp_url.startswith("ws"):
        return cdp_url
    import httpx

    base = cdp_url.rstrip("/")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(base + "/json/version")
        r.raise_for_status()
        reported = r.json().get("webSocketDebuggerUrl", "")
    path = reported.split("/devtools/", 1)
    if len(path) != 2:
        raise HTTPException(502, f"browser gave no usable CDP endpoint: {reported!r}")
    scheme = "wss" if base.startswith("https") else "ws"
    hostport = base.split("://", 1)[1]
    return f"{scheme}://{hostport}/devtools/{path[1]}"


class _Browser:
    """One lazily-built BrowserSession + Tools registry, rebuilt on failure."""

    def __init__(self) -> None:
        self._session: Optional[BrowserSession] = None
        self._tools = Tools(exclude_actions=list(EXCLUDED_ACTIONS))
        self._lock = asyncio.Lock()

    @property
    def tools(self) -> Tools:
        return self._tools

    async def session(self) -> BrowserSession:
        async with self._lock:
            if self._session is None:
                if not settings.cdp_ws_url:
                    raise HTTPException(503, "no browser attached (AGENT_CDP_WS_URL unset)")
                url = await _resolve_ws(settings.cdp_ws_url)
                logger.info("tools: connecting to browser at %s", url)
                s = BrowserSession(
                    cdp_url=url,
                    browser_profile=BrowserProfile(keep_alive=True),
                )
                await s.start()
                self._session = s
            return self._session

    async def reset(self) -> None:
        async with self._lock:
            s, self._session = self._session, None
            if s is not None:
                try:
                    await s.kill()
                except Exception:  # noqa: BLE001 — a dead session may refuse to die politely
                    pass


class StateRequest(BaseModel):
    screenshot: bool = False


class ActRequest(BaseModel):
    action: str
    params: dict[str, Any] = {}


def _state_payload(summary: Any, with_screenshot: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "url": summary.url,
        "title": summary.title,
        # The same distilled, indexed element view browser-use's own agent
        # prompts with — [index]<tag …> lines the engine clicks by number.
        "elements": summary.dom_state.llm_representation(),
        "tabs": [{"url": t.url, "title": t.title} for t in (summary.tabs or [])],
    }
    if with_screenshot and summary.screenshot:
        out["screenshot_b64"] = summary.screenshot
    return out


def build_tools_app() -> FastAPI:
    app = FastAPI(title="livellm-browser-tools")
    b = _Browser()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": "tools"}

    @app.get("/actions")
    async def actions() -> dict[str, Any]:
        reg = b.tools.registry.registry.actions
        return {
            name: {
                "description": a.description,
                "params": a.param_model.model_json_schema().get("properties", {}),
            }
            for name, a in reg.items()
        }

    @app.post("/state")
    async def state(req: StateRequest | None = None) -> dict[str, Any]:
        req = req or StateRequest()
        try:
            s = await b.session()
            summary = await s.get_browser_state_summary(include_screenshot=req.screenshot)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — surface it, then heal
            await b.reset()
            raise HTTPException(502, f"browser state failed (reconnecting): {e}") from e
        return _state_payload(summary, req.screenshot)

    @app.post("/act")
    async def act(req: ActRequest) -> dict[str, Any]:
        reg = b.tools.registry.registry.actions
        if req.action not in reg:
            raise HTTPException(
                400, f"unknown action {req.action!r} — GET /actions lists what exists"
            )
        Model = b.tools.registry.create_action_model(include_actions=[req.action])
        try:
            action = Model(**{req.action: req.params})
        except ValidationError as e:
            raise HTTPException(422, f"bad params for {req.action}: {e}") from e
        try:
            s = await b.session()
            result = await b.tools.act(action, s)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            await b.reset()
            raise HTTPException(502, f"action failed (reconnecting): {e}") from e
        out: dict[str, Any] = {
            "ok": result.error is None,
            "result": result.extracted_content,
            "error": result.error,
        }
        # One round-trip = act + observe: hand back the page the action left
        # behind so the engine doesn't need a second call to see what happened.
        try:
            summary = await (await b.session()).get_browser_state_summary(include_screenshot=False)
            out["state"] = _state_payload(summary, False)
        except Exception:  # noqa: BLE001 — the action's verdict still stands
            pass
        return out

    @app.get("/screenshot")
    async def screenshot() -> Response:
        try:
            s = await b.session()
            summary = await s.get_browser_state_summary(include_screenshot=True)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            await b.reset()
            raise HTTPException(502, f"screenshot failed (reconnecting): {e}") from e
        if not summary.screenshot:
            raise HTTPException(502, "browser returned no screenshot")
        return Response(content=base64.b64decode(summary.screenshot), media_type="image/png")

    return app
