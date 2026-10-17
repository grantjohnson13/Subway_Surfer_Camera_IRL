import time
from datetime import datetime

import numpy as np

from sscam.recorder import FrameWriter, match_offset, timecode


def test_timecode_is_time_of_day():
    wall = datetime(2026, 10, 16, 12, 28, 15, 500000).timestamp()
    assert timecode(wall, 30) == "12:28:15:15"
    assert timecode(wall, 60) == "12:28:15:30"


def test_match_offset_ignores_unmatched_starts():
    live = [104.3, 121.6, 138.5]
    video = [3.2, 20.5, 37.4, 50.0]          # one extra start seen only in the video
    off, n = match_offset(live, video)
    assert n == 3 and abs(off - 101.1) < 1e-6


def test_frame_writer_keeps_wall_clock_time(tmp_path):
    # 2 s of frames arriving at an uneven ~20 fps must still give a 30 fps file of ~2 s
    t0 = time.perf_counter()
    w = FrameWriter(tmp_path / "x.mp4", (64, 48), 30, "1M", t0)
    t = t0
    while t - t0 < 2.0:
        w.write(np.zeros((48, 64, 3), np.uint8), t)
        t += 0.05 if int((t - t0) * 10) % 2 else 0.04
        time.sleep(max(0.0, t - time.perf_counter()))   # frames arrive in real time, like a camera
    w.close()
    assert abs(w.written - 60) <= 2 and w.dropped == 0
    assert (tmp_path / "x.mp4").stat().st_size > 0
