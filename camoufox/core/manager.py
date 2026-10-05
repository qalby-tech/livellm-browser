"""The one Camoufox browser of this pod: launch, close, restart, pause, the
watchdog and the housekeeping.

States: starting -> running; restarting (a relaunch is under way, or the
running browser stopped answering and the watchdog is about to relaunch
it); paused (closed on purpose while the keeper copies the profile,
bounded); error (the browser can't start: a Chrome profile on the disk, a
profile from a newer Camoufox, or a launch that failed and is retried).
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from core import const, identity, prefs, profile_guard, session_cookies
from core.automation_proxy import AutomationProxy
from core.options import camoufox_kwargs, launch_server_options
from core.server import ServeError, ServeProcess, driver_paths
from core.settings import Settings

logger = logging.getLogger(__name__)

RESTART_GRACE = 90.0   # /health says "restarting" (200) this long into a relaunch
CLOSE_TIMEOUT = 15.0
PING_TIMEOUT = 10.0
PING_OVERDUE = 5.0     # a ping unanswered this long: /health says "restarting"
MISSES_TO_RELAUNCH = 3
WATCHDOG_EVERY = 10.0
SAVE_COOKIES_TICKS = 6  # x WATCHDOG_EVERY
PRUNE_TICKS = 3
CONTEXT_IDLE_SECONDS = 60
MAX_PAUSE_SECONDS = 600

PROFILE_ENGINE_MESSAGE = "This disk holds a Chrome browser's profile; a Camoufox browser can't open it."


def default_lib():
    """The camoufox library calls the launcher uses (a seam for the tests)."""
    from camoufox import fingerprints
    from camoufox.addons import DefaultAddons
    from camoufox.server import to_camel_case_dict
    from camoufox.utils import launch_options

    return SimpleNamespace(
        launch_options=launch_options,
        generate_fingerprint=fingerprints.generate_fingerprint,
        Screen=fingerprints.Screen,
        UBO=DefaultAddons.UBO,
        to_camel=to_camel_case_dict,
        core_counts=tuple(getattr(fingerprints, "PLAUSIBLE_CORE_COUNTS", identity.CORE_COUNTS)),
    )


def default_serve() -> ServeProcess:
    node, package = driver_paths()
    return ServeProcess(node, package)


def _iso(t: Optional[float]) -> Optional[str]:
    return None if t is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class CamoufoxManager:
    def __init__(self, proxy: AutomationProxy, profile_dir: Path = const.PROFILE_DIR,
                 camoufox_dir: Path = const.CAMOUFOX_DIR, lib=None,
                 serve_factory: Callable[[], ServeProcess] = default_serve,
                 settings_fn: Callable[[], Settings] = Settings.from_env, proc_root: Path = Path("/proc"),
                 cgroup_root: Path = Path("/sys/fs/cgroup")):
        self.proxy = proxy
        self.profile_dir = Path(profile_dir)
        self.camoufox_dir = Path(camoufox_dir)
        self._lib = lib
        self.serve_factory = serve_factory
        self.settings_fn = settings_fn
        self.proc_root = proc_root
        self.cgroup_root = Path(cgroup_root)
        self.proc: Optional[ServeProcess] = None
        self.state = "stopped"
        self.error: Optional[str] = None
        self.error_message = ""
        self.changing_since: Optional[float] = None
        self.started_at: Optional[float] = None
        self.paused_until: Optional[float] = None
        self.misses = 0
        self.relaunches = 0
        self._forced = False
        self._ping_since: Optional[float] = None
        self._ticks = 0
        self._booted = False
        self._lock: Optional[asyncio.Lock] = None
        self._resume_task: Optional[asyncio.Task] = None
        self._exit_task: Optional[asyncio.Task] = None

    @property
    def lib(self):
        if self._lib is None:
            self._lib = default_lib()
        return self._lib

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ── what the API reads ──

    def running(self) -> bool:
        return self.state == "running" and self.proc is not None and self.proc.alive()

    def paused(self) -> bool:
        return self.state == "paused"

    def health(self):
        now = time.monotonic()
        if self.paused():
            return 200, {"status": "paused"}
        if self.running():
            # Firefox stopped answering (a parent hang): the watchdog
            # relaunches it after MISSES_TO_RELAUNCH, so it is restarting
            # already. Bounded by the watchdog: a miss resets on an answer,
            # and the relaunch has its own RESTART_GRACE.
            if self.misses > 0 or self._ping_overdue(now):
                return 200, {"status": "restarting"}
            return 200, {"status": "ok"}
        if self.error in ("profile_engine", "profile_newer"):
            return 503, {"error": self.error, "message": self.error_message}
        if self.changing_since is not None and now - self.changing_since <= RESTART_GRACE:
            return 200, {"status": "restarting"}
        body = {"status": "down"}
        if self.error:
            body["error"] = self.error
        return 503, body

    def _ping_overdue(self, now: float) -> bool:
        return self._ping_since is not None and now - self._ping_since >= PING_OVERDUE

    def version(self) -> dict:
        v, _build = const.browser_version(self.camoufox_dir)
        return {
            "engine": "camoufox",
            "browserVersion": v,
            "browserMajor": const.major_of(v),
            "playwright": const.PLAYWRIGHT_VERSION,
            "image": const.IMAGE_VERSION,
            "pid": self.proc.firefox_pid if self.running() else None,
            "startedAt": _iso(self.started_at),
            "paused": self.paused(),
            # The browser container's settings, for the profile manifest.
            "timezone": os.environ.get("TZ") or "",
            "locale": (os.environ.get("BROWSER_LOCALE") or "").strip(),
        }

    # ── launch and close ──

    def _fail(self, code: str, message: str) -> bool:
        if self.error != code or self.error_message != message:
            logger.error(f"The browser can't start ({code}): {message}")
        self.state, self.error, self.error_message = "error", code, message
        return False

    def _new_identity(self, settings: Settings) -> dict:
        lib = self.lib
        screen = lib.Screen(max_width=const.DISPLAY_SIZE[0], max_height=const.DISPLAY_SIZE[1])
        version, _ = const.browser_version(self.camoufox_dir)

        def generate():
            return lib.generate_fingerprint(screen=screen, window=const.DISPLAY_SIZE, os="linux")

        def resolve(fp):
            kw = camoufox_kwargs(settings, {"fingerprint": fp, "pinned": {}}, {}, screen, lib.UBO)
            return lib.launch_options(**kw)

        ident = identity.create(generate, resolve, settings.locale, settings.timezone,
                                f"camoufox-{const.CAMOUFOX_LIB_VERSION}/v{version}", frame=const.WINDOW_FRAME)
        cores = ident["pinned"].get(identity.CORES_KEY)
        fitted = identity.fit_cores(cores, identity.cpu_limit(self.cgroup_root),
                                    getattr(lib, "core_counts", None) or identity.CORE_COUNTS)
        if fitted != cores:
            ident["pinned"][identity.CORES_KEY] = fitted
        identity.save(self.profile_dir, ident)
        logger.info(f"A new browser identity ({ident['pinned']})")
        return ident

    def _prepare(self, settings: Settings):
        """Everything before Firefox starts, with no Firefox running (sync:
        it runs in a thread). Returns (launchServer options, ok)."""
        profile = self.profile_dir
        if profile_guard.chrome_profile(profile):
            return None, ("profile_engine", PROFILE_ENGINE_MESSAGE)
        profile_guard.kill_all(self.camoufox_dir, self.proc_root)
        profile.mkdir(parents=True, exist_ok=True)
        profile_guard.remove_locks(profile)
        # The keeper's forced-import marker is removed only once this launch
        # has the browser up (_launch): a launch that fails is retried with it.
        self._forced = profile_guard.downgrade_marker(profile)
        try:
            extra = profile_guard.downgrade_args(profile, const.browser_version(self.camoufox_dir), forced=self._forced)
        except profile_guard.ProfileNewer as e:
            return None, ("profile_newer", str(e))
        prefs.clear_previous(profile)
        managed = prefs.managed_prefs(settings, profile)
        ident = identity.load(profile) or self._new_identity(settings)
        lib = self.lib
        screen = lib.Screen(max_width=const.DISPLAY_SIZE[0], max_height=const.DISPLAY_SIZE[1])
        lib_opts = lib.launch_options(**camoufox_kwargs(settings, ident, managed, screen, lib.UBO))
        prefs.record(profile, (lib_opts.get("firefox_user_prefs") or {}).keys())
        return launch_server_options(lib_opts, settings, profile, extra, to_camel=lib.to_camel), None

    async def _launch(self) -> bool:
        """Start the browser (the lock is held). False = it did not start."""
        if self.changing_since is None:
            self.changing_since = time.monotonic()
        self.state = "restarting" if self.started_at else "starting"
        settings = self.settings_fn()
        try:
            opts, refused = await asyncio.to_thread(self._prepare, settings)
        except Exception as e:
            logger.exception("Preparing the launch failed")
            return self._fail("launch_failed", f"{type(e).__name__}: {e}")
        if refused:
            return self._fail(*refused)
        proc = self.serve_factory()
        proc.on_exit = self._on_exit
        try:
            listening = await proc.start(opts)
        except Exception as e:
            proc.kill()
            await proc.wait(5)
            return self._fail("launch_failed", str(e))
        self.proc = proc
        if self._forced:
            self._forced = False
            await asyncio.to_thread(profile_guard.consume_downgrade_marker, self.profile_dir)
        await self._restore_cookies(proc, settings)
        self.proxy.retarget(int(listening["port"]), listening["wsPath"])
        self.state, self.error, self.error_message = "running", None, ""
        self.changing_since = None
        self.started_at = time.time()
        self.misses = 0
        logger.info(f"Camoufox {listening.get('version')} is up (pid {listening.get('pid')})")
        return True

    async def _stop(self, save: bool = True) -> None:
        """Close the browser (the lock is held). Never leaves Firefox behind."""
        self.proxy.untarget()
        proc, self.proc = self.proc, None
        if proc is not None:
            if proc.alive():
                if save:
                    await self._save_session_cookies(proc)
                await proc.close(CLOSE_TIMEOUT)
            else:
                proc.kill()
            await proc.wait(5)
        await asyncio.to_thread(profile_guard.kill_all, self.camoufox_dir, self.proc_root)

    def _on_exit(self, proc: ServeProcess) -> None:
        """serve.js ended. Unasked (Firefox crashed, or a client closed the
        default context): relaunch now rather than at the next tick."""
        if proc is not self.proc or proc.closing:
            return
        logger.warning(f"The browser went away (exit {proc.proc.returncode if proc.proc else '?'}); relaunching")
        self._exit_task = asyncio.ensure_future(self._relaunch(proc))

    async def _relaunch(self, dead: Optional[ServeProcess] = None) -> bool:
        async with self.lock:
            if dead is not None and dead is not self.proc:
                return False  # someone else already did
            if self.paused():
                return False
            self.relaunches += 1
            self.state = "restarting"
            if self.changing_since is None:
                self.changing_since = time.monotonic()
            if self.proc is not None:
                self.proc.kill()
            await self._stop(save=False)
            return await self._launch()

    async def start(self) -> bool:
        async with self.lock:
            return await self._launch()

    async def restart(self) -> bool:
        async with self.lock:
            if self.paused():
                raise RuntimeError("paused")
            self.state = "restarting"
            self.changing_since = time.monotonic()
            await self._stop(save=True)
            return await self._launch()

    async def shutdown(self) -> None:
        if self._resume_task:
            self._resume_task.cancel()
        async with self.lock:
            self.state = "stopped"
            await self._stop(save=True)

    # ── pause / resume (the keeper's profile copies) ──

    async def pause(self, max_seconds: float) -> None:
        if not (0 < max_seconds <= MAX_PAUSE_SECONDS):
            raise ValueError("maxSeconds out of range")
        async with self.lock:
            already = self.paused()
            self.state = "paused"
            self.paused_until = time.monotonic() + max_seconds
            if not already:
                await self._stop(save=True)
            if self._resume_task:
                self._resume_task.cancel()
            self._resume_task = asyncio.ensure_future(self._auto_resume(max_seconds))

    async def _auto_resume(self, max_seconds: float) -> None:
        await asyncio.sleep(max_seconds)
        if self.paused():
            logger.warning("A pause ran out its bound; resuming")
            try:
                await self.resume(auto=True)
            except Exception as e:
                logger.error(f"Auto-resume failed: {e}")

    async def resume(self, auto: bool = False) -> bool:
        if not auto and self._resume_task:
            self._resume_task.cancel()
        async with self.lock:
            if self.running():
                return True
            self.paused_until = None
            self.state = "restarting"
            self.changing_since = time.monotonic()
            ok = await self._launch()
        if not ok:
            raise RuntimeError(self.error_message or "the browser did not start")
        return True

    # ── cookies ──

    async def get_cookies(self) -> list:
        if not self.running():
            raise ServeError("the browser is not running")
        return await self.proc.request("cookies.get", timeout=15.0)

    async def add_cookies(self, cookies: list) -> dict:
        if not self.running():
            raise ServeError("the browser is not running")
        return await self.proc.request("cookies.add", {"cookies": cookies}, timeout=60.0)

    async def _save_session_cookies(self, proc: ServeProcess) -> None:
        """Only from a live browser: a dying one may answer with nothing, and
        that must not wipe what the last save kept."""
        try:
            cookies = await proc.request("cookies.get", timeout=5.0)
            session_cookies.write(self.profile_dir, session_cookies.session_only(cookies or []))
        except Exception as e:
            logger.warning(f"Could not keep the session cookies: {e}")

    async def _restore_cookies(self, proc: ServeProcess, settings: Settings) -> None:
        saved = session_cookies.read(self.profile_dir)
        if saved:
            try:
                r = await proc.request("cookies.add", {"cookies": saved, "skipExisting": True}, timeout=15.0)
                if r.get("added"):
                    logger.info(f"Restored {r['added']} session cookies")
            except Exception as e:
                logger.warning(f"Could not restore the session cookies: {e}")
        if not self._booted:
            self._booted = True
            path = settings.cookies_file
            if path and os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, list) and loaded:
                        r = await proc.request("cookies.add", {"cookies": loaded}, timeout=60.0)
                        logger.info(f"Added {r.get('added')} cookies from the cookies file ({r.get('dropped')} refused)")
                except (OSError, ValueError, ServeError, asyncio.TimeoutError) as e:
                    logger.warning(f"Failed to read cookies file {path}: {e}")

    # ── watchdog and housekeeping ──

    async def tick(self) -> None:
        """One watchdog round (every WATCHDOG_EVERY seconds)."""
        self._ticks += 1
        if self.lock.locked():
            return
        if self.paused():
            if self.paused_until is not None and time.monotonic() >= self.paused_until:
                await self.resume(auto=True)
            return
        if self.state == "stopped":
            return
        if self.error in ("profile_engine", "profile_newer"):
            # Retried each round: the keeper may have put a profile this
            # browser can open (it resumes the browser itself, too).
            if self.error == "profile_engine" and profile_guard.chrome_profile(self.profile_dir):
                return
            async with self.lock:
                await self._launch()
            return
        if not self.running():
            await self._relaunch(self.proc)
            return
        proc = self.proc
        self._ping_since = time.monotonic()
        try:
            await proc.request("ping", timeout=PING_TIMEOUT)
            self.misses = 0
        except Exception as e:
            self.misses += 1
            logger.warning(f"The browser did not answer ({self.misses}/{MISSES_TO_RELAUNCH}): {e or type(e).__name__}")
            if self.misses >= MISSES_TO_RELAUNCH:
                logger.error("The browser stopped answering; relaunching it")
                proc.kill()
                await self._relaunch(proc)
            return
        finally:
            self._ping_since = None
        if self._ticks % SAVE_COOKIES_TICKS == 0:
            await self._save_session_cookies(proc)
        if self._ticks % PRUNE_TICKS == 0:
            await self.prune()

    async def prune(self) -> Optional[dict]:
        """Close the contexts clients left behind: one with no page for
        CONTEXT_IDLE_SECONDS (also while a Browser API stays connected), and
        all of them once no client has been connected that long. The
        default context and its pages are never touched."""
        if not self.running():
            return None
        close_all = self.proxy.idle_for() >= CONTEXT_IDLE_SECONDS
        try:
            r = await self.proc.request("contexts.prune", {"idleSeconds": CONTEXT_IDLE_SECONDS, "closeAll": close_all}, timeout=15.0)
            if r and r.get("closed"):
                logger.info(f"Closed {r['closed']} contexts clients left")
            return r
        except Exception as e:
            logger.warning(f"Housekeeping failed: {e}")
            return None

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_EVERY)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Watchdog error: {e}")
