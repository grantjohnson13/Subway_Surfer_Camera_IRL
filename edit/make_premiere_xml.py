"""Builds a Premiere Pro project (Final Cut Pro 7 XML, File > Import in Premiere) from recorded
sessions: ONE vertical 1080x1920 / 60 fps sequence with the sessions back to back (GAP_S apart),
every view on its own track and in sync, with markers at each take and round start.

  V1  game.mp4      the game
  V2  overlay.mp4   webcam with lane lines
  V3  camera.mp4    clean webcam
  V4  other angle   from angles.json (edit/sync_angles.py) - on top
Clips are only placed in sync: no scaling, cropping or effects.
  A1  other angle's audio

Usage: .venv/bin/python edit/make_premiere_xml.py [session folders...] [-o out.xml]
"""
from __future__ import annotations

import argparse
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SEQ_FPS = 60
SEQ_W, SEQ_H = 1080, 1920
GAP_S = 3.0
FIT_LANDSCAPE = False   # True: scale landscape clips to the frame width; False: untouched (100%)


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate,channels,sample_rate:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True)
    d = json.loads(out.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return {"w": v["width"], "h": v["height"], "fps": round(float(Fraction(v["r_frame_rate"]))),
            "dur": float(d["format"]["duration"]),
            "channels": int(a["channels"]) if a else 0, "sr": int(a["sample_rate"]) if a else 48000}


def rate(fps: int) -> str:
    return f"<rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>"


class Builder:
    def __init__(self):
        self.files: dict[Path, str] = {}   # path -> file id (full definition written once)
        self.n = 0

    def uid(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}-{self.n}"

    def file_el(self, path: Path, info: dict) -> str:
        if path in self.files:
            return f'<file id="{self.files[path]}"/>'
        fid = self.uid("file")
        self.files[path] = fid
        audio = ""
        if info["channels"]:
            audio = (f"<audio><samplecharacteristics><depth>16</depth><samplerate>{info['sr']}</samplerate>"
                     f"</samplecharacteristics><channelcount>{info['channels']}</channelcount></audio>")
        return (f'<file id="{fid}"><name>{escape(path.name)}</name>'
                f"<pathurl>file://localhost{quote(str(path))}</pathurl>{rate(info['fps'])}"
                f"<duration>{int(info['dur'] * info['fps'])}</duration>"
                f"<media><video><samplecharacteristics>{rate(info['fps'])}<width>{info['w']}</width>"
                f"<height>{info['h']}</height></samplecharacteristics></video>{audio}</media></file>")

    def clip(self, path: Path, info: dict, start_s: float, kind: str, enabled: bool,
             seq_dur_s: float, channel: int = 1, block: tuple[float, float] = (0.0, 1e9)) -> str:
        """A clip placed so that its own time 0 is at sequence time start_s, cut to block (start, end)."""
        fps = info["fps"]
        src_in = max(0.0, block[0] - start_s)             # trim what falls before the block
        seq_start = start_s + src_in
        length = min(info["dur"] - src_in, min(seq_dur_s, block[1]) - seq_start)
        if length <= 0:
            return ""
        s0, s1 = round(seq_start * SEQ_FPS), round((seq_start + length) * SEQ_FPS)
        i0, i1 = round(src_in * fps), round((src_in + length) * fps)
        motion = ""
        if kind == "video" and info["w"] > info["h"] and FIT_LANDSCAPE:   # fit to the vertical frame's width
            motion = ("<filter><effect><name>Basic Motion</name><effectid>basic</effectid>"
                      "<effectcategory>motion</effectcategory><effecttype>motion</effecttype>"
                      "<mediatype>video</mediatype><parameter><parameterid>scale</parameterid>"
                      "<name>Scale</name><valuemin>0</valuemin><valuemax>1000</valuemax>"
                      f"<value>{round(SEQ_W / info['w'] * 100, 3)}</value></parameter></effect></filter>")
        src_track = f"<sourcetrack><mediatype>audio</mediatype><trackindex>{channel}</trackindex></sourcetrack>" \
            if kind == "audio" else ""
        return (f'<clipitem id="{self.uid("clip")}"><name>{escape(path.name)}</name>'
                f"<enabled>{'TRUE' if enabled else 'FALSE'}</enabled>"
                f"<duration>{int(info['dur'] * fps)}</duration>{rate(fps)}"
                f"<start>{s0}</start><end>{s1}</end><in>{i0}</in><out>{i1}</out>"
                f"{self.file_el(path, info)}{src_track}{motion}</clipitem>")


def session_block(sdir: Path) -> dict:
    m = json.loads((sdir / "markers.json").read_text())
    angles = json.loads((sdir / "angles.json").read_text()) if (sdir / "angles.json").exists() else []
    views = [p for p in (sdir / "game.mp4", sdir / "overlay.mp4", sdir / "camera.mp4") if p.exists()]
    ang = [(Path(a["file"]), a["offset_s"]) for a in angles if Path(a["file"]).exists()]
    infos = {p: probe(p) for p in views + [p for p, _ in ang]}
    # block time 0 = the earliest start of anything (other angles usually start first)
    shift = max([0.0] + [-off for _, off in ang])
    length = max([m["duration_s"] + shift] + [off + shift + infos[p]["dur"] for p, off in ang])
    return {"dir": sdir, "m": m, "views": views, "ang": ang, "infos": infos, "shift": shift, "len": length}


def combined_sequence(b: Builder, blocks: list[dict]) -> str:
    t, starts = 0.0, []
    for blk in blocks:
        starts.append(t)
        t += blk["len"] + GAP_S
    end = t - GAP_S
    tracks = {"v": [[], [], [], []], "a": [[]]}
    markers = []
    for n, (blk, base) in enumerate(zip(blocks, starts), 1):
        span = (base, base + blk["len"])
        z = base + blk["shift"]                       # sequence time of this take's camera t=0
        for i, p in enumerate(blk["views"]):
            tracks["v"][i].append(b.clip(p, blk["infos"][p], z, "video", True, end, block=span))
        for p, off in blk["ang"][:1]:
            tracks["v"][3].append(b.clip(p, blk["infos"][p], z + off, "video", True, end, block=span))
            for ch in range(1, blk["infos"][p]["channels"] + 1):
                tracks["a"][0].append(b.clip(p, blk["infos"][p], z + off, "audio", True, end, ch, block=span))
        markers.append((base, f"Take {n}", blk["dir"].name))
        r = 0
        for k in blk["m"]["markers"]:
            if k["kind"] == "round":
                r += 1
                markers.append((z + k["file_time_s"], f"Take {n} - Round {r}", k["time_of_day"]))
    vt = "".join(f"<track>{''.join(c)}</track>" for c in tracks["v"])
    at = "".join(f"<track>{''.join(c)}</track>" for c in tracks["a"])
    mk = "".join(f"<marker><name>{escape(nm)}</name><comment>{escape(cm)}</comment>"
                 f"<in>{round(tt * SEQ_FPS)}</in><out>-1</out></marker>" for tt, nm, cm in markers)
    fmt = (f"<format><samplecharacteristics>{rate(SEQ_FPS)}<width>{SEQ_W}</width><height>{SEQ_H}</height>"
           f"<pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance>"
           f"</samplecharacteristics></format>")
    return (f'<sequence id="{b.uid("seq")}"><name>Subway Surfers IRL - all takes</name>'
            f"<duration>{round(end * SEQ_FPS)}</duration>{rate(SEQ_FPS)}"
            f"<media><video>{fmt}{vt}</video><audio>{at}</audio></media>{mk}</sequence>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="*")
    ap.add_argument("-o", "--out", default=str(ROOT / "recordings" / "SubwaySurfers_edit.xml"))
    a = ap.parse_args()
    sessions = [Path(s) for s in a.sessions] if a.sessions else sorted((ROOT / "recordings").glob("*/"))
    sessions = [s.resolve() for s in sessions if (s / "markers.json").exists()]
    b = Builder()
    seqs = combined_sequence(b, [session_block(s) for s in sessions])
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
           f"<project><name>Subway Surfers IRL</name><children>{seqs}</children></project></xmeml>\n")
    Path(a.out).write_text(xml)
    print(f"wrote {a.out} ({len(sessions)} takes in one sequence)")


if __name__ == "__main__":
    main()
