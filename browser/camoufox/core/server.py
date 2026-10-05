"""serve.js, the browser's Playwright server, as a child of the launcher.

It runs on the playwright package's own node and playwright-core (no second
driver). The launcher talks to it over its stdin/stdout: one JSON line of
launch options, then JSON-line requests and answers (see serve.js). It is
started in a session of its own; Firefox, which Playwright starts detached,
is in another, so ``kill`` takes both groups.
"""
import asyncio
import itertools
import json
import logging
import os
import signal
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SERVE_JS = Path(__file__).resolve().parent.parent / "serve.js"
# A cookies answer may be large.
LINE_LIMIT = 32 << 20


class ServeError(Exception):
    pass


def driver_paths():
    """(node, playwright-core package dir) of the installed playwright package."""
    from playwright._impl._driver import compute_driver_executable

    node = compute_driver_executable()[0]
    if isinstance(node, tuple):
        node = node[0]
    return str(node), str(Path(node).parent / "package")


class ServeProcess:
    def __init__(self, node: str, package: str, script: Path = SERVE_JS, env: Optional[dict] = None):
        self.node = node
        self.package = package
        self.script = script
        self.env = env
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.listening: Optional[dict] = None
        self._ids = itertools.count(1)
        self._waiting: dict = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._listening = None
        self.on_exit: Optional[Callable[["ServeProcess"], None]] = None
        self.closing = False

    @property
    def firefox_pid(self) -> Optional[int]:
        return (self.listening or {}).get("pid")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, options: dict, timeout: float = 150.0) -> dict:
        """Launch; returns serve.js's listening line {port, wsPath, pid, version}."""
        self.proc = await asyncio.create_subprocess_exec(
            self.node, str(self.script), self.package,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=None,
            cwd=self.package, env=self.env, start_new_session=True, limit=LINE_LIMIT,
        )
        loop = asyncio.get_running_loop()
        self._listening = loop.create_future()
        self._reader_task = asyncio.create_task(self._read())
        self.proc.stdin.write((json.dumps({"options": options}) + "\n").encode())
        await self.proc.stdin.drain()
        try:
            self.listening = await asyncio.wait_for(asyncio.shield(self._listening), timeout=timeout)
        except asyncio.TimeoutError:
            self.kill()
            raise ServeError(f"the browser did not start within {timeout:.0f} s")
        return self.listening

    async def _read(self) -> None:
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg:
                    fut = self._waiting.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if msg.get("ok"):
                            fut.set_result(msg.get("result"))
                        else:
                            fut.set_exception(ServeError(msg.get("error") or "failed"))
                    continue
                event = msg.get("event")
                if event == "listening" and not self._listening.done():
                    self._listening.set_result(msg)
                elif event == "failed" and not self._listening.done():
                    self._listening.set_exception(ServeError(msg.get("error") or "the browser did not start"))
        except (ValueError, asyncio.LimitOverrunError) as e:
            logger.warning(f"serve.js: unreadable output ({e})")
        finally:
            await self.proc.wait()
            if self._listening is not None and not self._listening.done():
                self._listening.set_exception(ServeError(f"the browser server exited ({self.proc.returncode})"))
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(ServeError("the browser server exited"))
            self._waiting.clear()
            if self.on_exit is not None:
                try:
                    self.on_exit(self)
                except Exception as e:
                    logger.warning(f"exit handler: {e}")

    async def request(self, cmd: str, args: Optional[dict] = None, timeout: float = 10.0):
        if not self.alive():
            raise ServeError("the browser server is not running")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._waiting[rid] = fut
        try:
            self.proc.stdin.write((json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n").encode())
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        except (BrokenPipeError, ConnectionResetError) as e:
            raise ServeError(f"the browser server is gone ({e})")
        finally:
            self._waiting.pop(rid, None)

    async def close(self, timeout: float = 15.0) -> bool:
        """Close the browser cleanly (it saves its profile). False = killed."""
        self.closing = True
        if not self.alive():
            return True
        try:
            await self.request("close", timeout=5.0)
        except (ServeError, asyncio.TimeoutError):
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("The browser did not close in time; killing it")
            self.kill()
            return False

    def kill(self) -> None:
        """SIGKILL serve.js's group and Firefox's (a stopped process too)."""
        self.closing = True
        groups = []
        if self.proc is not None and self.proc.returncode is None:
            groups.append(self.proc.pid)
        if self.firefox_pid:
            groups.append(self.firefox_pid)
        for pgid in groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in groups:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    async def wait(self, timeout: float = 10.0) -> None:
        if self.proc is not None:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
