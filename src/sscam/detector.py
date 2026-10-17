"""Gesture detection from body landmarks — pure logic, no camera or MediaPipe imports,
so it can be driven by synthetic/recorded landmark sequences in tests.

Pipeline per frame:
    BodySample (torso centre + torso length, in pixels)
      -> One-Euro filter (centre) -> offset from baseline / baseline torso length, velocity
      -> per-axis latch state machine with hysteresis + cooldowns -> at most one gesture
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .config import Config

# MediaPipe Pose landmark indices
L_SHOULDER, R_SHOULDER, L_HIP, R_HIP = 11, 12, 23, 24
TORSO_IDS = (L_SHOULDER, R_SHOULDER, L_HIP, R_HIP)


@dataclass
class BodySample:
    """Torso measurements for one frame, in pixels of the (mirrored) frame. y grows downward."""
    t: float                 # capture timestamp, seconds
    cx: float                # torso centre x (mean of shoulder midpoint and hip midpoint)
    cy: float                # torso centre y
    torso: float             # shoulder-midpoint to hip-midpoint distance
    w: float = 0.0           # frame width in pixels (lanes mode divides it into lanes)


NOSE, L_WRIST, R_WRIST = 0, 15, 16


def hands_up(lms, min_visibility: float = 0.5) -> bool:
    """Both wrists raised above the nose (the "start a run" gesture)."""
    if lms is None:
        return False
    pts = []
    for i in (NOSE, L_WRIST, R_WRIST):
        lm = lms[i]
        x, y, vis = (lm.x, lm.y, lm.visibility if lm.visibility is not None else 1.0) if hasattr(lm, "x") else lm
        if vis < min_visibility:
            return False
        pts.append(y)
    nose, lw, rw = pts
    return lw < nose and rw < nose


def sample_from_landmarks(t: float, lms, width: int, height: int, min_visibility: float = 0.5
                          ) -> BodySample | None:
    """Build a BodySample from 33 normalized landmarks (objects with x, y, visibility, or
    an (33, 3) array of x, y, visibility). Returns None if the torso is not reliably visible."""
    pts = []
    for i in TORSO_IDS:
        lm = lms[i]
        if hasattr(lm, "x"):
            x, y, vis = lm.x, lm.y, (lm.visibility if lm.visibility is not None else 1.0)
        else:
            x, y, vis = lm[0], lm[1], lm[2]
        if vis < min_visibility:
            return None
        pts.append((x * width, y * height))
    (lsx, lsy), (rsx, rsy), (lhx, lhy), (rhx, rhy) = pts
    shx, shy = (lsx + rsx) / 2, (lsy + rsy) / 2
    hpx, hpy = (lhx + rhx) / 2, (lhy + rhy) / 2
    torso = math.hypot(shx - hpx, shy - hpy)
    if torso < 1:
        return None
    return BodySample(t, (shx + hpx) / 2, (shy + hpy) / 2, torso, float(width))


# --------------------------------------------------------------------------- filtering
class OneEuro:
    """One-Euro filter (Casiez et al. 2012) that also exposes its smoothed derivative."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = self.t = self.raw = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self):
        self.x = self.dx = self.t = self.raw = None

    def __call__(self, t: float, x: float, scale: float = 1.0) -> tuple[float, float]:
        """Filter x at time t. `scale` converts x to the units beta is defined in, so the
        filter behaves the same regardless of how big the person is in the frame."""
        if self.t is None or t <= self.t:
            self.x, self.dx, self.t, self.raw = x, 0.0, t, x
            return x, 0.0
        dt = t - self.t
        # Derivative of the raw signal (the reference filter differences against the previous
        # *filtered* value, which adds the filter's lag to the velocity and overestimates it).
        raw_dx = (x - self.raw) / dt
        self.raw = x
        a_d = self._alpha(self.d_cutoff, dt)
        self.dx = a_d * raw_dx + (1 - a_d) * self.dx
        cutoff = self.min_cutoff + self.beta * abs(self.dx) / scale
        a = self._alpha(cutoff, dt)
        self.x = a * x + (1 - a) * self.x
        self.t = t
        return self.x, self.dx


# --------------------------------------------------------------------------- calibration
@dataclass
class Baseline:
    cx: float
    cy: float
    torso: float


class Calibrator:
    """Collects samples while the player stands still; produces a Baseline.
    Restarts the window whenever the player moves too much or the pose is lost."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.samples: list[BodySample] = []
        self.baseline: Baseline | None = None
        self.message = "Stand in your play position"

    @property
    def progress(self) -> float:
        if self.baseline:
            return 1.0
        if len(self.samples) < 2:
            return 0.0
        return min(1.0, (self.samples[-1].t - self.samples[0].t) / self.cfg.calibration.duration_s)

    def update(self, s: BodySample | None) -> Baseline | None:
        if self.baseline:
            return self.baseline
        c = self.cfg.calibration
        if s is None:
            self.samples.clear()
            self.message = "No body detected - step into frame (shoulders + hips visible)"
            return None
        if s.torso < c.min_torso_px:
            self.samples.clear()
            self.message = "Too far away / too small in frame - step closer"
            return None
        self.samples.append(s)
        arr = np.array([(x.cx, x.cy, x.torso) for x in self.samples])
        torso = float(np.median(arr[:, 2]))
        jitter = float(np.max(np.std(arr[:, :2], axis=0))) / torso
        if jitter > c.max_jitter and len(self.samples) > 5:
            # Drop the oldest half so a single movement does not block calibration forever
            self.samples = self.samples[len(self.samples) // 2:]
            self.message = "Hold still..."
            return None
        self.message = f"Calibrating... hold still ({self.progress * 100:.0f}%)"
        if self.progress >= 1.0:
            self.baseline = Baseline(float(np.median(arr[:, 0])), float(np.median(arr[:, 1])), torso)
            self.message = "Calibrated"
        return self.baseline

    def reset(self):
        self.samples.clear()
        self.baseline = None


# --------------------------------------------------------------------------- detection
@dataclass
class Frame:
    """Detector output for one frame (also drives the preview overlay)."""
    t: float
    tracking: bool
    dx: float = 0.0          # offset from baseline, torso lengths (+ = player's right)
    dy: float = 0.0          # (+ = down)
    vx: float = 0.0          # torso lengths / s
    vy: float = 0.0
    gesture: str | None = None
    lateral_latch: str | None = None
    vertical_latch: str | None = None
    lane: int | None = None  # lanes mode: 0 left, 1 middle, 2 right (the third the body is in)


@dataclass
class _Axis:
    latch: str | None = None          # gesture currently latched on this axis
    peak: float = 0.0                 # furthest offset (toward the latched side) since it fired
    last_fire: dict = field(default_factory=dict)

    def fire(self, g: str, t: float, offset: float):
        self.latch, self.peak = g, offset
        self.last_fire[g] = t

    def update_latch(self, offset_toward_latch: float, release: float):
        """Release once the body has come back below min(release, peak/2). Relative to the peak, so
        it cannot release while still moving outward (gestures fire early, inside the release zone),
        and a small movement re-arms after returning half-way."""
        self.peak = max(self.peak, offset_toward_latch)
        if offset_toward_latch < min(release, 0.5 * self.peak):
            self.latch = None


class GestureDetector:
    """Turns BodySamples into at most one gesture per frame.

    Rules (all thresholds in config.yaml):
      * fire when offset >= min_offset, velocity >= min_velocity (toward that side) and
        offset + velocity*lookahead >= trigger  -> fires during the movement, not at its end;
      * afterwards the gesture is latched; nothing else fires on that axis in the same
        direction until |offset| < release (back near neutral) — one lean = one lane change;
      * per-gesture cooldown; after jump, duck is blocked for vertical_cross_block_s (landing
        squat), and vice versa;
      * axis dominance: a vertical gesture needs |vy| >= axis_dominance*|vx| and vice versa;
      * while fully neutral the baseline slowly follows the body (absorbs drift).
    """

    def __init__(self, cfg: Config, baseline: Baseline):
        self.cfg = cfg
        self.base = Baseline(baseline.cx, baseline.cy, baseline.torso)
        f = cfg.filter
        self.fx = OneEuro(f.min_cutoff_hz, f.beta, f.d_cutoff_hz)
        self.fy = OneEuro(f.min_cutoff_hz, f.beta, f.d_cutoff_hz)
        self.lat = _Axis()
        self.ver = _Axis()
        self.last_t: float | None = None
        self.last_seen: float | None = None
        self._adapt_t: float | None = None
        self.lane: int | None = None       # lanes mode only

    def _lane_of(self, x: float, w: float) -> int:
        b0, b1 = self.cfg.lanes.boundaries
        return 0 if x < b0 * w else (2 if x > b1 * w else 1)

    def _update_lane(self, x: float, vx_px: float, w: float) -> str | None:
        """Lanes mode: the third of the frame the torso is in is the lane. A switch needs the
        PROJECTED position (x + v*lookahead) past the line by `hysteresis` (so standing on a line
        never flickers), and lets a fast move fire up to `lead` before actually reaching the line."""
        L = self.cfg.lanes
        if self.lane is None:
            self.lane = self._lane_of(x, w)
            return None
        bounds = [b * w for b in L.boundaries]
        h, lead = L.hysteresis * w, L.lead * w
        px = x + vx_px * self.cfg.lookahead_s
        if self.lane < 2 and px > bounds[self.lane] + h and x > bounds[self.lane] - lead:
            self.lane += 1
            return "right"
        if self.lane > 0 and px < bounds[self.lane - 1] - h and x < bounds[self.lane - 1] + lead:
            self.lane -= 1
            return "left"
        return None

    def _cooled(self, axis: _Axis, g: str, t: float) -> bool:
        last = axis.last_fire.get(g)
        return last is None or t - last >= self.cfg.gestures[g].cooldown_s

    def _check(self, g: str, offset: float, vel: float) -> bool:
        """offset/vel already signed so that positive = toward gesture g."""
        gc = self.cfg.gestures[g]
        return (offset >= gc.min_offset and vel >= gc.min_velocity
                and offset + vel * self.cfg.lookahead_s >= gc.trigger)

    def update(self, s: BodySample | None) -> Frame:
        if s is None:
            t = self.last_t or 0.0
            return Frame(t, tracking=False, lateral_latch=self.lat.latch, vertical_latch=self.ver.latch)
        t = s.t
        # Pose lost for a while: restart filters so reacquisition does not look like a fast move.
        if self.last_seen is not None and t - self.last_seen > self.cfg.pose.lost_reset_s:
            self.fx.reset()
            self.fy.reset()
        self.last_seen = t
        self.last_t = t

        T = self.base.torso
        x, vx_px = self.fx(t, s.cx, scale=T)
        y, vy_px = self.fy(t, s.cy, scale=T)
        dx, dy = (x - self.base.cx) / T, (y - self.base.cy) / T
        vx, vy = vx_px / T, vy_px / T
        out = Frame(t, True, dx, dy, vx, vy)

        g_fired = None
        dom = self.cfg.axis_dominance
        # ---- lateral axis
        if self.cfg.lateral_mode == "lanes":
            g_fired = self._update_lane(x, vx_px, s.w)
        elif self.lat.latch:
            self.lat.update_latch(-dx if self.lat.latch == "left" else dx,
                                  self.cfg.gestures[self.lat.latch].release)
        if self.cfg.lateral_mode == "lean" and abs(vx) >= dom * abs(vy):
            for g, sign in (("left", -1), ("right", 1)):
                if self.lat.latch != g and self._cooled(self.lat, g, t) and self._check(g, sign * dx, sign * vx):
                    self.lat.fire(g, t, sign * dx)
                    g_fired = g
                    break
        # ---- vertical axis (y grows downward: jump = negative dy)
        if self.ver.latch:
            self.ver.update_latch(-dy if self.ver.latch == "jump" else dy,
                                  self.cfg.gestures[self.ver.latch].release)
        if g_fired is None and abs(vy) >= dom * abs(vx):
            for g, sign, other in (("jump", -1, "duck"), ("duck", 1, "jump")):
                # duck after jump: blocked for vertical_cross_block_s (the landing squat is not a duck).
                # jump after duck: blocked only for duck_blocks_jump_s (default 0): the dip before a jump
                # can look like a duck, and in the game a jump simply cancels a roll.
                block = self.cfg.vertical_cross_block_s if g == "duck" else self.cfg.duck_blocks_jump_s
                other_t = self.ver.last_fire.get(other)
                blocked = other_t is not None and t - other_t < block
                if (self.ver.latch != g and not blocked and self._cooled(self.ver, g, t)
                        and self._check(g, sign * dy, sign * vy)):
                    self.ver.fire(g, t, sign * dy)
                    g_fired = g
                    break

        # ---- baseline adaptation, per axis: while an axis has nothing latched and the body moves
        # slowly along it, that axis's baseline follows the body (absorbs slow drift, posture changes
        # and a slightly-off calibration). Deliberate moves are fast and get latched first, so they
        # are never absorbed. Axes adapt independently, so holding a lean does not freeze vertical
        # correction (and vice versa).
        tau, vmax = self.cfg.baseline_adapt_tau_s, self.cfg.baseline_adapt_max_velocity
        if tau > 0 and g_fired is None:
            a = min(1.0, (t - self._adapt_t) / tau) if self._adapt_t is not None else 0.0
            if self.lat.latch is None and abs(vx) < vmax:
                self.base.cx += a * (x - self.base.cx)
            if self.ver.latch is None and abs(vy) < vmax:
                self.base.cy += a * (y - self.base.cy)
            if self.lat.latch is None and self.ver.latch is None and math.hypot(vx, vy) < vmax:
                self.base.torso += a * (s.torso - self.base.torso)
        self._adapt_t = t

        out.gesture = g_fired
        out.lateral_latch, out.vertical_latch = self.lat.latch, self.ver.latch
        out.lane = self.lane
        return out
