"""Camera controller: webcam -> MediaPipe Pose -> gesture detector -> swipe injector.

Keys in the preview window:  q/Esc quit   c recalibrate   space pause/resume sending swipes
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from collections import Counter, deque
from pathlib import Path

import cv2
import numpy as np

from .config import ROOT, load_config
from .detector import Calibrator, GestureDetector, hands_up, sample_from_landmarks
from .gamestate import GameStateWatcher, RunStarter
from .injectors import GESTURE_TO_SWIPE, AsyncSwiper, NullInjector, make_injector, screen_size, start_best
from .pose import CONNECTIONS, PoseTracker

COLORS = {"left": (255, 160, 0), "right": (0, 160, 255), "jump": (0, 220, 0), "duck": (0, 0, 255)}


def guided_schedule(mode: str = "lanes") -> list[tuple[str, str | None, float, int | None]]:
    """(prompt text, expected gesture or None, seconds, target lane or None) for a guided recording."""
    if mode == "lean":
        sched = [("Get ready...", None, 4.0, None)]
        for g, text in (("left", "LEAN LEFT"), ("right", "LEAN RIGHT"), ("jump", "JUMP"), ("duck", "DUCK")):
            for i in range(5):
                sched += [(f"{text}  ({i + 1}/5)", g, 1.2, None), ("back to centre", None, 1.6, None)]
        return sched + [("STAND STILL (20 s)", None, 20.0, None), ("Done - thank you!", None, 2.0, None)]

    names = {0: "LEFT", 1: "MIDDLE", 2: "RIGHT"}
    sched, lane = [("Stand in the MIDDLE lane", None, 4.0, 1)], 1

    def go(target, dur=2.4):
        nonlocal lane
        two = "  (two lanes!)" if abs(target - lane) == 2 else ""
        sched.append((f"go to {names[target]} lane{two}", None, dur, target))
        lane = target

    for target in (0, 1, 2, 1, 0, 2, 1, 2, 0, 1):
        go(target)
    for g, text in (("jump", "JUMP"), ("duck", "DUCK")):
        for i in range(3):
            sched += [(f"{text}  ({i + 1}/3)", g, 1.2, lane), ("stand normally", None, 1.6, lane)]
    go(0)
    for g, text in (("jump", "JUMP"), ("duck", "DUCK"), ("jump", "JUMP")):
        sched += [(f"{text} in the left lane", g, 1.2, lane), ("stand normally", None, 1.6, lane)]
    go(2)
    for g, text in (("duck", "DUCK"), ("jump", "JUMP")):
        sched += [(f"{text} in the right lane", g, 1.2, lane), ("stand normally", None, 1.6, lane)]
    go(1)
    return sched + [("STAND STILL in the middle (15 s)", None, 15.0, 1), ("Done - thank you!", None, 2.0, 1)]


def draw_prompt(img, text: str, remaining: float):
    h, w = img.shape[:2]
    cv2.rectangle(img, (0, h - 150), (w, h - 40), (0, 0, 0), -1)
    size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 2.0, 5)[0]
    org = ((w - size[0]) // 2, h - 75)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 5, cv2.LINE_AA)
    cv2.rectangle(img, (0, h - 46), (int(w * min(1.0, remaining)), h - 40), (0, 255, 255), -1)


def place_windows(pcfg, aspect: float) -> tuple[int, int, int]:
    """Preview (x, y, width). In split layout also parks the emulator on its side of the screen."""
    if pcfg.layout == "split":
        from .layout import display_size, emulator_bounds, place_emulator, preview_rect
        try:
            screen = display_size()
        except Exception:  # noqa: BLE001
            screen = None
        if screen:
            try:
                return preview_rect(screen, place_emulator(pcfg.game_side, screen), aspect)
            except Exception as e:  # noqa: BLE001
                print(f"[sscam] could not move the emulator window ({e})", file=sys.stderr)
            emu = emulator_bounds()   # leave it where it is, and keep the preview off it
            if emu:
                x, y, w, h = emu
                return preview_rect(screen, (x, y, w + 58, h), aspect)
    return pcfg.x, pcfg.y, pcfg.width


# --------------------------------------------------------------------------- frame sources
class CameraSource:
    """Grabs frames on a background thread and always hands out the newest one, so
    processing never works on a stale, buffered frame. Timestamps are taken the moment
    the frame is returned by the driver."""

    def __init__(self, index: int, width: int, height: int, fps: int):
        self.cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        if not self.cap.isOpened():
            raise RuntimeError(
                f"cannot open camera {index}. If this is the first run, grant camera access to your "
                "terminal app in System Settings > Privacy & Security > Camera, then restart the terminal.")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, frame = self.cap.read()
        deadline = time.time() + 5
        while not ok and time.time() < deadline:  # some cameras need a moment to start
            time.sleep(0.05)
            ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("camera opened but returned no frames (camera permission denied, "
                               "or another app is using the camera)")
        self.size = (frame.shape[1], frame.shape[0])
        self.reported_fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.cv = threading.Condition()
        self.latest = None   # (seq, t_capture, frame)
        self.seq = 0
        self.running = True
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while self.running:
            ok, frame = self.cap.read()
            t = time.perf_counter()
            if not ok:
                time.sleep(0.005)
                continue
            with self.cv:
                self.seq += 1
                self.latest = (self.seq, t, frame)
                self.cv.notify_all()

    def read(self, last_seq: int, timeout: float = 1.0):
        """Block until a frame newer than last_seq is available. Returns (seq, t, frame) or None."""
        with self.cv:
            if not self.cv.wait_for(lambda: self.latest and self.latest[0] > last_seq, timeout):
                return None
            return self.latest

    def close(self):
        self.running = False
        self.t.join(timeout=1)
        self.cap.release()


class VideoSource:
    """Reads a video file frame by frame (for tests / offline evaluation). Detector time is the
    video's own timeline; latency is still measured with the wall clock."""

    def __init__(self, path: str, realtime: bool = False):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open video {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.size = (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        self.reported_fps = self.fps
        self.realtime = realtime
        self.seq = 0
        self.t_start = time.perf_counter()

    def read(self, last_seq: int, timeout: float = 1.0):
        if self.realtime:
            wait = self.t_start + self.seq / self.fps - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
        ok, frame = self.cap.read()
        if not ok:
            return None
        self.seq += 1
        return self.seq, time.perf_counter(), frame

    def video_time(self) -> float:
        return (self.seq - 1) / self.fps

    def close(self):
        self.cap.release()


# --------------------------------------------------------------------------- overlay
def draw_overlay(img, lms, cal: Calibrator, det: GestureDetector | None, frame, hud: dict, cfg):
    h, w = img.shape[:2]
    debug = hud.get("debug", True)   # False: just you and the lane lines (toggle with d)
    if lms is not None and debug:
        pts = []
        for lm in lms:
            pts.append((int(lm.x * w), int(lm.y * h)) if (lm.visibility or 0) > 0.3 else None)
        for a, b in CONNECTIONS:
            if pts[a] and pts[b]:
                cv2.line(img, pts[a], pts[b], (255, 255, 255), 2, cv2.LINE_AA)
        for p in pts:
            if p:
                cv2.circle(img, p, 3, (0, 200, 255), -1, cv2.LINE_AA)

    if cfg.lateral_mode == "lanes":
        for i, bx_ in enumerate(cfg.lanes.boundaries):
            x_ = int(bx_ * w)
            cv2.line(img, (x_, 0), (x_, h), (255, 255, 255), 2)
        if frame is not None and frame.lane is not None:
            x0, x1 = ([0] + [int(b_ * w) for b_ in cfg.lanes.boundaries] + [w])[frame.lane:frame.lane + 2]
            ov = img.copy()
            cv2.rectangle(ov, (x0, 0), (x1, h), (0, 255, 255), -1)
            cv2.addWeighted(ov, 0.12, img, 0.88, 0, img)
    if det is not None and debug:
        b = det.base
        bx, by, T = int(b.cx), int(b.cy), b.torso
        g = cfg.gestures
        # baseline cross-hair
        cv2.line(img, (bx, 0), (bx, h), (200, 200, 200), 1)
        cv2.line(img, (0, by), (w, by), (200, 200, 200), 1)
        # trigger lines for each gesture (lean mode only for left/right)
        if cfg.lateral_mode == "lean":
            for name, x in (("left", bx - g["left"].trigger * T), ("right", bx + g["right"].trigger * T)):
                cv2.line(img, (int(x), by - int(T)), (int(x), by + int(T)), COLORS[name], 1)
        for name, y in (("jump", by - g["jump"].trigger * T), ("duck", by + g["duck"].trigger * T)):
            cv2.line(img, (bx - int(T), int(y)), (bx + int(T), int(y)), COLORS[name], 1)
        if frame is not None and frame.tracking:
            cx, cy = int(bx + frame.dx * T), int(by + frame.dy * T)
            cv2.circle(img, (cx, cy), 9, (0, 255, 255), -1, cv2.LINE_AA)
            # velocity arrow (projected position after lookahead)
            px, py = int(cx + frame.vx * cfg.lookahead_s * T), int(cy + frame.vy * cfg.lookahead_s * T)
            cv2.arrowedLine(img, (cx, cy), (px, py), (0, 255, 255), 2, cv2.LINE_AA, tipLength=0.3)

    def text(s, y, color=(255, 255, 255), scale=0.6, x=10):
        cv2.putText(img, s, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, s, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)

    if not debug:
        if det is None:   # calibrating: the only thing shown besides the lanes
            text(cal.message, 40, (0, 255, 255), 0.9, x=20)
            cv2.rectangle(img, (20, 56), (20 + int(300 * cal.progress), 70), (0, 255, 255), -1)
            cv2.rectangle(img, (20, 56), (320, 70), (255, 255, 255), 1)
        return
    y = 24
    mode = "DRY RUN (no input sent)" if hud["dry_run"] else f"LIVE -> {hud['method']}"
    if hud.get("paused"):
        mode += "  [PAUSED - space to resume]"
    text(mode, y, (0, 255, 255) if hud["dry_run"] else (0, 255, 0))
    y += 24
    text(f"cam {hud['fps']:.0f} fps   pose {hud['pose_ms']:.1f} ms   frame age at decision {hud['age_ms']:.0f} ms", y)
    gate = hud.get("gate")
    if gate is not None:
        y += 24
        if gate.allows_swipe():
            fps = gate.fps
            warn = fps is not None and fps < 45
            text(f"game: running  {fps:.0f} fps" if fps else "game: running", y, (0, 0, 255) if warn else (0, 255, 0))
            if warn:
                text("emulator throttled - keep its window uncovered", y, (0, 0, 255), x=260)
        else:
            text(f"game: not in a run - swipes held ({hud['suppressed']} held)", y, (0, 200, 255))
            st = hud.get("starter")
            if st is not None:
                y += 24
                msg = st.status if st.busy or st.status else "raise both hands to start a run"
                text(msg if not hud.get("hands_up") or st.busy else "hands up - hold...", y, (0, 255, 255))
    y += 24
    lat = hud["latency"]
    if lat:
        text(f"latency capture->swipe sent: last {lat[-1]:.0f} ms  median {statistics.median(lat):.0f} ms  (n={len(lat)})", y)
    else:
        text("latency capture->swipe sent: (no gesture yet)", y)
    y += 24
    if det is None:
        text(cal.message, y, (0, 255, 255), 0.8)
        cv2.rectangle(img, (10, y + 10), (10 + int(300 * cal.progress), y + 24), (0, 255, 255), -1)
        cv2.rectangle(img, (10, y + 10), (310, y + 24), (255, 255, 255), 1)
    elif frame is not None:
        if frame.tracking:
            text(f"dx {frame.dx:+.2f}  dy {frame.dy:+.2f} torso   vx {frame.vx:+.1f}  vy {frame.vy:+.1f} /s", y)
        else:
            text("body lost - step back into frame", y, (0, 0, 255))
        y += 24
        if cfg.lateral_mode == "lanes":
            lanes = ("LEFT", "MIDDLE", "RIGHT")
            gl = hud.get("game_lane")
            text(f"your lane: {lanes[frame.lane] if frame.lane is not None else '-'}   game lane: "
                 f"{lanes[gl] if gl is not None else '- (no run)'}   vertical latch: {frame.vertical_latch or '-'}", y)
        else:
            text(f"latched: lateral={frame.lateral_latch or '-'}  vertical={frame.vertical_latch or '-'}", y)
        y += 24
        text("counts: " + "  ".join(f"{k} {hud['counts'][k]}" for k in COLORS), y)
    # big flash for the most recent gesture
    lg = hud.get("last_gesture")
    if lg and time.perf_counter() - lg[1] < 0.6:
        name = lg[0].upper()
        size = cv2.getTextSize(name, cv2.FONT_HERSHEY_SIMPLEX, 2.5, 6)[0]
        org = ((w - size[0]) // 2, h // 2)
        cv2.putText(img, name, org, cv2.FONT_HERSHEY_SIMPLEX, 2.5, (0, 0, 0), 12, cv2.LINE_AA)
        cv2.putText(img, name, org, cv2.FONT_HERSHEY_SIMPLEX, 2.5, COLORS[lg[0]], 6, cv2.LINE_AA)
    text("q quit   c recalibrate   space pause", h - 12, (200, 200, 200), 0.5)


# --------------------------------------------------------------------------- main loop
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="detect and display, but send no input")
    ap.add_argument("--method", help="override injector.method (see benchmark_results.md)")
    ap.add_argument("--camera", type=int, help="override camera.index")
    ap.add_argument("--video", help="read frames from a video file instead of the webcam")
    ap.add_argument("--realtime", action="store_true", help="with --video: pace frames at the video's fps")
    ap.add_argument("--no-preview", action="store_true", help="no window (for headless tests)")
    ap.add_argument("--duration", type=float, help="exit after this many seconds")
    ap.add_argument("--events", help="write one JSON line per detected gesture to this file")
    ap.add_argument("--record-landmarks", help="write per-frame torso landmarks (JSONL) for replay tests")
    ap.add_argument("--guided", action="store_true",
                    help="tuning session: on-screen prompts after calibration; prompts are logged to --events")
    ap.add_argument("--record", action="store_true",
                    help="record camera, preview and game into recordings/<date_time>/, time-of-day timecoded")
    ap.add_argument("--no-gate", action="store_true",
                    help="send swipes even when no run is in progress (unsafe: menus have ad/shop buttons)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.guided and not args.events:
        args.events = str(ROOT / "logs/guided_events.jsonl")
    if args.camera is not None:
        cfg.camera.index = args.camera
    elif cfg.camera.name and not args.video:
        from .layout import camera_index_by_name
        idx = camera_index_by_name(cfg.camera.name)
        if idx is None:
            print(f"[sscam] no camera named {cfg.camera.name!r}; using camera {cfg.camera.index}", file=sys.stderr)
        else:
            cfg.camera.index = idx
    if args.method:
        cfg.injector.method = args.method

    # ---- input injector
    if args.dry_run:
        injector = NullInjector(cfg.injector)
    else:
        injector = start_best(cfg.injector)
        if injector.name != cfg.injector.method:
            print(f"[sscam] {cfg.injector.method} unavailable, using {injector.name}", file=sys.stderr)
    method = injector.name

    # Only swipe while a run is in progress (menus/dialogs have ad and shop buttons where swipes land)
    gate = None
    if args.record:
        cfg.game_gate.interval_s = min(cfg.game_gate.interval_s, 0.03)   # sharper run-start times for sync
    if not args.dry_run and cfg.game_gate.enabled and not args.no_gate:
        try:
            gate = GameStateWatcher(cfg.game_gate, cfg.injector.console_port, injector.screen,
                                    serial=cfg.injector.serial).start()
        except Exception as e:  # noqa: BLE001
            print(f"[sscam] game-state guard unavailable ({e}); refusing to send swipes blindly. "
                  "Start the emulator with scripts/start_emulator.sh, or pass --no-gate.", file=sys.stderr)
            return 2
    starter = None
    if gate is not None and cfg.start.enabled:
        starter = RunStarter(cfg.start, gate, cfg.injector.serial, injector.screen)

    # ---- recording
    session = None
    if args.record:
        from .recorder import Session, available
        why = available()
        if why:
            print(f"[sscam] cannot record: {why}", file=sys.stderr)
            return 2
        if gate is None and not args.video:   # dry run: watch the game anyway, to align game.mp4
            try:
                gate = GameStateWatcher(cfg.game_gate, cfg.injector.console_port, screen_size(cfg.injector.serial),
                                        serial=cfg.injector.serial).start()
            except Exception as e:  # noqa: BLE001
                print(f"[sscam] game not reachable ({e}); recording the camera only", file=sys.stderr)
        session = Session(cfg.record, cfg.injector.serial if gate is not None else None,
                          record_game=cfg.record.game and gate is not None)
        if gate is not None:
            session.gate_template = (gate.tpl, gate.cfg.threshold)

    latencies: deque[float] = deque(maxlen=200)
    all_latencies: list[float] = []
    lat_lock = threading.Lock()
    events_f = open(args.events, "w") if args.events else None

    def on_sent(gesture, meta, t0, t1, err):
        lat = (t0 - meta["t_capture"]) * 1000
        with lat_lock:
            latencies.append(lat)
            all_latencies.append(lat)
        rec = {**meta, "gesture": gesture, "swipe": GESTURE_TO_SWIPE[gesture], "method": method,
               "latency_capture_to_sent_ms": round(lat, 2), "swipe_call_ms": round((t1 - t0) * 1000, 2),
               "error": str(err) if err else None}
        rec.pop("t_capture", None)
        print(f"[sscam] {gesture:5s} -> swipe {GESTURE_TO_SWIPE[gesture]:5s}  capture->sent {lat:6.1f} ms"
              + (f"  ERROR {err}" if err else ""), flush=True)
        if events_f:
            events_f.write(json.dumps(rec) + "\n")
            events_f.flush()

    swiper = AsyncSwiper(injector, on_sent)

    # ---- camera / video
    if args.video:
        src = VideoSource(args.video, realtime=args.realtime)
    else:
        src = CameraSource(cfg.camera.index, cfg.camera.width, cfg.camera.height, cfg.camera.fps)
    print(f"[sscam] source {src.size[0]}x{src.size[1]} @ {src.reported_fps:.0f} fps (reported); "
          f"injector {method}{' (dry run)' if args.dry_run else ''}", flush=True)

    tracker = PoseTracker(cfg.pose)
    cal = Calibrator(cfg)
    det: GestureDetector | None = None
    rec_f = open(args.record_landmarks, "w") if args.record_landmarks else None
    schedule = guided_schedule(cfg.lateral_mode) if args.guided else []
    lane_sync = {"in_run": False, "game_lane": 1, "since": 0.0}   # assumed lane of the runner
    guide_start = None   # detector time at which the guided schedule began
    guide_idx = -1

    hud = {"dry_run": args.dry_run, "method": method, "fps": 0.0, "pose_ms": 0.0, "age_ms": 0.0,
           "latency": latencies, "counts": Counter({g: 0 for g in COLORS}), "paused": False,
           "last_gesture": None, "gate": gate, "suppressed": 0, "starter": starter, "hands_up": False,
           "debug": cfg.preview.debug}
    pose_ms_hist: deque[float] = deque(maxlen=60)
    age_hist: list[float] = []
    frame_times: deque[float] = deque(maxlen=60)
    t_start = time.perf_counter()
    n_frames = 0
    last_seq = 0
    frame = None
    hands_since, hands_fired = None, False
    window = "Subway Surfers camera controller"
    preview = not args.no_preview
    exit_code = 0
    try:
        while True:
            got = src.read(last_seq)
            if got is None:
                if args.video:
                    break
                print("[sscam] camera stalled (no frame for 1 s)", file=sys.stderr)
                continue
            last_seq, t_cap, img = got
            if session:
                session.frame(img if cfg.camera.mirror else img.copy(), t_cap)
            n_frames += 1
            frame_times.append(t_cap)
            if len(frame_times) > 1:
                hud["fps"] = (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])
            if cfg.camera.mirror:
                img = cv2.flip(img, 1)
            h, w = img.shape[:2]
            scale = cfg.camera.inference_width / w if w > cfg.camera.inference_width else 1.0
            small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img
            t_det = src.video_time() if args.video else t_cap - t_start
            p0 = time.perf_counter()
            lms = tracker.detect(small, t_det)
            pose_ms_hist.append((time.perf_counter() - p0) * 1000)
            hud["pose_ms"] = statistics.median(pose_ms_hist)
            # landmarks are normalized, so measure in full-frame pixels
            sample = sample_from_landmarks(t_det, lms, w, h, cfg.pose.min_visibility) if lms else None
            # ---- both hands above the head (held) outside a run: press PLAY
            if cfg.start.enabled and hands_up(lms, cfg.pose.min_visibility):
                hands_since = t_det if hands_since is None else hands_since
                hud["hands_up"] = True
                if t_det - hands_since >= cfg.start.hold_s and not hands_fired:
                    hands_fired = True
                    if session and (gate is None or not gate.in_run):
                        session.marker("round", t_cap)
                    if starter is not None and starter.trigger():
                        print("[sscam] hands up -> starting a run", flush=True)
                    elif args.dry_run:
                        print("[sscam] hands up (dry run: would start a run)", flush=True)
            else:
                hands_since, hands_fired, hud["hands_up"] = None, False, False
            if rec_f:
                rec_f.write(json.dumps({"t": round(t_det, 4), "w": w, "h": h, "lms": None if lms is None else
                                        [[round(l.x, 5), round(l.y, 5), round(l.visibility or 0, 3)] for l in lms]}) + "\n")

            if det is None:
                if cal.update(sample) is not None:
                    det = GestureDetector(cfg, cal.baseline)
                    print(f"[sscam] calibrated: centre=({cal.baseline.cx:.0f},{cal.baseline.cy:.0f}) px, "
                          f"torso={cal.baseline.torso:.0f} px", flush=True)
            else:
                frame = det.update(sample)
                if args.guided:
                    if guide_start is None:
                        guide_start = t_det
                    el, i = t_det - guide_start, 0
                    while i < len(schedule) and el >= schedule[i][2]:
                        el -= schedule[i][2]
                        i += 1
                    if i >= len(schedule):
                        break
                    if i != guide_idx:
                        guide_idx = i
                        print(f"[guide] {schedule[i][0]}", flush=True)
                        if events_f:
                            events_f.write(json.dumps({"t": round(t_det, 3), "prompt": schedule[i][0],
                                                       "expect": schedule[i][1], "dur": schedule[i][2],
                                                       "lane": schedule[i][3]}) + "\n")
                    hud["prompt"] = (schedule[i][0], 1 - el / schedule[i][2])
                in_run = gate is None or gate.allows_swipe()
                if frame.gesture:
                    hud["counts"][frame.gesture] += 1
                    hud["last_gesture"] = (frame.gesture, time.perf_counter())
                    age = (time.perf_counter() - t_cap) * 1000
                    age_hist.append(age)
                    hud["age_ms"] = age
                    lanes_lateral = cfg.lateral_mode == "lanes" and frame.gesture in ("left", "right")
                    if hud["paused"] or (lanes_lateral and in_run):
                        pass   # lanes mode: lateral swipes are sent by the lane sync below
                    elif gate is not None and not gate.allows_swipe():
                        hud["suppressed"] += 1
                        print(f"[sscam] {frame.gesture:5s} detected but NOT sent (no run in progress)", flush=True)
                        if events_f:
                            events_f.write(json.dumps({"t": round(t_det, 3), "gesture": frame.gesture,
                                                       "suppressed": "no run in progress"}) + "\n")
                    else:
                        swiper.submit(frame.gesture, {"t_capture": t_cap, "t": round(t_det, 3),
                                                      "decision_ms": round(age, 2)})

                # ---- lanes mode: keep the game's lane equal to the lane the body is in.
                # Every run starts in the middle lane, so re-sync at each run start (after the intro).
                if cfg.lateral_mode == "lanes" and frame.lane is not None and not hud["paused"]:
                    now = time.perf_counter()
                    if in_run and not lane_sync["in_run"]:
                        lane_sync.update(game_lane=1, since=t_det)   # frame timeline (video-safe)
                    lane_sync["in_run"] = in_run
                    if in_run and t_det - lane_sync["since"] >= cfg.lanes.resync_delay_s:
                        while lane_sync["game_lane"] != frame.lane:
                            d = "right" if frame.lane > lane_sync["game_lane"] else "left"
                            lane_sync["game_lane"] += 1 if d == "right" else -1
                            swiper.submit(d, {"t_capture": t_cap, "t": round(t_det, 3),
                                              "decision_ms": round((now - t_cap) * 1000, 2),
                                              "lane": lane_sync["game_lane"]})
                    hud["game_lane"] = lane_sync["game_lane"] if in_run else None

            if preview:
                draw_overlay(img, lms, cal, det, frame, hud, cfg)
                if hud.get("prompt"):
                    draw_prompt(img, *hud["prompt"])
                if n_frames == 1:
                    # beside the emulator, never on top of it (macOS throttles a covered emulator to ~15 fps)
                    px, py, pw = place_windows(cfg.preview, w / h)
                    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
                    cv2.resizeWindow(window, pw, int(pw * h / w))
                    cv2.moveWindow(window, px, py)
                if session:
                    session.decorate(img, t_cap)
                    session.overlay(img, t_cap)
                cv2.imshow(window, img)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord("c"):
                    cal.reset()
                    det, frame = None, None
                if key == ord(" "):
                    hud["paused"] = not hud["paused"]
                if key == ord("d"):
                    hud["debug"] = not hud["debug"]
            if args.duration and time.perf_counter() - t_start >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[sscam] fatal: {type(e).__name__}: {e}", file=sys.stderr)
        exit_code = 1
    finally:
        elapsed = time.perf_counter() - t_start
        time.sleep(0.2)  # let the last swipe finish
        swiper.close()
        if session:
            session.finish(gate.run_starts if gate is not None else [])
        if gate is not None:
            gate.close()
        src.close()
        tracker.close()
        if preview:
            cv2.destroyAllWindows()
        for f in (events_f, rec_f):
            if f:
                f.close()
        with lat_lock:
            lats = list(all_latencies)
        summary = {
            "seconds": round(elapsed, 1), "frames": n_frames, "avg_fps": round(n_frames / max(elapsed, 1e-6), 1),
            "pose_ms_median": round(hud["pose_ms"], 2), "calibrated": det is not None,
            "gestures": dict(hud["counts"]), "suppressed_not_in_run": hud["suppressed"],
            "method": method, "dry_run": args.dry_run,
            "latency_capture_to_sent_ms": None if not lats else {
                "median": round(statistics.median(lats), 1), "max": round(max(lats), 1), "n": len(lats)},
        }
        print("[sscam] summary " + json.dumps(summary), flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
