"""Synthetic MediaPipe-style landmark sequences for testing the detector without a person.

A sequence is built from keyframes of the torso-centre offset (in torso lengths, + right,
+ down) and interpolated with minimum-jerk easing, which matches how people actually move
(smooth start, peak velocity mid-way). Landmarks are then generated for a simple 33-point
skeleton, with per-landmark Gaussian jitter and optional dropouts.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Template skeleton in torso-length units relative to the torso centre (x right, y down),
# loosely following MediaPipe's 33-landmark layout. Only 11/12/23/24 matter to the detector,
# but a full skeleton keeps the preview/recorder code paths realistic.
_T = {
    0: (0, -1.05), 1: (-0.04, -1.1), 2: (-0.07, -1.1), 3: (-0.1, -1.1), 4: (0.04, -1.1),
    5: (0.07, -1.1), 6: (0.1, -1.1), 7: (-0.15, -1.07), 8: (0.15, -1.07), 9: (-0.04, -0.98),
    10: (0.04, -0.98), 11: (-0.35, -0.5), 12: (0.35, -0.5), 13: (-0.45, 0.0), 14: (0.45, 0.0),
    15: (-0.5, 0.45), 16: (0.5, 0.45), 17: (-0.52, 0.52), 18: (0.52, 0.52), 19: (-0.5, 0.53),
    20: (0.5, 0.53), 21: (-0.48, 0.5), 22: (0.48, 0.5), 23: (-0.2, 0.5), 24: (0.2, 0.5),
    25: (-0.22, 1.3), 26: (0.22, 1.3), 27: (-0.23, 2.05), 28: (0.23, 2.05), 29: (-0.25, 2.15),
    30: (0.25, 2.15), 31: (-0.18, 2.2), 32: (0.18, 2.2),
}
TEMPLATE = np.array([_T[i] for i in range(33)], dtype=float)


def min_jerk(u: np.ndarray) -> np.ndarray:
    u = np.clip(u, 0, 1)
    return u ** 3 * (10 - 15 * u + 6 * u ** 2)


@dataclass
class Key:
    """At time t (s) the torso centre offset is (dx, dy) torso lengths and the body's
    torso length is scaled by `crouch` (a duck shortens the torso a little)."""
    t: float
    dx: float = 0.0
    dy: float = 0.0
    crouch: float = 1.0


def trajectory(keys: list[Key], fps: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (times, states[N, 3]) with min-jerk interpolation between keys."""
    keys = sorted(keys, key=lambda k: k.t)
    times = np.arange(0, keys[-1].t, 1.0 / fps)
    st = np.zeros((len(times), 3))
    vals = np.array([(k.dx, k.dy, k.crouch) for k in keys])
    kt = np.array([k.t for k in keys])
    for i, t in enumerate(times):
        j = np.searchsorted(kt, t, side="right") - 1
        if j >= len(keys) - 1:
            st[i] = vals[-1]
            continue
        u = min_jerk(np.array((t - kt[j]) / (kt[j + 1] - kt[j])))
        st[i] = vals[j] + (vals[j + 1] - vals[j]) * u
    return times, st


@dataclass
class Camera:
    width: int = 1280
    height: int = 720
    torso_px: float = 160.0          # torso length in pixels at baseline (distance from camera)
    centre: tuple[float, float] = (640.0, 330.0)


def landmarks_for(state: np.ndarray, cam: Camera, rng: np.random.Generator, jitter_px: float
                  ) -> np.ndarray:
    """(33, 3) normalized landmarks [x, y, visibility] for one state (dx, dy, crouch)."""
    dx, dy, crouch = state
    pts = TEMPLATE.copy()
    pts[:, 1] *= crouch                     # vertical squash for crouching
    pts = pts * cam.torso_px
    pts[:, 0] += cam.centre[0] + dx * cam.torso_px
    pts[:, 1] += cam.centre[1] + dy * cam.torso_px
    pts += rng.normal(0, jitter_px, pts.shape)
    out = np.empty((33, 3))
    out[:, 0] = pts[:, 0] / cam.width
    out[:, 1] = pts[:, 1] / cam.height
    out[:, 2] = 0.99
    return out


def sequence(keys: list[Key], fps: float = 30, cam: Camera | None = None, jitter: float = 0.01,
             seed: int = 0, dropouts: list[tuple[float, float]] = (), frame_jitter_s: float = 0.0):
    """Yield (t, landmarks or None) — `jitter` is per-landmark noise std in torso lengths
    (MediaPipe on a webcam is typically 0.5–1.5% of torso length when standing still)."""
    cam = cam or Camera()
    rng = np.random.default_rng(seed)
    times, states = trajectory(keys, fps)
    for t, s in zip(times, states):
        tt = float(t + (rng.uniform(-frame_jitter_s, frame_jitter_s) if frame_jitter_s else 0))
        if any(a <= t < b for a, b in dropouts):
            yield tt, None
        else:
            yield tt, landmarks_for(s, cam, rng, jitter * cam.torso_px)


# ---------------------------------------------------------------------------- scenarios
# Each returns keyframes. Calibration uses the first `CAL` seconds of standing still.
CAL = 2.5


def still(duration=8.0):
    return [Key(0), Key(CAL + duration)]


def lean(direction="left", amount=0.6, move_s=0.30, hold_s=1.0, back_s=0.45, after_s=1.2):
    sgn = -1 if direction == "left" else 1
    t0 = CAL + 0.5
    return [Key(0), Key(t0), Key(t0 + move_s, dx=sgn * amount), Key(t0 + move_s + hold_s, dx=sgn * amount),
            Key(t0 + move_s + hold_s + back_s), Key(t0 + move_s + hold_s + back_s + after_s)]


def jump(height=0.5, up_s=0.22, down_s=0.22, landing_squat=0.15, prep_dip=0.0, after_s=1.5):
    t = CAL + 0.5
    keys = [Key(0), Key(t)]
    if prep_dip:
        keys += [Key(t + 0.18, dy=prep_dip, crouch=0.97)]
        t += 0.18
    keys += [Key(t + up_s, dy=-height), Key(t + up_s + down_s, dy=landing_squat, crouch=0.95),
             Key(t + up_s + down_s + 0.25), Key(t + up_s + down_s + 0.25 + after_s)]
    return keys


def duck(depth=0.6, move_s=0.30, hold_s=0.8, back_s=0.4, after_s=1.5):
    t = CAL + 0.5
    return [Key(0), Key(t), Key(t + move_s, dy=depth, crouch=0.85), Key(t + move_s + hold_s, dy=depth, crouch=0.85),
            Key(t + move_s + hold_s + back_s), Key(t + move_s + hold_s + back_s + after_s)]


def drift(dx=0.9, dy=0.0, duration=12.0):
    return [Key(0), Key(CAL), Key(CAL + duration, dx=dx, dy=dy), Key(CAL + duration + 1, dx=dx, dy=dy)]
