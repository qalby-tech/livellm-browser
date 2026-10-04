"""Per-session proxy rotation hints and pause awareness."""
import time

import httpx
import pytest

from core import keeper_hint
from core.keeper_hint import PauseWatch, local_host

CONTENT = {"steps": 0, "idle": 0}


def test_local_host_only_for_workspace_services():
    assert local_host("ws://ws-b1.tenant-x.svc.cluster.local:9222/devtools/browser/b1") == "ws-b1.tenant-x.svc.cluster.local"
    assert local_host("wss://remote.example.com/devtools/browser/x") is None
    assert local_host("ws://10.0.0.5:9222/devtools/browser/x") is None
    assert local_host("") is None and local_host(None) is None


@pytest.fixture
def hints(pool, monkeypatch):
    """agent-1/agent-2 count as local browsers; session-start calls recorded."""
    calls = []
    answers = {"rotated": True, "reason": None}
    monkeypatch.setattr(keeper_hint, "local_host", lambda ws: ws.split("://", 1)[1].split(":", 1)[0] + ".svc" if ws else None)

    async def fake_start(host, n, client=None):
        calls.append((host, n))
        return answers["rotated"], answers["reason"]

    monkeypatch.setattr(keeper_hint, "session_start", fake_start)
    return pool, calls, answers


def test_start_session_hints_the_browser(hints):
    pool, calls, answers = hints
    r = pool.client.post("/start_session", headers={"X-Browser-Id": "agent-1"})
    assert r.status_code == 200
    assert r.json()["proxyRotated"] is True
    assert calls == [("agent-1.svc", 0)]

    # A second session while the first was used recently: counted.
    answers.update(rotated=False, reason="other sessions in use")
    r2 = pool.client.post("/start_session", headers={"X-Browser-Id": "agent-1"})
    body = r2.json()
    assert body["proxyRotated"] is False and body["proxyReason"] == "other sessions in use"
    assert calls[-1] == ("agent-1.svc", 1)


def test_only_recently_used_sessions_count(hints):
    pool, calls, _ = hints
    sid = pool.client.post("/start_session", headers={"X-Browser-Id": "agent-1"}).json()["session_id"]
    # unused for 11 minutes
    pool.manager.session_used[sid] = time.monotonic() - 660
    pool.client.post("/start_session", headers={"X-Browser-Id": "agent-1"})
    assert calls[-1] == ("agent-1.svc", 0)
    # a call with the session marks it used again
    pool.client.post("/content", json=CONTENT, headers={"X-Session-Id": sid})
    pool.client.post("/start_session", headers={"X-Browser-Id": "agent-1"})
    assert calls[-1][1] == 2  # sid + the second session


def test_ended_session_does_not_count(hints):
    pool, calls, _ = hints
    sid = pool.client.post("/start_session", headers={"X-Browser-Id": "agent-2"}).json()["session_id"]
    pool.client.request("DELETE", "/end_session", headers={"X-Session-Id": sid})
    pool.client.post("/start_session", headers={"X-Browser-Id": "agent-2"})
    assert calls[-1] == ("agent-2.svc", 0)


def test_remote_browsers_are_not_called(pool, monkeypatch):
    called = []

    async def fake_start(host, n, client=None):
        called.append(host)
        return True, None

    monkeypatch.setattr(keeper_hint, "session_start", fake_start)
    r = pool.client.post("/start_session")  # agent-N hosts are not *.svc.cluster.local
    assert r.status_code == 200 and r.json()["proxyRotated"] is False
    assert called == []


async def test_session_start_ignores_a_missing_sidecar():
    def handler(request):
        if request.url.host == "nokeeper":
            return httpx.Response(404)
        assert request.url.port == 9300 and request.url.path == "/v1/session-start"
        import json
        assert json.loads(request.content) == {"openSessions": 2}
        return httpx.Response(200, json={"rotated": False, "reason": "other sessions in use"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await keeper_hint.session_start("nokeeper", 0, client) == (False, None)
    assert await keeper_hint.session_start("b1", 2, client) == (False, "other sessions in use")

    def refused(request):
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refused))
    assert await keeper_hint.session_start("b1", 0, client) == (False, None)


async def test_pause_watch_reads_and_caches_health():
    hits = []

    def handler(request):
        hits.append(request.url.host)
        assert request.url.port == 9000 and request.url.path == "/health"
        if request.url.host == "paused":
            return httpx.Response(200, json={"status": "paused"})
        return httpx.Response(503)

    w = PauseWatch()
    w._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await w.refresh([("a", "paused"), ("b", "down"), ("c", None)])
    assert w.paused("a") and not w.paused("b") and not w.paused("c")
    await w.refresh([("a", "paused")])
    assert hits.count("paused") == 1  # cached for 2 s


def test_paused_browser_is_away_in_the_pick(pool, monkeypatch):
    # agent-1 disconnected and paused: the pick goes to agent-2 even though
    # agent-1 has no open tabs.
    pool.client.post("/start_session", headers={"X-Browser-Id": "agent-2"})
    pool.manager.drop_connection("agent-1")  # Chrome closed for a copy; reconnectable

    async def fake_refresh(browsers):
        for bid, _ in browsers:
            keeper_hint.pause_watch._seen[bid] = (time.monotonic(), bid == "agent-1")

    monkeypatch.setattr(keeper_hint.pause_watch, "refresh", fake_refresh)
    monkeypatch.setattr("core.dependencies.local_host", lambda ws: "h" if ws else None)
    picked = [pool.client.post("/content", json=CONTENT).headers["x-browser-id"] for _ in range(3)]
    assert picked == ["agent-2"] * 3
    assert pool.manager.paused.get("agent-1") is True
    keeper_hint.pause_watch._seen.clear()
