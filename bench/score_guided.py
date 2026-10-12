"""Scores a guided tuning session (`--guided --record-landmarks X --events Y`) against a config:
replays the recorded landmarks through the detector and matches every detection to the prompt
that asked for it. Lets you compare configs on the SAME movements.

Usage: .venv/bin/python bench/score_guided.py logs/tuning.jsonl logs/tuning_events.jsonl
           [--config a.yaml] [--set gestures.left.release=0.3 ...] [--skip "TWO LANES RIGHT"] [-v]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from sscam.config import load_config
from sscam.detector import Calibrator, GestureDetector, sample_from_landmarks

EARLY_S = 0.6    # people anticipate rhythmic prompts
LATE_S = 2.0     # reaction time + movement


def apply_override(cfg, expr: str):
    path, value = expr.split("=")
    obj = cfg
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj[p] if isinstance(obj, dict) else getattr(obj, p)
    cur = getattr(obj, parts[-1])
    setattr(obj, parts[-1], type(cur)(value))


def replay(cfg, recording, lanes=None):
    cal, det, fired = Calibrator(cfg), None, []
    for line in open(recording):
        r = json.loads(line)
        s = sample_from_landmarks(r["t"], r["lms"], r["w"], r["h"], cfg.pose.min_visibility) if r["lms"] else None
        if det is None:
            if cal.update(s):
                det = GestureDetector(cfg, cal.baseline)
            continue
        f = det.update(s)
        if lanes is not None and f.lane is not None:
            lanes.append((r["t"], f.lane))
        if f.gesture:
            fired.append((f.t, f.gesture))
    return fired


def lane_at(lanes, t):
    cur = None
    for tt, ln in lanes:
        if tt > t:
            break
        cur = ln
    return cur


def score_lanes(cfg, recording, prompts, verbose=False):
    """Lanes mode: for each 'go to X lane' prompt, did the body end up in lane X, using exactly
    the number of lane changes needed? Plus jump/duck matching; any other lane change is false."""
    lanes = []
    fired = replay(cfg, recording, lanes)
    sect = defaultdict(lambda: {"ok": 0, "miss": 0, "false": 0})
    lateral = [(t, g) for t, g in fired if g in ("left", "right")]
    claimed = set()
    prev_lane = 1
    for i, p in enumerate(prompts):
        end = prompts[i + 1]["t"] if i + 1 < len(prompts) else p["t"] + p["dur"]
        if p["prompt"].startswith("go to"):
            name = "lane moves (two lanes)" if "two" in p["prompt"] else "lane moves (one lane)"
            got = lane_at(lanes, end + 0.6)
            ev = [k for k, (t, g) in enumerate(lateral) if p["t"] - EARLY_S <= t <= end + 0.6 and k not in claimed]
            claimed.update(ev)
            need = abs(p["lane"] - prev_lane)
            if got == p["lane"]:
                sect[name]["ok"] += 1
            else:
                sect[name]["miss"] += 1
                if verbose:
                    print(f"  MISS  {p['prompt']} at {p['t']:.2f}: ended in lane {got}")
            if len(ev) > need:
                sect[name]["false"] += len(ev) - need
                if verbose:
                    print(f"  EXTRA {len(ev) - need} lane change(s) during '{p['prompt']}' at {p['t']:.2f}")
            prev_lane = p["lane"]
    for k, (t, g) in enumerate(lateral):
        if k not in claimed:
            cur = [p for p in prompts if p["t"] <= t]
            sect["(unexpected lane change)"]["false"] += 1
            if verbose:
                print(f"  FALSE {g} at {t:.2f} (during '{cur[-1]['prompt'] if cur else '-'}')")
    vert = [(t, g) for t, g in fired if g in ("jump", "duck")]
    used = set()
    for p in prompts:
        if p["expect"] in ("jump", "duck"):
            name = p["expect"].upper() + (" (side lane)" if "lane" in p["prompt"] else "")
            hit = next((k for k, (t, g) in enumerate(vert) if k not in used and g == p["expect"]
                        and p["t"] - EARLY_S <= t <= p["t"] + LATE_S), None)
            if hit is None:
                sect[name]["miss"] += 1
                if verbose:
                    print(f"  MISS  {p['prompt']} at {p['t']:.2f}")
            else:
                used.add(hit)
                sect[name]["ok"] += 1
    for k, (t, g) in enumerate(vert):
        if k not in used:
            cur = [p for p in prompts if p["t"] <= t]
            sect["(unexpected jump/duck)"]["false"] += 1
            if verbose:
                print(f"  FALSE {g} at {t:.2f} (during '{cur[-1]['prompt'] if cur else '-'}')")
    return dict(sect)


def score(cfg, recording, events, skip=(), verbose=False):
    prompts = [e for e in (json.loads(x) for x in open(events)) if "prompt" in e]
    if cfg.lateral_mode == "lanes" and any(p.get("lane") is not None for p in prompts):
        return score_lanes(cfg, recording, prompts, verbose)
    fired = replay(cfg, recording)
    used = set()
    sect = defaultdict(lambda: {"ok": 0, "miss": 0, "false": 0})
    skipped_windows = []
    for p in prompts:
        name = p["prompt"].split("(")[0].split(":")[0].strip()
        if any(s in p["prompt"] for s in skip):
            skipped_windows.append((p["t"] - EARLY_S, p["t"] + LATE_S))
            continue
        if not p["expect"]:
            continue
        hit = next((i for i, (t, g) in enumerate(fired) if i not in used and g == p["expect"]
                    and p["t"] - EARLY_S <= t <= p["t"] + LATE_S), None)
        if hit is None:
            sect[name]["miss"] += 1
            if verbose:
                print(f"  MISS  {p['prompt']} at {p['t']:.2f}")
        else:
            used.add(hit)
            sect[name]["ok"] += 1
    for i, (t, g) in enumerate(fired):
        if i in used or any(a <= t <= b for a, b in skipped_windows):
            continue
        cur = [p for p in prompts if p["t"] <= t]
        name = cur[-1]["prompt"].split("(")[0].split(":")[0].strip() if cur else "start"
        sect[name if "STILL" in name else "(unexpected)"]["false"] += 1
        if verbose:
            print(f"  FALSE {g} at {t:.2f} (during '{cur[-1]['prompt'] if cur else '-'}')")
    return dict(sect)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("events")
    ap.add_argument("--config")
    ap.add_argument("--set", action="append", default=[], help="override, e.g. gestures.left.release=0.3")
    ap.add_argument("--skip", action="append", default=[], help="ignore prompts containing this text")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config) if a.config else load_config()
    for o in a.set:
        apply_override(cfg, o)
    res = score(cfg, a.recording, a.events, a.skip, a.v)
    tot = {k: sum(r[k] for r in res.values()) for k in ("ok", "miss", "false")}
    for name, r in res.items():
        print(f"  {name:28s} ok {r['ok']:2d}  missed {r['miss']:2d}  false {r['false']:2d}")
    print(f"  {'TOTAL':28s} ok {tot['ok']:2d}  missed {tot['miss']:2d}  false {tot['false']:2d}")


if __name__ == "__main__":
    main()
