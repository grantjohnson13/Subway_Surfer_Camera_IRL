"""Swipe injectors for the Android emulator.

Every injector exposes the same interface:
    inj = make_injector("scrcpy", cfg); inj.start(); inj.swipe("left"); inj.close()

`swipe()` returns once the whole gesture has been *handed off* (for fire-and-forget
methods like the persistent shell that is before Android has processed it); the
benchmark measures when Android actually delivers it to an app.
"""
from __future__ import annotations

import os
import queue
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from .config import InjectorCfg, SwipeCfg

ANDROID_HOME = Path(os.environ.get("ANDROID_HOME", Path.home() / "Library/Android/sdk"))
ADB = str(ANDROID_HOME / "platform-tools/adb") if (ANDROID_HOME / "platform-tools/adb").exists() \
    else (shutil.which("adb") or "adb")
ROOT = Path(__file__).resolve().parents[2]

DIRECTIONS = ("left", "right", "up", "down")
# Gesture name -> swipe direction
GESTURE_TO_SWIPE = {"left": "left", "right": "right", "jump": "up", "duck": "down"}


class InjectorUnavailable(RuntimeError):
    """Raised when a method cannot run on this device/host (e.g. blocked by SELinux)."""


def adb(serial: str, *args: str, timeout: float = 15, check: bool = True) -> str:
    out = subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=True, timeout=timeout)
    if check and out.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout


def screen_size(serial: str) -> tuple[int, int]:
    out = adb(serial, "shell", "wm", "size")
    # "Physical size: 1080x2400" (+ optional "Override size: ...", which wins)
    size = None
    for line in out.splitlines():
        if "size:" in line:
            w, h = line.split(":")[1].strip().split("x")
            size = (int(w), int(h))
    if not size:
        raise RuntimeError(f"cannot parse wm size: {out!r}")
    return size


def swipe_path(direction: str, screen: tuple[int, int], s: SwipeCfg) -> list[tuple[int, int]]:
    """Points from DOWN to UP (inclusive), steps+2 points, in device pixels."""
    w, h = screen
    cx, cy = s.center_x * w, s.center_y * h
    half = s.length * w / 2
    dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[direction]
    x0, y0 = cx - dx * half, cy - dy * half
    x1, y1 = cx + dx * half, cy + dy * half
    n = s.steps + 1
    return [(round(x0 + (x1 - x0) * i / n), round(y0 + (y1 - y0) * i / n)) for i in range(n + 1)]


def _sleep_until(t: float) -> None:
    # time.sleep has ~1 ms granularity on macOS; spin the last bit for accurate step pacing.
    while True:
        rem = t - time.perf_counter()
        if rem <= 0:
            return
        time.sleep(rem - 0.0008 if rem > 0.0015 else 0)


class Injector:
    name = "base"
    #: True if swipe() returns before Android has received the events
    fire_and_forget = False

    def __init__(self, cfg: InjectorCfg, screen: tuple[int, int] | None = None):
        self.cfg = cfg
        self.serial = cfg.serial
        self.screen = screen or screen_size(cfg.serial)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def path(self, direction: str) -> list[tuple[int, int]]:
        return swipe_path(direction, self.screen, self.cfg.swipe)

    def swipe(self, direction: str) -> None:
        pts = self.path(direction)
        step_dt = self.cfg.swipe.duration_ms / 1000 / (len(pts) - 1)
        t = time.perf_counter()
        self._down(*pts[0])
        for x, y in pts[1:-1]:
            t += step_dt
            _sleep_until(t)
            self._move(x, y)
        t += step_dt
        _sleep_until(t)
        if self.cfg.swipe.hold_ms > 0:
            # Finger stays at the end point for a moment so frame-polling games (Unity)
            # see the moved touch on at least one frame before it lifts.
            self._move(*pts[-1])
            t += self.cfg.swipe.hold_ms / 1000
            _sleep_until(t)
        self._up(*pts[-1])

    # Event-level methods for streaming injectors
    def _down(self, x: int, y: int) -> None: raise NotImplementedError
    def _move(self, x: int, y: int) -> None: raise NotImplementedError
    def _up(self, x: int, y: int) -> None: raise NotImplementedError

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------- adb input
class AdbInputInjector(Injector):
    """`adb shell input swipe` — a new adb connection + a new `input` JVM per swipe."""
    name = "adb_input"

    def swipe(self, direction: str) -> None:
        (x0, y0), (x1, y1) = self.path(direction)[0], self.path(direction)[-1]
        adb(self.serial, "shell", "input", "swipe", str(x0), str(y0), str(x1), str(y1),
            str(int(self.cfg.swipe.duration_ms + self.cfg.swipe.hold_ms)))


# --------------------------------------------------------------------------- persistent shell
class PersistentShellInjector(Injector):
    """One long-lived `adb shell`; each swipe is a line written to its stdin.
    Saves the adb connection setup, but Android still starts an `input` process per swipe."""
    name = "persistent_shell"
    fire_and_forget = True

    def start(self) -> None:
        self.proc = subprocess.Popen([ADB, "-s", self.serial, "shell"], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)

    def swipe(self, direction: str) -> None:
        (x0, y0), (x1, y1) = self.path(direction)[0], self.path(direction)[-1]
        self.proc.stdin.write(
            f"input swipe {x0} {y0} {x1} {y1} {int(self.cfg.swipe.duration_ms + self.cfg.swipe.hold_ms)}\n".encode())
        self.proc.stdin.flush()

    def close(self) -> None:
        if getattr(self, "proc", None):
            try:
                self.proc.stdin.write(b"exit\n")
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()


# --------------------------------------------------------------------------- minitouch
class MinitouchInjector(Injector):
    """openstf/DeviceFarmer minitouch: writes straight to /dev/input/eventN on the device."""
    name = "minitouch"
    REMOTE = "/data/local/tmp/minitouch"

    def start(self) -> None:
        local = ROOT / "vendor/minitouch-arm64"
        if not local.exists():
            raise InjectorUnavailable("vendor/minitouch-arm64 missing")
        adb(self.serial, "push", str(local), self.REMOTE)
        adb(self.serial, "shell", "chmod", "755", self.REMOTE)
        self.proc = subprocess.Popen([ADB, "-s", self.serial, "shell", self.REMOTE, "-i"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, bufsize=0)
        header = b""
        deadline = time.time() + 5
        while time.time() < deadline and b"$" not in header:
            line = self.proc.stdout.readline()
            if not line:
                break
            header += line
        if b"^ " not in header:
            self.close()
            reason = header.decode(errors="replace").strip().splitlines()
            raise InjectorUnavailable("minitouch could not open the touch device: "
                                      + (reason[0] if reason else "no output"))
        for line in header.decode().splitlines():
            if line.startswith("^"):
                _, _contacts, self.max_x, self.max_y, _p = line.split()
                self.max_x, self.max_y = int(self.max_x), int(self.max_y)

    def _send(self, cmd: str) -> None:
        self.proc.stdin.write(cmd.encode())
        self.proc.stdin.flush()

    def _map(self, x, y):
        return x * self.max_x // self.screen[0], y * self.max_y // self.screen[1]

    def _down(self, x, y): self._send("d 0 %d %d 50\nc\n" % self._map(x, y))
    def _move(self, x, y): self._send("m 0 %d %d 50\nc\n" % self._map(x, y))
    def _up(self, x, y): self._send("u 0\nc\n")

    def close(self) -> None:
        if getattr(self, "proc", None):
            self.proc.kill()


# --------------------------------------------------------------------------- uiautomator2
class Uiautomator2Injector(Injector):
    """uiautomator2: HTTP JSON-RPC to an on-device uiautomator server."""
    name = "uiautomator2"

    def start(self) -> None:
        import uiautomator2 as u2
        self.d = u2.connect(self.serial)
        self.d.info  # forces the server up now rather than on the first swipe

    def swipe(self, direction: str) -> None:
        (x0, y0), (x1, y1) = self.path(direction)[0], self.path(direction)[-1]
        # uiautomator injects one MOVE per step, ~5 ms apart
        steps = max(2, round((self.cfg.swipe.duration_ms + self.cfg.swipe.hold_ms) / 5))
        self.d.swipe(x0, y0, x1, y1, steps=steps)


# --------------------------------------------------------------------------- scrcpy control
class ScrcpyInjector(Injector):
    """scrcpy-server with video/audio off, control only. A long-lived on-device process
    injects MotionEvents via InputManager; we speak its binary control protocol."""
    name = "scrcpy"
    REMOTE = "/data/local/tmp/scrcpy-server.jar"
    PORT = 27183
    ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
    POINTER_ID = 0x1234  # any id other than -1 (mouse) is treated as a finger

    @staticmethod
    def _server_jar() -> tuple[Path, str]:
        exe = shutil.which("scrcpy")
        if not exe:
            raise InjectorUnavailable("scrcpy not installed (brew install scrcpy)")
        version = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.split()[1]
        for cand in (Path(exe).resolve().parents[1] / "share/scrcpy/scrcpy-server",
                     Path("/opt/homebrew/share/scrcpy/scrcpy-server")):
            if cand.exists():
                return cand, version
        raise InjectorUnavailable("scrcpy-server jar not found")

    def start(self) -> None:
        jar, version = self._server_jar()
        adb(self.serial, "push", str(jar), self.REMOTE)
        adb(self.serial, "forward", "--remove", f"tcp:{self.PORT}", check=False)
        adb(self.serial, "forward", f"tcp:{self.PORT}", "localabstract:scrcpy")
        self.proc = subprocess.Popen(
            [ADB, "-s", self.serial, "shell", f"CLASSPATH={self.REMOTE}", "app_process", "/",
             "com.genymobile.scrcpy.Server", version, "tunnel_forward=true", "video=false",
             "audio=false", "control=true", "send_dummy_byte=true", "send_device_meta=false",
             "cleanup=false", "log_level=warn"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 8
        while True:
            try:
                s = socket.create_connection(("127.0.0.1", self.PORT), timeout=2)
                if s.recv(1) == b"\x00":
                    break
                s.close()
            except OSError:
                pass
            if time.time() > deadline:
                self.close()
                raise InjectorUnavailable("scrcpy server did not accept a connection")
            time.sleep(0.1)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(None)
        self.sock = s
        # The server may send device messages (clipboard etc.); drain them so it never blocks.
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        try:
            while self.sock.recv(4096):
                pass
        except OSError:
            pass

    def _touch(self, action: int, x: int, y: int, pressure: float) -> None:
        w, h = self.screen
        msg = struct.pack(">BBqiiHHHii", 2, action, self.POINTER_ID, x, y, w, h,
                          0xFFFF if pressure >= 1 else int(pressure * 0x10000), 0, 0)
        self.sock.sendall(msg)

    def _down(self, x, y): self._touch(self.ACTION_DOWN, x, y, 1.0)
    def _move(self, x, y): self._touch(self.ACTION_MOVE, x, y, 1.0)
    def _up(self, x, y): self._touch(self.ACTION_UP, x, y, 0.0)

    def close(self) -> None:
        if getattr(self, "sock", None):
            self.sock.close()
        if getattr(self, "proc", None):
            self.proc.terminate()
        adb(self.serial, "forward", "--remove", f"tcp:{self.PORT}", check=False)


# --------------------------------------------------------------------------- emulator console
class EmulatorConsoleInjector(Injector):
    """Host-side: the emulator's telnet console `event mouse x y 0 <buttons>` feeds the
    virtual touchscreen directly (below Android's SELinux), no adb in the path."""
    name = "emu_console"

    def start(self) -> None:
        token_file = Path.home() / ".emulator_console_auth_token"
        port = self.cfg.console_port
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
        except OSError as e:
            raise InjectorUnavailable(f"emulator console port {port} not reachable: {e}")
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self._expect_ok()  # banner
        if token_file.exists():
            s.sendall(f"auth {token_file.read_text().strip()}\n".encode())
            self._expect_ok()
        self._acks: queue.SimpleQueue = queue.SimpleQueue()
        threading.Thread(target=self._drain, daemon=True).start()

    def _expect_ok(self) -> None:
        buf = b""
        deadline = time.time() + 3
        while not buf.rstrip().endswith(b"OK") and time.time() < deadline:
            buf += self.sock.recv(4096)
            if b"KO" in buf:
                raise InjectorUnavailable(f"emulator console refused: {buf.decode(errors='replace')}")
        if not buf.rstrip().endswith(b"OK"):
            raise InjectorUnavailable("emulator console handshake timed out")

    def _drain(self) -> None:
        self.sock.settimeout(None)
        try:
            while self.sock.recv(4096):
                pass
        except OSError:
            pass

    def _cmd(self, x: int, y: int, buttons: int) -> None:
        self.sock.sendall(f"event mouse {x} {y} 0 {buttons}\n".encode())

    def _down(self, x, y): self._cmd(x, y, 1)
    def _move(self, x, y): self._cmd(x, y, 1)
    def _up(self, x, y): self._cmd(x, y, 0)

    def close(self) -> None:
        if getattr(self, "sock", None):
            try:
                self.sock.sendall(b"quit\n")
            except OSError:
                pass
            self.sock.close()


# --------------------------------------------------------------------------- scrcpy mirror + pyautogui
class PyAutoGuiMirrorInjector(Injector):
    """Fallback: mirror the device with scrcpy and drag the real macOS mouse over the window.
    Needs Accessibility permission for the terminal; takes over your mouse while swiping."""
    name = "pyautogui_mirror"
    WIN_X, WIN_Y, SCALE = 40, 80, 0.25

    def start(self) -> None:
        import pyautogui
        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = False
        self.pg = pyautogui
        w, h = self.screen
        self.win_w, self.win_h = int(w * self.SCALE), int(h * self.SCALE)
        self.proc = subprocess.Popen(
            [shutil.which("scrcpy") or "scrcpy", "-s", self.serial, "--no-audio",
             "--window-borderless", "--window-title", "sscam-mirror",
             f"--window-x={self.WIN_X}", f"--window-y={self.WIN_Y}",
             f"--window-width={self.win_w}", f"--window-height={self.win_h}",
             "--max-fps=60", "--video-bit-rate=4M"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.5)
        if self.proc.poll() is not None:
            raise InjectorUnavailable("scrcpy mirror window failed to open")

    def _map(self, x, y):
        return self.WIN_X + x * self.win_w / self.screen[0], self.WIN_Y + y * self.win_h / self.screen[1]

    def _down(self, x, y):
        self.pg.moveTo(*self._map(x, y), _pause=False)
        self.pg.mouseDown(_pause=False)

    def _move(self, x, y): self.pg.moveTo(*self._map(x, y), _pause=False)

    def _up(self, x, y):
        self.pg.moveTo(*self._map(x, y), _pause=False)
        self.pg.mouseUp(_pause=False)

    def close(self) -> None:
        if getattr(self, "proc", None):
            self.proc.terminate()


# --------------------------------------------------------------------------- dry run
class NullInjector(Injector):
    name = "dry_run"

    def __init__(self, cfg: InjectorCfg, screen=None):
        self.cfg, self.serial, self.screen = cfg, cfg.serial, screen or (1080, 2400)

    def swipe(self, direction: str) -> None:
        pass


METHODS: dict[str, type[Injector]] = {c.name: c for c in (
    AdbInputInjector, PersistentShellInjector, MinitouchInjector, Uiautomator2Injector,
    ScrcpyInjector, EmulatorConsoleInjector, PyAutoGuiMirrorInjector, NullInjector)}


def make_injector(method: str, cfg: InjectorCfg, screen=None) -> Injector:
    if method not in METHODS:
        raise ValueError(f"unknown injector {method!r}; choose from {sorted(METHODS)}")
    return METHODS[method](cfg, screen)


def start_best(cfg: InjectorCfg) -> Injector:
    """Start cfg.method, falling back to cfg.fallback if it is unavailable."""
    errors = []
    for method in dict.fromkeys([cfg.method, cfg.fallback, "adb_input"]):
        try:
            inj = make_injector(method, cfg)
            inj.start()
            return inj
        except Exception as e:  # noqa: BLE001 - report and try the next method
            errors.append(f"{method}: {e}")
    raise RuntimeError("no injector could start:\n  " + "\n  ".join(errors))


class AsyncSwiper:
    """Runs swipes on a worker thread so the camera loop never blocks on input delivery.
    Swipes are serialized (one finger). Calls on_sent(gesture, t_sent) when each is handed off."""

    def __init__(self, injector: Injector, on_sent=None):
        self.inj = injector
        self.on_sent = on_sent
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def submit(self, gesture: str, meta=None) -> None:
        self.q.put((gesture, meta))

    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                return
            gesture, meta = item
            t0 = time.perf_counter()
            try:
                self.inj.swipe(GESTURE_TO_SWIPE[gesture])
                err = None
            except Exception as e:  # noqa: BLE001
                err = e
            if self.on_sent:
                self.on_sent(gesture, meta, t0, time.perf_counter(), err)

    def close(self) -> None:
        self.q.put(None)
        self.t.join(timeout=2)
        self.inj.close()
