"""
A Browser API over two browsers (agent-1, agent-2): how a call finds its
browser. See the ``pool`` fixture in conftest.py.

Run with: uv run pytest tests/ -v
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

CONTENT = {"steps": 0, "idle": 0}


def answered(response):
    """(browser that answered, tab that answered) for a /content call."""
    return response.headers.get("x-browser-id"), response.text


def start(pool, **headers):
    r = pool.client.post("/start_session", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["session_id"], r.json()["browser_id"]


class TestPinning:
    def test_header_pins_browser(self, pool):
        for _ in range(3):
            r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "agent-2"})
            assert r.status_code == 200
            assert r.headers["x-browser-id"] == "agent-2"
            assert r.text.startswith("agent-2-")

    @pytest.mark.parametrize("prefix", ["", "/parser"])
    def test_path_pins_browser(self, pool, prefix):
        for _ in range(3):
            r = pool.client.post(f"{prefix}/browsers/agent-2/content", json=CONTENT)
            assert r.status_code == 200, r.text
            assert r.headers["x-browser-id"] == "agent-2"
            assert r.text.startswith("agent-2-")

    def test_path_and_same_header_agree(self, pool):
        r = pool.client.post(
            "/browsers/agent-1/content", json=CONTENT, headers={"X-Browser-Id": "agent-1"},
        )
        assert r.status_code == 200
        assert r.headers["x-browser-id"] == "agent-1"

    def test_path_and_other_header_disagree(self, pool):
        r = pool.client.post(
            "/browsers/agent-1/content", json=CONTENT, headers={"X-Browser-Id": "agent-2"},
        )
        assert r.status_code == 400
        assert "agent-1" in r.json()["detail"] and "agent-2" in r.json()["detail"]

    def test_unknown_browser(self, pool):
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "agent-9"})
        assert r.status_code == 404
        r = pool.client.post("/browsers/agent-9/content", json=CONTENT)
        assert r.status_code == 404

    def test_start_session_by_path(self, pool):
        r = pool.client.post("/parser/browsers/agent-2/start_session")
        assert r.status_code == 200
        assert r.json()["browser_id"] == "agent-2"
        assert r.headers["x-browser-id"] == "agent-2"

    def test_start_session_by_body(self, pool):
        r = pool.client.post("/start_session", json={"browser_id": "agent-2"})
        assert r.json()["browser_id"] == "agent-2"
        r = pool.client.post("/start_session", json={"browser_id": "agent-2"}, headers={"X-Browser-Id": "agent-2"})
        assert r.json()["browser_id"] == "agent-2"

    def test_start_session_body_and_other_name_disagree(self, pool):
        r = pool.client.post("/start_session", json={"browser_id": "agent-2"}, headers={"X-Browser-Id": "agent-1"})
        assert r.status_code == 400
        assert r.json()["detail"] == "The body names browser 'agent-2' but the call names 'agent-1'."
        r = pool.client.post("/browsers/agent-1/start_session", json={"browser_id": "agent-2"})
        assert r.status_code == 400
        assert pool.manager.sessions == {}


class TestManagementRoutes:
    """/browsers and /browsers/<name> stay management routes next to the prefix."""

    def test_list_browsers(self, pool):
        r = pool.client.get("/browsers")
        assert r.status_code == 200
        assert r.json() == [
            {"browser_id": "agent-1", "connected": True, "healthy": True, "open_tabs": 0, "session_count": 0},
            {"browser_id": "agent-2", "connected": True, "healthy": True, "open_tabs": 0, "session_count": 0},
        ]
        assert "x-browser-id" not in r.headers

    def test_list_counts_tabs_and_sessions(self, pool):
        start(pool, **{"X-Browser-Id": "agent-2"})
        # A tab a person opened counts too.
        pool.net.chrome("agent-2").context.pages.append(object())
        by_id = {b["browser_id"]: b for b in pool.client.get("/parser/browsers").json()}
        assert by_id["agent-2"]["open_tabs"] == 2
        assert by_id["agent-2"]["session_count"] == 1
        assert by_id["agent-1"]["open_tabs"] == 0

    def test_no_internal_addresses(self, pool):
        body = pool.client.get("/browsers").text + pool.client.get("/browsers/agent-1").text
        assert "ws://" not in body and ":9222" not in body

    def test_get_one_browser(self, pool):
        r = pool.client.get("/browsers/agent-1")
        assert r.status_code == 200
        assert r.json()["browser_id"] == "agent-1"
        assert pool.client.get("/browsers/agent-9").status_code == 404

    def test_membership_is_not_changed_through_the_api(self, pool):
        r = pool.client.post("/browsers", json={"browser_id": "x", "ws_url": "ws://x:9222/devtools/browser/x"})
        assert r.status_code == 403
        assert pool.client.delete("/browsers/agent-2").status_code == 403
        assert "agent-2" in pool.manager.browsers

    def test_delete_with_a_path_after_the_name_is_the_prefix(self, pool):
        sid, _ = start(pool, **{"X-Browser-Id": "agent-2"})
        r = pool.client.delete("/browsers/agent-2/end_session", headers={"X-Session-Id": sid})
        assert r.status_code == 200
        assert r.json()["message"] == f"Session {sid} ended"
        assert "agent-2" in pool.manager.browsers


class TestSessions:
    def test_start_session_response(self, pool):
        r = pool.client.post("/start_session", headers={"X-Browser-Id": "agent-2"})
        data = r.json()
        assert data["browser_id"] == "agent-2"
        assert r.headers["x-browser-id"] == "agent-2"
        assert "X-Browser-Id" not in data["message"]

    def test_session_alone_stays_on_its_browser(self, pool):
        sid, bid = start(pool, **{"X-Browser-Id": "agent-2"})
        assert bid == "agent-2"
        for _ in range(4):
            r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
            assert r.status_code == 200
            assert answered(r) == ("agent-2", "agent-2-p1")
        # No stray tab was opened anywhere else.
        assert pool.net.chrome("agent-1").context.pages == []
        assert len(pool.net.chrome("agent-2").context.pages) == 1

    def test_session_with_its_own_browser_named(self, pool):
        sid, _ = start(pool, **{"X-Browser-Id": "agent-2"})
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid, "X-Browser-Id": "agent-2"})
        assert answered(r) == ("agent-2", "agent-2-p1")
        r = pool.client.post("/browsers/agent-2/content", json=CONTENT, headers={"X-Session-Id": sid})
        assert answered(r) == ("agent-2", "agent-2-p1")

    def test_contradicting_browser_is_409(self, pool):
        sid, _ = start(pool, **{"X-Browser-Id": "agent-2"})
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid, "X-Browser-Id": "agent-1"})
        assert r.status_code == 409
        assert r.json()["detail"] == (
            f"Session '{sid}' is on browser 'agent-2', not 'agent-1'. "
            "Send X-Session-Id alone, or name 'agent-2'."
        )
        assert r.headers["x-browser-id"] == "agent-2"
        r = pool.client.post("/browsers/agent-1/content", json=CONTENT, headers={"X-Session-Id": sid})
        assert r.status_code == 409
        r = pool.client.delete("/end_session", headers={"X-Session-Id": sid, "X-Browser-Id": "agent-1"})
        assert r.status_code == 409
        assert pool.net.chrome("agent-1").context.pages == []

    def test_unknown_session_is_404(self, pool):
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": "nope"})
        assert r.status_code == 404
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": "nope", "X-Browser-Id": "agent-1"})
        assert r.status_code == 404
        assert pool.client.delete("/end_session", headers={"X-Session-Id": "nope"}).status_code == 404
        assert pool.net.chrome("agent-1").context.pages == []
        assert pool.net.chrome("agent-2").context.pages == []

    def test_end_session_alone_closes_the_right_tab(self, pool):
        sid, _ = start(pool, **{"X-Browser-Id": "agent-2"})
        tab = pool.net.chrome("agent-2").context.pages[0]
        r = pool.client.delete("/end_session", headers={"X-Session-Id": sid})
        assert r.status_code == 200
        assert r.json()["browser_id"] == "agent-2"
        assert r.headers["x-browser-id"] == "agent-2"
        assert tab.closed
        assert pool.net.chrome("agent-2").context.pages == []
        # Ended means gone.
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
        assert r.status_code == 404

    def test_tab_closed_by_hand_is_replaced(self, pool):
        sid, _ = start(pool, **{"X-Browser-Id": "agent-2"})
        tab = pool.net.chrome("agent-2").context.pages[0]
        # A person closes the session's tab in the browser.
        tab.closed = True
        pool.net.chrome("agent-2").context.pages.remove(tab)
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
        assert r.status_code == 200
        assert answered(r) == ("agent-2", "agent-2-p2")
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
        assert answered(r) == ("agent-2", "agent-2-p2")

    def test_session_ended_while_its_tab_opens(self, pool):
        sid, _ = start(pool, **{"X-Browser-Id": "agent-2"})
        # Its connection dropped, so the next session call reconnects first,
        # and end_session lands meanwhile.
        pool.manager.browsers["agent-2"].browser._open = False
        pool.net.delay["agent-2"] = 0.5
        out = {}

        def call():
            out["call"] = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.15)
        assert pool.client.delete("/end_session", headers={"X-Session-Id": sid}).status_code == 200
        t.join()
        assert out["call"].status_code == 404
        assert sid not in pool.manager.sessions
        # No tab is left open with no session to close it.
        assert pool.net.chrome("agent-2").context.pages == []

    def test_session_outlives_a_reconnect_of_its_browser(self, pool):
        sid, _ = start(pool, **{"X-Browser-Id": "agent-2"})
        # The browser restarts: the connection drops, then it is back.
        pool.net.down("agent-2")
        pool.net.chrome("agent-2").up = True
        pool.net.chrome("agent-2").context.pages.clear()
        pool.manager.browsers["agent-2"].browser._open = False
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
        assert r.status_code == 200
        assert answered(r) == ("agent-2", "agent-2-p2")


class TestFewestTabs:
    def test_calls_spread_across_browsers(self, pool):
        seen = [pool.client.post("/content", json=CONTENT).headers["x-browser-id"] for _ in range(6)]
        assert seen.count("agent-1") == 3 and seen.count("agent-2") == 3

    def test_busier_browser_is_avoided(self, pool):
        # A person has three tabs open on agent-1; sessions on agent-2 count too.
        pool.net.chrome("agent-1").context.pages.extend([object(), object(), object()])
        start(pool, **{"X-Browser-Id": "agent-2"})
        seen = [pool.client.post("/content", json=CONTENT).headers["x-browser-id"] for _ in range(4)]
        assert seen == ["agent-2"] * 4

    def test_sessions_spread_across_browsers(self, pool):
        browsers = [start(pool)[1] for _ in range(4)]
        assert browsers.count("agent-1") == 2 and browsers.count("agent-2") == 2

    def test_calls_in_flight_count(self, pool):
        # Four slow calls at once: each pick sees the ones still running.
        def call(_):
            r = pool.client.post("/content", json={"steps": 0, "idle": 0.5})
            return r.headers["x-browser-id"]

        with ThreadPoolExecutor(max_workers=4) as ex:
            seen = list(ex.map(call, range(4)))
        assert sorted(seen) == ["agent-1", "agent-1", "agent-2", "agent-2"]
        assert pool.manager._in_flight == {}

    def test_soft_cap_is_not_a_refusal(self, pool, monkeypatch):
        monkeypatch.setattr("core.browser.MAX_PAGES_PER_BROWSER", 1)
        pool.net.chrome("agent-1").context.pages.extend([object(), object()])
        pool.net.chrome("agent-2").context.pages.extend([object(), object(), object()])
        r = pool.client.post("/content", json=CONTENT)
        assert r.status_code == 200
        assert r.headers["x-browser-id"] == "agent-1"

    def test_pick_counts_in_flight(self, fresh_manager):
        m = fresh_manager
        first = m.pick_browser(["agent-1", "agent-2"])
        second = m.pick_browser(["agent-1", "agent-2"])
        assert {first, second} == {"agent-1", "agent-2"}
        m.end_call(first)
        assert m.pick_browser(["agent-1", "agent-2"]) == first


class TestConnecting:
    @pytest.mark.parametrize("pool", [["agent-1"]], indirect=True)
    def test_calls_arriving_together_open_one_connection(self, pool):
        # agent-2 joins with no tabs, so every call below picks it while it
        # is still connecting.
        pool.net.chrome("agent-1").context.pages.extend([object()] * 6)
        pool.net.delay["agent-2"] = 0.3
        pool.set_registry("agent-1", "agent-2")

        def call(_):
            r = pool.client.post("/content", json=CONTENT)
            return r.status_code, r.headers.get("x-browser-id")

        with ThreadPoolExecutor(max_workers=4) as ex:
            seen = list(ex.map(call, range(4)))
        assert seen == [(200, "agent-2")] * 4
        assert pool.net.connects.count("agent-2") == 1
        assert len(pool.net.open_connections("agent-2")) == 1

    async def test_connects_racing_keep_one_connection(self, fresh_manager, net):
        fresh_manager.playwright = net.playwright()
        net.delay["agent-1"] = 0.05
        url = "ws://agent-1:9222/devtools/browser/agent-1"
        first, second = await asyncio.gather(
            fresh_manager.connect_browser("agent-1", url),
            fresh_manager.connect_browser("agent-1", url),
        )
        assert first is second is fresh_manager.browsers["agent-1"]
        assert len(net.open_connections("agent-1")) == 1

    @pytest.mark.parametrize("pool", [["agent-1"]], indirect=True)
    def test_a_slow_browser_holds_no_call_up(self, pool, monkeypatch):
        monkeypatch.setattr("core.browser.PICK_CONNECT_WAIT", 0.5)
        pool.net.chrome("agent-1").context.pages.append(object())  # agent-2 is tried first
        pool.net.delay["agent-2"] = 1.5
        pool.set_registry("agent-1", "agent-2")

        began = time.monotonic()
        r = pool.client.post("/content", json=CONTENT)
        assert r.headers["x-browser-id"] == "agent-1"
        assert time.monotonic() - began < 1.2
        # While its connect goes on, calls go elsewhere at once and start no other.
        for _ in range(3):
            began = time.monotonic()
            r = pool.client.post("/content", json=CONTENT)
            assert r.headers["x-browser-id"] == "agent-1"
            assert time.monotonic() - began < 0.4
        assert pool.net.connects.count("agent-2") == 1
        # A call that names it waits for that same connect.
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "agent-2"})
        assert r.status_code == 200
        assert r.headers["x-browser-id"] == "agent-2"
        assert pool.net.connects.count("agent-2") == 1
        # Once up, it is picked like any other.
        assert pool.client.post("/content", json=CONTENT).headers["x-browser-id"] == "agent-2"

    @pytest.mark.parametrize(
        "pool", [{"agent-1": "Bearer t1", "agent-2": None}], indirect=True,
    )
    def test_startup_connect_sends_the_registry_headers(self, pool):
        assert pool.manager.is_connected("agent-1")
        assert pool.manager.is_healthy("agent-1")
        assert pool.net.connects.count("agent-1") == 1


class TestRegistryChanges:
    @pytest.mark.parametrize("pool", [["agent-1"]], indirect=True)
    def test_added_browser_is_picked(self, pool):
        assert list(pool.manager.browsers) == ["agent-1"]
        pool.set_registry("agent-1", "agent-2")
        seen = [pool.client.post("/content", json=CONTENT).headers["x-browser-id"] for _ in range(4)]
        assert "agent-2" in seen
        assert sorted(pool.manager.browsers) == ["agent-1", "agent-2"]

    def test_removed_browser_leaves(self, pool):
        sid, _ = start(pool, **{"X-Browser-Id": "agent-1"})
        pool.set_registry("agent-2")
        seen = {pool.client.post("/content", json=CONTENT).headers["x-browser-id"] for _ in range(4)}
        assert seen == {"agent-2"}
        assert list(pool.manager.browsers) == ["agent-2"]
        assert [b["browser_id"] for b in pool.client.get("/browsers").json()] == ["agent-2"]
        # Named calls and its sessions are cut too.
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "agent-1"})
        assert r.status_code == 404
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
        assert r.status_code == 404
        # The session's tab was closed on the way out.
        assert pool.net.chrome("agent-1").context.pages == []

    def test_removal_does_not_hang_on_a_gone_browser(self, pool, monkeypatch):
        import asyncio

        async def stall():
            await asyncio.sleep(3600)

        monkeypatch.setattr("core.browser.DISCONNECT_TIMEOUT", 0.1)
        pool.manager.browsers["agent-1"].browser.close = stall
        pool.set_registry("agent-2")
        r = pool.client.post("/content", json=CONTENT)
        assert r.status_code == 200
        assert r.headers["x-browser-id"] == "agent-2"
        assert list(pool.manager.browsers) == ["agent-2"]

    def test_empty_pool_is_503(self, pool):
        pool.set_registry()
        r = pool.client.post("/content", json=CONTENT)
        assert r.status_code == 503
        assert r.json()["detail"] == "This Browser API has no browsers yet."
        assert pool.client.get("/browsers").json() == []


class TestDeadMember:
    def test_dead_member_does_not_touch_other_sessions(self, pool):
        driver = pool.manager.playwright
        sid, _ = start(pool, **{"X-Browser-Id": "agent-1"})
        pool.net.down("agent-2")  # its pod restarts; its address stays in the registry
        # agent-2 has the fewest tabs, so a call that names no browser tries
        # it first, fails over to agent-1 and succeeds.
        r = pool.client.post("/content", json=CONTENT)
        assert r.status_code == 200
        assert r.headers["x-browser-id"] == "agent-1"
        assert pool.manager.sessions == {sid: "agent-1"}
        r = pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
        assert answered(r) == ("agent-1", "agent-1-p1")
        # The shared driver was not restarted.
        assert pool.manager.playwright is driver

    def test_dead_member_is_skipped_for_a_while(self, pool):
        pool.net.chrome("agent-1").context.pages.append(object())  # agent-2 is tried first
        pool.net.down("agent-2")
        attempts = pool.net.connects.count("agent-2")
        r = pool.client.post("/content", json=CONTENT)
        assert r.headers["x-browser-id"] == "agent-1"
        assert pool.net.connects.count("agent-2") == attempts + 1
        attempts += 1
        for _ in range(3):
            r = pool.client.post("/content", json=CONTENT)
            assert r.headers["x-browser-id"] == "agent-1"
        assert pool.net.connects.count("agent-2") == attempts
        by_id = {b["browser_id"]: b for b in pool.client.get("/browsers").json()}
        assert by_id["agent-2"]["healthy"] is False
        assert by_id["agent-2"]["connected"] is False
        assert by_id["agent-1"]["healthy"] is True

    def test_dead_member_named_is_502_without_its_address(self, pool):
        pool.net.down("agent-2")
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "agent-2"})
        assert r.status_code == 502
        assert r.json()["detail"] == "Browser 'agent-2' is not reachable right now."
        assert r.headers["x-browser-id"] == "agent-2"

    def test_every_member_dead_is_503(self, pool):
        pool.net.down("agent-1")
        pool.net.down("agent-2")
        r = pool.client.post("/content", json=CONTENT)
        assert r.status_code == 503
        assert "x-browser-id" not in r.headers
        assert pool.manager._in_flight == {}

    def test_member_back_is_picked_again(self, pool, monkeypatch):
        pool.net.down("agent-2")
        pool.client.post("/content", json=CONTENT)
        pool.net.chrome("agent-2").up = True
        monkeypatch.setattr("core.browser.UNHEALTHY_SECONDS", 0)
        pool.manager.mark_unhealthy("agent-2")
        seen = {pool.client.post("/content", json=CONTENT).headers["x-browser-id"] for _ in range(4)}
        assert seen == {"agent-1", "agent-2"}

    def test_dead_driver_is_restarted(self, pool, monkeypatch):
        manager = pool.manager
        restarts = []

        async def restart(browser_id, fresh_ws_url=None, headers=None):
            restarts.append(browser_id)
            pool.net.chrome("agent-2").up = True  # the new driver reaches it
            return await manager._reconnect_same_driver(browser_id, fresh_ws_url, headers)

        monkeypatch.setattr(manager, "_restart_playwright_and_reconnect", restart)
        pool.net.down("agent-2")
        manager.browsers["agent-2"].browser._open = False
        # A live driver: the browser alone is at fault, nothing restarts.
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "agent-2"})
        assert r.status_code == 502
        assert restarts == []
        # A dead driver fails every connect: then it is restarted.
        monkeypatch.setattr(manager, "driver_alive", lambda: False)
        r = pool.client.post("/content", json=CONTENT, headers={"X-Browser-Id": "agent-2"})
        assert r.status_code == 200
        assert r.headers["x-browser-id"] == "agent-2"
        assert restarts == ["agent-2"]
        assert manager.is_healthy("agent-2")

    def test_healthz_stays_ok_with_one_dead_member(self, pool):
        pool.net.down("agent-2")
        assert pool.client.get("/healthz").status_code == 200
        pool.net.down("agent-1")
        assert pool.client.get("/healthz").status_code == 503
