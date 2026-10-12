"""End-to-end: video file -> MediaPipe Pose -> detector -> (dry-run) injector, via app.main."""
from __future__ import annotations

import json

import pytest

from sscam import app
from videogen import make_video


@pytest.fixture(scope="module")
def gesture_video(tmp_path_factory):
    p = tmp_path_factory.mktemp("vid") / "gestures.mp4"
    truth = make_video(p, seed=1)
    return p, truth


def run_app(video, tmp_path, *extra):
    ev = tmp_path / "events.jsonl"
    rc = app.main(["--video", str(video), "--dry-run", "--no-preview", "--events", str(ev), *extra])
    assert rc == 0
    return [json.loads(line) for line in ev.read_text().splitlines()]


def test_video_pipeline_detects_each_gesture_once_at_movement_start(gesture_video, tmp_path):
    """Default (lanes) mode: stepping into a side third and back is two swipes (out, back)."""
    video, truth = gesture_video
    events = run_app(video, tmp_path)
    back = {"left": "right", "right": "left"}
    expected = [x for g, _, _ in truth for x in ([g, back[g]] if g in back else [g])]
    assert [e["gesture"] for e in events] == expected
    i = 0
    for g, start, end in truth:
        e = events[i]
        # the outgoing swipe fires after the move begins and before it is complete
        assert e["gesture"] == g and start < e["t"] < end, f"{g} fired at {e['t']:.3f}, move {start:.2f}-{end:.2f}"
        i += 2 if g in back else 1
    for e in events:
        assert e["latency_capture_to_sent_ms"] < 100


def test_video_pipeline_lean_mode(gesture_video, tmp_path):
    """Lean mode: each lean is one lane change; returning to centre sends nothing."""
    video, truth = gesture_video
    cfg = tmp_path / "lean.yaml"
    cfg.write_text(open(app.ROOT / "config.yaml").read().replace("lateral_mode: lanes", "lateral_mode: lean"))
    events = run_app(video, tmp_path, "--config", str(cfg))
    assert [e["gesture"] for e in events] == [g for g, _, _ in truth]


def test_video_pipeline_standing_still_fires_nothing(tmp_path):
    p = tmp_path / "still.mp4"
    make_video(p, seed=2, still_s=10)
    assert run_app(p, tmp_path) == []


def test_record_landmarks_replay_matches(gesture_video, tmp_path):
    """Landmarks recorded from a run can be replayed through the detector with identical results
    (this is how sessions recorded with --record-landmarks become regression tests)."""
    from sscam.config import load_config
    from sscam.detector import Calibrator, GestureDetector, sample_from_landmarks

    video, truth = gesture_video
    rec = tmp_path / "rec.jsonl"
    events = run_app(video, tmp_path, "--record-landmarks", str(rec))
    cfg = load_config()
    cal, det, fired = Calibrator(cfg), None, []
    for line in rec.read_text().splitlines():
        r = json.loads(line)
        s = sample_from_landmarks(r["t"], r["lms"], r["w"], r["h"], cfg.pose.min_visibility) if r["lms"] else None
        if det is None:
            if cal.update(s):
                det = GestureDetector(cfg, cal.baseline)
            continue
        f = det.update(s)
        if f.gesture:
            fired.append(f.gesture)
    assert fired == [e["gesture"] for e in events]
