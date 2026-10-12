"""Live end-to-end demo: plays a gesture video through the full live pipeline (MediaPipe ->
detector -> run guard -> injector) while a real Subway Surfers run is in progress.
Starts the app, then presses PLAY (only if it is visible) as the video begins.

Usage: .venv/bin/python bench/drive_game_with_video.py [video] [--events out.jsonl]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from game_swipe_check import start_run  # noqa: E402

from sscam.config import load_config  # noqa: E402
from sscam.gamestate import GameStateWatcher, Screen  # noqa: E402
from sscam.injectors import screen_size  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

ap = argparse.ArgumentParser()
ap.add_argument("video", nargs="?", default=str(ROOT / "tests/data/gestures_fast.mp4"))
ap.add_argument("--events", default=str(ROOT / "logs/live_video_into_game_events.jsonl"))
a = ap.parse_args()
cfg = load_config()
scr = screen_size(cfg.injector.serial)
gate = GameStateWatcher(cfg.game_gate, cfg.injector.console_port, scr)
shot = Screen(cfg.injector.console_port, scr[0] // 4, scr[1] // 4)
if gate.check_once():
    raise SystemExit("a run is already in progress; wait for it to end")
app = subprocess.Popen([sys.executable, "-m", "sscam.app", "--video", a.video, "--realtime", "--no-preview",
                        "--events", a.events], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       cwd=ROOT)
for line in app.stdout:
    if "[sscam]" in line:
        print(line, end="", flush=True)
    if "[sscam] source" in line:
        break
t0 = time.time()
ok = start_run(cfg, gate, shot, *scr, None, timeout=5)
print(f"[drive] run started: {ok} ({time.time() - t0:.1f} s after video start)", flush=True)
for line in app.stdout:
    if "[sscam]" in line:
        print(line, end="", flush=True)
app.wait()
