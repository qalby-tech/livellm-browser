"""Run recorder — captures the run as an MP4 via CDP screencast.

The agent drives an EXISTING browser over CDP (no Playwright video API), so
recording is `Page.startScreencast` on the active page: Chrome pushes JPEG
frames as `Page.screencastFrame` events, each of which must be acked
(`Page.screencastFrameAck`) or the stream stalls. Frames are spilled to
/tmp/rec-<task>/ as files (never buffered in RAM) with their capture
timestamps; on run end ffmpeg's concat demuxer assembles them into an MP4
with per-frame durations, so the video plays back in real time even though
the frame rate is irregular.

CDP access mirrors checkpoint.py: `session.get_or_create_cdp_session()`
(browser-use 0.12.9 + cdp-use 1.4.5) yields the active page's session_id and
the shared CDPClient, whose `register.Page.screencastFrame` hooks the event.

Everything here is best-effort by contract: any failure degrades to
"no video" and must never break the run — callers get None, not exceptions.
"""

import asyncio
import base64
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Screencast parameters: JPEG at moderate quality, capped width, every 2nd
# compositor frame — plenty for "watch what the agent did" playback while
# keeping a long run in the tens of MB.
FRAME_FORMAT = "jpeg"
FRAME_QUALITY = 60
FRAME_MAX_WIDTH = 1280
EVERY_NTH_FRAME = 2

# Hard cap on collected frames (disk, not RAM — but /tmp is finite and a
# runaway animation can push hundreds of frames/minute). ~4000 frames ≈ 200MB
# worst case; past it we keep acking but drop, and log once.
MAX_FRAMES = 4000

# Display duration of the final frame (the concat demuxer needs an explicit
# duration for it), and clamps so a stalled page or a paused run doesn't
# freeze the video for minutes on one frame.
TAIL_DURATION = 0.5
MIN_FRAME_DURATION = 0.02
MAX_FRAME_DURATION = 5.0

FFMPEG_TIMEOUT = 120  # seconds; assembly of a capped run takes well under this


def build_concat_file(frames: list[tuple[float, str]], tail: float = TAIL_DURATION) -> str:
    """Render an ffmpeg concat-demuxer script from (timestamp, path) frames.

    Each frame shows until the next one's timestamp (clamped to
    [MIN_FRAME_DURATION, MAX_FRAME_DURATION]); the last frame holds for
    `tail`. The demuxer only honors the final `duration` directive when the
    file is listed again after it, hence the trailing repeat.
    """
    if not frames:
        raise ValueError("no frames to assemble")
    lines = ["ffconcat version 1.0"]
    for i, (ts, path) in enumerate(frames):
        if i + 1 < len(frames):
            dur = frames[i + 1][0] - ts
        else:
            dur = tail
        dur = min(max(dur, MIN_FRAME_DURATION), MAX_FRAME_DURATION)
        lines.append(f"file '{path}'")
        lines.append(f"duration {dur:.3f}")
    lines.append(f"file '{frames[-1][1]}'")
    return "\n".join(lines) + "\n"


class Recorder:
    """Screencast recorder for one task. Lifecycle:

        rec = Recorder(task_id)
        await rec.ensure_started(session)   # per step — re-attaches after tab switches
        mp4 = await rec.finish()            # stop + assemble; None when no video
        rec.cleanup()                       # drop the spill dir when the runner exits
    """

    def __init__(self, task_id: str):
        self._task_id = task_id
        self._dir = Path(f"/tmp/rec-{task_id}")
        self._frames: list[tuple[float, str]] = []  # (epoch ts, file path)
        self._client = None
        self._session_id: Optional[str] = None
        self._active = False
        self._capped = False
        self._ffmpeg_missing_logged = False

    # ── capture ──────────────────────────────────────────────────────────
    async def ensure_started(self, session) -> None:
        """Start (or re-attach after a tab switch) the screencast on the active
        page. Never raises — a recording failure degrades to no-video."""
        try:
            # First step of the first pass: browser-use only connects the
            # shared session inside Agent.run(), which hasn't happened yet.
            # start() is idempotent (the agent calls it again per sub-goal).
            if getattr(session, "_cdp_client_root", None) is None:
                await session.start()
            cdp = await session.get_or_create_cdp_session(target_id=None, focus=False)
            if self._active and cdp.session_id == self._session_id:
                return
            self._dir.mkdir(parents=True, exist_ok=True)
            self._client = cdp.cdp_client
            self._session_id = cdp.session_id
            # One handler per method on the shared client; re-registering after
            # a target change just replaces it with an identical bound method.
            cdp.cdp_client.register.Page.screencastFrame(self._on_frame)
            await cdp.cdp_client.send.Page.startScreencast(
                params={
                    "format": FRAME_FORMAT,
                    "quality": FRAME_QUALITY,
                    "maxWidth": FRAME_MAX_WIDTH,
                    "everyNthFrame": EVERY_NTH_FRAME,
                },
                session_id=cdp.session_id,
            )
            self._active = True
            logger.info("recording: screencast started (task %s, session %s)",
                        self._task_id, cdp.session_id)
        except Exception as e:
            logger.warning("recording: could not start screencast: %s", e)

    async def _on_frame(self, ev, session_id: Optional[str]) -> None:
        """Page.screencastFrame handler — ALWAYS ack (or Chrome stops sending),
        then spill the frame to disk unless capped."""
        try:
            if self._client is not None:
                await self._client.send.Page.screencastFrameAck(
                    params={"sessionId": ev["sessionId"]},
                    session_id=session_id or self._session_id,
                )
        except Exception as e:
            logger.debug("recording: frame ack failed: %s", e)
        if self._capped:
            return
        if len(self._frames) >= MAX_FRAMES:
            self._capped = True
            logger.warning("recording: frame cap (%d) reached for task %s; "
                           "dropping further frames", MAX_FRAMES, self._task_id)
            return
        try:
            data = base64.b64decode(ev["data"])
            ts = float((ev.get("metadata") or {}).get("timestamp") or time.time())
            path = self._dir / f"frame-{len(self._frames):06d}.jpg"
            path.write_bytes(data)
            self._frames.append((ts, str(path)))
        except Exception as e:
            logger.debug("recording: frame save failed: %s", e)

    async def stop(self) -> None:
        """Stop the screencast (frames stay on disk — a restarted pass resumes
        via ensure_started and the next finish() re-assembles everything)."""
        if self._active and self._client is not None and self._session_id:
            try:
                await self._client.send.Page.stopScreencast(session_id=self._session_id)
            except Exception as e:
                logger.debug("recording: stopScreencast failed: %s", e)
        self._active = False

    # ── assembly ─────────────────────────────────────────────────────────
    async def finish(self) -> Optional[bytes]:
        """Stop the screencast and assemble the collected frames into an MP4.
        Returns None (never raises) when there's nothing to assemble, ffmpeg
        is missing, or assembly fails."""
        await self.stop()
        if not self._frames:
            return None
        if not shutil.which("ffmpeg"):
            if not self._ffmpeg_missing_logged:
                self._ffmpeg_missing_logged = True
                logger.warning("recording: ffmpeg not found in the image; skipping video")
            return None
        concat = self._dir / "frames.ffconcat"
        out = self._dir / "recording.mp4"
        try:
            concat.write_text(build_concat_file(sorted(self._frames)))
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat),
                "-vsync", "vfr",
                # JPEG frames can have odd dimensions; yuv420p needs even.
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-pix_fmt", "yuv420p",
                str(out),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                proc.kill()
                logger.warning("recording: ffmpeg timed out after %ds", FFMPEG_TIMEOUT)
                return None
            if proc.returncode != 0:
                logger.warning("recording: ffmpeg failed (%d): %.500s",
                               proc.returncode, stderr.decode(errors="replace"))
                return None
            mp4 = out.read_bytes()
            logger.info("recording: assembled %d frame(s) into %d bytes of MP4",
                        len(self._frames), len(mp4))
            return mp4
        except Exception as e:
            logger.warning("recording: assembly failed: %s", e)
            return None

    def cleanup(self) -> None:
        """Remove the spill directory. Call once, when the runner fully exits."""
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except Exception:
            pass
