"""MVR (My Virtual Rig) reader for the grandMA3 MCP server. Python standard library only.

An .mvr file is a zip containing GeneralSceneDescription.xml (units are millimetres, Z up, right handed) plus the
fixture (.gdtf) and 3D geometry files. This module extracts what position programming needs:

- every fixture with world position, orientation, FixtureID, GDTF spec/mode, layer
- scenic objects (SceneObject, Truss, Support, VideoScreen, FocusPoint ...) with world position and, when the zip
  carries a readable .glb / .3ds model, a world-space bounding box
- a stage estimate (deck bounds, centre, audience direction) with an honest confidence value
- an alignment between MVR coordinates and the grandMA3 patch coordinates (matched by FixtureID == FID)

Matrices follow the MVR convention: "{Ux,Uy,Uz}{Vx,Vy,Vz}{Wx,Wy,Wz}{Ox,Oy,Oz}" where U, V, W are the local axes
expressed in the parent frame and O is the origin (mm).
"""

from __future__ import annotations

import json
import math
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Tuple

Vec = Tuple[float, float, float]
Mat = Tuple[Vec, Vec, Vec]  # columns U, V, W of the rotation

IDENTITY: Mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
STAGE_WORDS = re.compile(r"(stage|deck|platform|riser|podium|dais|escenario|tarima|scenic floor|floor)", re.I)
AUDIENCE_WORDS = re.compile(r"(audience|public|crowd|foh|house|seat|tribune|publico|público|gradas)", re.I)
SCENIC_TAGS = {"SceneObject", "Truss", "Support", "VideoScreen", "Projector", "FocusPoint"}


# ---------------------------------------------------------------------------
# small linear algebra helpers
# ---------------------------------------------------------------------------

def _mat_vec(m: Mat, v: Vec) -> Vec:
    u, w, z = m
    return (u[0] * v[0] + w[0] * v[1] + z[0] * v[2],
            u[1] * v[0] + w[1] * v[1] + z[1] * v[2],
            u[2] * v[0] + w[2] * v[1] + z[2] * v[2])


def _mat_mul(a: Mat, b: Mat) -> Mat:
    return (_mat_vec(a, b[0]), _mat_vec(a, b[1]), _mat_vec(a, b[2]))


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def parse_matrix(text: Optional[str]) -> Tuple[Mat, Vec]:
    """Parse an MVR matrix string into (rotation columns, origin in mm)."""
    if not text:
        return IDENTITY, (0.0, 0.0, 0.0)
    rows = re.findall(r"\{([^}]*)\}", text)
    vecs: List[Vec] = []
    for row in rows[:4]:
        parts = [float(p) for p in re.split(r"[,\s]+", row.strip()) if p]
        if len(parts) != 3:
            return IDENTITY, (0.0, 0.0, 0.0)
        vecs.append((parts[0], parts[1], parts[2]))
    if len(vecs) != 4:
        return IDENTITY, (0.0, 0.0, 0.0)
    return (vecs[0], vecs[1], vecs[2]), vecs[3]


def euler_xyz_deg(m: Mat) -> Vec:
    """Rotation columns -> intrinsic X-Y-Z style Euler angles in degrees (for display only)."""
    r00, r10, r20 = m[0]
    r01, r11, r21 = m[1]
    r02, r12, r22 = m[2]
    sy = math.hypot(r00, r10)
    if sy > 1e-6:
        x = math.atan2(r21, r22)
        y = math.atan2(-r20, sy)
        z = math.atan2(r10, r00)
    else:
        x = math.atan2(-r12, r11)
        y = math.atan2(-r20, sy)
        z = 0.0
    return (math.degrees(x), math.degrees(y), math.degrees(z))


def _mm_to_m(v: Vec) -> Vec:
    return (v[0] / 1000.0, v[1] / 1000.0, v[2] / 1000.0)


# ---------------------------------------------------------------------------
# geometry bounding boxes (best effort)
# ---------------------------------------------------------------------------

def _glb_bbox(data: bytes) -> Optional[Tuple[Vec, Vec]]:
    if len(data) < 20 or data[:4] != b"glTF":
        return None
    json_len, chunk_type = struct.unpack_from("<II", data, 12)
    if chunk_type != 0x4E4F534A:
        return None
    doc = json.loads(data[20:20 + json_len].decode("utf-8", errors="replace"))
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            idx = prim.get("attributes", {}).get("POSITION")
            if idx is None:
                continue
            acc = doc["accessors"][idx]
            if "min" in acc and "max" in acc:
                for i in range(3):
                    lo[i] = min(lo[i], acc["min"][i])
                    hi[i] = max(hi[i], acc["max"][i])
    if lo[0] == math.inf:
        return None
    return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])


def _3ds_bbox(data: bytes) -> Optional[Tuple[Vec, Vec]]:
    lo = [math.inf] * 3
    hi = [-math.inf] * 3

    def walk(pos: int, end: int) -> None:
        while pos + 6 <= end:
            chunk_id, length = struct.unpack_from("<HI", data, pos)
            if length < 6:
                return
            body = pos + 6
            if chunk_id in (0x4D4D, 0x3D3D, 0x4000, 0x4100):
                if chunk_id == 0x4000:  # object: skip the zero terminated name
                    while body < end and data[body] != 0:
                        body += 1
                    body += 1
                walk(body, min(pos + length, end))
            elif chunk_id == 0x4110:
                count = struct.unpack_from("<H", data, body)[0]
                for i in range(count):
                    x, y, z = struct.unpack_from("<fff", data, body + 2 + 12 * i)
                    for k, v in enumerate((x, y, z)):
                        lo[k] = min(lo[k], v)
                        hi[k] = max(hi[k], v)
            pos += length

    try:
        walk(0, len(data))
    except struct.error:
        return None
    if lo[0] == math.inf:
        return None
    return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])


def _geometry_bbox_mm(archive: zipfile.ZipFile, name: str) -> Optional[Tuple[Vec, Vec]]:
    try:
        data = archive.read(name)
    except KeyError:
        return None
    lower = name.lower()
    box = _glb_bbox(data) if lower.endswith(".glb") else _3ds_bbox(data) if lower.endswith(".3ds") else None
    if not box:
        return None
    lo, hi = box
    extent = max(hi[i] - lo[i] for i in range(3))
    scale = 1000.0 if (lower.endswith(".glb") and extent < 500) else 1.0  # glb is metres, 3ds assumed mm
    return (lo[0] * scale, lo[1] * scale, lo[2] * scale), (hi[0] * scale, hi[1] * scale, hi[2] * scale)


def _world_bbox(lo: Vec, hi: Vec, rot: Mat, origin: Vec) -> Tuple[Vec, Vec]:
    corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    pts = [_add(_mat_vec(rot, c), origin) for c in corners]
    return (min(p[0] for p in pts), min(p[1] for p in pts), min(p[2] for p in pts)), \
           (max(p[0] for p in pts), max(p[1] for p in pts), max(p[2] for p in pts))


# ---------------------------------------------------------------------------
# scene parsing
# ---------------------------------------------------------------------------

def _tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1]


def _child(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in el:
        if _tag(c) == name:
            return c
    return None


def parse_mvr(path: str) -> Dict[str, Any]:
    """Read an .mvr file and return {'fixtures': [...], 'objects': [...], 'layers': [...], 'meta': {...}}."""
    with zipfile.ZipFile(path) as archive:
        xml_name = next((n for n in archive.namelist() if n.lower().endswith("generalscenedescription.xml")), None)
        if not xml_name:
            raise ValueError("not an MVR file: GeneralSceneDescription.xml is missing")
        root = ET.fromstring(archive.read(xml_name))
        scene: Dict[str, Any] = {
            "meta": {"path": path, "ver_major": root.get("verMajor"), "ver_minor": root.get("verMinor"), "units": "m (converted from mm)"},
            "layers": [], "fixtures": [], "objects": [], "warnings": [],
        }
        symdefs: Dict[str, str] = {}
        for sym in root.iter():
            if _tag(sym) == "Symdef":
                geo = next((g for g in sym.iter() if _tag(g) == "Geometry3D"), None)
                if geo is not None and geo.get("fileName"):
                    symdefs[sym.get("uuid", "")] = geo.get("fileName")

        layers = [el for el in root.iter() if _tag(el) == "Layer"]
        for layer_el in layers:
            lname = layer_el.get("name", "")
            scene["layers"].append(lname)
            lrot, lorigin = IDENTITY, (0.0, 0.0, 0.0)
            lm = _child(layer_el, "Matrix")
            if lm is not None:
                lrot, lorigin = parse_matrix(lm.text)
            cl = _child(layer_el, "ChildList")
            if cl is not None:
                for item in cl:
                    _visit_item(item, lrot, lorigin, lname, scene, symdefs, archive)
        if not scene["fixtures"]:
            scene["warnings"].append("no Fixture elements found")
        return scene


def _visit_item(el: ET.Element, rot: Mat, origin: Vec, layer: str, scene: Dict[str, Any],
                symdefs: Dict[str, str], archive: zipfile.ZipFile) -> None:
    kind = _tag(el)
    mel = _child(el, "Matrix")
    local_rot, local_origin = parse_matrix(mel.text if mel is not None else None)
    world_rot = _mat_mul(rot, local_rot)
    world_origin = _add(_mat_vec(rot, local_origin), origin)
    if kind == "GroupObject":
        cl = _child(el, "ChildList")
        if cl is not None:
            for item in cl:
                _visit_item(item, world_rot, world_origin, layer, scene, symdefs, archive)
        return
    if kind == "Fixture":
        def text(name: str) -> Optional[str]:
            c = _child(el, name)
            return (c.text or "").strip() if c is not None and c.text else None
        beam = _mat_vec(world_rot, (0.0, 0.0, -1.0))
        scene["fixtures"].append({
            "name": el.get("name", ""), "uuid": el.get("uuid", ""), "layer": layer,
            "fixture_id": text("FixtureID"), "unit_number": text("UnitNumber"),
            "gdtf_spec": text("GDTFSpec"), "gdtf_mode": text("GDTFMode"),
            "addresses": [a.text.strip() for a in el.iter() if _tag(a) == "Address" and a.text],
            "position_m": _mm_to_m(world_origin), "rotation_deg": euler_xyz_deg(world_rot), "home_beam_direction": beam,
        })
        return
    if kind in SCENIC_TAGS:
        files: List[str] = []
        for geo in el.iter():
            if _tag(geo) == "Geometry3D" and geo.get("fileName"):
                files.append(geo.get("fileName"))
            elif _tag(geo) == "Symbol" and geo.get("symdef") in symdefs:
                files.append(symdefs[geo.get("symdef")])
        boxes = []
        for fname in files:
            box = _geometry_bbox_mm(archive, fname)
            if box:
                boxes.append(_world_bbox(box[0], box[1], world_rot, world_origin))
        bbox = None
        if boxes:
            lo = tuple(min(b[0][i] for b in boxes) for i in range(3))
            hi = tuple(max(b[1][i] for b in boxes) for i in range(3))
            bbox = (_mm_to_m(lo), _mm_to_m(hi))  # type: ignore[arg-type]
        scene["objects"].append({"type": kind, "name": el.get("name", ""), "uuid": el.get("uuid", ""), "layer": layer,
                                 "position_m": _mm_to_m(world_origin), "rotation_deg": euler_xyz_deg(world_rot), "bbox_m": bbox})


# ---------------------------------------------------------------------------
# stage inference
# ---------------------------------------------------------------------------

def infer_stage(scene: Dict[str, Any], stage_names: Optional[List[str]] = None, audience: Optional[str] = None,
                deck_height_m: Optional[float] = None) -> Dict[str, Any]:
    """Estimate the stage frame from scenic objects. Always reports how it was decided and how sure it is.

    audience: optional "+x", "-x", "+y", "-y" in MVR coordinates (overrides the heuristic).
    """
    objects = scene["objects"]
    fixtures = scene["fixtures"]
    chosen: List[Dict[str, Any]] = []
    method = ""
    if stage_names:
        wanted = [n.lower() for n in stage_names]
        chosen = [o for o in objects if any(w in (o["name"] + " " + o["layer"]).lower() for w in wanted)]
        method = "named by user"
    if not chosen:
        chosen = [o for o in objects if STAGE_WORDS.search(o["name"] + " " + o["layer"]) and o["type"] != "FocusPoint"]
        method = "object/layer names matching stage words" if chosen else ""
    boxed = [o for o in chosen if o["bbox_m"]]
    notes: List[str] = []
    if boxed:
        lo = [min(o["bbox_m"][0][i] for o in boxed) for i in range(3)]
        hi = [max(o["bbox_m"][1][i] for o in boxed) for i in range(3)]
        confidence = 0.8 if method == "named by user" else 0.7
    elif chosen:
        xs = [o["position_m"][0] for o in chosen]
        ys = [o["position_m"][1] for o in chosen]
        lo = [min(xs) - 3, min(ys) - 3, min(o["position_m"][2] for o in chosen)]
        hi = [max(xs) + 3, max(ys) + 3, max(o["position_m"][2] for o in chosen)]
        confidence = 0.35
        notes.append("stage objects have no readable 3D geometry; bounds are positions +/- 3 m")
    else:
        focus = [o for o in objects if o["type"] == "FocusPoint"]
        pts = [o["position_m"] for o in focus] or [f["position_m"] for f in fixtures]
        if not pts:
            return {"found": False, "reason": "scene has no fixtures or scenic objects"}
        method = "focus points" if focus else "fixture footprint (no stage object found)"
        lo = [min(p[0] for p in pts), min(p[1] for p in pts), 0.0]
        hi = [max(p[0] for p in pts), max(p[1] for p in pts), 0.0]
        confidence = 0.25
        notes.append("no stage object found: ask the user which scenic object is the stage deck")
    if deck_height_m is not None:
        lo[2] = hi[2] = deck_height_m
    centre = ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, hi[2])
    audience_vec: Optional[Vec] = None
    audience_source = ""
    if audience:
        sign = -1.0 if audience.startswith("-") else 1.0
        axis = audience[-1].lower()
        audience_vec = (sign, 0.0, 0.0) if axis == "x" else (0.0, sign, 0.0)
        audience_source = "given by user"
    else:
        aud_objs = [o for o in objects if AUDIENCE_WORDS.search(o["name"] + " " + o["layer"])]
        if aud_objs:
            ax = sum(o["position_m"][0] for o in aud_objs) / len(aud_objs) - centre[0]
            ay = sum(o["position_m"][1] for o in aud_objs) / len(aud_objs) - centre[1]
            audience_source = "scenic objects named like audience/FOH"
        elif fixtures:
            # without a named audience, assume the rig faces the audience: the side with fewer fixtures
            ax = centre[0] - sum(f["position_m"][0] for f in fixtures) / len(fixtures)
            ay = centre[1] - sum(f["position_m"][1] for f in fixtures) / len(fixtures)
            audience_source = "guess: opposite the fixture centroid (low confidence)"
            confidence = min(confidence, 0.4)
        else:
            ax, ay = 0.0, -1.0
            audience_source = "default -Y"
        if abs(ax) >= abs(ay):
            audience_vec = (1.0 if ax >= 0 else -1.0, 0.0, 0.0)
        else:
            audience_vec = (0.0, 1.0 if ay >= 0 else -1.0, 0.0)
    return {
        "found": True, "method": method, "confidence": round(confidence, 2),
        "stage_objects": [o["name"] for o in chosen][:20],
        "bounds_min_m": tuple(round(v, 3) for v in lo), "bounds_max_m": tuple(round(v, 3) for v in hi),
        "centre_m": tuple(round(v, 3) for v in centre),
        "size_m": (round(hi[0] - lo[0], 3), round(hi[1] - lo[1], 3)),
        "deck_height_m": round(hi[2], 3),
        "audience_direction": audience_vec, "audience_source": audience_source,
        "notes": notes + (["confirm the audience direction with the user"] if "guess" in audience_source or "default" in audience_source else []),
    }


def stage_coordinates(point: Vec, stage: Dict[str, Any]) -> Dict[str, float]:
    """Express a world point in the stage frame: u = across (stage right positive when facing the audience's side),
    v = depth measured from the stage centre toward the audience, h = height above the deck."""
    cx, cy, cz = stage["centre_m"]
    ax, ay, _ = stage["audience_direction"]
    dx, dy = point[0] - cx, point[1] - cy
    v = dx * ax + dy * ay
    u = dx * (-ay) + dy * ax          # audience direction rotated by +90 degrees
    return {"u": round(u, 3), "v": round(v, 3), "h": round(point[2] - cz, 3)}


# ---------------------------------------------------------------------------
# aligning MVR fixtures with the MA3 patch
# ---------------------------------------------------------------------------

def align_with_ma3(scene: Dict[str, Any], ma3_fixtures: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Estimate the 2D rigid transform (rotation + translation, Z unchanged) that maps MVR coordinates to MA3 patch
    coordinates by matching MVR FixtureID with the MA3 FID. ma3_fixtures: [{'fid': int, 'x': m, 'y': m, 'z': m}]."""
    by_fid = {int(f["fid"]): f for f in ma3_fixtures if f.get("fid") is not None}
    pairs = []
    for f in scene["fixtures"]:
        try:
            fid = int(f["fixture_id"])
        except (TypeError, ValueError):
            continue
        if fid in by_fid:
            m = by_fid[fid]
            pairs.append(((f["position_m"][0], f["position_m"][1]), (m["x"], m["y"]), f["position_m"][2], m["z"]))
    if len(pairs) < 2:
        return {"matched": len(pairs), "ok": False, "reason": "need at least 2 fixtures whose MVR FixtureID equals the MA3 FID"}
    n = len(pairs)
    mx = sum(p[0][0] for p in pairs) / n
    my = sum(p[0][1] for p in pairs) / n
    tx = sum(p[1][0] for p in pairs) / n
    ty = sum(p[1][1] for p in pairs) / n
    sxx = sum((p[0][0] - mx) * (p[1][0] - tx) + (p[0][1] - my) * (p[1][1] - ty) for p in pairs)
    sxy = sum((p[0][0] - mx) * (p[1][1] - ty) - (p[0][1] - my) * (p[1][0] - tx) for p in pairs)
    angle = math.atan2(sxy, sxx)
    c, s = math.cos(angle), math.sin(angle)
    off_x = tx - (c * mx - s * my)
    off_y = ty - (s * mx + c * my)
    dz = sum(p[3] - p[2] for p in pairs) / n
    residuals = []
    for (px, py), (qx, qy), pz, qz in pairs:
        rx, ry = c * px - s * py + off_x, s * px + c * py + off_y
        residuals.append(math.hypot(rx - qx, ry - qy))
    return {"ok": True, "matched": n, "rotation_deg": round(math.degrees(angle), 3), "offset_m": (round(off_x, 3), round(off_y, 3), round(dz, 3)),
            "mean_residual_m": round(sum(residuals) / n, 4), "max_residual_m": round(max(residuals), 4)}


def apply_alignment(point: Vec, alignment: Dict[str, Any]) -> Vec:
    a = math.radians(alignment["rotation_deg"])
    c, s = math.cos(a), math.sin(a)
    ox, oy, oz = alignment["offset_m"]
    return (c * point[0] - s * point[1] + ox, s * point[0] + c * point[1] + oy, point[2] + oz)


def summarize(scene: Dict[str, Any]) -> Dict[str, Any]:
    types: Dict[str, int] = {}
    for f in scene["fixtures"]:
        key = f.get("gdtf_spec") or f.get("name") or "?"
        types[key] = types.get(key, 0) + 1
    obj_types: Dict[str, int] = {}
    for o in scene["objects"]:
        obj_types[o["type"]] = obj_types.get(o["type"], 0) + 1
    return {"file": scene["meta"]["path"], "mvr_version": "%s.%s" % (scene["meta"]["ver_major"], scene["meta"]["ver_minor"]),
            "layers": scene["layers"], "fixtures": len(scene["fixtures"]), "fixture_types": types,
            "scenic_objects": len(scene["objects"]), "scenic_types": obj_types,
            "objects_with_geometry": sum(1 for o in scene["objects"] if o["bbox_m"]), "warnings": scene["warnings"]}
