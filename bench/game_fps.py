"""Measures Subway Surfers' real frame rate from SurfaceFlinger frame timestamps
(no screenshots, which would themselves slow the emulator). Starts a run first if needed.

Usage: .venv/bin/python bench/game_fps.py [--no-start]
"""
from __future__ import annotations

import argparse
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_swipe_check import start_run  # noqa: E402

from sscam.config import load_config  # noqa: E402
from sscam.gamestate import GameStateWatcher, Screen  # noqa: E402
from sscam.injectors import ADB, screen_size  # noqa: E402


def game_layer(serial):
    out = subprocess.run([ADB, "-s", serial, "shell", "dumpsys", "SurfaceFlinger", "--list"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.search(r"(SurfaceView\[com\.kiloo\.subwaysurf[^\]]*\]\(BLAST\)#\d+)", line)
        if m:
            return m.group(1)
    raise SystemExit("game surface not found - is Subway Surfers open?")


def frame_times(serial, layer):
    out = subprocess.run([ADB, "-s", serial, "shell", "dumpsys", "SurfaceFlinger", "--latency", f"'{layer}'"],
                         capture_output=True, text=True).stdout.split("\n")
    ts = []
    for line in out[1:]:
        p = line.split()
        if len(p) == 3 and p[1] not in ("0", "9223372036854775807"):
            ts.append(int(p[1]) / 1e9)
    return sorted(ts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--start-tap", help="x,y fractions to tap if no PLAY button (home screen: 0.3,0.83)")
    a = ap.parse_args()
    cfg = load_config()
    serial = cfg.injector.serial
    scr = screen_size(serial)
    if not a.no_start:
        gate = GameStateWatcher(cfg.game_gate, cfg.injector.console_port, scr)
        if not start_run(cfg, gate, Screen(cfg.injector.console_port, scr[0] // 4, scr[1] // 4), *scr, a.start_tap):
            raise SystemExit(1)
    time.sleep(3.0)
    ts = frame_times(serial, game_layer(serial))
    ts = [t for t in ts if t > ts[-1] - 2.5]
    dts = [b - a for a, b in zip(ts, ts[1:])]
    fps = (len(ts) - 1) / (ts[-1] - ts[0])
    print(f"game fps {fps:.1f}  frame time median {statistics.median(dts) * 1000:.1f} ms  "
          f"p90 {sorted(dts)[int(len(dts) * 0.9)] * 1000:.1f} ms  ({len(ts)} frames)")


if __name__ == "__main__":
    main()
