"""MediaPipe Pose Landmarker (Tasks API) wrapper.

mediapipe >= 0.10.30 no longer ships the legacy `mp.solutions.pose`; the Tasks API needs a
.task model file (downloaded into models/ by scripts/setup.sh).
"""
from __future__ import annotations

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (PoseLandmarker, PoseLandmarkerOptions,
                                           PoseLandmarksConnections, RunningMode)

from .config import PoseCfg, resolve

CONNECTIONS = [(c.start, c.end) for c in PoseLandmarksConnections.POSE_LANDMARKS]


class PoseTracker:
    def __init__(self, cfg: PoseCfg):
        model = resolve(cfg.model)
        if not model.exists():
            raise FileNotFoundError(f"pose model not found: {model} (run scripts/setup.sh)")
        opts = PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model)),
            running_mode=RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=cfg.min_detection_confidence,
            min_pose_presence_confidence=cfg.min_presence_confidence,
            min_tracking_confidence=cfg.min_tracking_confidence,
        )
        self.landmarker = PoseLandmarker.create_from_options(opts)
        self._last_ts = -1

    def detect(self, bgr: np.ndarray, t: float):
        """Returns the 33 normalized landmarks of the first person, or None."""
        ts = max(int(t * 1000), self._last_ts + 1)  # VIDEO mode needs strictly increasing ms
        self._last_ts = ts
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        res = self.landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
        return res.pose_landmarks[0] if res.pose_landmarks else None

    def close(self):
        self.landmarker.close()
