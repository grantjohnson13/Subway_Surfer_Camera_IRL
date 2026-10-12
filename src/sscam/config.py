"""Loads config.yaml into typed dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config.yaml"

GESTURES = ("left", "right", "jump", "duck")


@dataclass
class CameraCfg:
    index: int = 0
    width: int = 1280
    height: int = 720
    fps: int = 60
    mirror: bool = True
    inference_width: int = 640


@dataclass
class PoseCfg:
    model: str = "models/pose_landmarker_lite.task"
    min_detection_confidence: float = 0.5
    min_presence_confidence: float = 0.5
    min_tracking_confidence: float = 0.5
    min_visibility: float = 0.5
    lost_reset_s: float = 0.25


@dataclass
class CalibrationCfg:
    duration_s: float = 2.0
    max_jitter: float = 0.06
    min_torso_px: float = 40


@dataclass
class FilterCfg:
    min_cutoff_hz: float = 2.5
    beta: float = 0.5
    d_cutoff_hz: float = 4.0


@dataclass
class GestureCfg:
    trigger: float = 0.4
    min_offset: float = 0.15
    min_velocity: float = 0.9
    release: float = 0.18
    cooldown_s: float = 0.3


@dataclass
class LanesCfg:
    boundaries: list = field(default_factory=lambda: [0.333, 0.667])
    hysteresis: float = 0.03
    lead: float = 0.04
    resync_delay_s: float = 1.0


@dataclass
class SwipeCfg:
    center_x: float = 0.5
    center_y: float = 0.6
    length: float = 0.28
    duration_ms: float = 40
    steps: int = 5
    hold_ms: float = 0


@dataclass
class InjectorCfg:
    method: str = "emu_console"
    fallback: str = "scrcpy"
    serial: str = "emulator-5554"
    console_port: int = 5554
    swipe: SwipeCfg = field(default_factory=SwipeCfg)


@dataclass
class PreviewCfg:
    x: int = 470
    y: int = 40
    width: int = 1240


@dataclass
class GameGateCfg:
    enabled: bool = True
    template: str = "assets/pause_button.png"
    threshold: float = 0.75
    interval_s: float = 0.10
    max_age_s: float = 0.8


@dataclass
class Config:
    camera: CameraCfg = field(default_factory=CameraCfg)
    pose: PoseCfg = field(default_factory=PoseCfg)
    calibration: CalibrationCfg = field(default_factory=CalibrationCfg)
    filter: FilterCfg = field(default_factory=FilterCfg)
    lateral_mode: str = "lanes"
    lanes: LanesCfg = field(default_factory=LanesCfg)
    lookahead_s: float = 0.10
    gestures: dict[str, GestureCfg] = field(
        default_factory=lambda: {g: GestureCfg() for g in GESTURES})
    vertical_cross_block_s: float = 0.55
    duck_blocks_jump_s: float = 0.0
    axis_dominance: float = 0.6
    baseline_adapt_tau_s: float = 3.0
    baseline_adapt_max_velocity: float = 0.35
    injector: InjectorCfg = field(default_factory=InjectorCfg)
    game_gate: GameGateCfg = field(default_factory=GameGateCfg)
    preview: PreviewCfg = field(default_factory=PreviewCfg)


def _build(cls, data: dict[str, Any]):
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys for {cls.__name__}: {sorted(unknown)}")
    kwargs = {}
    defaults = cls()
    for name, value in data.items():
        current = getattr(defaults, name)
        if is_dataclass(current) and isinstance(value, dict):
            kwargs[name] = _build(type(current), value)
        elif name == "gestures":
            missing = set(GESTURES) - set(value)
            if missing:
                raise ValueError(f"gestures missing: {sorted(missing)}")
            kwargs[name] = {g: _build(GestureCfg, value[g]) for g in GESTURES}
        else:
            kwargs[name] = type(current)(value) if current is not None else value
    cfg = cls(**kwargs)
    if cls is Config and cfg.lateral_mode not in ("lanes", "lean"):
        raise ValueError(f"lateral_mode must be 'lanes' or 'lean', not {cfg.lateral_mode!r}")
    return cfg


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return _build(Config, data)


def resolve(path: str) -> Path:
    """Resolve a config-relative path (e.g. the model file) against the project root."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p
