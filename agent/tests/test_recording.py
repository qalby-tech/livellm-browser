"""Unit tests for livellm_agent.recording — the ffmpeg concat-file builder.

The concat demuxer script is the load-bearing part of assembly: per-frame
durations come from the capture timestamps (so playback is real-time despite
an irregular frame rate), stalls are clamped, and the final frame must be
listed twice for its duration to be honored.
"""

import pytest

from livellm_agent.recording import (
    MAX_FRAME_DURATION,
    MIN_FRAME_DURATION,
    TAIL_DURATION,
    build_concat_file,
)


def test_empty_frames_raises():
    with pytest.raises(ValueError):
        build_concat_file([])


def test_header_and_single_frame():
    out = build_concat_file([(100.0, "/tmp/rec-t/frame-000000.jpg")])
    lines = out.splitlines()
    assert lines[0] == "ffconcat version 1.0"
    assert lines[1] == "file '/tmp/rec-t/frame-000000.jpg'"
    assert lines[2] == f"duration {TAIL_DURATION:.3f}"
    # the final frame is repeated so ffmpeg honors its duration directive
    assert lines[3] == "file '/tmp/rec-t/frame-000000.jpg'"


def test_durations_come_from_timestamp_deltas():
    frames = [
        (100.0, "/tmp/rec-t/frame-000000.jpg"),
        (100.25, "/tmp/rec-t/frame-000001.jpg"),
        (101.0, "/tmp/rec-t/frame-000002.jpg"),
    ]
    lines = build_concat_file(frames).splitlines()
    assert lines[2] == "duration 0.250"
    assert lines[4] == "duration 0.750"
    # last frame: the tail duration, then the trailing repeat
    assert lines[6] == f"duration {TAIL_DURATION:.3f}"
    assert lines[7] == "file '/tmp/rec-t/frame-000002.jpg'"


def test_stall_is_clamped_to_max_duration():
    # a page stall (or a paused run) must not freeze the video for minutes
    frames = [(100.0, "/a.jpg"), (900.0, "/b.jpg")]
    lines = build_concat_file(frames).splitlines()
    assert lines[2] == f"duration {MAX_FRAME_DURATION:.3f}"


def test_burst_is_clamped_to_min_duration():
    # near-identical timestamps (frame burst) still get a positive duration
    frames = [(100.0, "/a.jpg"), (100.000001, "/b.jpg")]
    lines = build_concat_file(frames).splitlines()
    assert lines[2] == f"duration {MIN_FRAME_DURATION:.3f}"


def test_every_frame_listed_in_order():
    frames = [(float(i), f"/f{i}.jpg") for i in range(5)]
    out = build_concat_file(frames)
    file_lines = [line for line in out.splitlines() if line.startswith("file ")]
    assert file_lines == [f"file '/f{i}.jpg'" for i in range(5)] + ["file '/f4.jpg'"]
