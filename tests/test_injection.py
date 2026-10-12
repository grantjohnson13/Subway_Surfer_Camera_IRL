"""Swipe geometry unit tests, plus integration tests that send real swipes to the emulator
and verify them with the SwipeTest probe app (skipped when no emulator is attached)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from sscam.config import SwipeCfg, load_config
from sscam.injectors import ADB, make_injector, swipe_path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))

CFG = load_config()


@pytest.mark.parametrize("direction,axis,sign", [("left", 0, -1), ("right", 0, 1), ("up", 1, -1), ("down", 1, 1)])
def test_swipe_path_geometry(direction, axis, sign):
    s = SwipeCfg(center_x=0.5, center_y=0.6, length=0.3, duration_ms=40, steps=4)
    pts = swipe_path(direction, (1080, 2400), s)
    assert len(pts) == s.steps + 2
    delta = pts[-1][axis] - pts[0][axis]
    assert sign * delta == pytest.approx(0.3 * 1080, abs=2)
    assert pts[-1][1 - axis] == pts[0][1 - axis]           # straight line
    mid = ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2)
    assert mid == pytest.approx((540, 1440), abs=1)        # centred on the configured point
    for x, y in pts:
        assert 0 <= x < 1080 and 0 <= y < 2400


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("lookahed_s: 0.1\n")
    with pytest.raises(ValueError, match="unknown config keys"):
        load_config(p)


def _emulator_attached() -> bool:
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return f"{CFG.injector.serial}\tdevice" in out


needs_emulator = pytest.mark.skipif(not _emulator_attached(), reason="emulator not running")


@pytest.fixture(scope="module")
def probe():
    from benchmark_injection import ProbeLog, ensure_probe
    ensure_probe(CFG.injector.serial)
    log = ProbeLog(CFG.injector.serial)
    time.sleep(0.5)
    yield log
    log.close()
    # put the game back in front (the probe app was brought up for these tests)
    subprocess.run([ADB, "-s", CFG.injector.serial, "shell", "monkey", "-p", "com.kiloo.subwaysurf",
                    "-c", "android.intent.category.LAUNCHER", "1"], capture_output=True)


@needs_emulator
@pytest.mark.parametrize("method", sorted({CFG.injector.method, CFG.injector.fallback, "adb_input"}))
def test_all_four_swipes_register_on_device(method, probe):
    inj = make_injector(method, CFG.injector)
    inj.start()
    try:
        for direction in ("left", "right", "up", "down"):
            mark = probe.mark()
            inj.swipe(direction)
            evs = probe.wait_up(mark, 3.0)
            assert evs, f"{method}: {direction} swipe never reached the app"
            up = [e for e in evs if e["kind"] == "UP"][0]
            assert up["dir"].lower() == direction
            assert any(e["kind"] == "CROSS" for e in evs), "swipe too short to be recognised"
            time.sleep(0.3)
    finally:
        inj.close()


def test_game_gate_fails_closed_when_stale():
    """The run guard only allows swipes on fresh 'in a run' information."""
    import time as _t

    from sscam.gamestate import GameStateWatcher
    g = GameStateWatcher.__new__(GameStateWatcher)
    g.cfg = CFG.game_gate
    g.in_run, g.updated = True, _t.perf_counter()
    assert g.allows_swipe()
    g.updated = _t.perf_counter() - CFG.game_gate.max_age_s - 0.1   # watcher stalled
    assert not g.allows_swipe()
    g.in_run, g.updated = False, _t.perf_counter()                  # menu / dialog
    assert not g.allows_swipe()


@needs_emulator
def test_game_gate_sees_no_run_outside_the_game():
    """With the probe app in front there is no pause button -> swipes must be held."""
    from benchmark_injection import ensure_probe

    from sscam.gamestate import GameStateWatcher
    from sscam.injectors import screen_size
    ensure_probe(CFG.injector.serial)
    g = GameStateWatcher(CFG.game_gate, CFG.injector.console_port, screen_size(CFG.injector.serial))
    try:
        assert g.check_once() is False and g.score < CFG.game_gate.threshold
    finally:
        subprocess.run([ADB, "-s", CFG.injector.serial, "shell", "monkey", "-p", "com.kiloo.subwaysurf",
                        "-c", "android.intent.category.LAUNCHER", "1"], capture_output=True)
