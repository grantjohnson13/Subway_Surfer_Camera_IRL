"""Gesture detector tests on synthetic landmark sequences.

Each test calibrates on the first synth.CAL seconds of standing still, then feeds the rest
of the sequence through GestureDetector and checks exactly which gestures fire, and when.
"""
from __future__ import annotations

import copy

import pytest

from sscam import synth
from sscam.config import load_config
from sscam.detector import Calibrator, GestureDetector, sample_from_landmarks
from sscam.synth import Camera, Key

CFG = load_config()
CFG.lateral_mode = "lean"   # the tests below exercise lean mode; lanes mode has its own section
LANES = copy.deepcopy(CFG)
LANES.lateral_mode = "lanes"
LANES.lanes.boundaries = [0.333, 0.667]   # geometry the lane tests below are written for
LANES.lanes.hysteresis = 0.03
FPS_OPTIONS = (30, 60)


def run(keys, fps=30, cam=None, jitter=0.01, seed=0, dropouts=(), frame_jitter_s=0.0, cfg=CFG):
    cam = cam or Camera()
    cal = Calibrator(cfg)
    det = None
    fired = []
    for t, lms in synth.sequence(keys, fps, cam, jitter, seed, dropouts, frame_jitter_s):
        s = None if lms is None else sample_from_landmarks(t, lms, cam.width, cam.height,
                                                           cfg.pose.min_visibility)
        if det is None:
            if cal.update(s) is not None:
                det = GestureDetector(cfg, cal.baseline)
            continue
        f = det.update(s)
        if f.gesture:
            fired.append((round(f.t, 3), f.gesture))
    assert det is not None, f"never calibrated: {cal.message}"
    return fired


def names(fired):
    return [g for _, g in fired]


# ------------------------------------------------------------------ calibration
def test_calibration_baseline_matches_body():
    cam = Camera(torso_px=150)
    cal = Calibrator(CFG)
    for t, lms in synth.sequence(synth.still(1), 30, cam, jitter=0.01):
        b = cal.update(sample_from_landmarks(t, lms, cam.width, cam.height))
        if b:
            break
    assert b is not None
    assert abs(b.cx - cam.centre[0]) < 3 and abs(b.cy - cam.centre[1]) < 3
    assert abs(b.torso - 150) / 150 < 0.03


def test_calibration_waits_for_stillness():
    # Person keeps swaying for the first 3 s -> calibration must not complete then.
    keys = [Key(0), Key(0.4, dx=0.4), Key(0.8, dx=-0.4), Key(1.2, dx=0.4), Key(1.6, dx=-0.4),
            Key(2.0, dx=0.4), Key(2.4, dx=-0.4), Key(2.8, dx=0.4), Key(3.0), Key(8.0)]
    cal = Calibrator(CFG)
    done_at = None
    cam = Camera()
    for t, lms in synth.sequence(keys, 30, cam):
        if cal.update(sample_from_landmarks(t, lms, cam.width, cam.height)) and done_at is None:
            done_at = t
    assert done_at is not None and done_at >= 3.0 + CFG.calibration.duration_s - 0.2


def test_calibration_rejects_missing_or_tiny_body():
    cal = Calibrator(CFG)
    assert cal.update(None) is None and "No body" in cal.message
    cam = Camera(torso_px=20)
    t, lms = next(synth.sequence(synth.still(1), 30, cam))
    assert cal.update(sample_from_landmarks(t, lms, cam.width, cam.height)) is None
    assert "too small" in cal.message.lower()


def test_occluded_hips_give_no_sample():
    cam = Camera()
    t, lms = next(synth.sequence(synth.still(1), 30, cam))
    lms[23, 2] = 0.1  # left hip barely visible
    assert sample_from_landmarks(t, lms, cam.width, cam.height, 0.5) is None


# ------------------------------------------------------------------ the four gestures
@pytest.mark.parametrize("fps", FPS_OPTIONS)
@pytest.mark.parametrize("direction", ["left", "right"])
def test_lean_fires_once(direction, fps):
    fired = run(synth.lean(direction), fps=fps)
    assert names(fired) == [direction]


@pytest.mark.parametrize("fps", FPS_OPTIONS)
def test_jump_fires_once_despite_landing_squat(fps):
    assert names(run(synth.jump(), fps=fps)) == ["jump"]


@pytest.mark.parametrize("fps", FPS_OPTIONS)
def test_duck_fires_once(fps):
    assert names(run(synth.duck(), fps=fps)) == ["duck"]


# ------------------------------------------------------------------ fires at the START
@pytest.mark.parametrize("keys,gesture,move_s", [
    (synth.lean("left", move_s=0.30), "left", 0.30),
    (synth.lean("right", move_s=0.30), "right", 0.30),
    (synth.jump(up_s=0.25), "jump", 0.25),
    (synth.duck(move_s=0.30), "duck", 0.30),
])
def test_fires_early_in_the_movement(keys, gesture, move_s):
    fired = run(keys, fps=60)
    assert names(fired) == [gesture]
    move_start = synth.CAL + 0.5
    elapsed = fired[0][0] - move_start
    # Must fire well before the movement completes (spec: "start of a movement, not its completion")
    assert elapsed < 0.75 * move_s, f"{gesture} fired {elapsed * 1000:.0f} ms into a {move_s * 1000:.0f} ms move"


# ------------------------------------------------------------------ no false positives
@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("jitter", [0.01, 0.02])
def test_standing_still_with_jitter_never_fires(seed, jitter):
    assert run(synth.still(15), jitter=jitter, seed=seed, frame_jitter_s=0.004) == []


@pytest.mark.parametrize("dx,dy", [(0.9, 0), (-0.9, 0), (0, -0.5), (0, 0.6), (0.6, 0.4)])
def test_slow_drift_never_fires(dx, dy):
    assert run(synth.drift(dx, dy, duration=12)) == []


def test_slow_drift_then_real_lean_still_detected():
    # Drift 0.6 to the right over 10 s, then lean left from the drifted position.
    t = synth.CAL + 10
    keys = [Key(0), Key(synth.CAL), Key(t, dx=0.6), Key(t + 1, dx=0.6), Key(t + 1.3, dx=0.0),
            Key(t + 2.3, dx=0.0), Key(t + 2.7, dx=0.6), Key(t + 4, dx=0.6)]
    assert names(run(keys)) == ["left"]


def test_holding_a_lean_does_not_repeat():
    keys = synth.lean("left", hold_s=4.0)
    assert names(run(keys, jitter=0.02)) == ["left"]


def test_wobbling_around_threshold_fires_once():
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.3, dx=-0.5)]
    t = t0 + 0.3
    for i in range(8):  # wobble +-0.12 around a held lean
        t += 0.15
        keys.append(Key(t, dx=-0.5 + (0.12 if i % 2 == 0 else -0.12)))
    keys += [Key(t + 0.5), Key(t + 1.5)]
    assert names(run(keys)) == ["left"]


def test_partial_return_does_not_rearm():
    # Lean left, come back only halfway (not past release), lean left again -> still one LEFT.
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.3, dx=-0.65), Key(t0 + 0.8, dx=-0.65), Key(t0 + 1.1, dx=-0.35),
            Key(t0 + 1.5, dx=-0.35), Key(t0 + 1.8, dx=-0.65), Key(t0 + 2.5, dx=-0.65), Key(t0 + 3.5)]
    assert names(run(keys)) == ["left"]


def test_two_leans_with_return_fire_twice():
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.3, dx=-0.6), Key(t0 + 0.8, dx=-0.6), Key(t0 + 1.2),
            Key(t0 + 1.7), Key(t0 + 2.0, dx=-0.6), Key(t0 + 2.6, dx=-0.6), Key(t0 + 3.0), Key(t0 + 4)]
    assert names(run(keys)) == ["left", "left"]


def test_left_then_right_through_neutral():
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.3, dx=-0.6), Key(t0 + 0.9, dx=-0.6), Key(t0 + 1.5, dx=0.6),
            Key(t0 + 2.2, dx=0.6), Key(t0 + 2.6), Key(t0 + 3.6)]
    assert names(run(keys)) == ["left", "right"]


def test_returning_to_neutral_from_lean_fires_nothing_extra():
    # Return speed as fast as the lean itself must not register as the opposite lean.
    assert names(run(synth.lean("right", back_s=0.3))) == ["right"]


def test_jump_with_preparatory_dip_is_only_a_jump():
    assert names(run(synth.jump(prep_dip=0.12))) == ["jump"]


def test_lean_with_sag_is_only_a_lean():
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.3, dx=-0.65, dy=0.15), Key(t0 + 1.3, dx=-0.65, dy=0.15),
            Key(t0 + 1.7), Key(t0 + 3)]
    assert names(run(keys)) == ["left"]


def test_jump_then_quick_second_jump_respects_cooldown_but_allows_later_jump():
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.2, dy=-0.5), Key(t0 + 0.4), Key(t0 + 1.4),
            Key(t0 + 1.6, dy=-0.5), Key(t0 + 1.8), Key(t0 + 3)]
    assert names(run(keys)) == ["jump", "jump"]


def test_jump_while_leaning():
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.3, dx=-0.6), Key(t0 + 1.0, dx=-0.6), Key(t0 + 1.22, dx=-0.6, dy=-0.5),
            Key(t0 + 1.44, dx=-0.6), Key(t0 + 2.0, dx=-0.6), Key(t0 + 2.4), Key(t0 + 3.5)]
    assert names(run(keys)) == ["left", "jump"]


# ------------------------------------------------------------------ robustness
def test_pose_dropout_and_reacquire_does_not_fire():
    # Pose lost for 0.6 s; person reappears slightly displaced (0.2 torso) - not a gesture.
    keys = [Key(0), Key(synth.CAL + 1), Key(synth.CAL + 1.6, dx=0.2), Key(synth.CAL + 5, dx=0.2)]
    fired = run(keys, dropouts=[(synth.CAL + 1.0, synth.CAL + 1.6)])
    assert fired == []


def test_single_frame_dropouts_during_lean():
    t0 = synth.CAL + 0.5
    drops = [(t0 + 0.1, t0 + 0.134), (t0 + 0.5, t0 + 0.534)]
    assert names(run(synth.lean("left"), dropouts=drops)) == ["left"]


@pytest.mark.parametrize("torso_px", [70, 160, 320])
def test_distance_invariance(torso_px):
    """Same body movement, person near/far from camera -> same result."""
    cam = Camera(torso_px=torso_px, centre=(640, 300 if torso_px < 300 else 250))
    for keys, g in ((synth.lean("left"), "left"), (synth.jump(), "jump"), (synth.duck(), "duck")):
        assert names(run(keys, cam=cam)) == [g]


@pytest.mark.parametrize("amount", [0.2])   # the fast filter (min_cutoff 5 Hz) lets 0.25 through
def test_too_small_lean_ignored(amount):
    assert run(synth.lean("left", amount=amount)) == []


def test_small_hop_ignored():
    # fast filter (min_cutoff 5 Hz, beta 1.0) trades a smaller dead zone for ~20 ms earlier jumps
    assert run(synth.jump(height=0.10, landing_squat=0.04)) == []


def test_full_sequence_every_gesture_once():
    t = synth.CAL + 0.5
    keys = [Key(0), Key(t)]

    def add(*ks):
        nonlocal t
        for dt, dx, dy in ks:
            t += dt
            keys.append(Key(t, dx=dx, dy=dy))

    add((0.3, -0.6, 0), (0.6, -0.6, 0), (0.4, 0, 0), (0.8, 0, 0))          # left
    add((0.3, 0.6, 0), (0.6, 0.6, 0), (0.4, 0, 0), (0.8, 0, 0))            # right
    add((0.22, 0, -0.5), (0.22, 0, 0.12), (0.25, 0, 0), (1.0, 0, 0))       # jump
    add((0.3, 0, 0.6), (0.8, 0, 0.6), (0.4, 0, 0), (1.0, 0, 0))            # duck
    assert names(run(keys, fps=30, jitter=0.015)) == ["left", "right", "jump", "duck"]


# ------------------------------------------------------------------ lanes mode
# Camera(): 1280 wide, body centred at x=640, torso 160 px -> lane lines at x=426 and 854, i.e.
# 1.34 torso lengths either side of centre; hysteresis 0.03*1280 = 38 px.
def lane_names(keys, **kw):
    return names(run(keys, cfg=LANES, **kw))


def step(dx, move_s=0.35, hold_s=1.0, back_s=0.4, after_s=1.0, back=True):
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + move_s, dx=dx), Key(t0 + move_s + hold_s, dx=dx)]
    if back:
        keys += [Key(t0 + move_s + hold_s + back_s), Key(t0 + move_s + hold_s + back_s + after_s)]
    else:
        keys += [Key(t0 + move_s + hold_s + after_s, dx=dx)]
    return keys


@pytest.mark.parametrize("fps", FPS_OPTIONS)
def test_lanes_step_left_and_back(fps):
    assert lane_names(step(-1.8), fps=fps) == ["left", "right"]


@pytest.mark.parametrize("fps", FPS_OPTIONS)
def test_lanes_step_right_and_back(fps):
    assert lane_names(step(1.8), fps=fps) == ["right", "left"]


def test_lanes_staying_in_a_side_lane_sends_one_swipe():
    assert lane_names(step(-1.8, hold_s=4.0, back=False), jitter=0.02) == ["left"]


def test_lanes_left_third_to_right_third_is_two_lanes():
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.4, dx=-1.8), Key(t0 + 1.4, dx=-1.8),
            Key(t0 + 2.1, dx=1.8), Key(t0 + 3.5, dx=1.8)]
    assert lane_names(keys) == ["left", "right", "right"]


def test_lanes_fire_before_the_step_completes():
    fired = run(step(-1.8, move_s=0.4), fps=60, cfg=LANES)
    assert names(fired)[0] == "left"
    assert fired[0][0] - (synth.CAL + 0.5) < 0.4


@pytest.mark.parametrize("seed", range(3))
def test_lanes_standing_on_a_line_does_not_flicker(seed):
    # Step fast to 4 px inside the middle lane, right next to the left line, then stand there with
    # webcam-like jitter. The fast step may fire early (lead), but inside the hysteresis band the
    # lane must never flip back and forth: at most one change.
    assert lane_names(step(-1.31, hold_s=6.0, back=False), jitter=0.02, seed=seed) in ([], ["left"])
    # Arriving slowly (no prediction) it stays in the middle lane.
    t0 = synth.CAL + 0.5
    slow = [Key(0), Key(t0), Key(t0 + 4.0, dx=-1.31), Key(t0 + 10.0, dx=-1.31)]
    assert lane_names(slow, jitter=0.02, seed=seed) == []


@pytest.mark.parametrize("seed", range(3))
def test_lanes_just_past_a_line_switches_once(seed):
    assert lane_names(step(-1.65, hold_s=6.0, back=False), jitter=0.02, seed=seed) == ["left"]


def test_lanes_slow_drift_across_a_line_changes_lane():
    # position decides the lane, so drifting into another third is a lane change (once)
    assert lane_names(synth.drift(-1.8, 0, duration=10)) == ["left"]


def test_lanes_calibrating_in_a_side_lane():
    cam = Camera(centre=(300.0, 330.0))   # standing in the left third
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.4, dx=1.5), Key(t0 + 2.0, dx=1.5)]   # step to the middle
    assert names(run(keys, cam=cam, cfg=LANES)) == ["right"]


def test_lanes_jump_and_duck_still_work():
    assert lane_names(synth.jump()) == ["jump"]
    assert lane_names(synth.duck()) == ["duck"]


def test_lanes_jump_while_in_side_lane():
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.4, dx=-1.8), Key(t0 + 1.5, dx=-1.8),
            Key(t0 + 1.72, dx=-1.8, dy=-0.5), Key(t0 + 1.94, dx=-1.8), Key(t0 + 3.0, dx=-1.8)]
    assert lane_names(keys) == ["left", "jump"]


@pytest.mark.parametrize("seed", range(3))
def test_lanes_standing_still_never_fires(seed):
    assert lane_names(synth.still(15), jitter=0.02, seed=seed) == []


# ------------------------------------------------------------------ regressions from real sessions
def test_deep_dip_before_jump_still_jumps():
    """A deep, fast wind-up dip may register as a duck, but the jump that follows must fire
    (duck_blocks_jump_s = 0; in the game a jump cancels the roll)."""
    for dip in (0.12, 0.27):
        assert names(run(synth.jump(prep_dip=dip)))[-1] == "jump"


def test_early_fire_does_not_self_release():
    """Gestures fire early (inside the release zone); the latch must not release while the body
    is still moving outward, or a single lean fires twice."""
    c = copy.deepcopy(CFG)
    for g in ("left", "right"):
        c.gestures[g].release = 0.30
    assert names(run(synth.lean("left", amount=1.2, move_s=0.6), cfg=c)) == ["left"]


def test_short_return_still_rearms_lean():
    """Returning only to 0.2 of neutral after a big lean re-arms (seen in a real session)."""
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.35, dx=-1.1), Key(t0 + 1.0, dx=-1.1), Key(t0 + 1.4, dx=-0.2),
            Key(t0 + 2.4, dx=-0.2), Key(t0 + 2.75, dx=-1.1), Key(t0 + 3.6, dx=-1.1), Key(t0 + 4.0), Key(t0 + 5)]
    assert names(run(keys)) == ["left", "left"]


def test_vertical_baseline_corrects_while_lean_is_held():
    """Calibrated slightly crouched, then standing taller while holding a lean: the vertical
    baseline must still adapt (per-axis), so a later small bob is not a jump."""
    t0 = synth.CAL + 0.5
    keys = [Key(0), Key(t0), Key(t0 + 0.35, dx=-1.0), Key(t0 + 1.0, dx=-1.0, dy=-0.15),
            Key(t0 + 8.0, dx=-1.0, dy=-0.15), Key(t0 + 8.3, dx=-1.0, dy=-0.25),
            Key(t0 + 8.6, dx=-1.0, dy=-0.15), Key(t0 + 10, dx=-1.0, dy=-0.15)]
    assert names(run(keys)) == ["left"]


def test_lanes_narrow_middle_from_config():
    # config.yaml narrows the middle lane well inside the thirds: a small step changes lane there
    narrow = copy.deepcopy(LANES)
    narrow.lanes = copy.deepcopy(load_config().lanes)
    assert narrow.lanes.boundaries[1] - narrow.lanes.boundaries[0] < 0.2
    assert names(run(step(-0.7, hold_s=3.0, back=False), cfg=LANES)) == []
    assert names(run(step(-0.7, hold_s=3.0, back=False), cfg=narrow)) == ["left"]
    assert names(run(step(-0.7, hold_s=3.0, back=True), cfg=narrow)) == ["left", "right"]


def test_hands_up():
    from sscam.detector import hands_up
    lms = [[0.5, 0.5, 1.0] for _ in range(33)]
    lms[0] = [0.5, 0.3, 1.0]                                   # nose
    lms[15], lms[16] = [0.4, 0.6, 1.0], [0.6, 0.6, 1.0]        # wrists at the hips
    assert not hands_up(lms)
    lms[15] = [0.4, 0.1, 1.0]                                  # one hand up
    assert not hands_up(lms)
    lms[16] = [0.6, 0.1, 1.0]                                  # both up
    assert hands_up(lms)
    lms[16] = [0.6, 0.1, 0.2]                                  # not visible -> not up
    assert not hands_up(lms)
