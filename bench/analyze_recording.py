"""Replays a session recorded with `--record-landmarks` through the detector and reports what
fired, what almost fired, and how big/fast your movements were — the data for tuning config.yaml.

Usage: .venv/bin/python bench/analyze_recording.py logs/tuning.jsonl [--config other.yaml]
"""
from __future__ import annotations

import argparse
import json

from sscam.config import load_config
from sscam.detector import Calibrator, GestureDetector, sample_from_landmarks

AXES = {"left": ("dx", "vx", -1), "right": ("dx", "vx", 1), "jump": ("dy", "vy", -1), "duck": ("dy", "vy", 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config) if a.config else load_config()

    cal, det = Calibrator(cfg), None
    frames, fired, lost = [], [], 0
    for line in open(a.recording):
        r = json.loads(line)
        s = sample_from_landmarks(r["t"], r["lms"], r["w"], r["h"], cfg.pose.min_visibility) if r["lms"] else None
        if det is None:
            if cal.update(s):
                det = GestureDetector(cfg, cal.baseline)
                print(f"calibrated at t={r['t']:.1f}s  torso={cal.baseline.torso:.0f}px")
            continue
        f = det.update(s)
        lost += not f.tracking
        if f.tracking:
            frames.append(f)
        if f.gesture:
            fired.append(f)
    if det is None:
        raise SystemExit(f"never calibrated: {cal.message}")

    print(f"\n{len(frames)} tracked frames, {lost} without a body")
    print("\nGestures fired:")
    for f in fired:
        print(f"  t={f.t:7.2f}s  {f.gesture:5s}  dx={f.dx:+.2f} dy={f.dy:+.2f}  vx={f.vx:+.1f} vy={f.vy:+.1f}")

    print("\nPer gesture: how far your movements went (torso lengths, torso lengths/s)")
    print(f"  {'':6s} {'trigger':>7s} {'min_off':>7s} {'min_vel':>7s} | {'peak offset':>11s} {'peak vel':>8s}  {'fired':>5s}  near-misses")
    for g, (ok, vk, sg) in AXES.items():
        gc = cfg.gestures[g]
        offs = [sg * getattr(f, ok) for f in frames]
        vels = [sg * getattr(f, vk) for f in frames]
        n_fired = sum(1 for f in fired if f.gesture == g)
        # near-miss episodes: projection reached >= 70% of trigger with real movement, but no fire nearby
        near, last_t = [], -9.0
        fire_ts = [f.t for f in fired if f.gesture == g]
        for f, o, v in zip(frames, offs, vels):
            proj = o + max(v, 0) * cfg.lookahead_s
            # o > 0: on the gesture's side of neutral (a return toward neutral is not a near-miss)
            if o > 0 and proj >= 0.7 * gc.trigger and v > 0.3 and f.t - last_t > 0.6 \
                    and not any(abs(f.t - ft) < 0.8 for ft in fire_ts):
                near.append((f.t, o, v, proj))
                last_t = f.t
        print(f"  {g:6s} {gc.trigger:7.2f} {gc.min_offset:7.2f} {gc.min_velocity:7.1f} | "
              f"{max(offs, default=0):11.2f} {max(vels, default=0):8.1f}  {n_fired:5d}  {len(near)}")
        for t, o, v, p in near[:5]:
            why = []
            if o < gc.min_offset:
                why.append(f"offset {o:.2f} < min_offset")
            if v < gc.min_velocity:
                why.append(f"velocity {v:.1f} < min_velocity")
            if p < gc.trigger:
                why.append(f"projected {p:.2f} < trigger")
            print(f"           near-miss t={t:.2f}s: " + ", ".join(why or ["blocked by latch/cooldown/dominance"]))

    print("\nHow to read this: if you performed a gesture and it is listed as a near-miss, lower the "
          "threshold named in the reason (e.g. to ~90% of the value you reached). If a gesture fired "
          "when you did not mean it, raise its trigger/min_offset above the peak it reached then.")


if __name__ == "__main__":
    main()
