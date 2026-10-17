"""`--record`: one folder per session with every view of the session, all starting at the same instant.

  camera.mp4   the clean webcam feed (not mirrored)
  overlay.mp4  the preview as shown on screen (mirrored, lane lines)
  game.mp4     the emulator screen at full resolution (scrcpy), trimmed to start with the others
  markers.json start time and each hands-up round start, as file time and time of day

Silent sync for an external camera: every file carries a time-of-day timecode (and a creation
time to the millisecond) taken from the Mac's clock. Record the other camera with time-of-day
timecode too (e.g. the Blackmagic Camera app on an iPhone), and an editor lines them up by
timecode (Premiere: select the clips > Synchronize > Timecode). For checking or fine-tuning by
eye, the preview flashes white at the start and at every hands-up round start - film the TV in
the other shot and match the flash.

How the game video is aligned: scrcpy's recording starts at an unknown moment, so its offset is
measured afterwards. Every run start (the in-game pause button appearing) is found in game.mp4
and matched with the moment the run guard saw it live (within ~15 ms).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from .config import RecordCfg, resolve
from .injectors import ADB

def log(msg: str):
    print(f"[record] {msg}", file=sys.stderr, flush=True)


class FrameWriter:
    """Constant-frame-rate H.264 (VideoToolbox, hardware) from frames stamped with wall-clock time.
    Frame k of the file shows what was newest at t0 + k/fps (repeats or skips frames as needed), so
    file time == session time. Encoding happens on a background thread; never blocks the caller."""

    def __init__(self, path: Path, size: tuple[int, int], fps: int, bitrate: str, t0: float):
        self.path, self.fps, self.t0 = path, fps, t0
        w, h = size
        self.p = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
             "-r", str(fps), "-i", "-", "-c:v", "h264_videotoolbox", "-b:v", bitrate, "-pix_fmt", "yuv420p",
             str(path)], stdin=subprocess.PIPE)
        self.q: queue.Queue = queue.Queue(maxsize=fps)
        self.written = 0
        self.dropped = 0
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def write(self, frame: np.ndarray, t: float):
        try:
            self.q.put_nowait((frame, t))
        except queue.Full:
            self.dropped += 1

    def _run(self):
        last = None
        while True:
            item = self.q.get()
            if item is None:
                break
            frame, t = item
            target = int(round((t - self.t0) * self.fps)) + 1   # frames that should exist by now
            buf = frame.tobytes()
            if last is None:
                last = buf
            while self.written < target - 1:   # gap: repeat the previous frame
                self.p.stdin.write(last)
                self.written += 1
            if self.written < target:
                self.p.stdin.write(buf)
                self.written += 1
            last = buf
        self.p.stdin.close()
        self.p.wait()

    def close(self):
        self.q.put(None)
        self.t.join(timeout=30)


def run_starts_in_video(path: Path, tpl: np.ndarray, threshold: float, min_frames: int = 5) -> list[float]:
    """Times (s, file time) at which the in-run pause button appears in a game recording."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        w, h = (int(v) for v in probe.stdout.strip().split(",")[:2])
    except ValueError:
        log(f"cannot read {path.name}: {probe.stderr.strip()[-200:]}")
        return []
    rw, rh = int(w * 0.3) // 2 * 2, int(h * 0.16) // 2 * 2   # same corner the live run guard checks
    ow, oh = rw // 2, rh // 2                                 # live guard works at half resolution
    # Decode onto a fixed 120 fps grid: frame i is at file time i/120 (relative to the file's
    # start, the same reference ffmpeg's -ss uses). scrcpy's recording is variable frame rate.
    grid = 120
    dec = subprocess.Popen(["ffmpeg", "-loglevel", "fatal", "-i", str(path),
                            "-vf", f"crop={rw}:{rh}:0:0,scale={ow}:{oh},format=gray,fps={grid}",
                            "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    need = min_frames * grid // 60   # same duration as min_frames game frames
    starts, run, prev_in, i = [], 0, False, 0
    while True:
        buf = dec.stdout.read(ow * oh)
        if len(buf) < ow * oh:
            break
        img = np.frombuffer(buf, np.uint8).reshape(oh, ow)
        in_run = float(cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED).max()) >= threshold
        run = run + 1 if in_run else 0
        if run == need and not prev_in:
            starts.append((i - need + 1) / grid)
            prev_in = True
        if not in_run:
            prev_in = False
        i += 1
    dec.stdout.close()
    dec.wait()
    return starts


def match_offset(live: list[float], video: list[float], tol: float = 0.15) -> tuple[float | None, int]:
    """Offset (live - video) that lines up the most run starts; median over the matched pairs."""
    best, best_n = None, 0
    for s in live:
        for v in video:
            off = s - v
            pairs = [s2 - v2 for s2 in live for v2 in video if abs((s2 - v2) - off) <= tol]
            if len(pairs) > best_n:
                best, best_n = float(np.median(pairs)), len(pairs)
    return best, best_n


def timecode(wall: float, fps: int) -> str:
    """Time-of-day timecode HH:MM:SS:FF (local time) for a wall-clock time.time() value."""
    d = datetime.fromtimestamp(wall)
    ff = int(d.microsecond / 1e6 * fps)
    return f"{d:%H:%M:%S}:{ff:02d}"


class Session:
    def __init__(self, cfg: RecordCfg, serial: str | None, record_game: bool):
        self.cfg = cfg
        self.dir = resolve(cfg.dir) / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.serial = serial
        self.wall_off = time.time() - time.perf_counter()   # perf_counter -> wall clock
        self.t0: float | None = None
        self.cam = self.ovl = None
        self.markers: list[tuple[str, float]] = []
        self.flash_until = 0.0
        self.scrcpy = None
        self.t_scrcpy = None
        self._video_starts: list[float] = []
        if record_game and serial:
            self.scrcpy = subprocess.Popen(
                ["scrcpy", "-s", serial, "--no-window", "--no-audio", "--no-control", "--max-fps=60",
                 f"--video-bit-rate={cfg.game_bitrate}", "--record", str(self.dir / "game_raw.mkv")],
                stdout=subprocess.DEVNULL, stderr=open(self.dir / "scrcpy.log", "w"),
                env={**os.environ, "ADB": str(ADB)})
            self.t_scrcpy = time.perf_counter()
        log(f"recording to {self.dir}")

    def frame(self, raw: np.ndarray, t: float):
        """Called with every camera frame (un-mirrored) as soon as it arrives."""
        if self.t0 is None:
            self.t0 = t
            h, w = raw.shape[:2]
            self.cam = FrameWriter(self.dir / "camera_raw.mp4", (w, h), self.cfg.fps, self.cfg.bitrate, t)
            self.marker("start", t)
        self.cam.write(raw, t)

    def overlay(self, img: np.ndarray, t: float):
        if self.t0 is None:
            return
        if self.ovl is None:
            h, w = img.shape[:2]
            self.ovl = FrameWriter(self.dir / "overlay_raw.mp4", (w, h), self.cfg.fps, self.cfg.bitrate, self.t0)
        self.ovl.write(img, t)

    def marker(self, kind: str, t: float):
        """t: capture time of the camera frame the marker belongs to."""
        self.markers.append((kind, t))
        self.flash_until = t + 0.2   # camera-frame time: the frames of the next 0.2 s flash
        log(f"marker: {kind} at {datetime.fromtimestamp(t + self.wall_off):%H:%M:%S.%f}"[:-3])

    def decorate(self, img: np.ndarray, t: float):
        """The white marker flash on the preview (visible on the TV for other cameras)."""
        if t < self.flash_until:
            cv2.addWeighted(img, 0.3, np.full_like(img, 255), 0.7, 0, dst=img)

    def finish(self, gate_starts: list[float]):
        """Stops everything and writes the synced files. gate_starts: perf_counter times at which
        the live run guard saw each run start. Ctrl-C is ignored while saving."""
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self._finish(gate_starts)
        finally:
            signal.signal(signal.SIGINT, old)

    def _finish(self, gate_starts: list[float]):
        log("saving the recording - takes a few seconds, don't close this window...")
        if self.scrcpy:
            self.scrcpy.send_signal(signal.SIGTERM)
            try:
                self.scrcpy.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.scrcpy.kill()
        for wr in (self.cam, self.ovl):
            if wr:
                wr.close()
        if self.t0 is None:
            log("no camera frames; nothing recorded")
            return
        duration = self.cam.written / self.cfg.fps
        wall0 = self.t0 + self.wall_off
        out = {"start_time": datetime.fromtimestamp(wall0).isoformat(timespec="milliseconds"),
               "timecode": {"30fps": timecode(wall0, 30), "60fps": timecode(wall0, 60)},
               "duration_s": round(duration, 2),
               "markers": [{"kind": k, "file_time_s": round(t - self.t0, 3),
                            "time_of_day": datetime.fromtimestamp(t + self.wall_off).isoformat(timespec="milliseconds")}
                           for k, t in self.markers],
               "dropped_frames": {"camera": self.cam.dropped, "overlay": self.ovl.dropped if self.ovl else 0}}

        (self.dir / "markers.json").write_text(json.dumps(out, indent=2))
        for name in ("camera", "overlay"):
            src = self.dir / f"{name}_raw.mp4"
            if src.exists():
                self._mux(src, self.dir / f"{name}.mp4", wall0, self.cfg.fps, duration)

        raw = self.dir / "game_raw.mkv"
        if raw.exists() and raw.stat().st_size > 0:
            off, n, how = self._game_offset(raw, gate_starts)
            lead = self.t0 - off   # game file time at the first camera frame
            out["game"] = {"offset_s": round(off - self.t0, 3), "matched_run_starts": n, "method": how,
                           "run_starts_live_s": [round(t - self.t0, 3) for t in gate_starts],
                           "run_starts_video_s": [round(v - lead, 3) for v in self._video_starts]}
            self._mux(raw, self.dir / "game.mp4", wall0, 60, duration, start=lead)
        elif self.scrcpy:
            log(f"game recording failed, see {self.dir / 'scrcpy.log'}")
        (self.dir / "markers.json").write_text(json.dumps(out, indent=2))
        if not self.cfg.keep_raw:
            for p in [*self.dir.glob("*_raw.mp4"), *self.dir.glob("*_raw.mkv")]:
                if (self.dir / (p.stem.replace("_raw", "") + ".mp4")).exists():
                    p.unlink()
        log(f"done: {self.dir}  ({duration:.0f} s, starts {out['start_time']}, "
            f"{len(self.markers) - 1} round marker(s))")
        if "game" in out:
            log(f"game video aligned by {out['game']['method']} "
                f"({out['game']['matched_run_starts']} run starts matched)")

    def _game_offset(self, raw: Path, gate_starts: list[float]) -> tuple[float, int, str]:
        tpl, thr = self.gate_template
        video = run_starts_in_video(raw, tpl, thr) if tpl is not None else []
        self._video_starts = video
        off, n = match_offset(gate_starts, video)
        if off is not None:
            return off, n, "run starts"
        # no run started while recording: scrcpy's first frame arrives ~1 s after launch
        return self.t_scrcpy + 1.0, 0, "estimate (no run started; may be off by a few frames)"

    gate_template: tuple = (None, 0.0)   # (template, threshold) of the run guard, set by the app

    def _mux(self, src: Path, dst: Path, wall0: float, fps: int, duration: float, start: float | None = None):
        """Adds the time-of-day timecode and creation time; for the game also trims/pads it to start
        at the first camera frame and makes it constant 60 fps."""
        meta = ["-timecode", timecode(wall0, fps), "-metadata",
                "creation_time=" + datetime.fromtimestamp(wall0, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")]
        if start is None:
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v", "-c:v", "copy", *meta, str(dst)]
        else:
            pre, vf = (["-ss", f"{start:.3f}"], "fps=60") if start >= 0 else \
                ([], f"tpad=start_duration={-start:.3f}:color=black,fps=60")
            cmd = ["ffmpeg", "-loglevel", "error", "-y", *pre, "-i", str(src), "-map", "0:v", "-vf", vf,
                   "-t", f"{duration:.3f}", "-c:v", "h264_videotoolbox", "-b:v", self.cfg.game_bitrate,
                   "-pix_fmt", "yuv420p", *meta, str(dst)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            log(f"ffmpeg failed for {dst.name}: {r.stderr.strip()[-300:]}")


def available() -> str | None:
    """Why recording can't run, or None."""
    if not shutil.which("ffmpeg"):
        return "ffmpeg not found (brew install ffmpeg)"
    return None
