import json
import math
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import companion_server
import mvr
import patterns


def make_glb(lo, hi):
    doc = {"asset": {"version": "2.0"}, "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "accessors": [{"min": list(lo), "max": list(hi), "count": 8, "componentType": 5126, "type": "VEC3"}]}
    payload = json.dumps(doc).encode()
    payload += b" " * (-len(payload) % 4)
    return b"glTF" + struct.pack("<II", 2, 12 + 8 + len(payload)) + struct.pack("<II", len(payload), 0x4E4F534A) + payload


def make_mvr(path, with_audience=True, group_offset_mm=(1000, 0, 0)):
    gx, gy, gz = group_offset_mm
    audience = ('<SceneObject name="Audience Block" uuid="a1"><Matrix>{1,0,0}{0,1,0}{0,0,1}{0,-12000,0}</Matrix></SceneObject>'
                if with_audience else "")
    xml = f'''<?xml version="1.0"?>
<GeneralSceneDescription verMajor="1" verMinor="6"><Scene><Layers>
 <Layer name="Scenery" uuid="L1"><ChildList>
  <SceneObject name="Main Stage Deck" uuid="s1"><Matrix>{{1,0,0}}{{0,1,0}}{{0,0,1}}{{0,0,1200}}</Matrix>
    <Geometries><Geometry3D fileName="deck.glb"/></Geometries></SceneObject>
  {audience}
 </ChildList></Layer>
 <Layer name="Lighting" uuid="L2"><ChildList>
  <GroupObject name="Truss group" uuid="g1"><Matrix>{{1,0,0}}{{0,1,0}}{{0,0,1}}{{{gx},{gy},{gz}}}</Matrix><ChildList>
   <Fixture name="Spot A" uuid="f1"><Matrix>{{1,0,0}}{{0,1,0}}{{0,0,1}}{{-2000,3000,9000}}</Matrix>
     <GDTFSpec>Acme@LEO.gdtf</GDTFSpec><GDTFMode>25 DMX</GDTFMode><FixtureID>101</FixtureID><Addresses><Address break="0">1</Address></Addresses></Fixture>
   <Fixture name="Spot B" uuid="f2"><Matrix>{{1,0,0}}{{0,-1,0}}{{0,0,-1}}{{2000,3000,9000}}</Matrix>
     <GDTFSpec>Acme@LEO.gdtf</GDTFSpec><FixtureID>102</FixtureID></Fixture>
   <Fixture name="Spot C" uuid="f3"><Matrix>{{1,0,0}}{{0,1,0}}{{0,0,1}}{{0,-4000,9000}}</Matrix><FixtureID>103</FixtureID></Fixture>
  </ChildList></GroupObject>
 </ChildList></Layer>
</Layers></Scene></GeneralSceneDescription>'''
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("GeneralSceneDescription.xml", xml)
        z.writestr("deck.glb", make_glb((-6, -4, 0), (6, 4, 0.2)))   # metres: 12 x 8 deck, 0.2 thick


class MvrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rig.mvr")
        make_mvr(self.path)
        self.scene = mvr.parse_mvr(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixtures_get_world_positions_through_group_matrix(self):
        fx = {f["fixture_id"]: f for f in self.scene["fixtures"]}
        self.assertEqual(len(fx), 3)
        self.assertAlmostEqual(fx["101"]["position_m"][0], -1.0)   # -2000 mm + group 1000 mm
        self.assertAlmostEqual(fx["101"]["position_m"][2], 9.0)
        self.assertEqual(fx["101"]["gdtf_mode"], "25 DMX")
        self.assertEqual(fx["101"]["addresses"], ["1"])

    def test_home_beam_direction_follows_rotation(self):
        fx = {f["fixture_id"]: f for f in self.scene["fixtures"]}
        self.assertAlmostEqual(fx["101"]["home_beam_direction"][2], -1.0)   # points down
        self.assertAlmostEqual(fx["102"]["home_beam_direction"][2], 1.0)    # flipped 180 deg about X points up

    def test_glb_geometry_gives_world_bbox(self):
        deck = next(o for o in self.scene["objects"] if o["name"].startswith("Main Stage"))
        lo, hi = deck["bbox_m"]
        self.assertAlmostEqual(hi[0] - lo[0], 12.0, places=2)
        self.assertAlmostEqual(lo[2], 1.2, places=2)

    def test_stage_is_found_by_name_with_audience_object(self):
        stage = mvr.infer_stage(self.scene)
        self.assertTrue(stage["found"])
        self.assertEqual(stage["size_m"], (12.0, 8.0))
        self.assertEqual(stage["audience_direction"], (0.0, -1.0, 0.0))
        self.assertGreaterEqual(stage["confidence"], 0.7)
        self.assertAlmostEqual(stage["deck_height_m"], 1.4, places=2)

    def test_audience_guess_is_flagged_for_confirmation(self):
        path = os.path.join(self.tmp.name, "noaud.mvr")
        make_mvr(path, with_audience=False)
        stage = mvr.infer_stage(mvr.parse_mvr(path))
        self.assertIn("guess", stage["audience_source"])
        self.assertTrue(any("confirm" in n for n in stage["notes"]))

    def test_user_audience_direction_wins(self):
        stage = mvr.infer_stage(self.scene, audience="+x")
        self.assertEqual(stage["audience_direction"], (1.0, 0.0, 0.0))
        self.assertEqual(stage["audience_source"], "given by user")

    def test_stage_coordinates(self):
        stage = mvr.infer_stage(self.scene)
        pos = mvr.stage_coordinates((3.0, -5.0, 10.0), stage)
        self.assertAlmostEqual(pos["v"], 5.0)        # 5 m toward the audience (-Y)
        self.assertAlmostEqual(pos["h"], 10.0 - stage["centre_m"][2], places=2)

    def test_not_an_mvr_is_rejected(self):
        bad = os.path.join(self.tmp.name, "bad.mvr")
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("readme.txt", "hi")
        with self.assertRaises(ValueError):
            mvr.parse_mvr(bad)

    def test_alignment_recovers_rotation_and_offset(self):
        angle = math.radians(90)
        ma3 = []
        for f in self.scene["fixtures"]:
            x, y, z = f["position_m"]
            ma3.append({"fid": int(f["fixture_id"]), "x": math.cos(angle) * x - math.sin(angle) * y + 5.0,
                        "y": math.sin(angle) * x + math.cos(angle) * y - 2.0, "z": z + 0.5})
        result = mvr.align_with_ma3(self.scene, ma3)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["rotation_deg"], 90.0, places=2)
        self.assertLess(result["max_residual_m"], 1e-3)
        moved = mvr.apply_alignment(self.scene["fixtures"][0]["position_m"], result)
        self.assertAlmostEqual(moved[0], ma3[0]["x"], places=3)

    def test_alignment_needs_matches(self):
        result = mvr.align_with_ma3(self.scene, [{"fid": 999, "x": 0, "y": 0, "z": 0}])
        self.assertFalse(result["ok"])


class PatternTests(unittest.TestCase):
    def fixtures(self):
        out = {}
        fid = 100
        for gy in range(3):                               # grid row 0 = front
            for col in range(5):
                fid += 1
                out[fid] = {"x": -7.0 + 3.5 * col, "y": -0.15 + 3.0 * gy, "z": 10.2, "rotz": 0.0, "type": "LEO", "group": 27, "gy": gy, "gx": 10 + col}
        return out

    def test_aim_and_landing_round_trip(self):
        fx = {"x": -7.0, "y": 5.9, "z": 10.2, "rotz": 0.0}
        pan, tilt = patterns.aim_at(fx, 0.0, 2.0)
        land = patterns.landing(fx, pan, tilt)
        self.assertAlmostEqual(land[0], 0.0, places=6)
        self.assertAlmostEqual(land[1], 2.0, places=6)
        self.assertLess(pan, -90.0)                       # verified convention: audience side, mirrored pan

    def test_straight_at_audience_is_minus_180_and_zero_for_rotz_180(self):
        pan, _ = patterns.aim_at({"x": 0, "y": 5, "z": 10, "rotz": 0.0}, 0.0, -5.0)
        self.assertAlmostEqual(pan, -180.0)
        pan2, _ = patterns.aim_at({"x": 0, "y": 5, "z": 10, "rotz": 180.0}, 0.0, -5.0)
        self.assertAlmostEqual(pan2, 0.0)

    def test_tilt_90_or_more_never_lands(self):
        self.assertIsNone(patterns.landing({"x": 0, "y": 0, "z": 10, "rotz": 0.0}, -180.0, 95.0))

    def test_parse_preset_xml(self):
        xml = ('<Preset Name="Mid Straight"><PresetData><Phaser IDType="0" ID="101" Attribute="Pan"><Step Function="Pan" AbsolutePhys="-180.000"/></Phaser>'
               '<Phaser IDType="0" ID="101" Attribute="Tilt"><Step Function="Tilt" AbsolutePhys="34.550"/></Phaser></PresetData></Preset>')
        self.assertEqual(patterns.parse_preset_xml(xml), {101: {"Pan": -180.0, "Tilt": 34.55}})
        self.assertEqual(patterns.preset_name_from_xml(xml), "Mid Straight")

    def make_preset(self, fixtures, slope, depths):
        values = {}
        for fid, fx in fixtures.items():
            lx = slope * fx["x"]
            pan, tilt = patterns.aim_at(fx, lx, -depths[fx["gy"]])    # audience is -Y: v = depth
            values[fid] = {"Pan": pan, "Tilt": tilt}
        return {"name": "Mid Fan In", "values": values}

    def test_build_learns_rows_and_lateral_rule(self):
        fx = self.fixtures()
        preset = self.make_preset(fx, 0.5, {0: 12.0, 1: 8.0, 2: 4.0})
        built = patterns.build_patterns({8: preset}, fx)
        pat = built["patterns"][0]
        self.assertEqual(pat["group"], 27)
        self.assertAlmostEqual(pat["lateral"]["landing_u_slope"], 0.5, places=2)
        depths = {r["grid_row"]: r["landing_v_m"] for r in pat["rows"]}
        # landing v is measured from the stage centre (y=2) toward the audience (-Y)
        self.assertAlmostEqual(depths[0], 12.0 + 2.0, places=2)
        self.assertAlmostEqual(depths[2], 4.0 + 2.0, places=2)

    def test_generate_reproduces_the_learned_preset(self):
        fx = self.fixtures()
        preset = self.make_preset(fx, 1.5, {0: 12.0, 1: 8.0, 2: 4.0})
        pat = patterns.build_patterns({11: preset}, fx)["patterns"][0]
        made = patterns.generate(pat, fx)
        for fid in fx:
            self.assertAlmostEqual(made[fid]["Pan"], preset["values"][fid]["Pan"], delta=0.05)
            self.assertAlmostEqual(made[fid]["Tilt"], preset["values"][fid]["Tilt"], delta=0.05)

    def test_generate_adapts_to_a_group_with_a_different_row_count(self):
        fx = self.fixtures()
        pat = patterns.build_patterns({8: self.make_preset(fx, 1.0, {0: 12.0, 1: 8.0, 2: 4.0})}, fx)["patterns"][0]
        two_rows = {fid: dict(f, gy=f["gy"] // 2 * 1) for fid, f in fx.items() if f["gy"] != 1}
        made = patterns.generate(pat, two_rows)
        front = [made[f]["Tilt"] for f, d in two_rows.items() if d["gy"] == 0][0]
        back = [made[f]["Tilt"] for f, d in two_rows.items() if d["gy"] == 1][0]
        self.assertGreater(front, back)       # the front row still throws farther than the back row


class ServerToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (companion_server.PATTERNS_PATH, companion_server.STAGE_PATH, companion_server.SNAPSHOT_DIR,
                      companion_server.scan_ma3, companion_server.plugin_request)
        companion_server.PATTERNS_PATH = os.path.join(self.tmp.name, "patterns.json")
        companion_server.STAGE_PATH = os.path.join(self.tmp.name, "stage.json")
        companion_server.SNAPSHOT_DIR = os.path.join(self.tmp.name, "snaps")
        companion_server.STATE.update({"scene": None, "mvr_stage": None, "alignment": None})

    def tearDown(self):
        (companion_server.PATTERNS_PATH, companion_server.STAGE_PATH, companion_server.SNAPSHOT_DIR,
         companion_server.scan_ma3, companion_server.plugin_request) = self.saved
        self.tmp.cleanup()

    def call(self, name, arguments):
        response = companion_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        result = response["result"]
        return result.get("isError", False), json.loads(result["content"][0]["text"]) if not result.get("isError") else result["content"][0]["text"]

    def test_tools_listed_and_instructions_ask_for_mvr(self):
        names = {t["name"] for t in companion_server.TOOLS}
        for n in ("ma3_mvr_load", "ma3_mvr_stage", "ma3_mvr_fixtures", "ma3_mvr_align", "ma3_patterns_extract", "ma3_patterns", "ma3_patterns_generate"):
            self.assertIn(n, names)
        self.assertIn("ask the user for an MVR", companion_server.instructions())

    def test_mvr_flow_load_stage_fixtures(self):
        path = os.path.join(self.tmp.name, "rig.mvr")
        make_mvr(path)
        err, summary = self.call("ma3_mvr_load", {"path": path})
        self.assertFalse(err)
        self.assertEqual(summary["fixtures"], 3)
        err, stage = self.call("ma3_mvr_stage", {})
        self.assertTrue(stage["found"])
        self.assertTrue(os.path.exists(companion_server.STAGE_PATH))
        err, listing = self.call("ma3_mvr_fixtures", {"limit": 2})
        self.assertEqual(len(listing["items"]), 2)
        self.assertIn("stage_frame", listing["items"][0])

    def test_stage_requires_a_loaded_mvr(self):
        err, text = self.call("ma3_mvr_stage", {})
        self.assertTrue(err)
        self.assertIn("ma3_mvr_load", text)

    def test_align_uses_the_patch(self):
        path = os.path.join(self.tmp.name, "rig.mvr")
        make_mvr(path)
        self.call("ma3_mvr_load", {"path": path})

        def fake(request):
            if request["op"] == "fixtures":
                items = [{"fields": {"FID": str(101 + i), "POSX": str(p[0]), "POSY": str(p[1]), "POSZ": str(p[2])}}
                         for i, p in enumerate([(-1.0, 3.0, 9.0), (3.0, 3.0, 9.0), (1.0, -4.0, 9.0)])]
                return {"ok": True, "items": items, "truncated": False}
            raise AssertionError(request)
        companion_server.plugin_request = fake
        err, result = self.call("ma3_mvr_align", {})
        self.assertTrue(result["ok"])
        self.assertLess(result["max_residual_m"], 1e-3)

    def test_extract_then_generate(self):
        fx = PatternTests().fixtures()
        values = PatternTests().make_preset(fx, 0.5, {0: 12.0, 1: 8.0, 2: 4.0})["values"]
        directory = os.path.join(companion_server.SNAPSHOT_DIR, "patterns")

        def fake_scan(groups, preset_numbers, dirname):
            if preset_numbers:
                os.makedirs(dirname, exist_ok=True)
                body = "".join(f'<Phaser IDType="0" ID="{fid}" Attribute="{a}"><Step Function="{a}" AbsolutePhys="{v[a]:.3f}"/></Phaser>'
                               for fid, v in values.items() for a in ("Pan", "Tilt"))
                with open(os.path.join(dirname, "pos_8.xml"), "w", encoding="utf-8") as h:
                    h.write('<Preset Name="Mid Fan In"><PresetData>' + body + "</PresetData></Preset>")
            return {"fixtures": [dict(f, fid=fid) for fid, f in fx.items()], "exported": [{"preset": 8, "ok": True, "name": "Mid Fan In"}] if preset_numbers else []}
        companion_server.scan_ma3 = fake_scan
        err, out = self.call("ma3_patterns_extract", {"presets": [8], "groups": [27]})
        self.assertFalse(err)
        self.assertEqual(out["stored"], 1)
        self.assertAlmostEqual(out["summary"][0]["lateral_slope"], 0.5, places=2)
        err, read = self.call("ma3_patterns", {"preset": 8})
        self.assertEqual(read["count"], 1)
        err, gen = self.call("ma3_patterns_generate", {"preset": 8, "source_group": 27, "target_group": 27})
        first = str(min(fx))
        self.assertAlmostEqual(gen["values"][first]["Tilt"], values[min(fx)]["Tilt"], delta=0.1)

    def test_patterns_before_extract_explains_what_to_do(self):
        err, text = self.call("ma3_patterns", {})
        self.assertTrue(err)
        self.assertIn("ma3_patterns_extract", text)


if __name__ == "__main__":
    unittest.main()
