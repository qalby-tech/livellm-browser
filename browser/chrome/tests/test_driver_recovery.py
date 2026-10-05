"""
Driver-death detection and recovery.

The Patchright Node driver is a single point of failure: an OOM kill takes
every playwright object with it. These tests fake the private process chain
(no real Chrome needed) and check that the manager sees the death, recovers
in-place, and never drops a browser from the registry on a failed relaunch.

Run with: uv run pytest tests/ -v
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.local_browser as local_browser
from core.local_browser import (
    LocalBrowserManager, LocalBrowserInfo, kill_stray_chrome,
)


def fake_playwright(returncode=None):
    """A playwright lookalike exposing the private driver-process chain."""
    proc = SimpleNamespace(
        pid=4242, returncode=returncode, kill=MagicMock(), wait=AsyncMock()
    )
    pw = SimpleNamespace(
        _impl_obj=SimpleNamespace(
            _connection=SimpleNamespace(_transport=SimpleNamespace(_proc=proc))
        ),
        stop=AsyncMock(),
    )
    return pw, proc


def make_info(profile_path=None, proxy_port=9222):
    return LocalBrowserInfo(
        browser=SimpleNamespace(close=AsyncMock()),
        context=SimpleNamespace(close=AsyncMock()),
        proxy_port=proxy_port,
        ws_endpoint="/devtools/browser/x",
        cdp_proxy=SimpleNamespace(stop=AsyncMock(), retarget=MagicMock()),
        profile_path=profile_path,
        is_ephemeral=profile_path is None,
        proxy_config=None,
    )


# ── driver_alive ──

def test_driver_alive_reflects_process_state():
    m = LocalBrowserManager()
    assert m.driver_alive() is False  # not started yet

    pw, proc = fake_playwright()
    m.playwright = pw
    assert m.driver_alive() is True

    proc.returncode = -9  # OOM kill
    assert m.driver_alive() is False


def test_driver_alive_assumes_alive_when_chain_is_gone():
    # If a patchright upgrade moves the private attributes, a False here would
    # 503 /health forever and crash-loop the pod — so it must assume alive.
    m = LocalBrowserManager()
    m.playwright = SimpleNamespace()  # no private guts at all
    assert m.driver_alive() is True


# ── _stop_driver ──

async def test_stop_driver_graceful_stop_skips_the_kill():
    m = LocalBrowserManager()
    pw, proc = fake_playwright()

    async def stop():
        proc.returncode = 0

    pw.stop = AsyncMock(side_effect=stop)
    m.playwright = pw

    await m._stop_driver()

    proc.kill.assert_not_called()
    assert m.playwright is None


async def test_stop_driver_kills_a_hung_driver():
    m = LocalBrowserManager()
    pw, proc = fake_playwright()
    pw.stop = AsyncMock(side_effect=RuntimeError("driver hung"))
    m.playwright = pw

    await m._stop_driver(timeout=0.1)

    proc.kill.assert_called_once()
    assert m.playwright is None


# ── recover_driver ──

@pytest.fixture
def recovery_env(monkeypatch):
    """Fresh driver + swept-orphans instrumentation for recover_driver tests."""
    new_pw, _ = fake_playwright()
    starter = SimpleNamespace(start=AsyncMock(return_value=new_pw))
    monkeypatch.setattr(local_browser, "async_playwright", lambda: starter)
    swept = []
    monkeypatch.setattr(local_browser, "kill_stray_chrome", lambda: swept.append(True))
    return new_pw, starter, swept


async def test_recover_driver_replaces_driver_and_relaunches_profiles(
    recovery_env, monkeypatch, tmp_path
):
    new_pw, _, swept = recovery_env
    m = LocalBrowserManager()
    dead_pw, _ = fake_playwright(returncode=-9)
    m.playwright = dead_pw

    persistent = make_info(profile_path=tmp_path / "default")
    ephemeral = make_info(profile_path=None, proxy_port=40001)
    m.browsers = {"default": persistent, "eph": ephemeral}

    relaunched = {}

    async def fake_launch(browser_id, profile_path, proxy_config, cdp_proxy, proxy_port, is_ephemeral):
        relaunched[browser_id] = (profile_path, cdp_proxy, proxy_port)
        return "NEW_INFO"

    monkeypatch.setattr(m, "_launch_chrome_for", fake_launch)

    await m.recover_driver()

    assert m.playwright is new_pw
    assert swept  # orphaned Chrome was killed before the relaunch
    # persistent browser relaunched onto the SAME proxy and public port
    assert relaunched["default"] == (persistent.profile_path, persistent.cdp_proxy, 9222)
    assert m.browsers["default"] == "NEW_INFO"
    # ephemeral browser has no profile to relaunch from — dropped, proxy stopped
    assert "eph" not in m.browsers
    ephemeral.cdp_proxy.stop.assert_awaited()


async def test_recover_driver_skips_when_alive_unless_forced(recovery_env):
    new_pw, starter, _ = recovery_env
    m = LocalBrowserManager()
    alive_pw, _ = fake_playwright()
    m.playwright = alive_pw

    await m.recover_driver()
    assert m.playwright is alive_pw  # healthy driver untouched
    starter.start.assert_not_awaited()

    await m.recover_driver(force=True)
    assert m.playwright is new_pw  # forced: replaced even though it looked alive


async def test_recover_driver_keeps_browser_registered_when_relaunch_fails(
    recovery_env, monkeypatch, tmp_path
):
    m = LocalBrowserManager()
    m.playwright = fake_playwright(returncode=-9)[0]
    persistent = make_info(profile_path=tmp_path / "default")
    m.browsers = {"default": persistent}

    async def fail_launch(*args, **kwargs):
        raise RuntimeError("chrome would not start")

    monkeypatch.setattr(m, "_launch_chrome_for", fail_launch)

    await m.recover_driver()  # must not raise

    assert m.browsers["default"] is persistent  # still there for the next retry
    # The dead-driver Browser is dropped from the entry: its is_connected() is
    # a stale local True that would make the watchdog think all is well.
    assert persistent.browser is None


async def test_recover_driver_skips_browsers_already_on_the_new_driver(
    recovery_env, monkeypatch, tmp_path
):
    # An API restart can win the race and rebuild a browser on the new driver
    # before the recovery loop reaches it — recovering over it would leak the
    # freshly-launched Chrome.
    new_pw, _, _ = recovery_env
    m = LocalBrowserManager()
    m.playwright = fake_playwright(returncode=-9)[0]
    rebuilt = make_info(profile_path=tmp_path / "default")
    rebuilt.browser = SimpleNamespace(
        _impl_obj=SimpleNamespace(_connection=new_pw._impl_obj._connection)
    )
    m.browsers = {"default": rebuilt}

    launched = []

    async def fake_launch(*args, **kwargs):
        launched.append(args)
        return "NEW_INFO"

    monkeypatch.setattr(m, "_launch_chrome_for", fake_launch)

    await m.recover_driver()

    assert not launched  # no second Chrome on the same profile
    assert m.browsers["default"] is rebuilt


# ── restart_browser ──


async def test_restart_aborts_when_close_times_out(tmp_path):
    # A close TIMEOUT (vs an instant dead-driver raise) means Chrome may still
    # be running — the restart must bail out before touching the profile or
    # launching a second Chrome over it.
    m = LocalBrowserManager()
    m.close_timeout = 0.05
    m.playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=AsyncMock())
    )
    profile = tmp_path / "default"
    profile.mkdir()
    old = make_info(profile_path=profile)

    async def hang():
        await asyncio.sleep(30)

    old.context.close = hang
    m.browsers = {"default": old}

    with pytest.raises(asyncio.TimeoutError):
        await m.restart_browser("default")

    assert m.browsers["default"] is old
    m.playwright.chromium.launch_persistent_context.assert_not_called()
    assert not m.restarting("default")

async def test_failed_restart_keeps_browser_registered(tmp_path):
    m = LocalBrowserManager()
    m.playwright = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(side_effect=RuntimeError("boom"))
        )
    )
    profile = tmp_path / "default"
    profile.mkdir()
    old = make_info(profile_path=profile)
    m.browsers = {"default": old}

    with pytest.raises(RuntimeError):
        await m.restart_browser("default")

    # the entry survives the failed relaunch — the watchdog can retry
    assert m.browsers["default"] is old
    assert not m.restarting("default")  # and the lock was released


# ── kill_stray_chrome ──

def test_kill_stray_chrome_targets_only_chrome(tmp_path, monkeypatch):
    def proc_entry(pid, argv):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(argv)

    proc_entry(100, b"/opt/google/chrome/chrome\x00--type=browser\x00")
    proc_entry(200, b"/usr/bin/python3\x00launch.py\x00")
    proc_entry(300, b"/opt/google/chrome/chrome_crashpad_handler\x00")
    (tmp_path / "self").mkdir()  # non-numeric /proc entries are skipped

    killed = []
    monkeypatch.setattr(local_browser.os, "kill", lambda pid, sig: killed.append(pid))

    kill_stray_chrome(proc_root=tmp_path)

    assert sorted(killed) == [100, 300]


# ── /health ──

def test_health_is_503_while_the_driver_is_down():
    from fastapi.testclient import TestClient
    import launch

    # TestClient without a `with` block skips lifespan — no real Chrome here.
    client = TestClient(launch.app)
    assert launch.local_browser_manager.playwright is None  # driver "not running"

    resp = client.get("/health")

    assert resp.status_code == 503
    assert "driver" in resp.text.lower()
