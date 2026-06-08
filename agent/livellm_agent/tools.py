"""Controller tool client — decision C.

When a BrowserAgent targets a Controller, its deterministic endpoints are
registered as browser-use tools so the LLM offloads fast, no-LLM work
(structured search, clean page read, bulk attribute extraction) instead of
reasoning over raw DOM. These are URL-driven *fetch & parse* tools: they run on
the controller's own session/page, so they don't disturb the agent's live page.
Live interaction stays with browser-use's native actions.

When no controller is configured, none of this is wired and browser-use falls
back to its built-in DOM extraction.

The wrapping into browser-use `@tools.action` lives in engine.py (P1 cont.);
this module is the pure HTTP client and is independently testable.
"""

import logging
from typing import Any, Literal, Optional

import httpx

logger = logging.getLogger(__name__)

SearchKind = Literal["web", "news", "images", "videos"]
_SEARCH_PATH = {
    "web": "/search",
    "news": "/search_news",
    "images": "/search_images",
    "videos": "/search_videos",
}


class ControllerTools:
    """HTTP client over a Controller's /parser endpoints, scoped to one session.

    Lazily opens a controller session (a page) on the target browser and reuses
    it for the client's lifetime; call `close()` to release it.
    """

    def __init__(self, base_url: str, browser_id: str, timeout: float = 60.0):
        # base_url e.g. "http://<controller>.<ns>:8000/parser"
        self._base = base_url.rstrip("/")
        self._browser_id = browser_id
        self._session_id: Optional[str] = None
        self._http = httpx.AsyncClient(base_url=self._base, timeout=timeout)

    # ── session lifecycle ────────────────────────────────────────────────
    async def _ensure_session(self) -> None:
        if self._session_id:
            return
        r = await self._http.post(
            "/start_session", json={"browser_id": self._browser_id}
        )
        r.raise_for_status()
        self._session_id = r.json()["session_id"]
        logger.info("controller tools: opened session %s on browser %s",
                    self._session_id, self._browser_id)

    def _headers(self) -> dict[str, str]:
        return {"X-Browser-Id": self._browser_id, "X-Session-Id": self._session_id or ""}

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        await self._ensure_session()
        r = await self._http.post(path, json=payload, headers=self._headers())
        r.raise_for_status()
        return r

    async def close(self) -> None:
        try:
            if self._session_id:
                await self._http.request(
                    "DELETE", "/end_session", headers=self._headers()
                )
        except Exception as e:  # best-effort
            logger.warning("controller tools: end_session failed: %s", e)
        finally:
            await self._http.aclose()
            self._session_id = None

    # ── tools ─────────────────────────────────────────────────────────────
    async def web_search(self, query: str, kind: SearchKind = "web", count: int = 5) -> dict:
        """Structured search (web/news/images/videos) with wiki + AI-review panels."""
        r = await self._post(_SEARCH_PATH[kind], {"query": query, "count": count})
        return r.json()

    async def search_suggestions(self, query: str) -> list[str]:
        """Google autocomplete suggestions for a query."""
        r = await self._post("/search_hints", {"query": query})
        return r.json().get("hints", [])

    async def read_page(self, url: str, html: bool = False, steps: int = 8) -> str:
        """Navigate + auto-scroll a URL and return clean text (or html)."""
        r = await self._post("/content", {
            "url": url,
            "output_action": "html" if html else "text",
            "steps": steps,
        })
        return r.text

    async def extract(self, url: str, selectors: list[dict[str, Any]]) -> list[dict]:
        """Bulk-extract elements/attributes by CSS/XPath selectors.

        `selectors` items: {name, selector, type?(css|xpath), attribute?(e.g. href)}.
        Returns [{name, values:[...]}, ...].
        """
        r = await self._post("/attribute", {"url": url, "selectors": selectors})
        return r.json()
