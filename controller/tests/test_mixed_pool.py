"""One Browser API over browsers of both engines, in one process.

The registry names a browser's engine: a Chrome browser is reached over CDP
on Chrome's driver (patchright), a Camoufox one through its own Playwright
server on Camoufox's driver (stock Playwright), started at the first Camoufox
browser. A dead driver is restarted alone and only its engine's browsers
reconnect. POST /start_session takes an engine.
"""
import asyncio
import json
import pathlib
import re

import pytest

from core import pw
from core.registry import BrowserRegistry

CONTENT = {"steps": 0, "idle": 0}
MIXED = {"ch-1": None, "ch-2": None, "cf-1": "camoufox", "cf-2": "camoufox"}
CAMOUFOX_ONLY = {"cf-1": "camoufox", "cf-2": "camoufox"}


def engines_started(pool):
    return [c.args[0] if c.args else "chrome" for c in pool.starter.call_args_list]


def start(pool, body=None, **headers):
    return pool.client.post("/start_session", json=body or {}, headers=headers)


class TestShim:
    def test_both_clients(self):
        assert pw.async_playwright.__module__ == "core.pw"
        assert pw._chrome_playwright.__module__.startswith("patchright.")
        assert pw._camoufox_playwright.__module__.startswith("playwright.")
        with pytest.raises(ValueError):
            pw.async_playwright("firefox")

    def test_no_module_imports_a_playwright_but_the_shim(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        bad = []
        for f in root.rglob("*.py"):
            rel = f.relative_to(root)
            if rel.parts[0] in ("tests", ".venv") or str(rel) == "core/pw.py":
                continue
            if re.search(r"^\s*(from|import)\s+(patchright|playwright)\b", f.read_text(), re.M):
                bad.append(str(rel))
        assert not bad, f"import the Playwright names from core.pw: {bad}"

    def test_no_engine_switch_in_the_environment(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for f in root.rglob("*.py"):
            rel = f.relative_to(root)
            if rel.parts[0] in ("tests", ".venv"):
                continue
            assert "BROWSER_ENGINE" not in f.read_text(), rel


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


class TestDrivers:
    def test_a_chrome_only_pool_runs_one_driver(self, pool):
        for _ in range(4):
            assert pool.client.post("/content", json=CONTENT).status_code == 200
        assert engines_started(pool) == ["chrome"]
        assert set(pool.manager.drivers) == {"chrome"}
        assert {p for p, _, _ in pool.net.protocols} == {"cdp"}

    @pytest.mark.parametrize("pool", [MIXED], indirect=True)
    def test_each_engine_has_its_own_driver(self, pool):
        """Chrome's starts with the API, Camoufox's at the first Camoufox
        browser (the warm connect at start here), once."""
        for b in MIXED:
            r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": b})
            assert r.status_code == 200, r.text
        assert engines_started(pool) == ["chrome", "camoufox"]
        chrome, camoufox = pool.manager.drivers["chrome"].number, pool.manager.drivers["camoufox"].number
        assert chrome != camoufox
        by = {(d, p, b) for d, p, b in pool.net.via}
        assert {(d, p) for d, p, b in by if b.startswith("ch-")} == {(chrome, "cdp")}
        assert {(d, p) for d, p, b in by if b.startswith("cf-")} == {(camoufox, "playwright")}

    @pytest.mark.parametrize("pool", [MIXED], indirect=True)
    def test_a_dead_driver_restarts_alone_and_only_its_engine_reconnects(self, pool, monkeypatch):
        manager = pool.manager
        for b in MIXED:
            assert pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": b}).status_code == 200
        chrome_conns = {b: manager.browsers[b].browser for b in ("ch-1", "ch-2")}
        old_cf = manager.drivers["camoufox"]
        # Camoufox's driver dies: its connections break and connects on it fail.
        dead = {"camoufox"}
        monkeypatch.setattr(manager, "driver_alive", lambda engine="chrome": engine not in dead)
        for b in ("cf-1", "cf-2"):
            manager.browsers[b].browser._open = False
        pool.net.down("cf-1")

        def back_up():
            dead.discard("camoufox")
            pool.net.chrome("cf-1").up = True  # the new driver reaches it
            return pool.net.playwright()

        pool.starter.return_value.start.side_effect = back_up
        assert pool.client.get("/healthz").status_code == 503
        before = len(pool.net.via)
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "cf-1"})
        assert r.status_code == 200, r.text
        assert engines_started(pool) == ["chrome", "camoufox", "camoufox"]
        assert manager.drivers["camoufox"] is not old_cf
        old_cf.stop.assert_awaited()
        new_cf = manager.drivers["camoufox"].number
        after = pool.net.via[before:]
        # the failed try on the old driver, then both Camoufox browsers on the new one
        assert after[0] == (old_cf.number, "playwright", "cf-1"), after
        assert sorted(b for d, p, b in after[1:]) == ["cf-1", "cf-2"], after
        assert {(d, p) for d, p, b in after[1:]} == {(new_cf, "playwright")}
        # Chrome's driver and connections were not touched.
        assert {b: manager.browsers[b].browser for b in ("ch-1", "ch-2")} == chrome_conns
        assert pool.client.get("/healthz").status_code == 200

    @pytest.mark.parametrize("pool", [MIXED], indirect=True)
    def test_healthz_follows_every_started_driver(self, pool, monkeypatch):
        manager = pool.manager
        assert pool.client.get("/healthz").status_code == 200
        monkeypatch.setattr(manager, "driver_alive", lambda engine="chrome": engine != "camoufox")
        r = pool.client.get("/healthz")
        assert r.status_code == 503 and "camoufox" in r.text
        monkeypatch.setattr(manager, "driver_alive", lambda engine="chrome": engine != "chrome")
        r = pool.client.get("/healthz")
        assert r.status_code == 503 and "chrome" in r.text

    def test_a_driver_never_started_is_not_dead(self, pool):
        assert "camoufox" not in pool.manager.drivers
        assert pool.manager.dead_drivers() == []
        assert pool.client.get("/healthz").status_code == 200

    @pytest.mark.parametrize("pool", [MIXED], indirect=True)
    def test_shutdown_stops_both_drivers(self, pool):
        drivers = dict(pool.manager.drivers)
        assert set(drivers) == {"chrome", "camoufox"}
        pool.client.__exit__(None, None, None)
        for d in drivers.values():
            d.stop.assert_awaited()


@pytest.mark.parametrize("pool", [MIXED], indirect=True)
class TestStartSessionEngine:
    def test_without_engine_any_member(self, pool):
        seen = set()
        for _ in range(8):
            r = start(pool)
            assert r.status_code == 200, r.text
            seen.add((r.json()["browser_id"], r.json()["engine"]))
        assert seen == {("ch-1", "chrome"), ("ch-2", "chrome"), ("cf-1", "camoufox"), ("cf-2", "camoufox")}

    def test_with_an_engine_the_fewest_tabs_of_that_engine(self, pool):
        for engine, members in (("camoufox", {"cf-1", "cf-2"}), ("chrome", {"ch-1", "ch-2"})):
            got = [start(pool, {"engine": engine}).json() for _ in range(4)]
            assert {g["browser_id"] for g in got} == members, got
            assert {g["engine"] for g in got} == {engine}
        # the sessions stay on their browsers
        sid = start(pool, {"engine": "camoufox"}).json()
        for _ in range(3):
            r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid["session_id"]})
            assert r.headers["x-browser-id"] == sid["browser_id"]

    def test_an_unknown_engine_is_422(self, pool):
        assert start(pool, {"engine": "firefox"}).status_code == 422
        assert start(pool, {"engine": "Chrome"}).status_code == 422

    def test_a_pinned_browser_of_the_other_engine_is_409(self, pool):
        r = start(pool, {"engine": "chrome"}, **{"X-Browser-Id": "cf-1"})
        assert r.status_code == 409
        assert r.json()["detail"] == "Browser 'cf-1' is a Camoufox browser, not Chrome."
        r = start(pool, {"engine": "camoufox", "browser_id": "ch-2"})
        assert r.status_code == 409
        assert r.json()["detail"] == "Browser 'ch-2' is a Chrome browser, not Camoufox."
        r = pool.client.post("/browsers/ch-1/start_session", json={"engine": "camoufox"})
        assert r.status_code == 409
        # the same engine, pinned: taken
        r = start(pool, {"engine": "camoufox"}, **{"X-Browser-Id": "cf-2"})
        assert r.status_code == 200 and r.json()["browser_id"] == "cf-2"
        assert pool.manager._in_flight == {}

    def test_members_of_the_engine_but_none_reachable_is_503(self, pool):
        pool.net.down("cf-1")
        pool.net.down("cf-2")
        r = start(pool, {"engine": "camoufox"})
        assert r.status_code == 503
        assert r.json()["detail"] == "None of this Browser API's browsers can be reached right now."
        # Chrome is not used instead, and still answers its own engine
        assert start(pool, {"engine": "chrome"}).status_code == 200

    def test_pins_and_one_off_calls_are_unchanged(self, pool):
        r = pool.client.post("/browsers/cf-2/content", json=CONTENT)
        assert r.status_code == 200 and r.headers["x-browser-id"] == "cf-2"
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "ch-1"})
        assert r.status_code == 200 and r.headers["x-browser-id"] == "ch-1"

    def test_browsers_list_each_engine(self, pool):
        by = {b["browser_id"]: b["engine"] for b in pool.client.get("/browsers").json()}
        assert by == {"ch-1": "chrome", "ch-2": "chrome", "cf-1": "camoufox", "cf-2": "camoufox"}
        assert pool.client.get("/browsers/cf-1").json()["engine"] == "camoufox"


@pytest.mark.parametrize("pool", [{"ch-1": None}], indirect=True)
def test_no_member_of_the_engine_is_409(pool):
    r = start(pool, {"engine": "camoufox"})
    assert r.status_code == 409
    assert r.json()["detail"] == "This Browser API holds no Camoufox browser."
    assert "camoufox" not in pool.manager.drivers  # nothing was started for it


@pytest.mark.parametrize("pool", [CAMOUFOX_ONLY], indirect=True)
def test_no_chrome_member_is_409(pool):
    r = start(pool, {"engine": "chrome"})
    assert r.status_code == 409
    assert r.json()["detail"] == "This Browser API holds no Chrome browser."


def test_an_empty_pool_with_an_engine_is_503_as_without(pool):
    pool.set_registry()
    r = start(pool, {"engine": "camoufox"})
    assert r.status_code == 503
    assert r.json()["detail"] == "This Browser API has no browsers yet."


@pytest.mark.parametrize("pool", [CAMOUFOX_ONLY], indirect=True)
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

    def test_recovery_reconnects_over_playwright(self, pool):
        manager = pool.manager
        assert pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "cf-1"}).status_code == 200
        before = len(pool.net.protocols)
        # The connection breaks: the next call reconnects the same way.
        manager.browsers["cf-1"].browser._open = False
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "cf-1"})
        assert r.status_code == 200
        assert [p for p, b, _ in pool.net.protocols[before:] if b == "cf-1"] == ["playwright"]
        assert manager.browsers["cf-1"].engine == "camoufox"


@pytest.mark.parametrize("pool", [{"cf-1": "camoufox", "agent-1": "Bearer remote"}], indirect=True)
def test_a_remote_chrome_member_beside_camoufox_keeps_its_headers(pool):
    for b in ("cf-1", "agent-1"):
        assert pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": b}).status_code == 200
    by = {b: (p, h) for p, b, h in pool.net.protocols}
    assert by["agent-1"] == ("cdp", {"Authorization": "Bearer remote"})
    assert by["cf-1"][0] == "playwright"


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


def test_a_browser_outside_the_registry_is_chrome(fresh_manager):
    from core.browser import BrowserManager

    assert BrowserManager.engine_of("adhoc") == "chrome"
