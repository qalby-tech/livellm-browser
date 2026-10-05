"""The launcher API's guards: /version is loopback only; pause and resume
also need the keeper's header; the routes a Camoufox browser does not serve
answer 404. The browser itself is faked (the lifespan does not run)."""
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

import launch

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("10.0.0.7", 50000)  # Traefik's pod IP, the edge


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def pause(max_seconds):
        seen.append(("pause", max_seconds))

    async def resume(auto=False):
        seen.append(("resume",))
        return True

    monkeypatch.setattr(launch.manager, "pause", pause)
    monkeypatch.setattr(launch.manager, "resume", resume)
    monkeypatch.setattr(launch.manager, "version", lambda: {"engine": "camoufox"})
    return seen


def client(peer):
    return TestClient(launch.app, client=peer)


def test_version_is_loopback_only(calls):
    assert client(REMOTE).get("/version").status_code == 403
    r = client(LOCAL).get("/version")
    assert r.status_code == 200 and r.json() == {"engine": "camoufox"}


@pytest.mark.parametrize("path,body", [("/browsers/default/pause", {"maxSeconds": 30}), ("/browsers/default/resume", None)])
def test_pause_and_resume_need_loopback_and_the_keeper_header(calls, path, body):
    hdr = {"X-Livellm-Keeper": "1"}
    assert client(REMOTE).post(path, json=body, headers=hdr).status_code == 403
    assert client(LOCAL).post(path, json=body).status_code == 403  # a page in the browser sends no such header
    assert client(LOCAL).post(path, json=body, headers={"X-Livellm-Keeper": "0"}).status_code == 403
    assert calls == []
    assert client(LOCAL).post(path, json=body, headers=hdr).status_code == 200
    assert len(calls) == 1


def test_a_pause_is_bounded(calls):
    hdr = {"X-Livellm-Keeper": "1"}
    assert client(LOCAL).post("/browsers/default/pause", json={"maxSeconds": 601}, headers=hdr).status_code == 422
    assert client(LOCAL).post("/browsers/default/pause", json={"maxSeconds": 0}, headers=hdr).status_code == 422
    assert calls == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/browsers"),
    ("GET", "/browsers/default/extensions"),
    ("POST", "/browsers/default/extensions"),
    ("DELETE", "/browsers/default/extensions/abc"),
    ("PATCH", "/browsers/default/extensions/abc"),
])
def test_routes_a_camoufox_browser_does_not_serve(method, path):
    assert client(REMOTE).request(method, path).status_code == 404


def test_the_public_entry_names_the_engine_and_the_stable_path():
    r = client(REMOTE).get("/browsers")
    assert r.status_code == 200
    entry = r.json()[0]
    assert entry["engine"] == "camoufox" and entry["ws_stable_endpoint"] == "/playwright/default"
    assert entry["browser_id"] == "default"


def test_health_without_a_browser_is_503():
    r = client(REMOTE).get("/health")
    assert r.status_code == 503 and r.json()["status"] == "down"


def test_cookie_imports_are_bounded():
    r = client(REMOTE).post("/browsers/default/cookies", json=[{"name": "a"}] * 5001)
    assert r.status_code == 400
