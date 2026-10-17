"""Screen layout and camera lookup for playing on a TV: the emulator window is parked on one side
of the main display (full height), and the camera preview fills the rest, so the game is never
covered (macOS throttles a covered emulator to ~13 fps)."""
from __future__ import annotations

import re
import subprocess
import time

MENU_BAR = 30   # points at the top of the screen that windows cannot use
GAP = 10


def display_size() -> tuple[int, int]:
    """Main display size in screen points."""
    import Quartz
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return int(b.size.width), int(b.size.height)


def camera_index_by_name(name: str) -> int | None:
    """AVFoundation index of the first video device whose name contains `name` (case-insensitive).
    OpenCV's AVFoundation backend numbers devices in the same order ffmpeg lists them."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                             capture_output=True, text=True, timeout=10).stderr
    except (OSError, subprocess.TimeoutExpired):
        return None
    video = out.split("audio devices")[0]
    for m in re.finditer(r"\[(\d+)\] (.+)", video):
        if name.lower() in m.group(2).lower():
            return int(m.group(1))
    return None


def _osascript(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def emulator_bounds() -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the emulator window from the window server; works on any Space."""
    import Quartz
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID):
        if "qemu" in w.get("kCGWindowOwnerName", "") and "Android Emulator" in (w.get("kCGWindowName") or ""):
            b = w["kCGWindowBounds"]
            return int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"])
    return None


def place_emulator(side: str, screen: tuple[int, int], toolbar: int = 58) -> tuple[int, int, int, int]:
    """Brings the emulator to the front and parks it full-height on `side` ("left"/"right").
    If it is on another Space (e.g. the terminal is a full-screen app), macOS switches to it, which
    takes a moment, so this retries until the window can be moved. The emulator keeps its aspect
    ratio, so the size is read back. Returns (x, y, w, h) of the area it occupies, including its
    side toolbar."""
    sw, sh = screen
    win = 'first window whose name contains "Android Emulator"'
    proc = 'first process whose name contains "qemu"'
    _osascript(f'tell application "System Events" to set frontmost of ({proc}) to true')
    size = f'size of ({win})'
    deadline = time.time() + 5
    while True:   # wait until the window is reachable (Space switch)
        try:
            out = _osascript(f'tell application "System Events" to tell ({proc}) to get {size}')
            break
        except RuntimeError:
            if time.time() > deadline:
                raise
            time.sleep(0.3)
    w, h = (int(v) for v in out.split(", "))
    target_h = sh - MENU_BAR - 20
    _osascript(f'tell application "System Events" to tell ({proc}) to set {size} to '
               f'{{{round(w * target_h / h)}, {target_h}}}')
    # the emulator snaps to its own aspect ratio (and the dock) a moment later: wait until stable
    prev, stable_since = None, time.time()
    while time.time() < deadline + 3:
        cur = _osascript(f'tell application "System Events" to tell ({proc}) to get {size}')
        if cur != prev:
            prev, stable_since = cur, time.time()
        elif time.time() - stable_since > 0.4:
            break
        time.sleep(0.1)
    w, h = (int(v) for v in prev.split(", "))
    x = sw - w - toolbar if side == "right" else 0
    _osascript(f'tell application "System Events" to tell ({proc}) to set position of ({win}) to {{{x}, {MENU_BAR + 10}}}')
    return x, MENU_BAR + 10, w + toolbar, h


def preview_rect(screen: tuple[int, int], emu: tuple[int, int, int, int], aspect: float) -> tuple[int, int, int]:
    """Largest preview (x, y, width) with the given aspect (w/h) beside the emulator."""
    sw, sh = screen
    ex, _, ew, _ = emu
    left = ex == 0
    avail_w = (sw - ew - 2 * GAP) if left else (ex - 2 * GAP)
    avail_h = sh - MENU_BAR - 2 * GAP - 60   # leave room for the dock
    width = int(min(avail_w, avail_h * aspect))
    x = (ew + GAP) if left else max(0, (ex - width) // 2)
    y = MENU_BAR + GAP + max(0, (avail_h - int(width / aspect)) // 2)
    return x, y, width
