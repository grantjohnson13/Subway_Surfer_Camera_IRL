"""Knows whether a Subway Surfers run is in progress, so swipes are only sent during play.

Why: outside a run the game shows menus and dialogs whose buttons sit where the swipes go —
the "Continue?" dialog's watch-an-ad button is exactly at screen centre. A body movement there
would press it. The watcher looks for the in-run pause button (top-left) several times per
second. It fails closed: if its information is stale, swipes are suppressed.

Screenshots come from the emulator's gRPC API (host-side framebuffer, downscaled by the
emulator: ~3-8 ms each). `adb screencap` takes seconds while the game is rendering, far too slow.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import cv2

# grpc's fork handler logs an INFO line per fd whenever we spawn adb subprocesses
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
import grpc  # noqa: E402
import numpy as np

from .config import GameGateCfg, StartCfg, resolve
from .emu_grpc import emulator_controller_pb2 as pb
from .emu_grpc import emulator_controller_pb2_grpc as rpc
from .injectors import ADB

DISCOVERY = Path.home() / "Library/Caches/TemporaryItems/avd/running"


def emulator_grpc(console_port: int = 5554):
    """(stub, metadata) for the running emulator with this console port, via its discovery file."""
    for ini in glob.glob(str(DISCOVERY / "*.ini")):
        kv = dict(line.rstrip("\n").split("=", 1) for line in open(ini) if "=" in line)
        if kv.get("port.serial") == str(console_port) and "grpc.port" in kv:
            stub = rpc.EmulatorControllerStub(grpc.insecure_channel(
                f"127.0.0.1:{kv['grpc.port']}", options=[("grpc.max_receive_message_length", 64 << 20)]))
            md = [("authorization", f"Bearer {kv['grpc.token']}")] if "grpc.token" in kv else []
            return stub, md
    raise RuntimeError("emulator gRPC endpoint not found - start the emulator with scripts/start_emulator.sh "
                       "(it passes -grpc 8554 -grpc-use-token)")


class Screen:
    """Fast downscaled screenshots of the emulator display."""

    def __init__(self, console_port: int, width: int, height: int):
        self.stub, self.md = emulator_grpc(console_port)
        self.fmt = pb.ImageFormat(format=pb.ImageFormat.RGB888, width=width, height=height)

    def grab(self) -> np.ndarray:
        img = self.stub.getScreenshot(self.fmt, metadata=self.md, timeout=2)
        w, h = img.format.width, img.format.height
        return cv2.cvtColor(np.frombuffer(img.image, np.uint8).reshape(h, w, 3), cv2.COLOR_RGB2BGR)


def game_fps(serial: str, window_s: float = 2.0) -> float | None:
    """Frame rate of the game's surface over the last window_s, from SurfaceFlinger timestamps."""
    out = subprocess.run([ADB, "-s", serial, "shell", "dumpsys", "SurfaceFlinger", "--list"],
                         capture_output=True, text=True, timeout=3).stdout
    m = re.search(r"(SurfaceView\[com\.kiloo\.subwaysurf[^\]]*\]\(BLAST\)#\d+)", out)
    if not m:
        return None
    out = subprocess.run([ADB, "-s", serial, "shell", "dumpsys", "SurfaceFlinger", "--latency", f"'{m.group(1)}'"],
                         capture_output=True, text=True, timeout=3).stdout.split("\n")
    ts = sorted(int(p[1]) / 1e9 for p in (line.split() for line in out[1:])
                if len(p) == 3 and p[1] not in ("0", "9223372036854775807"))
    ts = [t for t in ts if t > ts[-1] - window_s] if ts else []
    return (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) > 2 and ts[-1] > ts[0] else None


class GameStateWatcher:
    SCALE = 0.5  # screenshots at half resolution
    FPS_EVERY_S = 2.0

    def __init__(self, cfg: GameGateCfg, console_port: int, screen: tuple[int, int], serial: str | None = None):
        self.cfg = cfg
        self.serial = serial
        self.fps: float | None = None
        self._fps_t = 0.0
        w, h = screen
        self.screen = Screen(console_port, int(w * self.SCALE), int(h * self.SCALE))
        tpl = cv2.imread(str(resolve(cfg.template)), cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(cfg.template)
        s = self.SCALE * w / 1080  # template was cut from a 1080-wide screen
        self.tpl = cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        self.roi = (int(w * self.SCALE * 0.3), int(h * self.SCALE * 0.16))  # top-left corner
        self.in_run = False
        self.run_starts: list[float] = []   # perf_counter times at which a run was seen to start
        self._last_grab = None
        self.score = 0.0
        self.updated = 0.0
        self.grab_ms = 0.0
        self.running = True
        self.t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.check_once()
        self.t.start()
        return self

    def check_once(self) -> bool:
        t0 = time.perf_counter()
        img = self.screen.grab()
        self.grab_ms = (time.perf_counter() - t0) * 1000
        rw, rh = self.roi
        roi = cv2.cvtColor(img[:rh, :rw], cv2.COLOR_BGR2GRAY)
        self.score = float(cv2.matchTemplate(roi, self.tpl, cv2.TM_CCOEFF_NORMED).max())
        was = self.in_run
        self.in_run = self.score >= self.cfg.threshold
        if self.in_run and not was and self._last_grab is not None:
            self.run_starts.append((self._last_grab + t0) / 2)   # appeared between the two grabs
        self._last_grab = t0
        self.updated = time.perf_counter()
        return self.in_run

    def _run(self):
        while self.running:
            t0 = time.perf_counter()
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - transient; state goes stale -> fail closed
                pass
            if self.serial and self.in_run and t0 - self._fps_t > self.FPS_EVERY_S:
                self._fps_t = t0
                try:
                    self.fps = game_fps(self.serial)
                except Exception:  # noqa: BLE001
                    self.fps = None
            time.sleep(max(0.0, self.cfg.interval_s - (time.perf_counter() - t0)))

    def allows_swipe(self) -> bool:
        return self.in_run and time.perf_counter() - self.updated <= self.cfg.max_age_s

    def close(self):
        self.running = False


class RunStarter:
    """Starts a run by tapping what is actually on screen: the PLAY button, or a "Tap to continue"
    overlay in front of it. Never taps blindly: other popups (e.g. "Continue? (watch ad)") are
    left alone until they time out. Runs in the background; `busy` while trying."""

    def __init__(self, cfg: StartCfg, gate: GameStateWatcher, serial: str, screen: tuple[int, int]):
        self.cfg, self.gate, self.serial = cfg, gate, serial
        self.w, self.h = screen
        s = gate.SCALE * self.w / 1080   # templates were cut from a 1080-wide screen
        self.tpls = []
        for name in (cfg.play_template, cfg.continue_template):
            t = cv2.imread(str(resolve(name)), cv2.IMREAD_GRAYSCALE)
            if t is None:
                raise FileNotFoundError(name)
            self.tpls.append((Path(name).stem, cv2.resize(t, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)))
        self.busy = False
        self.status = ""

    def trigger(self) -> bool:
        if self.busy or self.gate.in_run:
            return False
        self.busy = True
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def find(self, img) -> tuple[str, int, int] | None:
        """(template name, x, y in device pixels) of the best visible start button, if any."""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for name, t in self.tpls:
            _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED))
            if score >= self.cfg.threshold:
                return name, int((x + t.shape[1] / 2) / self.gate.SCALE), int((y + t.shape[0] / 2) / self.gate.SCALE)
        return None

    def _run(self):
        try:
            deadline = time.perf_counter() + self.cfg.timeout_s
            self.status = "looking for PLAY"
            prev = None
            while time.perf_counter() < deadline and not self.gate.in_run:
                hit = self.find(self.gate.screen.grab())
                # tap only once the button is in the same place twice in a row: taps on a screen
                # that is still sliding in are ignored by the game
                stable = hit is not None and prev is not None and hit[0] == prev[0] \
                    and abs(hit[1] - prev[1]) + abs(hit[2] - prev[2]) <= 8
                prev = hit
                if stable:
                    name, x, y = hit
                    prev = None
                    self.status = f"tapping {name.replace('_', ' ')}"
                    subprocess.run([ADB, "-s", self.serial, "shell", "input", "tap", str(x), str(y)],
                                   capture_output=True, timeout=5)
                    time.sleep(1.0)   # let the screen change before looking again
                else:
                    time.sleep(0.25)
            self.status = "run started" if self.gate.in_run else "no PLAY button found"
        except Exception as e:  # noqa: BLE001
            self.status = f"start failed: {e}"
        finally:
            self.busy = False
