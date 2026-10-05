"""The launcher's browser manager through its seams (a fake camoufox library,
a fake serve.js process, a fake /proc and cgroup): /health's states, the
watchdog, the relaunch on exit, pause/resume, the profile guards' error
states and their retries, the one-shot downgrade marker, session cookies."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from core import identity, manager as manager_mod, profile_guard, session_cookies
from core.automation_proxy import AutomationProxy
from core.manager import CamoufoxManager
from core.server import ServeError
from core.settings import Settings

OWN_VERSION = "156.0.1-beta.34"


class FakeLib:
    """camoufox's calls: a fingerprint, and launch_options resolving it."""

    def __init__(self):
        self.launches = 0
        self.fail_launch_options = 0

    class Screen:
        def __init__(self, **kw):
            self.kw = kw

    UBO = object()
    core_counts = identity.CORE_COUNTS

    def generate_fingerprint(self, screen=None, window=None, os=None):
        return {"window": {"outerWidth": 1920, "outerHeight": 1080, "innerWidth": 1920, "innerHeight": 994, "screenX": 0, "screenY": 0}}

    def launch_options(self, **kw):
        if self.fail_launch_options:
            self.fail_launch_options -= 1
            raise RuntimeError("launch_options broke")
        self.launches += 1
        fp = kw.get("fingerprint") or {}
        w = fp.get("window", {})
        config = dict(kw.get("config") or {})
        config.update({"screen.availWidth": 1920, "screen.availHeight": 1053,
                       "window.outerWidth": min(w.get("outerWidth", 1920), 1920),
                       "window.outerHeight": min(w.get("outerHeight", 1080), 1053),
                       "window.screenX": 0, "window.screenY": 0})
        config.setdefault("navigator.hardwareConcurrency", 32)
        config.setdefault("navigator.platform", "Linux x86_64")
        config.setdefault("navigator.oscpu", "Linux x86_64")
        return {"executable_path": "/opt/camoufox/camoufox-bin", "args": [], "env": {"CAMOU_CONFIG_1": json.dumps(config)},
                "firefox_user_prefs": dict(kw.get("firefox_user_prefs") or {}), "headless": False}

    @staticmethod
    def to_camel(d):
        return d


class FakeServe:
    """serve.js as the manager sees it."""

    def __init__(self, world):
        self.world = world
        self.on_exit = None
        self.closing = False
        self.proc = None  # the asyncio process (its returncode is logged on an exit)
        self.started = False
        self.killed = False
        self.closed = False
        self.options = None
        self.listening = None
        self.requests = []
        world.serves.append(self)

    @property
    def firefox_pid(self):
        return (self.listening or {}).get("pid")

    def alive(self):
        return self.started and not self.killed and not self.closed

    async def start(self, options, timeout=150.0):
        self.options = options
        if self.world.fail_starts:
            self.world.fail_starts -= 1
            raise ServeError("the browser did not start within 150 s")
        self.started = True
        self.listening = {"event": "listening", "port": 40000 + len(self.world.serves), "wsPath": "/" + "a" * 32,
                          "pid": 1000 + len(self.world.serves), "version": OWN_VERSION}
        return self.listening

    async def request(self, cmd, args=None, timeout=10.0):
        if not self.alive():
            raise ServeError("the browser server is not running")
        self.requests.append((cmd, args))
        if cmd == "ping":
            if self.world.hang:
                raise asyncio.TimeoutError()
            return {"cookies": len(self.world.jar)}
        if cmd == "cookies.get":
            return list(self.world.jar)
        if cmd == "cookies.add":
            got = (args or {}).get("cookies") or []
            self.world.jar.extend(got)
            return {"added": len(got), "dropped": 0}
        if cmd == "contexts.prune":
            return {"closed": 0, "open": 1}
        raise ServeError("unknown command " + cmd)

    async def close(self, timeout=15.0):
        self.closing = True
        self.closed = True
        return True

    def kill(self):
        self.closing = True
        self.killed = True

    async def wait(self, timeout=10.0):
        return None

    def exit(self):
        """serve.js went away on its own (Firefox crashed, contexts[0] closed)."""
        self.killed = True
        if self.on_exit:
            self.on_exit(self)


class World:
    def __init__(self, tmp_path: Path, cpu_max="200000 100000"):
        self.tmp = tmp_path
        self.serves = []
        self.fail_starts = 0
        self.hang = False
        self.jar = []
        self.profile = tmp_path / "profiles" / "default"
        self.camoufox = tmp_path / "camoufox"
        self.camoufox.mkdir()
        (self.camoufox / "application.ini").write_text(f"[App]\nVersion={OWN_VERSION}\nBuildID=20261003194815\n")
        (tmp_path / "proc").mkdir()
        cg = tmp_path / "cgroup"
        cg.mkdir()
        if cpu_max:
            (cg / "cpu.max").write_text(cpu_max + "\n")
        self.lib = FakeLib()
        self.proxy = AutomationProxy(0, "/playwright/default")
        self.m = CamoufoxManager(self.proxy, profile_dir=self.profile, camoufox_dir=self.camoufox, lib=self.lib,
                                 serve_factory=lambda: FakeServe(self), settings_fn=lambda: Settings.from_env({}),
                                 proc_root=tmp_path / "proc", cgroup_root=cg)

    @property
    def serve(self):
        return self.serves[-1]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


async def test_a_launch_runs_and_says_ok(world):
    m = world.m
    assert m.health() == (503, {"status": "down"})
    assert await m.start()
    assert m.state == "running" and m.health() == (200, {"status": "ok"})
    v = m.version()
    assert v["engine"] == "camoufox" and v["browserVersion"] == OWN_VERSION and v["browserMajor"] == 156
    assert v["pid"] == world.serve.firefox_pid and v["paused"] is False
    # the proxy points at the live server
    assert world.proxy._target == (world.serve.listening["port"], world.serve.listening["wsPath"])
    # the launch options: the profile, the context defaults off, the strict file origin
    opts = world.serve.options
    assert opts["_userDataDir"] == str(world.profile) and opts["noDefaultViewport"] is True
    assert opts["firefox_user_prefs"]["security.fileuri.strict_origin_policy"] is True
    # the identity: the core count fitted to the 2-CPU limit, the window to the frame
    ident = identity.load(world.profile)
    assert ident["pinned"]["navigator.hardwareConcurrency"] == 4
    assert ident["fingerprint"]["window"]["outerWidth"] == 1920 - 10
    assert ident["fingerprint"]["window"]["outerHeight"] == 1053 - 34


async def test_no_cpu_limit_keeps_what_camoufox_resolved(tmp_path):
    w = World(tmp_path, cpu_max="max 100000")
    assert await w.m.start()
    assert identity.load(w.profile)["pinned"]["navigator.hardwareConcurrency"] == 32


async def test_an_unanswered_ping_says_restarting_then_the_watchdog_relaunches(world, monkeypatch):
    m = world.m
    await m.start()
    first = world.serve
    world.hang = True
    await m.tick()
    assert m.misses == 1
    # liveness reads 200 meanwhile (3 x 10 s would kill the container mid-relaunch)
    assert m.health() == (200, {"status": "restarting"})
    await m.tick()
    assert m.health() == (200, {"status": "restarting"}) and len(world.serves) == 1
    await m.tick()  # the third miss
    assert first.killed and len(world.serves) == 2 and world.serve.alive()
    assert m.relaunches == 1 and m.misses == 0 and m.state == "running"
    world.hang = False
    assert m.health() == (200, {"status": "ok"})


async def test_an_answer_resets_the_misses(world):
    m = world.m
    await m.start()
    world.hang = True
    await m.tick()
    world.hang = False
    await m.tick()
    assert m.misses == 0 and m.health() == (200, {"status": "ok"}) and len(world.serves) == 1


async def test_a_ping_waiting_too_long_already_says_restarting(world):
    m = world.m
    await m.start()
    m._ping_since = time.monotonic() - manager_mod.PING_OVERDUE - 0.1
    assert m.health() == (200, {"status": "restarting"})
    m._ping_since = time.monotonic()
    assert m.health() == (200, {"status": "ok"})


async def test_a_browser_that_went_away_is_relaunched_at_once(world):
    m = world.m
    await m.start()
    first = world.serve
    first.exit()
    await m._exit_task
    assert len(world.serves) == 2 and m.running() and m.relaunches == 1
    # an exit the manager asked for is not a crash
    world.serve.closing = True
    world.serve.exit()
    assert m._exit_task.done()


async def test_restarting_is_bounded_then_503(world, monkeypatch):
    m = world.m
    world.fail_starts = 1
    assert not await m.start()
    assert m.error == "launch_failed"
    assert m.health() == (200, {"status": "restarting"})  # within RESTART_GRACE
    m.changing_since -= manager_mod.RESTART_GRACE + 1
    assert m.health() == (503, {"status": "down", "error": "launch_failed"})
    await m.tick()  # retried
    assert m.running() and m.health() == (200, {"status": "ok"})


async def test_a_chrome_profile_is_never_opened(world):
    m = world.m
    world.profile.mkdir(parents=True)
    (world.profile / "Local State").write_text("{}")
    assert not await m.start()
    status, body = m.health()
    assert status == 503 and body["error"] == "profile_engine"
    await m.tick()
    assert world.serves == []  # retried each round, never launched over it
    assert (world.profile / "Local State").exists()  # nothing deleted
    (world.profile / "Local State").unlink()
    await m.tick()
    assert m.running()


def newer_profile(world, version="157.0-beta.1", build="20261101000000"):
    world.profile.mkdir(parents=True, exist_ok=True)
    (world.profile / "compatibility.ini").write_text(f"[Compatibility]\nLastVersion={version}_{build}/{build}\n")


async def test_a_newer_major_waits_for_a_forced_import(world):
    m = world.m
    newer_profile(world)
    assert not await m.start()
    status, body = m.health()
    assert status == 503 and body["error"] == "profile_newer" and "newer Camoufox" in body["message"]
    await m.tick()
    assert world.serves == []
    (world.profile / profile_guard.DOWNGRADE_MARKER).write_text("157.0-beta.1\n")
    await m.tick()
    assert m.running() and "-allow-downgrade" in world.serve.options["args"]
    assert not (world.profile / profile_guard.DOWNGRADE_MARKER).exists()  # used once the browser is up


@pytest.mark.parametrize("where", ["launch_options", "start"])
async def test_the_downgrade_marker_survives_a_failed_launch(world, where):
    m = world.m
    newer_profile(world)
    (world.profile / profile_guard.DOWNGRADE_MARKER).write_text("157.0-beta.1\n")
    if where == "start":
        world.fail_starts = 1
    else:
        world.lib.fail_launch_options = 1
    assert not await m.start()
    assert m.error == "launch_failed"
    assert (world.profile / profile_guard.DOWNGRADE_MARKER).exists()
    await m.tick()  # the retry still opens the imported profile
    assert m.running() and "-allow-downgrade" in world.serve.options["args"]
    assert not (world.profile / profile_guard.DOWNGRADE_MARKER).exists()


async def test_pause_saves_session_cookies_and_resume_brings_them_back(world):
    m = world.m
    await m.start()
    world.jar[:] = [{"name": "s", "value": "1", "domain": "shop.test", "path": "/", "expires": -1},
                    {"name": "p", "value": "2", "domain": "shop.test", "path": "/", "expires": 2_000_000_000}]
    await m.pause(30)
    assert m.paused() and m.health() == (200, {"status": "paused"}) and world.serve.closed
    saved = session_cookies.read(world.profile)
    assert [c["name"] for c in saved] == ["s"]
    with pytest.raises(RuntimeError):
        await m.restart()  # paused: the keeper resumes it
    await m.tick()  # within the bound: stays paused
    assert m.paused()
    world.jar.clear()  # Firefox drops session cookies over a restart
    assert await m.resume()
    assert m.running() and len(world.serves) == 2
    adds = [a for c, a in world.serve.requests if c == "cookies.add"]
    assert adds and adds[0]["skipExisting"] is True and adds[0]["cookies"][0]["name"] == "s"
    with pytest.raises(ValueError):
        await m.pause(0)
    with pytest.raises(ValueError):
        await m.pause(manager_mod.MAX_PAUSE_SECONDS + 1)


async def test_a_pause_runs_out_its_bound(world):
    m = world.m
    await m.start()
    await m.pause(30)
    m.paused_until -= 31
    await m.tick()
    assert m.running() and not m.paused()


async def test_shutdown_saves_and_closes(world):
    m = world.m
    await m.start()
    world.jar[:] = [{"name": "s", "value": "1", "domain": "shop.test", "path": "/", "expires": -1}]
    await m.shutdown()
    assert m.state == "stopped" and world.serve.closed
    assert session_cookies.read(world.profile)[0]["name"] == "s"
    await m.tick()
    assert len(world.serves) == 1  # stopped stays stopped


async def test_cookies_need_a_running_browser(world):
    with pytest.raises(ServeError):
        await world.m.get_cookies()
    await world.m.start()
    assert (await world.m.add_cookies([{"name": "a", "value": "1", "domain": "x", "path": "/"}]))["added"] == 1
    assert (await world.m.get_cookies())[0]["name"] == "a"


def test_session_cookies_file(tmp_path):
    rows = [{"name": "s", "value": "1", "domain": "x", "path": "/", "expires": -1, "partitionKey": "k"},
            {"name": "p", "value": "2", "domain": "x", "path": "/", "expires": 2_000_000_000},
            {"name": "", "value": "3", "domain": "x", "expires": -1},
            "junk"]
    only = session_cookies.session_only(rows)
    assert only == [{"name": "s", "value": "1", "domain": "x", "path": "/", "expires": -1}]
    assert session_cookies.write(tmp_path, only)
    assert oct(os.stat(session_cookies.file_path(tmp_path)).st_mode & 0o777) == "0o600"
    assert not session_cookies.write(tmp_path, only)  # unchanged: not rewritten
    assert session_cookies.read(tmp_path) == only
    assert session_cookies.write(tmp_path, [])  # none left: the file goes
    assert not session_cookies.file_path(tmp_path).exists() and session_cookies.read(tmp_path) == []
    session_cookies.file_path(tmp_path).write_text("{not json")
    assert session_cookies.read(tmp_path) == []
