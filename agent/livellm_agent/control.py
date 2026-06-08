"""Control channel — the cloud_gateways WebSocket the UI connects to for live
per-step review.

The agent emits AgentEvent messages (plan.ready, step.event, run.done) and
consumes ClientMsg messages (verdict, control pause/resume/cancel,
restart_from). A single reader task pumps inbound messages and routes them:
verdicts resolve a per-step Future the runner awaits; control ops flip flags /
fire hooks. This keeps the runner's "execute a step, then maybe block for a
verdict" loop simple.

Transport: `websockets`. Auth is a short-lived HMAC token minted by tenant-api,
sent both as a query param (matching the VNC/agent gateway convention) and an
Authorization header; the gateway validates and scopes the stream to the task.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import websockets
from pydantic import BaseModel, TypeAdapter

from livellm_agent.models import (
    ClientMsg,
    ControlMsg,
    RestartFrom,
    Verdict,
    VerdictMsg,
)

logger = logging.getLogger(__name__)

_CLIENT_MSG = TypeAdapter(ClientMsg)


class ControlChannel:
    def __init__(self, url: str, token: Optional[str] = None):
        self._url = url
        self._token = token
        self._ws = None  # websockets client connection
        self._reader: Optional[asyncio.Task] = None

        # runner awaits a verdict for the step it just executed
        self._verdict_futures: dict[int, asyncio.Future[Verdict]] = {}

        # control flags the runner polls / awaits
        self.paused = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()

        # optional hook the runner sets to handle a mid-run restart request
        self.on_restart: Optional[Callable[[int], Awaitable[None]]] = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def connect(self) -> None:
        url = self._url
        headers = {}
        if self._token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={self._token}"
            headers["Authorization"] = f"Bearer {self._token}"
        self._ws = await websockets.connect(url, additional_headers=headers)
        self._reader = asyncio.create_task(self._read_loop())
        logger.info("control channel connected")

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    # ── outbound ───────────────────────────────────────────────────────────
    async def send(self, event: BaseModel) -> None:
        if not self._ws:
            logger.debug("control channel not connected; dropping %s", type(event).__name__)
            return
        await self._ws.send(event.model_dump_json())

    # ── inbound ────────────────────────────────────────────────────────────
    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = _CLIENT_MSG.validate_json(raw)
                except Exception as e:  # malformed — ignore, keep the channel up
                    logger.warning("control: bad message: %s", e)
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("control: read loop ended: %s", e)

    async def _dispatch(self, msg: ClientMsg) -> None:
        if isinstance(msg, VerdictMsg):
            fut = self._verdict_futures.get(msg.verdict.step_idx)
            if fut and not fut.done():
                fut.set_result(msg.verdict)
        elif isinstance(msg, ControlMsg):
            if msg.op == "pause":
                self.paused.set()
                self._resume.clear()
            elif msg.op == "resume":
                self.paused.clear()
                self._resume.set()
            elif msg.op == "cancel":
                self.cancelled.set()
                self._resume.set()  # unblock any waiter
                self._fail_pending(RuntimeError("cancelled"))
        elif isinstance(msg, RestartFrom):
            if self.on_restart:
                asyncio.create_task(self.on_restart(msg.step_idx))

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in self._verdict_futures.values():
            if not fut.done():
                fut.set_exception(exc)

    # ── runner helpers ───────────────────────────────────────────────────
    async def wait_verdict(self, step_idx: int) -> Verdict:
        """Block until the UI returns a verdict for this step (or cancel)."""
        fut: asyncio.Future[Verdict] = asyncio.get_event_loop().create_future()
        self._verdict_futures[step_idx] = fut
        try:
            return await fut
        finally:
            self._verdict_futures.pop(step_idx, None)

    async def gate(self) -> None:
        """Honor a pause requested from the UI; raises if cancelled."""
        if self.cancelled.is_set():
            raise RuntimeError("cancelled")
        if self.paused.is_set():
            logger.info("control: paused, awaiting resume")
            await self._resume.wait()
        if self.cancelled.is_set():
            raise RuntimeError("cancelled")
