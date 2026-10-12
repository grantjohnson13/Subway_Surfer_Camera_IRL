"""Benchmark every swipe-injection method against the SwipeTest probe app.

For each method and each of the 4 directions, sends N swipes and reads the probe's
logcat lines (device wall-clock timestamps, corrected by a measured host<->guest
clock offset) to get:
  down_ms   host send-start -> app receives ACTION_DOWN
  cross_ms  host send-start -> finger has travelled 40 dp (where games recognise a swipe)
  up_ms     host send-start -> app receives ACTION_UP
  call_ms   how long the host-side swipe() call blocked
and whether the app saw the correct direction.

Usage: .venv/bin/python bench/benchmark_injection.py [--trials 5] [--methods a,b,...]
Writes bench/benchmark_results.json and benchmark_results.md.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import subprocess
import threading
import time
from pathlib import Path

from sscam.config import load_config
from sscam.injectors import ADB, METHODS, InjectorUnavailable, adb, make_injector, screen_size

ROOT = Path(__file__).resolve().parents[1]
PROBE_APK = ROOT / "tools/swipetest/swipetest.apk"
PROBE_PKG = "com.sscam.swipetest"
LINE = re.compile(r"(DOWN|CROSS|UP)(?: dir=(\w+))?.*?evt_us=(\d+) recv_us=(\d+)")
TIE_MS = 5.0
IN_GAME = """## In the real game (bench/game_swipe_check.py)

The probe app proves delivery; the game has its own rules. Subway Surfers (Unity) samples touches once
per rendered frame, so the finger has to be seen moving on at least ~2 frames:

| swipe shape | result in Subway Surfers (60 fps) |
|---|---|
| 8 ms travel, 1 step, 32 ms hold | **missed jumps** (DOWN and the whole move land in one frame) |
| 24 ms travel, 3 steps, 16 ms hold | 12/12 registered |
| **40 ms travel, 5 steps, 16 ms hold (chosen)** | **11/11 registered**, response visible within 150 ms; more margin when the game drops a frame |

Evidence: `evidence/game_A60_run*.png`, `evidence/game_C60_run*.png`, `evidence/game_check_B_8ms_hold32.png`.

**Emulator frame rate matters more than the injector.** macOS throttles the emulator to ~12-15 fps whenever its
window is covered by other windows (measured with `bench/game_fps.py`: 13.5 fps covered vs 60.0 fps visible),
adding 50-100 ms per frame. `scripts/start_emulator.sh` parks the emulator at the top-left and the camera
preview opens beside it; the preview shows the game's live fps and warns if it drops.
"""
NOTES = {
    "adb_input": "New adb connection + a fresh `input` process (app_process JVM start) per swipe.",
    "persistent_shell": "Saves the adb connection, but Android still starts an `input` JVM per swipe; returns instantly (fire-and-forget) so no back-pressure.",
    "minitouch": "Needs write access to /dev/input/event*; SELinux (enforcing) denies it on Android 10+ without root, and Play Store images cannot `adb root`.",
    "uiautomator2": "JSON-RPC over HTTP to an on-device uiautomator server; per-call overhead and coarse 5 ms move steps.",
    "scrcpy": "Long-lived scrcpy-server (video off) injecting MotionEvents via InputManager; tiny binary messages over an adb-forwarded socket. Also works on real phones.",
    "emu_console": "Emulator telnet console `event mouse`: host-side, feeds the virtual touchscreen below Android; no adb or on-device process in the path. Emulator-only.",
    "pyautogui_mirror": "Drags the real macOS cursor over a scrcpy mirror window: extra hops (macOS event -> scrcpy -> device), hijacks your mouse, and drops swipes when the window is not focused.",
}
ORDER = ["adb_input", "persistent_shell", "minitouch", "uiautomator2", "scrcpy", "emu_console",
         "pyautogui_mirror"]


def clock_offset(serial: str, samples: int = 200) -> tuple[float, float]:
    """Return (guest_minus_host_seconds, uncertainty_seconds) using the min-RTT sample.
    Uses one persistent shell (RTT ~1 ms) rather than spawning adb per sample (RTT ~40 ms)."""
    sh = subprocess.Popen([ADB, "-s", serial, "shell"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True, bufsize=1)
    best = None
    try:
        for _ in range(samples):
            t0 = time.time()
            sh.stdin.write("echo $EPOCHREALTIME\n")
            sh.stdin.flush()
            out = sh.stdout.readline().strip()
            t1 = time.time()
            rtt = t1 - t0
            if best is None or rtt < best[1]:
                best = (float(out) - (t0 + t1) / 2, rtt)
    finally:
        sh.kill()
    return best[0], best[1] / 2


class ProbeLog:
    """Streams the probe's logcat lines into a list (thread-safe)."""

    def __init__(self, serial: str):
        adb(serial, "logcat", "-c")
        self.proc = subprocess.Popen([ADB, "-s", serial, "logcat", "-v", "raw", "-s", "SWIPETEST:I"],
                                     stdout=subprocess.PIPE, text=True, bufsize=1)
        self.events: list[dict] = []
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            m = LINE.search(line)
            if m:
                with self.cv:
                    self.events.append({"kind": m[1], "dir": m[2], "evt": int(m[3]) / 1e6,
                                        "recv": int(m[4]) / 1e6})
                    self.cv.notify_all()

    def wait_up(self, start_index: int, timeout: float) -> list[dict] | None:
        deadline = time.time() + timeout
        with self.cv:
            while True:
                evs = self.events[start_index:]
                if any(e["kind"] == "UP" for e in evs):
                    return evs
                rem = deadline - time.time()
                if rem <= 0:
                    return None
                self.cv.wait(rem)

    def mark(self) -> int:
        with self.cv:
            return len(self.events)

    def close(self):
        self.proc.kill()


def ensure_probe(serial: str):
    if PROBE_PKG not in adb(serial, "shell", "pm", "list", "packages", PROBE_PKG):
        if not PROBE_APK.exists():
            subprocess.run([str(ROOT / "tools/swipetest/build.sh")], check=True)
        adb(serial, "install", "-r", str(PROBE_APK))
    adb(serial, "shell", "am", "start", "-W", "-n", f"{PROBE_PKG}/.MainActivity")
    time.sleep(1.0)


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def bench_method(name, cfg, screen, log, offset, trials, gap):
    res = {"method": name, "available": True, "error": None, "trials": []}
    inj = make_injector(name, cfg.injector, screen)
    t0 = time.perf_counter()
    try:
        inj.start()
    except InjectorUnavailable as e:
        res.update(available=False, error=str(e))
        return res
    except Exception as e:  # noqa: BLE001
        res.update(available=False, error=f"{type(e).__name__}: {e}")
        return res
    res["startup_ms"] = (time.perf_counter() - t0) * 1000
    try:
        # warm-up swipe (not recorded) — first call often pays JIT / connection costs
        mark = log.mark()
        inj.swipe("left")
        log.wait_up(mark, 3)
        time.sleep(gap)
        for i in range(trials):
            for direction in ("left", "right", "up", "down"):
                mark = log.mark()
                t_send = time.time()
                c0 = time.perf_counter()
                try:
                    inj.swipe(direction)
                    err = None
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"
                call_ms = (time.perf_counter() - c0) * 1000
                evs = log.wait_up(mark, 3.0) if err is None else None
                tr = {"dir": direction, "call_ms": call_ms, "ok": False, "error": err}
                if evs:
                    by = {e["kind"]: e for e in evs}
                    seen = by.get("UP", {}).get("dir", "").lower()
                    to_host = lambda guest_t: (guest_t - offset - t_send) * 1000  # noqa: E731
                    tr.update(seen=seen, ok=(seen == direction),
                              down_ms=to_host(by["DOWN"]["recv"]) if "DOWN" in by else None,
                              cross_ms=to_host(by["CROSS"]["recv"]) if "CROSS" in by else None,
                              up_ms=to_host(by["UP"]["recv"]))
                elif err is None:
                    tr["error"] = "no touch registered within 3 s"
                res["trials"].append(tr)
                time.sleep(gap)
    finally:
        inj.close()
    ok = [t for t in res["trials"] if t["ok"]]
    res["success"] = f"{len(ok)}/{len(res['trials'])}"
    res["success_rate"] = len(ok) / max(1, len(res["trials"]))
    for k in ("down_ms", "cross_ms", "up_ms", "call_ms"):
        xs = [t[k] for t in ok if t.get(k) is not None]
        if xs:
            res[k] = {"median": statistics.median(xs), "p95": pct(xs, 95), "min": min(xs), "max": max(xs)}
    return res


def fmt(d, key="median"):
    return "—" if not d else f"{d[key]:.1f}"


def write_report(results, meta, path_md: Path):
    usable = [r for r in results if r.get("available") and r.get("success_rate", 0) == 1.0 and r.get("cross_ms")]
    best = None
    if usable:
        # MOVE events reach apps batched on the 60 Hz display frame, so "cross" times are
        # quantized to ~16 ms and differences of a few ms between runs are noise. Methods within
        # TIE_MS of the fastest are a tie, broken by DOWN latency (pure transport time; DOWN is
        # delivered immediately, not batched).
        fastest = min(r["cross_ms"]["median"] for r in usable)
        tied = [r for r in usable if r["cross_ms"]["median"] <= fastest + TIE_MS]
        best = min(tied, key=lambda r: r["down_ms"]["median"])
    lines = [
        "# Swipe injection benchmark",
        "",
        f"Run: {meta['when']} — host {meta['host']}, device {meta['device']} ({meta['screen'][0]}x{meta['screen'][1]}), "
        f"{meta['trials']} trials x 4 directions per method, swipe {meta['swipe']}.",
        f"Host↔guest clock offset {meta['offset_ms']:+.2f} ms (±{meta['offset_err_ms']:.2f} ms), corrected for.",
        "",
        "Latencies are from the host starting the swipe to the SwipeTest app's UI thread receiving the event. "
        "**cross** = the finger has travelled 40 dp, which is roughly when a game decides \"that was a swipe\" — "
        "this is the number that matters for gameplay.",
        "",
        "| method | works | success | cross median (ms) | cross p95 | DOWN median | UP median | host call blocks (ms) | startup (ms) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if not r.get("available"):
            lines.append(f"| {r['method']} | **no** — {r['error']} | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {r['method']}{' **(chosen)**' if best is r else ''} | yes | {r['success']} | {fmt(r.get('cross_ms'))} | "
            f"{fmt(r.get('cross_ms'), 'p95')} | {fmt(r.get('down_ms'))} | {fmt(r.get('up_ms'))} | "
            f"{fmt(r.get('call_ms'))} | {r.get('startup_ms', 0):.0f} |")
    lines.append("")
    if best:
        lines.append(f"**Chosen: `{best['method']}`** — median {best['cross_ms']['median']:.1f} ms to swipe recognition "
                     f"(p95 {best['cross_ms']['p95']:.1f} ms), {best['down_ms']['median']:.1f} ms to first touch, "
                     f"{best['success']} delivered.")
        lines.append("")
        lines.append(f"Selection rule: 100% delivery required; methods within {TIE_MS:.0f} ms of the fastest median "
                     "recognition time are treated as tied (MOVE events are batched to the 60 Hz display frame, so "
                     "smaller differences are run-to-run noise), and the tie is broken by DOWN latency, which is "
                     "pure transport time.")
    lines += ["", IN_GAME]
    lines += ["", "## Notes per method", ""]
    lines += [f"- **{r['method']}** — {NOTES.get(r['method'], '')}" for r in results]
    failures = [(r["method"], t) for r in results for t in r.get("trials", []) if not t["ok"]]
    if failures:
        lines += ["", "Failed trials:", ""]
        lines += [f"- {m}: {t['dir']} — {t.get('error') or 'saw ' + str(t.get('seen'))}" for m, t in failures[:30]]
    path_md.write_text("\n".join(lines) + "\n")
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--gap", type=float, default=0.35, help="seconds between swipes")
    ap.add_argument("--methods", default=",".join(ORDER))
    ap.add_argument("--out", default=str(ROOT / "bench/benchmark_results.json"))
    ap.add_argument("--report", default=str(ROOT / "benchmark_results.md"))
    ap.add_argument("--duration-ms", type=float, help="override swipe.duration_ms")
    ap.add_argument("--steps", type=int, help="override swipe.steps")
    ap.add_argument("--hold-ms", type=float, help="override swipe.hold_ms")
    args = ap.parse_args()

    cfg = load_config()
    for k in ("duration_ms", "steps", "hold_ms"):
        if getattr(args, k) is not None:
            setattr(cfg.injector.swipe, k, getattr(args, k))
    serial = cfg.injector.serial
    screen = screen_size(serial)
    ensure_probe(serial)
    offset, err = clock_offset(serial)
    print(f"clock offset guest-host = {offset*1000:+.2f} ms ± {err*1000:.2f}")
    log = ProbeLog(serial)
    time.sleep(0.5)
    results = []
    for name in args.methods.split(","):
        if name not in METHODS:
            raise SystemExit(f"unknown method {name}")
        print(f"--- {name}", flush=True)
        ensure_probe(serial)  # keep the probe in front (pyautogui/scrcpy windows can steal nothing on device, but be safe)
        r = bench_method(name, cfg, screen, log, offset, args.trials, args.gap)
        results.append(r)
        if not r["available"]:
            print(f"    unavailable: {r['error']}")
        else:
            print(f"    success {r['success']}  cross median {fmt(r.get('cross_ms'))} ms  "
                  f"p95 {fmt(r.get('cross_ms'), 'p95')} ms  call {fmt(r.get('call_ms'))} ms")
    log.close()
    s = cfg.injector.swipe
    meta = {"when": time.strftime("%Y-%m-%d %H:%M"), "host": f"{platform.machine()} macOS {platform.mac_ver()[0]}",
            "device": serial, "screen": screen, "trials": args.trials, "offset_ms": offset * 1000,
            "offset_err_ms": err * 1000,
            "swipe": f"length {s.length:.2f}×width, travel {s.duration_ms:.0f} ms in {s.steps + 1} moves, hold {s.hold_ms:.0f} ms, "
                     f"centre ({s.center_x}, {s.center_y})"}
    Path(args.out).write_text(json.dumps({"meta": meta, "results": results}, indent=2))
    best = write_report(results, meta, Path(args.report))
    print(f"wrote {args.report}; best = {best['method'] if best else None}")


if __name__ == "__main__":
    main()
