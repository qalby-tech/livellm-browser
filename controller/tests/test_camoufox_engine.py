"""A Browser API of Camoufox browsers (BROWSER_ENGINE=camoufox, its own image):
the registry names a browser's engine, a Camoufox browser is reached through
its own Playwright server (firefox.connect, with the headers) at every connect
site, and the routes, sessions and pinning work as for Chrome.

These run in both test runs: on Python 3.9 with patchright (the Chrome image's
environment) and on 3.12 with stock Playwright and BROWSER_ENGINE=camoufox.
"""
import asyncio
import json
import os

import pytest

from core import pw
from core.registry import BrowserRegistry

CONTENT = {"steps": 0, "idle": 0}
CAMOUFOX_POOL = {"cf-1": "camoufox", "cf-2": "camoufox"}


class TestShim:
    def test_engine_follows_the_environment(self):
        want = "camoufox" if os.environ.get("BROWSER_ENGINE", "").strip().lower() == "camoufox" else "chrome"
        assert pw.ENGINE == want
        module = pw.async_playwright.__module__
        assert module.startswith("playwright." if want == "camoufox" else "patchright."), module

    def test_no_module_imports_a_playwright_but_the_shim(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        bad = []
        for f in root.rglob("*.py"):
            rel = f.relative_to(root)
            if rel.parts[0] in ("tests", ".venv") or str(rel) == "core/pw.py":
                continue
            if re.search(r"^\s*(from|import)\s+(patchright|playwright)\b", f.read_text(), re.M):
                bad.append(str(rel))
        assert not bad, f"import the Playwright names from core.pw: {bad}"


class TestRegistryEngine:
    def test_entries(self, tmp_path):
        reg = tmp_path / "b.json"
        reg.write_text(json.dumps({"browsers": {
            "chrome-str": "ws://a:9222/devtools/browser/default",
            "chrome-obj": {"wsUrl": "ws://b:9222/devtools/browser/default", "headers": {"Authorization": "Bearer x"}},
            "cf": {"wsUrl": "ws://c:9222/playwright/default", "engine": "camoufox"},
            "odd": {"wsUrl": "ws://d:9222/x", "engine": "netscape"},
        }}))
        r = BrowserRegistry(str(reg))
        assert r.get_browser_engine("chrome-str") == "chrome"
        assert r.get_browser_engine("chrome-obj") == "chrome"
        assert r.get_browser_engine("cf") == "camoufox"
        assert r.get_browser_engine("odd") == "chrome"
        assert r.get_browser_engine("absent") is None
        assert r.get_browser_ws_url("cf") == "ws://c:9222/playwright/default"
        assert r.get_all_browsers()["cf"] == "ws://c:9222/playwright/default"


@pytest.mark.parametrize("pool", [CAMOUFOX_POOL], indirect=True)
class TestCamoufoxPool:
    def test_calls_reach_camoufox_over_playwright(self, pool):
        for _ in range(4):
            r = pool.client.post("/content", json=CONTENT)
            assert r.status_code == 200, r.text
        assert pool.net.protocols, "no connect was made"
        assert all(p == "playwright" for p, _, _ in pool.net.protocols), pool.net.protocols
        assert {b for _, b, _ in pool.net.protocols} == {"cf-1", "cf-2"}

    def test_sessions_and_pinning(self, pool):
        r = pool.client.post("/start_session", headers={"X-Browser-Id": "cf-2"})
        assert r.status_code == 200
        sid = r.json()["session_id"]
        for _ in range(3):
            r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
            assert r.headers["x-browser-id"] == "cf-2"
        r = pool.client.post("/browsers/cf-1/content", json=CONTENT)
        assert r.status_code == 200 and r.headers["x-browser-id"] == "cf-1"

    def test_recovery_reconnects_over_playwright(self, pool, monkeypatch):
        manager = pool.manager
        assert pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "cf-1"}).status_code == 200
        before = len(pool.net.protocols)
        # The connection breaks: the next call reconnects the same way.
        manager.browsers["cf-1"].browser._open = False
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "cf-1"})
        assert r.status_code == 200
        assert [p for p, b, _ in pool.net.protocols[before:] if b == "cf-1"] == ["playwright"]
        assert manager.browsers["cf-1"].engine == "camoufox"

    def test_driver_restart_reconnects_over_playwright(self, pool, monkeypatch):
        manager = pool.manager
        for b in ("cf-1", "cf-2"):
            assert pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": b}).status_code == 200
        net = pool.net

        class FakeStarter:
            async def start(self):
                net.chrome("cf-1").up = True  # the new driver reaches it
                return net.playwright()

        monkeypatch.setattr(pw, "async_playwright", lambda: FakeStarter())
        before = len(net.protocols)
        # A dead driver fails the connect: the driver is restarted and every
        # browser reconnected, each through its own engine's connect.
        net.down("cf-1")
        manager.browsers["cf-1"].browser._open = False
        monkeypatch.setattr(manager, "driver_alive", lambda: False)
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "cf-1"})
        assert r.status_code == 200, r.text
        assert "cf-2" in [b for p, b, _ in net.protocols[before:] if p == "playwright"]
        assert not [p for p, _, _ in net.protocols[before:] if p != "playwright"]


@pytest.mark.parametrize("pool", [{"cf-1": "camoufox", "agent-1": None}], indirect=True)
def test_each_browser_is_reached_by_its_own_engine(pool):
    """An entry without an engine is Chrome's CDP address, whatever the
    controller's own engine (the operator gives a pool one engine; this only
    proves the entry decides)."""
    for b in ("cf-1", "agent-1"):
        assert pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": b}).status_code == 200
    by = {b: p for p, b, _ in pool.net.protocols}
    assert by == {"cf-1": "playwright", "agent-1": "cdp"}


def test_a_camoufox_browser_without_a_context_gets_one_sized_by_its_window():
    from core.browser import BrowserManager

    calls = []

    class Bare:
        contexts = []

        async def new_context(self, **kw):
            calls.append(kw)
            return "ctx"

    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(BrowserManager._default_context(Bare(), "camoufox")) == "ctx"
        assert loop.run_until_complete(BrowserManager._default_context(Bare(), "chrome")) == "ctx"
    finally:
        loop.close()
    assert calls == [{"no_viewport": True}, {}]


def test_a_browser_outside_the_registry_has_the_controllers_engine(fresh_manager, monkeypatch):
    from core.browser import BrowserManager

    assert BrowserManager.engine_of("adhoc") == pw.ENGINE
    monkeypatch.setattr(pw, "ENGINE", "camoufox")
    assert BrowserManager.engine_of("adhoc") == "camoufox"
