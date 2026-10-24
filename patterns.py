"""Position patterns for the grandMA3 MCP server. Python standard library only.

A pattern describes WHERE a position preset points, relative to the stage, instead of raw pan/tilt numbers. It is
learned from presets the user programmed by hand: each preset is exported to XML (ma3_patterns_extract), converted to
floor landing points with the verified conventions of this show, and summarised per fixture group and per selection
grid row. generate() runs the same relation backwards so new fixtures can get matching pan/tilt from their position.

Conventions (verified on onPC 2.5.1.0 with the user watching the 3D view)
- MA3 patch coordinates: X right, Y toward the back wall, Z up, metres. The audience is toward -Y by default.
- A hanging fixture at rotation 0 points straight down at Pan 0 / Tilt 0 and Tilt measures degrees from straight down.
- bearing (degrees, from +Y toward +X) = 180 + delta, and pan = (rotz - 180) - delta, so "straight at the audience" is
  pan -180 for rotz 0 and pan 0 for rotz 180.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_STAGE = {
    "centre_m": (0.0, 2.0, 0.0),
    "audience_direction": (0.0, -1.0, 0.0),
    "size_m": None,
    "deck_height_m": 0.0,
    "source": "default estimate from the patch (not confirmed)",
}


def _norm(a: float) -> float:
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def parse_preset_xml(text: str) -> Dict[int, Dict[str, float]]:
    """{fixture id: {'Pan': deg, 'Tilt': deg}} from a grandMA3 exported position preset."""
    out: Dict[int, Dict[str, float]] = {}
    pattern = r'<Phaser IDType="0" ID="(\d+)" Attribute="(\w+)"[^>]*>\s*<Step Function="\w+" AbsolutePhys="([-\d.]+)"'
    for m in re.finditer(pattern, text):
        out.setdefault(int(m.group(1)), {})[m.group(2)] = float(m.group(3))
    return out


def preset_name_from_xml(text: str) -> Optional[str]:
    m = re.search(r'<Preset Name="([^"]*)"', text)
    return m.group(1) if m else None


def landing(fx: Dict[str, Any], pan: float, tilt: float, deck_h: float = 0.0) -> Optional[Tuple[float, float]]:
    """Where the beam centre hits the deck plane, in MA3 patch coordinates, or None if it never does (tilt >= 90)."""
    if tilt >= 89.5:
        return None
    delta = (fx.get("rotz", 0.0) - 180.0) - pan
    bearing = math.radians(180.0 + delta)
    throw = max(fx["z"] - deck_h, 0.0) * math.tan(math.radians(tilt))
    return fx["x"] + throw * math.sin(bearing), fx["y"] + throw * math.cos(bearing)


def aim_at(fx: Dict[str, Any], lx: float, ly: float, deck_h: float = 0.0) -> Tuple[float, float]:
    """Pan and Tilt (degrees) that put the beam centre on the deck point (lx, ly)."""
    dx, dy = lx - fx["x"], ly - fx["y"]
    horizontal = math.hypot(dx, dy)
    tilt = math.degrees(math.atan2(horizontal, max(fx["z"] - deck_h, 1e-6)))
    bearing = math.degrees(math.atan2(dx, dy)) if horizontal > 1e-9 else 180.0
    delta = _norm(bearing - 180.0)
    return (fx.get("rotz", 0.0) - 180.0) - delta, tilt


def stage_frame(point: Tuple[float, float], stage: Dict[str, Any]) -> Tuple[float, float]:
    """(u, v): u across the stage, v from the stage centre toward the audience."""
    cx, cy, _ = stage["centre_m"]
    ax, ay, _ = stage["audience_direction"]
    dx, dy = point[0] - cx, point[1] - cy
    return dx * (-ay) + dy * ax, dx * ax + dy * ay


def from_stage_frame(u: float, v: float, stage: Dict[str, Any]) -> Tuple[float, float]:
    cx, cy, _ = stage["centre_m"]
    ax, ay, _ = stage["audience_direction"]
    return cx + u * (-ay) + v * ax, cy + u * ax + v * ay


def _fit_line(xs: List[float], ys: List[float]) -> Tuple[float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var < 1e-9:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    return slope, my - slope * mx


def build_patterns(presets: Dict[int, Dict[str, Any]], fixtures: Dict[int, Dict[str, Any]], stage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """presets: {number: {'name': str, 'values': {fid: {'Pan':..,'Tilt':..}}}}
    fixtures: {fid: {'x','y','z','rotz','type','group','gy','rows'}}  (gy = selection grid row, 0 = front)
    Returns {'stage': ..., 'patterns': [...]} where each pattern is one preset x fixture group x row."""
    stage = stage or DEFAULT_STAGE
    deck = stage.get("deck_height_m", 0.0) or 0.0
    patterns: List[Dict[str, Any]] = []
    for number, preset in sorted(presets.items()):
        by_group: Dict[int, List[Tuple[int, Dict[str, Any], Dict[str, float]]]] = {}
        for fid, vals in preset["values"].items():
            fx = fixtures.get(fid)
            if fx and "Tilt" in vals:
                by_group.setdefault(fx["group"], []).append((fid, fx, vals))
        for group, items in sorted(by_group.items()):
            rows: Dict[int, List[Tuple[int, Dict[str, Any], Dict[str, float]]]] = {}
            for entry in items:
                rows.setdefault(entry[1]["gy"], []).append(entry)
            row_count = max(1, max(rows) - min(rows) + 1)
            per_row = []
            fit_x: List[float] = []
            fit_y: List[float] = []
            for gy, entries in sorted(rows.items()):
                lands = []
                for fid, fx, vals in entries:
                    pt = landing(fx, vals.get("Pan", fx.get("rotz", 0.0) - 180.0), vals["Tilt"], deck)
                    if pt:
                        u_fix, _ = stage_frame((fx["x"], fx["y"]), stage)
                        u_land, v_land = stage_frame(pt, stage)
                        lands.append((u_fix, u_land, v_land))
                        fit_x.append(u_fix)
                        fit_y.append(u_land)
                tilts = [e[2]["Tilt"] for e in entries]
                per_row.append({
                    "grid_row": gy, "row_fraction": 0.5 if row_count == 1 else round((gy - min(rows)) / (row_count - 1), 4),
                    "fixtures": len(entries), "tilt_mean_deg": round(sum(tilts) / len(tilts), 3),
                    "landing_v_m": round(sum(l[2] for l in lands) / len(lands), 3) if lands else None,
                    "reaches_deck": bool(lands),
                })
            slope, intercept = _fit_line(fit_x, fit_y)
            patterns.append({
                "preset": number, "name": preset.get("name"), "group": group,
                "fixture_type": items[0][1].get("type"), "fixtures": len(items), "rows": per_row,
                "lateral": {"landing_u_slope": round(slope, 4), "landing_u_offset_m": round(intercept, 3),
                            "meaning": "landing across-stage = slope * fixture across-stage + offset (1 = parallel, 0 = converge, >1 fan out, <0 cross)"},
            })
    return {"version": 1, "stage": stage, "patterns": patterns}


def generate(pattern: Dict[str, Any], fixtures: Dict[int, Dict[str, Any]], stage: Optional[Dict[str, Any]] = None) -> Dict[int, Dict[str, float]]:
    """Pan/Tilt for the given fixtures by applying a learned pattern. Rows are interpolated by row fraction, so a group
    with a different number of rows still gets a consistent look. Fixtures whose row never reached the deck use the
    stored mean tilt and pan straight out."""
    stage = stage or DEFAULT_STAGE
    deck = stage.get("deck_height_m", 0.0) or 0.0
    rows = [r for r in pattern["rows"] if r["landing_v_m"] is not None]
    lat = pattern["lateral"]
    gys = [f["gy"] for f in fixtures.values()]
    lo, hi = (min(gys), max(gys)) if gys else (0, 0)
    out: Dict[int, Dict[str, float]] = {}
    for fid, fx in fixtures.items():
        s = 0.5 if hi == lo else (fx["gy"] - lo) / (hi - lo)
        if rows:
            ordered = sorted(rows, key=lambda r: r["row_fraction"])
            v = ordered[0]["landing_v_m"]
            for a, b in zip(ordered, ordered[1:]):
                if s >= a["row_fraction"]:
                    span = (b["row_fraction"] - a["row_fraction"]) or 1.0
                    v = a["landing_v_m"] + (b["landing_v_m"] - a["landing_v_m"]) * min(1.0, (s - a["row_fraction"]) / span)
            if s >= ordered[-1]["row_fraction"]:
                v = ordered[-1]["landing_v_m"]
            u_fix, _ = stage_frame((fx["x"], fx["y"]), stage)
            u_land = lat["landing_u_slope"] * u_fix + lat["landing_u_offset_m"]
            lx, ly = from_stage_frame(u_land, v, stage)
            pan, tilt = aim_at(fx, lx, ly, deck)
            out[fid] = {"Pan": round(pan, 2), "Tilt": round(tilt, 2)}
        else:
            tilt = pattern["rows"][0]["tilt_mean_deg"] if pattern["rows"] else 0.0
            out[fid] = {"Pan": round(fx.get("rotz", 0.0) - 180.0, 2), "Tilt": round(tilt, 2)}
    return out


def save(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, ensure_ascii=False)


def load(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None
