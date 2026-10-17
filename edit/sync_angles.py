"""Syncs external camera clips (other angles) to recorded sessions by your body's movement.

For every clip it tracks your pose, takes the vertical motion of your torso (jumps and ducks look
the same from any angle), and cross-correlates it with the same signal from each session's
camera.mp4. The best match gives both which session a clip belongs to and its offset.

Usage: .venv/bin/python edit/sync_angles.py ~/Downloads/Angle_1.mov ~/Downloads/Angle_2.mov ...
Writes recordings/<session>/angles.json:  [{"file": ..., "offset_s": ...}]
  offset_s = session time (camera.mp4 time) at which the clip starts (negative: clip starts first)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sscam.config import load_config  # noqa: E402
from sscam.pose import PoseTracker  # noqa: E402

RATE = 30.0          # analysis frame rate
WIDTH = 640          # analysis width


def probe_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    line = next(ln for ln in out.stdout.splitlines() if ln.strip())
    w, h = (int(v) for v in line.split(",")[:2])
    return w, h


def torso_track(path: Path, cache: Path) -> np.ndarray:
    """Torso centre y (in torso lengths, NaN when not seen) at RATE Hz from the file's start."""
    if cache.exists():
        return np.load(cache)
    w, h = probe_size(path)
    ow, oh = WIDTH, int(round(WIDTH * h / w / 2)) * 2
    dec = subprocess.Popen(["ffmpeg", "-loglevel", "fatal", "-i", str(path), "-vf",
                            f"fps={RATE},scale={ow}:{oh}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
                           stdout=subprocess.PIPE)
    tracker = PoseTracker(load_config().pose)
    ys, i = [], 0
    while True:
        buf = dec.stdout.read(ow * oh * 3)
        if len(buf) < ow * oh * 3:
            break
        img = np.frombuffer(buf, np.uint8).reshape(oh, ow, 3)
        lms = tracker.detect(img, i / RATE)
        y = np.nan
        if lms is not None and min(lms[k].visibility or 0 for k in (11, 12, 23, 24)) > 0.4:
            sy = (lms[11].y + lms[12].y) / 2 * oh
            hy = (lms[23].y + lms[24].y) / 2 * oh
            sx = (lms[11].x + lms[12].x) / 2 * ow
            hx = (lms[23].x + lms[24].x) / 2 * ow
            torso = np.hypot(sx - hx, sy - hy)
            if torso > 5:
                y = (sy + hy) / 2 / torso
        ys.append(y)
        i += 1
        if i % 1800 == 0:
            print(f"  {path.name}: {i / RATE / 60:.0f} min", flush=True)
    tracker.close()
    dec.wait()
    a = np.array(ys, dtype=float)
    np.save(cache, a)
    return a


def motion(y: np.ndarray) -> np.ndarray:
    """Vertical velocity, normalized, 0 where untracked; big moves dominate."""
    v = np.diff(y, prepend=np.nan)
    v[~np.isfinite(v)] = 0
    v = np.clip(v, -np.percentile(np.abs(v), 99.5), np.percentile(np.abs(v), 99.5))
    s = v.std()
    return v / s if s > 0 else v


def best_lag(ref: np.ndarray, clip: np.ndarray) -> tuple[float, float]:
    """Lag (s) such that clip sample k matches ref sample k + lag*RATE, and a peak-sharpness score
    (peak height over the typical correlation, ~>8 means unambiguous)."""
    n = len(ref) + len(clip)
    size = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(ref, size) * np.conj(np.fft.rfft(clip, size)), size)
    # index L: ref[k + L] ~ clip[k]; negative lags wrap to the end
    lags = np.concatenate([np.arange(0, len(ref)), np.arange(-len(clip) + 1, 0)])
    vals = np.concatenate([corr[:len(ref)], corr[size - len(clip) + 1:]])
    # normalize by overlap so short overlaps at the edges don't win
    overlap = np.minimum(len(ref), lags + len(clip)) - np.maximum(0, lags)
    vals = np.where(overlap > RATE * 20, vals / np.sqrt(np.maximum(overlap, 1)), 0)
    k = int(np.argmax(vals))
    lag = float(lags[k])
    if 0 < k < len(vals) - 1:   # sub-frame refinement (parabola through the peak)
        a, b, c = vals[k - 1], vals[k], vals[k + 1]
        d = a - 2 * b + c
        if d != 0:
            lag += 0.5 * (a - c) / d
    score = vals[k] / (np.median(np.abs(vals[vals != 0])) + 1e-9)
    return lag / RATE, float(score)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--sessions", nargs="*", help="session folders (default: all in recordings/)")
    a = ap.parse_args()
    sessions = [Path(s) for s in a.sessions] if a.sessions else sorted((ROOT / "recordings").glob("*/"))
    sessions = [s for s in sessions if (s / "camera.mp4").exists()]

    refs = {}
    for s in sessions:
        print(f"tracking {s.name}/camera.mp4", flush=True)
        refs[s] = motion(torso_track(s / "camera.mp4", s / "pose_track.npy"))

    results = {s: [] for s in sessions}
    for c in map(Path, a.clips):
        c = c.expanduser().resolve()
        print(f"tracking {c.name}", flush=True)
        cache = ROOT / "recordings" / f".pose_{c.stem}.npy"
        m = motion(torso_track(c, cache))
        scores = []
        for s in sessions:
            lag, score = best_lag(refs[s], m)
            scores.append((score, s, lag))
            print(f"  vs {s.name}: offset {lag:+.3f} s  match {score:.1f}")
        scores.sort(key=lambda x: -x[0])
        score, s, lag = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else 0
        print(f"  -> {s.name} at {lag:+.3f} s (match {score:.1f}, next best {runner_up:.1f})")
        results[s].append({"file": str(c), "offset_s": round(lag, 4), "match": round(score, 1)})

    for s, items in results.items():
        if items:
            (s / "angles.json").write_text(json.dumps(items, indent=2))
            print(f"wrote {s / 'angles.json'}")


if __name__ == "__main__":
    main()
