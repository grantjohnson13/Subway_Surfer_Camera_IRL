"""Generates a test video of a real person (tests/data/person.jpg, from the MediaPipe test
assets) performing the four gestures, by moving/squashing the photo on a canvas along a
min-jerk timeline. Ground truth is returned alongside, so the full pipeline
(video -> MediaPipe -> detector -> events) can be checked end to end.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from sscam.synth import Key, trajectory

DATA = Path(__file__).parent / "data"
PERSON = DATA / "person.jpg"
W, H, FPS = 1280, 720, 30
TORSO_PX = 162  # approx. shoulder->hip distance of the person after scaling to H

# (gesture, movement start time). Each move takes ~0.3 s and returns to neutral.
SCRIPT_START = 3.0


def keys_and_truth(fast: bool = False):
    """fast=True packs the four gestures into ~4 s (for driving a live game run)."""
    t = SCRIPT_START
    keys, truth = [Key(0), Key(t)], []

    def move(name, dx=0.0, dy=0.0, crouch=1.0, move_s=0.35, hold_s=0.7, back_s=0.4, rest_s=1.0):
        nonlocal t
        truth.append((name, t, t + move_s))
        keys.extend([Key(t + move_s, dx, dy, crouch), Key(t + move_s + hold_s, dx, dy, crouch),
                     Key(t + move_s + hold_s + back_s), Key(t + move_s + hold_s + back_s + rest_s)])
        t += move_s + hold_s + back_s + rest_s

    kw = dict(hold_s=0.3, back_s=0.3, rest_s=0.35) if fast else {}
    # +-1.8 torso lengths: far enough to cross into the side third of the frame (lanes mode)
    move("left", dx=-1.8, **kw)
    move("right", dx=1.8, **kw)
    move("jump", dy=-0.5, move_s=0.22, hold_s=0.0, back_s=0.22, rest_s=0.7 if fast else 1.2)
    move("duck", dy=0.6, crouch=0.85, **kw)
    keys.append(Key(t + 1.0))
    return keys, truth


def make_video(path: Path, seed: int = 0, still_s: float | None = None, fast: bool = False
               ) -> list[tuple[str, float, float]]:
    """Write the gesture video (or, with still_s, a video of the person just standing there
    for that long) and return the ground truth [(gesture, move_start, move_end)]."""
    keys, truth = keys_and_truth(fast) if still_s is None else ([Key(0), Key(still_s)], [])
    times, states = trajectory(keys, FPS)
    src = cv2.imread(str(PERSON))
    s = H / src.shape[0]
    person = cv2.resize(src, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ph, pw = person.shape[:2]
    rng = np.random.default_rng(seed)
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    # pivot for squash = feet (bottom of the photo) so a crouch lowers the torso
    for (dx, dy, crouch) in states:
        M = np.float32([[1, 0, (W - pw) / 2 + dx * TORSO_PX],
                        [0, crouch, dy * TORSO_PX + (1 - crouch) * ph]])
        frame = cv2.warpAffine(person, M, (W, H), borderMode=cv2.BORDER_REPLICATE)
        noise = rng.normal(0, 4, frame.shape).astype(np.int16)  # camera sensor noise
        frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        # The pipeline mirrors frames (selfie view); pre-flip so "left" in the script is
        # the player's left after mirroring.
        out.write(cv2.flip(frame, 1))
    out.release()
    return truth


if __name__ == "__main__":
    for name, fast in (("gestures.mp4", False), ("gestures_fast.mp4", True)):
        p = DATA / name
        print(make_video(p, fast=fast), "->", p)
