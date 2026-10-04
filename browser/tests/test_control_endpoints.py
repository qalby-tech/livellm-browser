"""Pod-local endpoints (pause/resume/version), /health while paused, and
POST /browsers input checks. No Chrome: the manager is faked."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import launch
from core.local_browser import local_browser_manager

LOCAL = {"x-livellm-keeper": "1"}


def client(host="127.0.0.1"):
    transport = httpx.ASGITransport(app=launch.app, client=(host, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://launcher")


@pytest.fixture
def fake_manager(monkeypatch):
    info = SimpleNamespace(
        browser=SimpleNamespace(is_connected=lambda: True, version="154.0.8037.57"),
        proxy_port=9222, ws_endpoint="/devtools/browser/x", chrome_port=4321,
        started_at=1_700_000_000.0, profile_path="/p",
    )
    monkeypatch.setattr(local_browser_manager, "browsers", {"default": info})
    monkeypatch.setattr(local_browser_manager, "driver_alive", lambda: True)
    monkeypatch.setattr(local_browser_manager, "_paused_until", {})
    pause = AsyncMock()
    resume = AsyncMock()
    monkeypatch.setattr(local_browser_manager, "pause_browser", pause)
    monkeypatch.setattr(local_browser_manager, "resume_browser", resume)
    return info, pause, resume


@pytest.mark.parametrize("host,headers", [
    ("10.0.0.7", LOCAL),          # Traefik / any pod IP
    ("127.0.0.1", {}),            # loopback without the header (a page in Chrome)
])
async def test_pod_local_endpoints_refuse_others(fake_manager, host, headers):
    async with client(host) as c:
        assert (await c.post("/browsers/default/pause", json={"maxSeconds": 30}, headers=headers)).status_code == 403
        assert (await c.post("/browsers/default/resume", headers=headers)).status_code == 403
        if host != "127.0.0.1":
            assert (await c.get("/version", headers=headers)).status_code == 403
    _, pause, resume = fake_manager
    pause.assert_not_called()
    resume.assert_not_called()


async def test_pause_resume_from_loopback(fake_manager):
    _, pause, resume = fake_manager
    async with client("127.0.0.1") as c:
        r = await c.post("/browsers/default/pause", json={"maxSeconds": 30}, headers=LOCAL)
        assert r.status_code == 200 and r.json()["status"] == "paused"
        assert (await c.post("/browsers/default/pause", json={"maxSeconds": 601}, headers=LOCAL)).status_code == 422
        assert (await c.post("/browsers/default/resume", headers=LOCAL)).status_code == 200
    pause.assert_awaited_once_with("default", 30)
    resume.assert_awaited_once()


async def test_ipv6_loopback_is_local(fake_manager):
    async with client("::1") as c:
        assert (await c.get("/version", headers=LOCAL)).status_code == 200


async def test_version(fake_manager, monkeypatch):
    monkeypatch.setattr(launch, "_chrome_pid", lambda port: 777 if port == 4321 else None)
    async with client() as c:
        v = (await c.get("/version", headers=LOCAL)).json()
    assert v["chrome"] == "154.0.8037.57" and v["chromeMajor"] == 154
    assert v["image"] == "2.3.0" and v["pid"] == 777
    assert v["startedAt"] == "2023-11-14T22:13:20Z"


def test_chrome_pid_skips_renderers(tmp_path):
    def proc(pid, *argv):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    proc(10, "/opt/google/chrome/chrome", "--type=renderer", "--remote-debugging-port=4321")
    proc(11, "/opt/google/chrome/chrome", "--remote-debugging-port=4321", "--start-maximized")
    proc(12, "/opt/google/chrome/chrome", "--remote-debugging-port=9999")
    (tmp_path / "self").mkdir()
    assert launch._chrome_pid(4321, str(tmp_path)) == 11
    assert launch._chrome_pid(None, str(tmp_path)) is None


async def test_health_answers_paused(fake_manager, monkeypatch):
    info, _, _ = fake_manager
    import time
    monkeypatch.setattr(local_browser_manager, "_paused_until", {"default": time.monotonic() + 30})
    info.browser = SimpleNamespace(is_connected=lambda: False)
    async with client("10.0.0.1") as c:
        r = await c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "paused"}
    # Bounded: a pause long past its deadline is not healthy any more.
    monkeypatch.setattr(local_browser_manager, "_paused_until", {"default": time.monotonic() - 3600})
    async with client("10.0.0.1") as c:
        assert (await c.get("/health")).status_code == 503


@pytest.mark.parametrize("uid", ["../etc", "a/b", "", "x" * 65, "a b", ".."])
async def test_profile_uid_must_be_a_plain_name(fake_manager, monkeypatch, uid):
    create = AsyncMock()
    monkeypatch.setattr(local_browser_manager, "create_browser", create)
    async with client("10.0.0.1") as c:
        r = await c.post("/browsers", json={"profile_uid": uid})
    assert r.status_code == 422
    create.assert_not_called()


@pytest.mark.parametrize("uid", ["default", "shipuchka-b1", "A_b-9"])
async def test_profile_uid_accepts_existing_ids(fake_manager, monkeypatch, uid):
    info, _, _ = fake_manager
    create = AsyncMock(return_value=(uid, info))
    monkeypatch.setattr(local_browser_manager, "create_browser", create)
    async with client("10.0.0.1") as c:
        r = await c.post("/browsers", json={"profile_uid": uid})
    assert r.status_code == 200, r.text


async def test_version_needs_loopback_only(fake_manager):
    async with client("127.0.0.1") as c:
        assert (await c.get("/version")).status_code == 200
    async with client("10.0.0.9") as c:
        assert (await c.get("/version", headers=LOCAL)).status_code == 403
