"""Unit tests for the runner's recording glue.

Recording itself is browser-use's native RecordingWatchdog (armed via
BrowserProfile.record_video_dir); the runner only finalizes/uploads via the
watchdog's public stop_recording() and re-arms a restart pass with
start_recording(). These tests drive that glue with a fake watchdog — the
contract under test is fail-open: no video is always acceptable, a broken
run never is, and a later run.done must not blank an already-reported ref.
"""

from pathlib import Path
from typing import Optional

from livellm_agent import runner as runner_module
from livellm_agent.control import ControlChannel
from livellm_agent.models import Task
from livellm_agent.runner import Runner


class FakeWatchdog:
    def __init__(self, path: Optional[Path] = None, fail_stop: bool = False, recording: bool = True):
        self._path = path
        self._fail_stop = fail_stop
        self._recording = recording
        self.started_paths: list[Path] = []

    @property
    def is_recording(self) -> bool:
        return self._recording

    async def stop_recording(self) -> Optional[Path]:
        if self._fail_stop:
            raise RuntimeError("cdp went away")
        self._recording = False
        return self._path

    async def start_recording(self, output_path: Path) -> Path:
        self._recording = True
        self.started_paths.append(output_path)
        return output_path


class FakeSession:
    def __init__(self, wd: Optional[FakeWatchdog]):
        self._recording_watchdog = wd


class FakeStore:
    def put_video(self, task_id: str, mp4: bytes) -> str:
        assert mp4 == b"mp4-bytes"
        return f"s3://browser-agent/tenant/{task_id}/recording.mp4"


def make_runner(record_dir: Optional[str], wd: Optional[FakeWatchdog]) -> Runner:
    r = Runner(Task(id="t-1", prompt="do things"), ControlChannel(None, "t-1", None))
    r._record_dir = record_dir
    r._session = FakeSession(wd)
    return r


async def test_finish_is_noop_when_recording_off():
    r = make_runner(None, None)
    r._session = None  # recording off → the session must not even be touched
    assert await r._finish_recording() == ""


async def test_finish_uploads_and_caches_ref(tmp_path, monkeypatch):
    video = tmp_path / "0123.mp4"
    video.write_bytes(b"mp4-bytes")
    wd = FakeWatchdog(path=video)
    r = make_runner(str(tmp_path), wd)
    monkeypatch.setattr(runner_module.artifacts, "store", lambda: FakeStore())

    ref = await r._finish_recording()
    assert ref == "s3://browser-agent/tenant/t-1/recording.mp4"
    # a later run.done (e.g. after a restart pass with no new video) keeps the
    # last uploaded ref instead of blanking video_ref in tenant-api
    wd._path = None
    assert await r._finish_recording() == ref


async def test_finish_fails_open_on_stop_error():
    r = make_runner("/tmp/rec-t-1", FakeWatchdog(fail_stop=True))
    assert await r._finish_recording() == ""


async def test_finish_fails_open_on_upload_error(tmp_path, monkeypatch):
    video = tmp_path / "0123.mp4"
    video.write_bytes(b"mp4-bytes")
    r = make_runner(str(tmp_path), FakeWatchdog(path=video))

    def boom():
        raise RuntimeError("minio down")

    monkeypatch.setattr(runner_module.artifacts, "store", boom)
    assert await r._finish_recording() == ""


async def test_resume_rearms_only_when_idle(tmp_path):
    wd = FakeWatchdog(recording=True)
    r = make_runner(str(tmp_path), wd)
    await r._resume_recording()  # mid-pass restart: watchdog still live → no-op
    assert wd.started_paths == []

    wd._recording = False  # post-run.done restart: previous pass finalized
    await r._resume_recording()
    assert len(wd.started_paths) == 1
    assert wd.started_paths[0].parent == tmp_path
    assert wd.started_paths[0].suffix == ".mp4"


async def test_resume_is_noop_when_recording_off():
    r = make_runner(None, None)
    r._session = None
    await r._resume_recording()  # must not raise
