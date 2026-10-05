import asyncio
import asyncio.subprocess
import functools
import logging
import os
import signal
import time
from typing import Iterable, Optional

from core import pw
from core.pw import Playwright, Browser, BrowserContext, Page

from core.config import settings
from core.registry import browser_registry

logger = logging.getLogger(__name__)

# Soft cap: a browser with this many open tabs is picked only when every
# other one is as busy. Never a refusal.
MAX_PAGES_PER_BROWSER = settings.max_pages_per_browser

# How long a browser that could not be reached stays out of the pick.
UNHEALTHY_SECONDS = 30.0

# Closing tabs over a connection whose browser is gone can stall (a deleted
# pod's address answers nothing, so the socket never closes). The close runs
# in the background, bounded by this; no call ever waits for it.
DISCONNECT_TIMEOUT = 3.0

# A CDP connect that gets no answer gives up after this.
CONNECT_TIMEOUT = 5.0

# Opening a tab on a connection that still reads as connected but whose
# browser is gone never returns on its own. After this the connection is
# dropped and the browser marked unhealthy.
TAB_TIMEOUT = 5.0

# How long a call that names no browser waits for the one it picked to
# connect before it tries the next. The connect itself goes on, and the
# browser comes last in the pick until it is up.
PICK_CONNECT_WAIT = 3.0


async def bounded(coro, timeout: float):
    """``coro``'s result, or asyncio.TimeoutError after ``timeout`` seconds.

    Unlike asyncio.wait_for, the caller never waits for the cancelled call to
    finish: a Playwright call on a connection whose other end is gone may not
    end on cancellation either (its abort waits on the same connection).
    """
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not done:
        task.cancel()
        task.add_done_callback(_retrieve)
        raise asyncio.TimeoutError()
    return task.result()


def _retrieve(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


class BrowserInfo:
    """Container for a connected browser, its default context, and active pages."""

    def __init__(self, browser: Browser, context: BrowserContext, ws_url: str = "", browser_id: str = "", headers: Optional[dict] = None, engine: str = pw.CHROME):
        self.browser = browser
        self.context = context
        self.ws_url = ws_url
        self.browser_id = browser_id
        # "chrome" (a CDP connection) or "camoufox" (a Playwright one).
        self.engine = engine
        # Optional auth headers sent on CDP connect (BYO/remote browsers).
        self.headers = headers or {}
        # Tabs of the sessions that live on this browser, by session id.
        # Emptied when the connection is rebuilt; the session itself stays
        # on this browser (BrowserManager.sessions) and gets a new tab.
        self.pages: dict[str, Page] = {}


class BrowserManager:
    """
    Agnostic browser manager — connects to browsers purely by their addresses.

    The manager does NOT know about launchers, profiles, or orchestration.
    External systems (operator, API calls) register browsers by providing a
    ``browser_id`` and a ``ws_url``. A browser's engine (its registry entry's)
    picks the client: Chrome over CDP, Camoufox through its Playwright server.
    Each engine's client has its own Node driver (``drivers``): Chrome's from
    ``start``, Camoufox's from the first Camoufox browser; a dead driver is
    restarted alone and only its engine's browsers reconnect.
    """

    def __init__(self):
        # engine -> the Node driver of its client (core.pw).
        self.drivers: dict[str, Playwright] = {}
        self._driver_pids: dict[str, int] = {}
        # engine -> held while that driver starts or restarts. Made on first use.
        self._driver_locks: dict[str, asyncio.Lock] = {}
        # engine -> a client to .start() (core.pw.async_playwright); set by start().
        self._starter = pw.async_playwright
        self._started = False
        # Set while the process ends: a driver that exits then is not restarted.
        self._stopping = False
        # engine -> the task that waits for that driver's process to exit.
        self._watchers: dict[str, asyncio.Task] = {}
        self.browsers: dict[str, BrowserInfo] = {}
        # session id -> browser id. Kept apart from BrowserInfo.pages so a
        # session outlives a reconnect of its browser: it stays there and
        # gets a new tab. Dropped on end_session and when the browser leaves.
        self.sessions: dict[str, str] = {}
        # session id -> monotonic time of its last call (per-session proxy
        # rotation counts only recently used sessions).
        self.session_used: dict[str, float] = {}
        # browser id -> True while its launcher says it is paused (set by
        # core.keeper_hint before a pick; read here with no await).
        self.paused: dict[str, bool] = {}
        # Calls running on each browser, counted from the moment the browser
        # is chosen until the response is sent (see pick_browser/end_call).
        self._in_flight: dict[str, int] = {}
        # browser id -> monotonic deadline; a browser that failed to connect
        # is skipped by pick_browser until then.
        self._unhealthy_until: dict[str, float] = {}
        # browser id -> the connect running for it, and when it started. Every
        # call that finds the browser down waits on that one attempt, so calls
        # arriving together open one connection, and a slow browser never
        # holds up another.
        self._connecting: dict[str, asyncio.Future] = {}
        self._connect_started: dict[str, float] = {}
        self._rr = 0
        # Closes of dropped connections still running in the background.
        self._closing: set = set()

    async def start(self, playwright: Playwright, starter=None):
        """Begin with Chrome's driver, started by the caller. ``starter``
        (engine -> a client to .start(); default core.pw.async_playwright)
        starts another engine's driver at its first browser, and restarts
        any. No auto-connections."""
        if starter is not None:
            self._starter = starter
        self.drivers = {pw.CHROME: playwright}
        self._started = True
        self._track_driver(pw.CHROME)
        logger.info("Browser manager started (agnostic mode — waiting for registrations)")

    def _lock(self, engine: str) -> asyncio.Lock:
        lock = self._driver_locks.get(engine)
        if lock is None:
            lock = self._driver_locks[engine] = asyncio.Lock()
        return lock

    async def _driver(self, engine: str) -> Playwright:
        """The engine's Node driver, started now when it has none yet (the
        first Camoufox browser starts Camoufox's)."""
        driver = self.drivers.get(engine)
        if driver is not None:
            return driver
        async with self._lock(engine):
            driver = self.drivers.get(engine)
            if driver is None:
                driver = await self._start_driver(engine)
        return driver

    async def _start_driver(self, engine: str) -> Playwright:
        driver = await self._starter(engine).start()
        self.drivers[engine] = driver
        self._track_driver(engine)
        logger.info(f"Playwright driver for {engine} started")
        return driver

    async def stop_drivers(self, timeout: float = 5.0) -> None:
        """Stop every driver (the end of the process), each bounded."""
        self._stopping = True
        for task in list(self._watchers.values()):
            task.cancel()
        for engine, driver in list(self.drivers.items()):
            try:
                await bounded(driver.stop(), timeout)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout stopping the {engine} Playwright driver, continuing shutdown")
            except Exception as e:
                logger.warning(f"Error stopping the {engine} Playwright driver: {e}")
        self.drivers.clear()

    def _driver_proc(self, engine: str):
        """The Node driver subprocess behind an engine's pipe transport, or None.

        Reaches through private attributes (impl connection -> pipe transport),
        so every step is guarded — a layout change just disables the feature.
        """
        try:
            return self.drivers[engine]._impl_obj._connection._transport._proc
        except (AttributeError, KeyError):
            return None

    def _track_driver(self, engine: str):
        proc = self._driver_proc(engine)
        if proc is not None:
            self._driver_pids[engine] = proc.pid
            logger.info(f"Tracking the {engine} Playwright driver PID: {proc.pid}")
            if isinstance(proc, asyncio.subprocess.Process):  # never a test double
                # An older watcher (its driver replaced) ends by itself; it may
                # be the task running this very restart, so it is not cancelled.
                self._watchers[engine] = asyncio.ensure_future(
                    self._watch_driver(engine, self.drivers[engine], proc)
                )

    async def _watch_driver(self, engine: str, driver: Playwright, proc) -> None:
        """When a driver's process exits on its own, close its engine's
        connections at once and restart it, reconnecting only that engine's
        browsers (the other engine's driver and browsers go on)."""
        try:
            await proc.wait()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if self._stopping or self.drivers.get(engine) is not driver:
            return  # stopped on purpose, or already replaced by a restart
        logger.warning(f"The {engine} Playwright driver exited ({proc.returncode}); restarting it")
        try:  # its pipe's error, which nothing else reads now
            driver._impl_obj._connection._transport.on_error_future.add_done_callback(_retrieve)
        except AttributeError:
            pass
        infos = [i for i in self.browsers.values() if i.engine == engine]
        for info in infos:
            self._fail_remote(info.browser, f"The {engine} Playwright driver exited")
        async with self._lock(engine):
            if self._stopping or self.drivers.get(engine) is not driver:
                return
            try:
                await self._restart_driver(engine)
            except Exception as e:
                logger.error(f"Restarting the {engine} Playwright driver failed: {type(e).__name__}: {e}")

    @staticmethod
    def _fail_remote(browser: Browser, reason: str) -> None:
        """Close a Camoufox connection as its pipe closing would.

        A firefox.connect connection is a pipe inside the local driver; when
        that driver dies the pipe never reports closed (Playwright 1.62), so
        every call on it, and its abort, would wait forever. Emitting the
        pipe's close rejects them, closes the browser's contexts and pages,
        and ends the connection. Reaches through private attributes, so every
        step is guarded. A CDP connection (Chrome) fails on its own.
        """
        try:
            transport = browser._impl_obj._connection._transport
        except AttributeError:
            return
        if type(transport).__name__ != "JsonPipeTransport":
            return
        try:
            transport.emit("close", reason)
        except Exception as e:
            logger.warning(f"Closing a connection of a dead driver: {type(e).__name__}: {e}")
        stopped = getattr(transport, "_stopped_future", None)
        if stopped is not None and not stopped.done():
            stopped.set_result(None)

    def driver_alive(self, engine: str = pw.CHROME) -> bool:
        """Whether an engine's Node driver process is still running (False
        for one not started).

        Returns True when the process can't be introspected (private layout
        changed) so a false negative can never crash-loop the pod.
        """
        if engine not in self.drivers:
            return False
        proc = self._driver_proc(engine)
        if proc is None:
            return True
        # returncode is None while running, an int once exited. Only a
        # definite int counts as dead (doubles/mocks stay "alive").
        return not isinstance(proc.returncode, int)

    def dead_drivers(self) -> list:
        """Chrome's driver when it is not running, and any other started one
        that died (for /healthz). A driver never started is not dead."""
        return [e for e in pw.ENGINES
                if (e == pw.CHROME or e in self.drivers) and not self.driver_alive(e)]

    def _kill_driver(self, engine: str):
        pid = self._driver_pids.pop(engine, None)
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning(f"Force-killed the old {engine} Playwright driver PID {pid}")
            except (ProcessLookupError, PermissionError):
                pass

    # ── connect / disconnect ─────────────────────────────────

    async def connect_browser(self, browser_id: str, ws_url: str, headers: Optional[dict] = None) -> BrowserInfo:
        """
        Connect to a browser (Chrome over CDP, Camoufox through its
        Playwright server, by its registry entry's engine).

        If ``browser_id`` is already connected **with the same URL** and the
        connection is still alive, the existing connection is returned.
        If the connection is dead (e.g. browser restarted), it auto-reconnects.
        If the URL differs, the old connection is dropped and a new one opened.

        ``headers`` are optional HTTP headers sent on the CDP connect, used for
        BYO/remote browsers that require auth (e.g. an Authorization bearer).
        """
        if not self._started:
            raise RuntimeError("Browser manager not started")

        if browser_id in self.browsers:
            existing = self.browsers[browser_id]
            if existing.ws_url == ws_url and existing.browser.is_connected():
                logger.info(f"Browser '{browser_id}' already connected (idempotent)")
                return existing
            if existing.ws_url != ws_url:
                logger.info(
                    f"Browser '{browser_id}' ws_url changed "
                    f"({existing.ws_url} -> {ws_url}), reconnecting"
                )
            else:
                logger.info(f"Browser '{browser_id}' connection is dead, reconnecting...")
            await self.disconnect_browser(browser_id)

        logger.info(f"Connecting to browser '{browser_id}' via {ws_url}")
        engine = self.engine_of(browser_id)
        browser = await self._open(ws_url, headers, engine)
        current = self.browsers.get(browser_id)
        if current is not None and current.ws_url == ws_url and self._alive(current):
            # Another call connected it while this one waited: keep that
            # connection and let this one go, or it would stay open for good.
            try:
                await browser.close()
            except Exception:
                pass
            return current
        context = await self._default_context(browser, engine)

        info = BrowserInfo(browser, context, ws_url=ws_url, browser_id=browser_id, headers=headers or {}, engine=engine)
        self.browsers[browser_id] = info
        self.mark_healthy(browser_id)
        logger.info(f"Connected to browser '{browser_id}'")
        return info

    @staticmethod
    def engine_of(browser_id: str) -> str:
        """The engine of a browser: its registry entry's, else Chrome (an
        entry that names none, and a browser connected through POST /browsers)."""
        return browser_registry.get_browser_engine(browser_id) or pw.CHROME

    @staticmethod
    def _connect_call(driver: Playwright, ws_url: str, headers: Optional[dict], engine: str):
        """The connect for a browser's engine on its driver: CDP for Chrome,
        the browser's own Playwright server for Camoufox (Firefox)."""
        if engine == pw.CAMOUFOX:
            return driver.firefox.connect(ws_url, headers=headers or None)
        return driver.chromium.connect_over_cdp(ws_url, headers=headers or None)

    async def _open(self, ws_url: str, headers: Optional[dict], engine: str = pw.CHROME):
        """One connect on the engine's driver (started first when it has none
        yet), given up after CONNECT_TIMEOUT."""
        driver = await self._driver(engine)
        return await bounded(self._connect_call(driver, ws_url, headers, engine), CONNECT_TIMEOUT)

    @staticmethod
    async def _default_context(browser: Browser, engine: str) -> BrowserContext:
        """The browser's default context (its cookies and sign-ins). Only a
        browser that has none gets a new one; a Camoufox one sized by its
        window, as its default context is (no fixed viewport)."""
        if browser.contexts:
            return browser.contexts[0]
        if engine == pw.CAMOUFOX:
            return await browser.new_context(no_viewport=True)
        return await browser.new_context()

    async def disconnect_browser(self, browser_id: str) -> bool:
        """Drop a browser's connection; its session tabs and the connection
        are closed in the background (see ``drop_connection``)."""
        return self.drop_connection(browser_id)

    def drop_connection(self, browser_id: str, info: Optional[BrowserInfo] = None) -> bool:
        """Forget a browser's connection now and close it in the background.

        With ``info``, only that connection is dropped (a newer one made
        meanwhile stays). The sessions stay on the browser; a reconnect gives
        them new tabs. Never waits: a browser that is gone can leave the close
        hanging, and it is bounded by DISCONNECT_TIMEOUT on its own.
        """
        current = self.browsers.get(browser_id)
        if current is None or (info is not None and current is not info):
            return False
        del self.browsers[browser_id]
        task = asyncio.ensure_future(self._close_connection(current))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)
        logger.info(f"Disconnected browser '{browser_id}'")
        return True

    async def _close_connection(self, info: BrowserInfo) -> None:
        async def close_all():
            for page in list(info.pages.values()):
                try:
                    await page.close()
                except Exception as e:
                    logger.warning(
                        f"Error closing a tab of '{info.browser_id}': {type(e).__name__}: {e}"
                    )
            await info.browser.close()

        try:
            await bounded(close_all(), DISCONNECT_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Error disconnecting '{info.browser_id}': {type(e).__name__}: {e}")

    async def remove_browser(self, browser_id: str) -> bool:
        """Disconnect a browser for good: its sessions end with it.

        ``disconnect_browser`` alone keeps the sessions (a reconnect gives them
        new tabs on the same browser); this is for a browser that left.
        """
        for sid in [s for s, b in self.sessions.items() if b == browser_id]:
            del self.sessions[sid]
        self._unhealthy_until.pop(browser_id, None)
        return self.drop_connection(browser_id)

    async def open_tab(self, info: BrowserInfo) -> Page:
        """Open a tab on ``info``, given up after TAB_TIMEOUT.

        A failure marks the browser unhealthy and drops the connection (a
        connection that reads as alive to a browser that is gone would
        otherwise be handed out again), then raises.
        """
        try:
            return await bounded(info.context.new_page(), TAB_TIMEOUT)
        except Exception as e:
            logger.warning(
                f"Could not open a tab on '{info.browser_id}': {type(e).__name__}: {e}"
            )
            self.mark_unhealthy(info.browser_id)
            self.drop_connection(info.browser_id, info)
            raise

    # ── lookup helpers ───────────────────────────────────────

    def get_browser(self, browser_id: str) -> BrowserInfo:
        """Get a connected browser by ID. Raises ``KeyError`` if not found."""
        if browser_id not in self.browsers:
            raise KeyError(f"Browser '{browser_id}' not connected")
        return self.browsers[browser_id]

    # ── sessions ─────────────────────────────────────────────

    def session_browser(self, session_id: str) -> Optional[str]:
        """The browser a session lives on, or None for an unknown session."""
        return self.sessions.get(session_id)

    def add_session(self, browser_id: str, session_id: str, page: Page) -> None:
        self.sessions[session_id] = browser_id
        self.session_used[session_id] = time.monotonic()
        info = self.browsers.get(browser_id)
        if info is not None:  # absent mid-reconnect: the session gets a new tab next call
            info.pages[session_id] = page

    def end_session(self, session_id: str) -> Optional[Page]:
        """Forget a session; returns its tab (if it has one) for the caller to close."""
        browser_id = self.sessions.pop(session_id, None)
        self.session_used.pop(session_id, None)
        info = self.browsers.get(browser_id) if browser_id else None
        return info.pages.pop(session_id, None) if info else None

    def session_count(self, browser_id: str) -> int:
        return sum(1 for b in self.sessions.values() if b == browser_id)

    def touch_session(self, session_id: str) -> None:
        if session_id in self.sessions:
            self.session_used[session_id] = time.monotonic()

    def recent_sessions(self, browser_id: str, exclude: str = "", window: float = 600.0) -> int:
        """Other sessions on this browser used within ``window`` seconds."""
        now = time.monotonic()
        return sum(
            1 for sid, b in self.sessions.items()
            if b == browser_id and sid != exclude and now - self.session_used.get(sid, 0.0) < window
        )

    # ── load and health ──────────────────────────────────────

    @staticmethod
    def _alive(info: BrowserInfo) -> bool:
        try:
            return bool(info.browser.is_connected())
        except Exception:
            return False

    def is_connected(self, browser_id: str) -> bool:
        info = self.browsers.get(browser_id)
        return info is not None and self._alive(info)

    def open_tabs(self, browser_id: str) -> int:
        """Every tab open in the browser: sessions, one-off calls and tabs a
        person opened. 0 for a browser that is not connected."""
        if not self.is_connected(browser_id):
            return 0
        info = self.browsers[browser_id]
        try:
            return sum(len(ctx.pages) for ctx in info.browser.contexts)
        except Exception:
            return len(info.pages)

    def load(self, browser_id: str) -> int:
        return self.open_tabs(browser_id) + self._in_flight.get(browser_id, 0)

    def begin_call(self, browser_id: str) -> None:
        self._in_flight[browser_id] = self._in_flight.get(browser_id, 0) + 1

    def end_call(self, browser_id: str) -> None:
        left = self._in_flight.get(browser_id, 0) - 1
        if left > 0:
            self._in_flight[browser_id] = left
        else:
            self._in_flight.pop(browser_id, None)

    def is_healthy(self, browser_id: str) -> bool:
        until = self._unhealthy_until.get(browser_id)
        return until is None or time.monotonic() >= until

    def mark_unhealthy(self, browser_id: str) -> None:
        self._unhealthy_until[browser_id] = time.monotonic() + UNHEALTHY_SECONDS

    def mark_healthy(self, browser_id: str) -> None:
        self._unhealthy_until.pop(browser_id, None)

    def slow_to_connect(self, browser_id: str) -> bool:
        """A connect to the browser has been running longer than a call that
        names no browser waits for one."""
        started = self._connect_started.get(browser_id)
        return started is not None and time.monotonic() - started >= PICK_CONNECT_WAIT

    def pick_browser(self, pool: Iterable[str], exclude: Iterable[str] = ()) -> Optional[str]:
        """Choose a browser for a call that names none, and count the call on it.

        Fewest open tabs plus calls in flight wins. Browsers that recently
        failed to connect, or are still slow to, come last, then those at the
        soft tab cap; ties rotate. Picking and counting happen with no await
        in between, so calls that arrive together spread out. The caller must
        end_call().
        """
        skip = set(exclude)
        options = [b for b in pool if b not in skip]
        if not options:
            return None
        rank = {}
        for b in options:
            load = self.load(b)
            # Paused: Chrome closed for a moment while its profile is copied.
            away = (not self.is_healthy(b) or self.slow_to_connect(b)
                    or (self.paused.get(b, False) and not self.is_connected(b)))
            rank[b] = (away, load >= MAX_PAGES_PER_BROWSER, load)
        best = min(rank.values())
        ties = [b for b in options if rank[b] == best]
        browser_id = ties[self._rr % len(ties)]
        self._rr += 1
        self.begin_call(browser_id)
        return browser_id

    async def ensure_connected(
        self, browser_id: str, ws_url: Optional[str], headers: Optional[dict] = None,
        picked: bool = False,
    ) -> BrowserInfo:
        """Return a live connection to ``browser_id``, connecting or recovering it.

        Calls that find the browser down share one attempt (``_attempt``). A
        call that named no browser (``picked``) waits for it at most
        PICK_CONNECT_WAIT seconds and then gets ``asyncio.TimeoutError``; the
        attempt goes on. A failure marks the browser unhealthy and raises; it
        never touches the other browsers unless its engine's Playwright
        driver itself is dead (then only that engine's).
        """
        info = self.browsers.get(browser_id)
        if info is None:
            if not ws_url:
                raise KeyError(f"Browser '{browser_id}' not connected")
        else:
            drifted = bool(ws_url) and ws_url != info.ws_url
            if not drifted and self._alive(info):
                return info
            if drifted:
                logger.info(
                    f"Browser '{browser_id}' ws_url drift ({info.ws_url} -> {ws_url}), reconnecting..."
                )
            else:
                logger.info(f"Browser '{browser_id}' connection is dead, auto-reconnecting...")
            ws_url = ws_url or info.ws_url
        return await self._attempt(
            browser_id, ws_url, headers, wait=PICK_CONNECT_WAIT if picked else None,
        )

    async def recover_connection(self, browser_id: str, ws_url: str, headers: Optional[dict] = None) -> BrowserInfo:
        """Reconnect a browser whose connection broke (see ``_recover``),
        joining an attempt already running for it."""
        return await self._attempt(browser_id, ws_url, headers)

    # ── recovery ────────────────────────────────────────────

    async def _attempt(
        self, browser_id: str, ws_url: str, headers: Optional[dict], wait: Optional[float] = None,
    ) -> BrowserInfo:
        """Wait on the one connect running for ``browser_id``, starting it if none is.

        The attempt is shielded: a caller that stops waiting (``wait`` ran
        out, or its client went away) leaves it running for the others.
        """
        task = self._connecting.get(browser_id)
        if task is None:
            task = asyncio.ensure_future(self._recover(browser_id, ws_url, headers))
            self._connecting[browser_id] = task
            self._connect_started[browser_id] = time.monotonic()
            task.add_done_callback(functools.partial(self._attempt_done, browser_id))
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait)

    def _attempt_done(self, browser_id: str, task: asyncio.Future) -> None:
        if self._connecting.get(browser_id) is task:
            del self._connecting[browser_id]
            self._connect_started.pop(browser_id, None)
        if not task.cancelled():
            task.exception()  # retrieved: an attempt nobody waits for any more stays quiet

    async def _recover(self, browser_id: str, ws_url: str, headers: Optional[dict] = None) -> BrowserInfo:
        """
        Bring a browser's connection up, the first time or after it broke.

        Runs as the one attempt for the browser (``_attempt``). Two levels:

        * Level 1 – drop the stale entry, if any, and connect via the
          **existing** Playwright driver of the browser's engine.
        * Level 2 – only when that driver process is dead, restart it and
          reconnect every browser of that engine (the other engine's driver
          and browsers are not touched).

        ``headers`` defaults to the existing connection's headers (so BYO auth
        survives a reconnect) when not provided by the caller.
        """
        existing = self.browsers.get(browser_id)
        if existing is not None:
            if headers is None:
                headers = existing.headers
            # Only short-circuit if the URL also matches — ws_url drift
            # (browser pod restart with new IP/port) means we MUST reconnect.
            if existing.ws_url == ws_url and self._alive(existing):
                return existing

        engine = self.engine_of(browser_id)
        # ── Level 1: connect with the same Playwright driver ──
        try:
            info = await self._reconnect_same_driver(browser_id, ws_url, headers)
            self.mark_healthy(browser_id)
            return info
        except Exception as e:
            logger.warning(f"Level-1 connect failed for '{browser_id}': {type(e).__name__}: {e}")
            # With a live driver the fault is this browser (not ready,
            # offline, gone). Restarting the driver would drop every other
            # browser's connection and tabs, so fail this one alone.
            if self.driver_alive(engine):
                self.mark_unhealthy(browser_id)
                raise

        # ── Level 2: restart the engine's Playwright driver ──
        async with self._lock(engine):
            # Another browser's attempt may have restarted it while this one waited.
            current = self.browsers.get(browser_id)
            if current is not None and current.ws_url == ws_url and self._alive(current):
                return current
            try:
                if self.driver_alive(engine):
                    info = await self._reconnect_same_driver(browser_id, ws_url, headers)
                else:
                    info = await self._restart_playwright_and_reconnect(browser_id, ws_url, headers)
            except Exception:
                self.mark_unhealthy(browser_id)
                raise
        self.mark_healthy(browser_id)
        return info

    async def _reconnect_same_driver(
        self, browser_id: str, ws_url: str, headers: Optional[dict] = None
    ) -> BrowserInfo:
        """Drop the stale entry (closed in the background) and open a fresh
        connection, bounded by CONNECT_TIMEOUT."""
        self.drop_connection(browser_id)

        engine = self.engine_of(browser_id)
        browser = await self._open(ws_url, headers, engine)
        context = await self._default_context(browser, engine)
        info = BrowserInfo(browser, context, ws_url=ws_url, browser_id=browser_id, headers=headers or {}, engine=engine)
        self.browsers[browser_id] = info
        logger.info(f"Connected browser '{browser_id}' (same driver)")
        return info

    async def _restart_playwright_and_reconnect(
        self, browser_id: str, fresh_ws_url: Optional[str] = None, headers: Optional[dict] = None
    ) -> BrowserInfo:
        """Restart the Playwright driver of ``browser_id``'s engine and
        reconnect that engine's browsers. The caller holds that engine's lock;
        the other engine's driver and browsers are not touched."""
        engine = self.engine_of(browser_id)
        info = await self._restart_driver(engine, browser_id, fresh_ws_url, headers)
        if info is None:
            raise RuntimeError(
                f"Failed to recover browser '{browser_id}' "
                "after Playwright restart"
            )
        return info

    async def _restart_driver(
        self, engine: str, browser_id: Optional[str] = None,
        fresh_ws_url: Optional[str] = None, headers: Optional[dict] = None,
    ) -> Optional[BrowserInfo]:
        """Restart one engine's driver and reconnect every browser of that
        engine that was connected (plus ``browser_id`` at ``fresh_ws_url``).
        Returns ``browser_id``'s new connection, if it was asked for and made.
        The caller holds the engine's lock."""
        logger.warning(f"Restarting the {engine} Playwright driver for full recovery")

        saved = {bid: (info.ws_url, info.headers) for bid, info in self.browsers.items() if info.engine == engine}
        if browser_id and fresh_ws_url:
            saved[browser_id] = (fresh_ws_url, headers if headers is not None else saved.get(browser_id, (None, {}))[1])
        for bid in saved:
            self.browsers.pop(bid, None)

        old_pw = self.drivers.pop(engine, None)
        if old_pw is not None:
            try:
                await bounded(old_pw.stop(), 5.0)
            except Exception:
                logger.warning(
                    f"Old {engine} Playwright driver did not stop cleanly, force-killing"
                )
                self._kill_driver(engine)
        self._driver_pids.pop(engine, None)

        # Brief pause to let the OS reclaim sockets / pipes
        await asyncio.sleep(0.5)

        driver = await self._start_driver(engine)
        logger.info(f"The {engine} Playwright driver restarted")

        new_info: Optional[BrowserInfo] = None
        for bid, (url, hdrs) in saved.items():
            try:
                browser = await bounded(self._connect_call(driver, url, hdrs, engine), 15.0)
                context = await self._default_context(browser, engine)
                info = BrowserInfo(browser, context, ws_url=url, browser_id=bid, headers=hdrs or {}, engine=engine)
                self.browsers[bid] = info
                self.mark_healthy(bid)
                if bid == browser_id:
                    new_info = info
                logger.info(f"Reconnected '{bid}' after the {engine} Playwright restart")
            except Exception as e:
                logger.error(
                    f"Failed to reconnect '{bid}' after the {engine} Playwright restart: {e}"
                )
        return new_info

    # ── lifecycle ────────────────────────────────────────────

    async def cleanup_stale_pages(self) -> int:
        """Close session pages that are no longer usable.

        Patchright's Node driver leaks memory as dead/zombie pages accumulate
        (each holds driver-side references), eventually OOM-crashing it. We drop
        pages whose handle is closed or whose context is gone. Returns the count.
        """
        closed = 0
        for info in list(self.browsers.values()):
            for sid, page in list(info.pages.items()):
                dead = False
                try:
                    if page.is_closed():
                        dead = True
                    else:
                        _ = page.url  # touch — raises if the page/context is gone
                except Exception:
                    dead = True
                if dead:
                    info.pages.pop(sid, None)
                    try:
                        await bounded(page.close(), DISCONNECT_TIMEOUT)
                    except Exception:
                        pass
                    closed += 1
        if closed:
            logger.info(f"Stale page cleanup: closed {closed} dead page(s)")
        return closed

    async def shutdown(self, timeout: float = 25.0):
        """Disconnect all browsers."""
        logger.info("Starting browser manager shutdown…")
        for task in list(self._connecting.values()) + list(self._closing):
            task.cancel()

        async def _shutdown():
            for bid in list(self.browsers.keys()):
                info = self.browsers[bid]
                await self._close_connection(info)
            self.browsers.clear()
            logger.info("All browsers disconnected")

        try:
            await bounded(_shutdown(), timeout)
        except asyncio.TimeoutError:
            logger.error(f"Shutdown timed out after {timeout}s, forcing cleanup")
            self.browsers.clear()


# Global singleton
browser_manager = BrowserManager()
