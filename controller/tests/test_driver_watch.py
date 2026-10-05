"""A driver whose process exits is restarted at once, alone; a Camoufox
connection on it is closed as its pipe closing would; a bounded call returns
at its deadline whatever the cancelled call does."""
import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import browser as browser_mod
from core.browser import BrowserInfo, BrowserManager


class FakeDriver(SimpleNamespace):
    pass


async def _real_proc():
    return await asyncio.create_subprocess_exec("sleep", "60")


async def test_bounded_returns_even_when_the_cancelled_call_never_ends():
    release = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()  # as a Playwright call whose abort waits on a dead pipe

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await browser_mod.bounded(stubborn(), 0.2)
    assert loop.time() - t0 < 1.0
    release.set()
    await asyncio.sleep(0)


async def test_bounded_passes_results_and_errors():
    async def ok():
        return 7

    async def bad():
        raise ValueError("x")

    assert await browser_mod.bounded(ok(), 1) == 7
    with pytest.raises(ValueError):
        await browser_mod.bounded(bad(), 1)


def test_fail_remote_closes_a_pipe_connection_only():
    class JsonPipeTransport:
        def __init__(self):
            self.emitted = []
            self._stopped_future = asyncio.get_event_loop_policy().new_event_loop().create_future()

        def emit(self, event, reason):
            self.emitted.append((event, reason))

    class PipeTransport(JsonPipeTransport):
        pass

    def conn(transport):
        return SimpleNamespace(_impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=transport)))

    pipe, root = JsonPipeTransport(), PipeTransport()
    BrowserManager._fail_remote(conn(pipe), "gone")
    BrowserManager._fail_remote(conn(root), "gone")
    BrowserManager._fail_remote(object(), "gone")  # no private layout: nothing happens
    assert pipe.emitted == [("close", "gone")] and pipe._stopped_future.done()
    assert root.emitted == [] and not root._stopped_future.done()


async def test_a_dead_driver_is_restarted_alone(monkeypatch):
    m = BrowserManager()
    procs = {"chrome": await _real_proc(), "camoufox": await _real_proc()}
    monkeypatch.setattr(m, "_driver_proc", lambda engine: procs.get(engine) if engine in m.drivers else None)
    new = FakeDriver(name="camoufox-2", stop=AsyncMock(), firefox=SimpleNamespace(connect=AsyncMock()))
    starts = []

    class Starter:
        def __init__(self, engine):
            self.engine = engine

        async def start(self):
            starts.append(self.engine)
            procs[self.engine] = await _real_proc()
            return new

    chrome = FakeDriver(name="chrome-1", stop=AsyncMock())
    old_cf = FakeDriver(name="camoufox-1", stop=AsyncMock())
    await m.start(chrome, starter=Starter)
    m.drivers["camoufox"] = old_cf
    m._track_driver("camoufox")
    chrome_conn = SimpleNamespace(is_connected=lambda: True, contexts=[object()])
    m.browsers["ch"] = BrowserInfo(chrome_conn, object(), ws_url="ws://ch", browser_id="ch", engine="chrome")
    cf_conn = SimpleNamespace(is_connected=lambda: True, contexts=[object()])
    new.firefox.connect.return_value = cf_conn
    m.browsers["cf"] = BrowserInfo(object(), object(), ws_url="ws://cf", browser_id="cf", engine="camoufox")

    procs["camoufox"].send_signal(signal.SIGKILL)
    for _ in range(50):
        await asyncio.sleep(0.1)
        if m.drivers.get("camoufox") is new and "cf" in m.browsers and m.browsers["cf"].browser is cf_conn:
            break
    assert starts == ["camoufox"]
    assert m.drivers["camoufox"] is new and m.drivers["chrome"] is chrome
    new.firefox.connect.assert_awaited_once_with("ws://cf", headers=None)
    assert m.browsers["ch"].browser is chrome_conn  # Chrome untouched
    # a stop on purpose is not restarted
    await m.stop_drivers()
    procs["camoufox"].send_signal(signal.SIGKILL)
    procs["chrome"].send_signal(signal.SIGKILL)
    await asyncio.sleep(0.3)
    assert starts == ["camoufox"]
    for p in procs.values():
        await p.wait()
