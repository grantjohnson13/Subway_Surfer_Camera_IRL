"""Checks that swipes register in the real game.

Waits until a Subway Surfers run is in progress (pause button visible — start a run first, or
pass --start-tap), then sends a short swipe sequence with the configured injector. After each
swipe it grabs a burst of emulator screenshots (gRPC, fast) and writes a contact sheet so you
can see the lane change / jump / roll. It never sends a swipe unless the run is still going.

Usage: .venv/bin/python bench/game_swipe_check.py [--seq left,right,up,down] [--method M]
       [--duration-ms N --steps N --hold-ms N --length F] [--start-tap 0.5,0.5] [--out PNG]
"""
from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from sscam.config import load_config
from sscam.gamestate import GameStateWatcher, Screen, game_fps
from sscam.injectors import ADB, make_injector

ROOT = Path(__file__).resolve().parents[1]
DELAYS = (0.0, 0.15, 0.30, 0.50)   # screenshot times after the swipe starts (0 = just before)


PLAY_TPL = ROOT / "assets/play_button.png"


def start_run(cfg, gate, shot, w, h, start_tap, timeout=60.0) -> bool:
    """Start a run only by tapping a PLAY button that is actually visible (template match), or
    an explicit --start-tap point. Never taps blindly: popups make it wait, then give up."""
    tpl = cv2.imread(str(PLAY_TPL))
    tpl = cv2.resize(tpl, None, fx=shot.fmt.width / 1080, fy=shot.fmt.height / 2400)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gate.check_once():
            return True
        img = shot.grab()
        res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        tap = None
        if score >= 0.85:
            sx, sy = w / img.shape[1], h / img.shape[0]
            tap = (int((x + tpl.shape[1] / 2) * sx), int((y + tpl.shape[0] / 2) * sy))
        elif start_tap:
            fx, fy = (float(v) for v in start_tap.split(","))
            tap, start_tap = (int(fx * w), int(fy * h)), None  # explicit point: use once
        if tap:
            subprocess.run([ADB, "-s", cfg.injector.serial, "shell", "input", "tap", str(tap[0]), str(tap[1])])
            t = time.time() + 6
            while time.time() < t:
                if gate.check_once():
                    return True
                time.sleep(0.2)
        time.sleep(1.0)
    print("could not start a run (no PLAY button visible - a popup may be open)")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq", default="left,right,up,down")
    ap.add_argument("--method")
    for k in ("duration-ms", "hold-ms", "length"):
        ap.add_argument(f"--{k}", type=float)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--start-tap", help="x,y fractions to tap to start a run, e.g. 0.5,0.5")
    ap.add_argument("--gap", type=float, default=0.9)
    ap.add_argument("--settle", type=float, default=2.5, help="seconds to wait after the run starts")
    ap.add_argument("--runs", type=int, default=1, help="repeat the sequence over this many runs")
    ap.add_argument("--out", default=str(ROOT / "evidence/game_swipe_check.png"))
    a = ap.parse_args()
    cfg = load_config()
    s = cfg.injector.swipe
    for k in ("duration_ms", "hold_ms", "length", "steps"):
        if getattr(a, k) is not None:
            setattr(s, k, getattr(a, k))
    inj = make_injector(a.method or cfg.injector.method, cfg.injector)
    inj.start()
    w, h = inj.screen
    gate = GameStateWatcher(cfg.game_gate, cfg.injector.console_port, inj.screen)
    shot = Screen(cfg.injector.console_port, w // 4, h // 4)
    rows, sent = [], []
    try:
        for run in range(a.runs):
            if not gate.check_once():
                if not start_run(cfg, gate, shot, w, h, a.start_tap):
                    break
            time.sleep(a.settle)  # let the run-start camera intro finish
            fps = game_fps(cfg.injector.serial)
            print(f"run {run + 1}: game fps {fps:.0f}" if fps else f"run {run + 1}: game fps unknown")
            for d in a.seq.split(","):
                if not gate.check_once():
                    print(f"run {run + 1}: ended before swipe {d}")
                    break
                frames = [shot.grab()]
                t = time.perf_counter()
                inj.swipe(d)
                for delay in DELAYS[1:]:
                    time.sleep(max(0, t + delay - time.perf_counter()))
                    frames.append(shot.grab())
                rows.append((f"{d} (run {run + 1})", frames))
                sent.append(d)
                time.sleep(max(0, a.gap - (time.perf_counter() - t)))
            # wait for this run to end (we stop steering, so the runner crashes soon)
            t_end = time.time() + 90
            while gate.check_once() and time.time() < t_end:
                time.sleep(0.3)
    finally:
        inj.close()
    if not rows:
        raise SystemExit("no swipes sent")
    # one sheet per run, cropped to the runner, so each swipe is easy to judge by eye
    out = Path(a.out)
    by_run: dict[str, list] = {}
    for d, frames in rows:
        by_run.setdefault(d.split("(run ")[1].rstrip(")"), []).append((d, frames))
    for run, items in by_run.items():
        strips = []
        for d, frames in items:
            ims = []
            for f, dt in zip(frames, DELAYS):
                fh, fw = f.shape[:2]
                im = cv2.resize(f[int(fh * 0.45):int(fh * 0.92), int(fw * 0.1):int(fw * 0.9)], None, fx=1.6, fy=1.6)
                cv2.putText(im, f"+{int(dt * 1000)}ms", (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                ims.append(im)
            strip = np.hstack(ims)
            cv2.putText(strip, f"swipe {d}", (6, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 5)
            cv2.putText(strip, f"swipe {d}", (6, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
            strips.append(strip)
        cv2.imwrite(str(out.with_name(f"{out.stem}_run{run}{out.suffix}")), np.vstack(strips))
    print(f"sent {sent}; profile travel {s.duration_ms:.0f} ms / {s.steps} steps / hold {s.hold_ms:.0f} ms / "
          f"length {s.length:.2f}; wrote {out.stem}_run*.png")


if __name__ == "__main__":
    main()
