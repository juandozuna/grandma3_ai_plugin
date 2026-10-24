#!/usr/bin/env python3
"""MCP stdio server for the grandMA3 onPC showfile bridge.

The AI host launches this process over stdio. The process exposes MCP tools and
forwards calls to the Lua plugin listening on loopback after it is run in the
showfile. The AI host owns the conversation and provider credentials.

Every change the AI makes (ma3_execute with confirm, ma3_lua, workflow runs) is recorded in a
journal so ma3_rollback can ask the plugin to undo it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from typing import Any, Dict, List, Optional

import mvr
import patterns
import noconfirm

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_HOST = os.environ.get("GRANDMA3_AI_PLUGIN_HOST", "127.0.0.1")
PLUGIN_PORT = int(os.environ.get("GRANDMA3_AI_PLUGIN_PORT", "17844"))
WORKFLOWS: Dict[str, Dict[str, Any]] = {}
NOTES_PATH = os.environ.get("GRANDMA3_AI_NOTES_PATH", os.path.join(HERE, "console_notes.json"))
JOURNAL_PATH = os.environ.get("GRANDMA3_AI_JOURNAL_PATH", os.path.join(HERE, "journal.json"))
SNAPSHOT_DIR = os.environ.get("GRANDMA3_AI_SNAPSHOT_DIR", os.path.join(HERE, "snapshots"))
API_INDEX_PATH = os.environ.get("GRANDMA3_AI_API_INDEX", os.path.join(HERE, "api_index.json"))
MAX_REPLY_BYTES = 4 * 1024 * 1024
PATTERNS_PATH = os.environ.get("GRANDMA3_AI_PATTERNS", os.path.join(HERE, "position_patterns.json"))
STAGE_PATH = os.environ.get("GRANDMA3_AI_STAGE", os.path.join(HERE, "stage.json"))
STATE: Dict[str, Any] = {"scene": None, "mvr_stage": None, "alignment": None}

# Console behavior the AI should rely on instead of asking the user or probing first.
BASE_KNOWLEDGE = [
    "The grandMA3 command line acts on the currently selected sequence when no Sequence is named. Store/Delete/Label/Assign Cue commands without 'Sequence N' target it, so 'Store Cue 1 Thru 5' creates five cues in the selected sequence.",
    "ma3_context reports the fixture selection (with selection-grid x/y/z) and the selected sequence when the console exposes it. An empty selection or unknown sequence does NOT mean the sequence is missing: do not ask which sequence or select one first. Just send the store commands.",
    "When the user says 'selected', 'current' or 'active' sequence/cue/group/preset, issue the command without a target and let the console resolve it. Only name a target if the user gave one.",
    "Cue creation: 'Store Cue 1 Thru 5' (empty cues need programmer content; add '/merge' or 'Store Cue N /overwrite' as needed). Use 'Store Cue 1 Thru 5 /noconfirm' to avoid popups when appropriate.",
    "Always preview ma3_execute with confirm=false, then execute with confirm=true within the user's authorized scope. An explicit request to implement a reviewed plan is approval for its actions; do not ask again for each command. Read the result detail, and if a command fails, record what worked with ma3_notes.",
    "User preference: execute grandMA3 commands without interactive console confirmation. Append /noconfirm to every command that supports it, including Store, Delete, Assign, Label, Copy, and SaveShow; apply it to each command in a batch and to cue or macro commands. Resolve merge/overwrite choices from the authorized task and inspected show before executing so no console dialog is needed. /noconfirm suppresses console popups; ma3_execute confirm=true executes an authorized action and is a separate mechanism.",
    "SEEING THE SHOW: ma3_inspect (one object: class, all properties, children), ma3_list (ObjectList patterns or children of a base, optionally recursive by class), ma3_fixtures (selection | all | group:N with properties and selection-grid x/y/z), ma3_sequences, ma3_presets, ma3_api (live Lua API search: GetApiDescriptor/GetObjApiDescriptor plus Enums). Inspect first, then act. Do not guess property names: read them with ma3_inspect on a real object.",
    "ma3_lua runs any Lua in the plugin with the full console API. Everything runs inside one Oops group, UndoDisable is blocked, Set calls record the old value, and Create/Append/Insert/Delete/Remove/Copy get the undo handle injected. Cmd/CmdIndirect/CmdIndirectWait inside the snippet default to the same undo handle. Each call is journaled; undo it with ma3_rollback. Pass touches=[addresses] to snapshot objects (Export) before a risky edit. Snippets mentioning Export/Save/SetVar/Keyboard/Mouse/RemoteCommand etc. are flagged as not fully undoable by Oops.",
    "Lua snippets get a helper table `ma3`: ma3.h(target) resolves an address or handle id (#...), ma3.props(h, fields), ma3.children(h), ma3.find(ObjectList pattern), ma3.walk(root, class), ma3.fixtures{scope=...}, ma3.aim(from, to) for direction math, ma3.split(items, keyfn, parts) to split by x/y/z, ma3.cmd(command). Return values are JSON-encoded; handles become {_h, class, addr, name}.",
    "SelectionFirst/SelectionNext return selection-GRID indexes (integers), not world positions. World position/rotation are properties of the Patch fixture objects, confirmed on 2.5.1.0 as POSX/POSY/POSZ and ROTX/ROTY/ROTZ (property names are UPPERCASE; Fixture objects live under Stage; handle ids look like '#00015DE4'). Read them with ma3_fixtures (scope=all, fields=[NAME, FID, POSX, POSY, POSZ, ROTX, ROTY, ROTZ]).",
    "Groups from 3D positions: read ma3_fixtures scope=all, split by position (ma3.split on x, y or z), select the ids with 'Fixture 101 Thru 110' style commands, then 'Store Group N /noconfirm' and 'Label Group N \"name\"'. Use MAtricks / Grid commands so the stored selection grid follows real positions.",
    "Position presets from a selection: parse the preset name for intent (front, back, centre, crowd, down...), compute a target point, derive per-fixture aim with ma3.aim, apply Pan/Tilt, then 'Store Preset 2.N /noconfirm' (Position presets live in preset pool 2). Verify the Pan/Tilt axis mapping on one fixture before applying it to the rig.",
    "Phasers: a phaser recipe is a recipe with two or more steps built from a selection plus values per step. Read existing phasers with ma3_presets (GetPresetData) and ma3_inspect on the recipe objects; programmatic helpers are GetProgPhaser/SetProgPhaser/GeneratePhasersForChannels. Check the Recipe object schema with ma3_inspect before creating one.",
    "Sequence CRUD: ma3_sequences lists/reads (cues are children). Create with 'Store Sequence N /noconfirm', rename with 'Label Sequence N \"name\"', delete with 'Delete Sequence N /noconfirm'. For the selected sequence use ma3_sequences with no target to see it, and commands without a target to edit it.",
    "STANDING RULE for Store/Delete/Assign/Label on big or bulk jobs (presets, groups, sequences): always append /noconfirm, and for preset stores use '/merge' by default (e.g. 'Store Preset 2.1 /merge /noconfirm') so existing content for other fixtures is kept. Decide merge vs overwrite vs remove from what the show already holds (read the target first); if the context does not settle it, ask the user which of merge, overwrite or remove they want before running.",
    "MAtricks via commands (verified by exporting stored presets): 'Reset Selection MAtricks', 'Set Selection MAtricks \"XWings\" 2', '\"XInvW\" \"Yes\"', '\"PhaserTransform\" \"Mirror\"'. The state is readable via Selection():Get(\"XWINGS\"/\"XINVW\"/\"PHASERTRANSFORM\"). WARNING: Wings+Mirror does NOT give opposite pan on the two wings for an absolute 'Attribute \"Pan\" At A Thru B' (Wings gives a symmetric ramp, Mirror only works on RELATIVE values and breaks on an odd centre column). For fans/crosses set each fixture's Pan/Tilt individually and verify by exporting the preset. Do not reset MAtricks before Store. Working examples in the show: MAtricks 36, 695, 166-173.",
    "To READ preset values safely: ma3.h(\"Preset 2.5\"):Export(\"/absolute/dir\", \"name\") then parse the XML (<Phaser ID=<FID> Attribute=Pan|Tilt><Step AbsolutePhys=...>). Never GetPresetDataFast. Save the show before the first Export of a new object type.",
    "POSITIONS FROM SCRATCH NEED AN MVR: before creating position presets for a rig you have not learned yet, ask the user for an MVR export with the scenic data (stage deck, trusses, screens) AND the fixtures (File > Export MVR from the CAD/visualiser). Load it with ma3_mvr_load, find the deck with ma3_mvr_stage (confirm the audience direction with the user), align it to the patch with ma3_mvr_align (matches MVR FixtureID to MA3 FID), then use ma3_patterns / ma3_patterns_generate. Never invent a stage.",
    "LEARNING FROM THE USER'S OWN PRESETS: when the user says their hand-made positions are done, run ma3_patterns_extract (it exports those presets to XML and stores where each grid row lands relative to the stage in position_patterns.json). Save the show first. Then ma3_patterns_generate applies a stored pattern to any group.",
    "NEVER call GetPresetDataFast (from ma3_lua or anywhere): it crashed the grandMA3 onPC 2.5.1.0 engine and lost unsaved work. Treat any untested GetPreset*/bulk API call as able to crash the console: save the show first, or try it on a single object in a copy.",
    "If a tool answers 'unknown operation', the plugin loaded on the console is older than this server: run ReloadAllPlugins (or re-import the plugin) and 'Plugin <n> \"Start\"' again.",
]


def load_notes() -> list:
    try:
        with open(NOTES_PATH, "r", encoding="utf-8") as handle:
            notes = json.load(handle)
        return [str(n) for n in notes] if isinstance(notes, list) else []
    except (OSError, ValueError):
        return []


def add_note(note: str) -> list:
    notes = load_notes()
    note = note.strip()
    if note and note not in notes:
        notes.append(note)
        with open(NOTES_PATH, "w", encoding="utf-8") as handle:
            json.dump(notes, handle, ensure_ascii=False, indent=2)
    return notes


def instructions() -> str:
    lines = ["How the grandMA3 console behaves (keep following and extending this):"]
    lines += ["- " + item for item in BASE_KNOWLEDGE]
    learned = load_notes()
    if learned:
        lines.append("Learned from earlier sessions:")
        lines += ["- " + item for item in learned]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def load_journal() -> List[Dict[str, Any]]:
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_journal(entries: List[Dict[str, Any]]) -> None:
    with open(JOURNAL_PATH, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, ensure_ascii=False, indent=2)


def journal_add(entry: Dict[str, Any]) -> Dict[str, Any]:
    entries = load_journal()
    next_id = max([int(e.get("id", 0)) for e in entries] + [0]) + 1
    entry = dict(entry, id=next_id, ts=time.strftime("%Y-%m-%d %H:%M:%S"), rolled_back=False)
    entries.append(entry)
    save_journal(entries)
    return entry


def journal_update(entry_id: int, **fields: Any) -> None:
    entries = load_journal()
    for entry in entries:
        if entry.get("id") == entry_id:
            entry.update(fields)
    save_journal(entries)


def rollback_strategy(entry: Dict[str, Any]) -> str:
    parts = []
    if entry.get("oops_steps"):
        parts.append("oops")
    if entry.get("set_log"):
        parts.append("property-restore")
    if any(s.get("ok") for s in entry.get("snapshots", [])):
        parts.append("snapshot")
    if entry.get("inverse_code"):
        parts.append("inverse-script")
    return "+".join(parts) if parts else "none"


def record_change(tool: str, label: str, request: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    entry = {
        "tool": tool,
        "label": label,
        "code": request.get("code"),
        "actions": request.get("actions"),
        "inverse_code": request.get("inverse"),
        "undo_id": result.get("undo_id"),
        "oops_steps": result.get("oops_steps", 0),
        "set_log": result.get("set_log", []),
        "snapshots": result.get("snapshots", []),
        "flags": result.get("flags", []),
        "completed": result.get("completed"),
        "error": result.get("lua_error"),
    }
    entry["strategy"] = rollback_strategy(entry)
    return journal_add(entry)


def journal_summary(entry: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("id", "ts", "tool", "label", "strategy", "flags", "completed", "error", "rolled_back", "undo_id")
    summary = {k: entry.get(k) for k in keys}
    if entry.get("code"):
        summary["code"] = entry["code"] if len(entry["code"]) <= 400 else entry["code"][:400] + "..."
    if entry.get("actions"):
        summary["actions"] = entry["actions"]
    return summary


def do_rollback(arguments: Dict[str, Any]) -> Dict[str, Any]:
    entries = [e for e in load_journal() if not e.get("rolled_back")]
    if "id" in arguments:
        wanted = [e for e in entries if e.get("id") == int(arguments["id"])]
        if not wanted:
            raise RuntimeError("no un-rolled-back journal entry with id " + str(arguments["id"]))
    else:
        count = max(1, int(arguments.get("last", 1)))
        wanted = list(reversed(entries))[:count]
        if not wanted:
            raise RuntimeError("journal has nothing to roll back")
    reports = []
    for entry in wanted:  # newest first
        if entry.get("strategy") == "none":
            reports.append({"id": entry["id"], "strategy": "none", "note": "nothing recorded to undo (no undo group, property log, snapshot or inverse script)"})
            continue
        request = {
            "op": "rollback",
            "undo_id": entry.get("undo_id"),
            "oops_steps": entry.get("oops_steps", 0),
            "set_log": entry.get("set_log", []),
            "snapshots": entry.get("snapshots", []),
            "inverse_code": entry.get("inverse_code"),
            "force_oops": bool(arguments.get("force_oops", False)),
        }
        report = plugin_request(request)
        report.pop("ok", None)
        skipped = "skipped" in report.get("oops", {})
        failed = report.get("properties", {}).get("unresolved", 0) or report.get("snapshots", {}).get("failed", 0)
        clean = not skipped and not failed
        journal_update(entry["id"], rolled_back=clean, rollback_report=report)
        reports.append({"id": entry["id"], "strategy": entry.get("strategy"), "flags": entry.get("flags", []), "clean": clean, "report": report})
    return {"rolled_back": reports}


# ---------------------------------------------------------------------------
# Plugin transport and tools
# ---------------------------------------------------------------------------

def plugin_request(request: Dict[str, Any]) -> Dict[str, Any]:
    request = noconfirm.prepare_request(request)
    with socket.create_connection((PLUGIN_HOST, PLUGIN_PORT), timeout=150) as client:
        client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_REPLY_BYTES:
                raise RuntimeError("grandMA3 response exceeded %d bytes; use limit/offset or fields" % MAX_REPLY_BYTES)
    if not data:
        raise RuntimeError("grandMA3 plugin returned no response")
    result = json.loads(data.decode("utf-8", errors="replace"))
    if not result.get("ok", False):
        message = result.get("error", "grandMA3 plugin request failed")
        if str(message).startswith("unknown operation"):
            message += " (the plugin on the console is older than this server: ReloadAllPlugins or re-import it, then run Plugin <n> \"Start\")"
        raise RuntimeError(message)
    return result


SCAN_LUA = r"""
local GROUPS = __GROUPS__
local PRESETS = __PRESETS__
local DIR = __DIR__
if SelectionCount() ~= 0 then Cmd("ClearAll") end
local out, exported = {}, {}
for _, g in ipairs(GROUPS) do
  Cmd("ClearAll"); Cmd("Group " .. g)
  local seen = {}
  local idx, gx, gy = SelectionFirst()
  local guard = 0
  while idx and guard < 4000 do
    guard = guard + 1
    local sub = GetSubfixture(idx)
    local fx = sub and (sub:FindParent("Fixture") or sub)
    local fid = fx and tonumber(tostring(fx:Get("FID")))
    if fid and not seen[fid] then
      seen[fid] = true
      local ft = fx:Get("FIXTURETYPE")
      out[#out+1] = {fid = fid, group = g, type = ft and tostring(ft:Get("Name")) or "", x = tonumber(fx:Get("POSX")), y = tonumber(fx:Get("POSY")),
        z = tonumber(fx:Get("POSZ")), rotz = tonumber(fx:Get("ROTZ")) or 0, gy = gy, gx = gx}
    end
    idx, gx, gy = SelectionNext(idx)
  end
end
Cmd("ClearAll")
if #PRESETS > 0 then
  pcall(CreateDirectoryRecursive, DIR)
  for _, n in ipairs(PRESETS) do
    local h = ma3.h("Preset 2." .. n)
    local ok, res = pcall(function() return h:Export(DIR, "pos_" .. n) end)
    exported[#exported+1] = {preset = n, ok = ok and res == true, name = h and tostring(h:Get("Name")) or nil}
  end
end
return {fixtures = out, exported = exported}
"""


def scan_ma3(groups: List[int], preset_numbers: List[int], directory: str) -> Dict[str, Any]:
    code = (SCAN_LUA.replace("__GROUPS__", "{" + ",".join(str(int(g)) for g in groups) + "}")
            .replace("__PRESETS__", "{" + ",".join(str(int(p)) for p in preset_numbers) + "}")
            .replace("__DIR__", json.dumps(directory)))
    result = plugin_request({"op": "lua", "code": code, "label": "scan groups / export presets (read-only)", "timeout_s": 90})
    if not result.get("completed"):
        raise RuntimeError("scan failed: " + str(result.get("lua_error")))
    return result["result"]


def current_stage() -> Dict[str, Any]:
    stage = STATE.get("mvr_stage") or patterns.load(STAGE_PATH)
    if stage:
        return stage
    return dict(patterns.DEFAULT_STAGE)


def stage_in_ma3(stage: Dict[str, Any], alignment: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert an MVR-frame stage to patch coordinates using the alignment (rotation + offset)."""
    if not alignment or not alignment.get("ok"):
        return stage
    import math
    cx, cy, cz = mvr.apply_alignment(tuple(stage["centre_m"]), alignment)
    a = math.radians(alignment["rotation_deg"])
    ax, ay, _ = stage["audience_direction"]
    rx, ry = math.cos(a) * ax - math.sin(a) * ay, math.sin(a) * ax + math.cos(a) * ay
    out = dict(stage)
    out.update({"centre_m": (round(cx, 3), round(cy, 3), round(cz, 3)), "audience_direction": (round(rx, 4), round(ry, 4), 0.0),
                "source": "MVR stage converted to patch coordinates"})
    return out


PAGING = {"limit": {"type": "integer", "description": "page size, default 100, max 1000"}, "offset": {"type": "integer"}}
FIELDS = {"type": "array", "items": {"type": "string"}, "description": "property names to return; omit for all properties"}


def schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


# Read tools are forwarded to the plugin unchanged: arguments become request fields.
READ_TOOLS: Dict[str, Dict[str, Any]] = {
    "ma3_status": {"op": "status", "description": "Check whether the grandMA3 onPC bridge plugin is running and its version.", "inputSchema": schema({})},
    "ma3_context": {"op": "context", "description": "Read the current grandMA3 selection (subfixture index plus selection-grid x/y/z) and the selected sequence. An empty selection does not mean the target sequence is unknown or missing: for requests about the selected sequence, skip this and send commands (e.g. Store Cue 1 Thru 5) directly, since the console applies them to the selected sequence.", "inputSchema": schema({})},
    "ma3_api": {"op": "api", "description": "Search the LIVE Lua API of this console build (GetApiDescriptor / GetObjApiDescriptor). kind: fn | obj | all | enums. query matches the function name (in_args=true also matches arguments). enum=<name> lists one Enums table. drift=true compares the live API with api_index.json.", "inputSchema": schema({"query": {"type": "string"}, "kind": {"type": "string", "enum": ["fn", "obj", "all", "enums"]}, "in_args": {"type": "boolean"}, "names_only": {"type": "boolean"}, "enum": {"type": "string"}, "drift": {"type": "boolean"}, **PAGING})},
    "ma3_inspect": {"op": "inspect", "description": "Inspect any show object by address (e.g. 'Group 1', 'Sequence 3') or handle id: class, parent, every property with value, and a page of children. Use fields to limit properties, child_fields for per-child properties, dump=true for the raw Dump().", "inputSchema": schema({"target": {"type": "string"}, "fields": FIELDS, "child_fields": FIELDS, "props": {"type": "boolean"}, "children": {"type": "boolean"}, "dump": {"type": "boolean"}, "max_value_len": {"type": "integer"}, **PAGING}, ["target"])},
    "ma3_list": {"op": "list", "description": "List objects. Either pattern (ObjectList command-selection syntax, e.g. 'Group 1 Thru 50') or base (address/handle id of a parent; recursive=true with class='Fixture' walks the subtree). fields returns those properties per object.", "inputSchema": schema({"pattern": {"type": "string"}, "base": {"type": "string"}, "recursive": {"type": "boolean"}, "class": {"type": "string"}, "cap": {"type": "integer"}, "fields": FIELDS, **PAGING})},
    "ma3_fixtures": {"op": "fixtures", "description": "Fixtures with their properties (including patch position/rotation as exposed by this build) and selection-grid x/y/z. scope: selection (default) | all | group:<n>. For scope=all, class defaults to 'Fixture' and root to Patch().", "inputSchema": schema({"scope": {"type": "string"}, "fields": FIELDS, "class": {"type": "string"}, "root": {"type": "string"}, "cap": {"type": "integer"}, **PAGING})},
    "ma3_sequences": {"op": "sequences", "description": "List sequences (with cue counts and which one is selected), or pass target ('Sequence 3' or the selected one's handle id (#...)) to read its properties and cues.", "inputSchema": schema({"target": {"type": "string"}, "fields": FIELDS, "child_fields": FIELDS, **PAGING})},
    "ma3_presets": {"op": "presets", "description": "List preset pools, list a pool (pool=2 or 'Position'), read a preset's data (target=handle id or address; GetPresetData, phasers_only, by_fixtures), or read the programmer phaser of a UI channel (ui_channel).", "inputSchema": schema({"pool": {"type": "string"}, "target": {"type": "string"}, "phasers_only": {"type": "boolean"}, "by_fixtures": {"type": "boolean"}, "ui_channel": {"type": "integer"}, "phaser_only": {"type": "boolean"}, **PAGING})},
}

ACTION_ARRAY = {"type": "array", "maxItems": 32, "items": {"type": "object"}}

OTHER_TOOLS: List[Dict[str, Any]] = [
    {"name": "ma3_execute", "description": "Preview or execute validated grandMA3 actions. Set confirm=false to preview and confirm=true to execute actions authorized by the user, including an approved implementation plan. Append /noconfirm to console commands that support it to avoid interactive popups. Actions may be cmd, set, or wait. Confirmed runs are journaled and can be undone with ma3_rollback.", "inputSchema": {"type": "object", "properties": {"actions": ACTION_ARRAY, "confirm": {"type": "boolean"}}, "required": ["actions", "confirm"], "additionalProperties": False}},
    {"name": "ma3_lua", "description": "Run ANY Lua in the grandMA3 plugin with the full console API (open execution: it runs immediately, no preview). Safety net: one Oops group per call, UndoDisable blocked, Set old values recorded, Create/Append/Insert/Delete/Remove/Copy and Cmd* get the undo handle, and every call is journaled for ma3_rollback. `return` a value to get it back as JSON. touches=[addresses] snapshots those objects first (Export). inverse=<Lua> is stored and run on rollback. Helper table `ma3` and variable `undo` are available. Snippets using Export/Save/SetVar/Keyboard/Mouse/RemoteCommand are flagged: Oops cannot undo them.", "inputSchema": schema({"code": {"type": "string"}, "label": {"type": "string"}, "touches": {"type": "array", "items": {"type": "string"}}, "inverse": {"type": "string"}, "timeout_s": {"type": "number"}}, ["code"])},
    {"name": "ma3_journal", "description": "List what the AI changed in the show (newest last): label, code, rollback strategy (oops / property-restore / snapshot / inverse-script / none), flags for non-undoable calls, and whether it was rolled back.", "inputSchema": schema({"limit": {"type": "integer"}})},
    {"name": "ma3_rollback", "description": "Undo journaled changes, newest first. Pass id=<journal id> or last=<n>. Uses Oops when the entry is the newest undo group, then restores recorded property values, snapshots and the inverse script. The result states exactly what was and was not undone. force_oops=true presses Oops even when newer undo groups exist.", "inputSchema": schema({"id": {"type": "integer"}, "last": {"type": "integer"}, "force_oops": {"type": "boolean"}})},
    {"name": "ma3_mvr_load", "description": "Read an .mvr file (scenic objects AND fixtures from a CAD/visualiser export) so positions can be planned against the real stage. Returns layers, fixture types and counts, scenic objects and how many have readable 3D geometry. ASK THE USER FOR THIS FILE before creating positions for a rig you have not learned.", "inputSchema": schema({"path": {"type": "string"}}, ["path"])},
    {"name": "ma3_mvr_stage", "description": "Estimate the stage from the loaded MVR: deck bounds, centre, size, deck height and the audience direction, with a confidence and how it was decided. Pass stage_names (words in the scenic object or layer names, e.g. ['STAGE','DECK']) and/or audience ('-y' etc., in MVR axes) when the user tells you. The result is saved to stage.json.", "inputSchema": schema({"stage_names": {"type": "array", "items": {"type": "string"}}, "audience": {"type": "string"}, "deck_height_m": {"type": "number"}})},
    {"name": "ma3_mvr_fixtures", "description": "List the MVR fixtures with FixtureID, GDTF spec/mode, world position, home beam direction and position in the stage frame (u across, v toward the audience, h above the deck).", "inputSchema": schema({"layer": {"type": "string"}, "spec_contains": {"type": "string"}, **PAGING})},
    {"name": "ma3_mvr_align", "description": "Match the MVR fixtures to the MA3 patch by FixtureID == FID and compute the rotation/offset between MVR and patch coordinates (with residuals). Needed before MVR stage data is used for patch-space positions.", "inputSchema": schema({})},
    {"name": "ma3_patterns_extract", "description": "LEARN from the user's hand-made position presets. Exports the given position presets (pool 2) to XML, reads the fixtures of the given groups, converts every pan/tilt to where the beam lands relative to the stage, and stores the result per preset x group x grid row in position_patterns.json. Read-only on the show (it selects groups and clears the selection). Ask the user to SAVE THE SHOW first.", "inputSchema": schema({"presets": {"type": "array", "items": {"type": "integer"}}, "groups": {"type": "array", "items": {"type": "integer"}}}, ["presets", "groups"])},
    {"name": "ma3_patterns", "description": "Read the stored position patterns (what each preset does relative to the stage): per row landing depth, mean tilt, and the lateral rule (landing across-stage = slope * fixture across-stage + offset: 1 parallel, 0 converge, >1 fan out, <0 cross).", "inputSchema": schema({"preset": {"type": "integer"}, "group": {"type": "integer"}})},
    {"name": "ma3_patterns_generate", "description": "Apply a stored pattern to a group and return the Pan/Tilt per fixture. Does NOT change the show: store the values yourself (ClearAll, Fixture N, Attribute Pan/Tilt, Store Preset 2.N /merge /noconfirm).", "inputSchema": schema({"preset": {"type": "integer"}, "source_group": {"type": "integer"}, "target_group": {"type": "integer"}}, ["preset", "source_group", "target_group"])},
    {"name": "ma3_notes", "description": "Read or extend persistent notes about how this console behaves. Pass 'add' when you learn something new (a command that worked, a quirk, a property name) so future sessions do not have to rediscover it.", "inputSchema": {"type": "object", "properties": {"add": {"type": "string"}}, "additionalProperties": False}},
    {"name": "ma3_teach_start", "description": "Begin recording a named workflow; the plugin observes supported object changes until teach_stop.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}},
    {"name": "ma3_teach_record", "description": "Add an explicit command to an active taught workflow when an action is not observable as an object change.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "command": {"type": "string"}}, "required": ["name", "command"], "additionalProperties": False}},
    {"name": "ma3_teach_stop", "description": "Stop teaching and return captured before/after context and observed changes so the AI can infer a reusable workflow.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}},
    {"name": "ma3_workflow_save", "description": "Save an AI-inferred workflow after reviewing its actions.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "actions": ACTION_ARRAY}, "required": ["name", "actions"], "additionalProperties": False}},
    {"name": "ma3_workflow_run", "description": "Preview or run a saved workflow. Set confirm=true only after reviewing its actions.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["name", "confirm"], "additionalProperties": False}},
]

TOOLS = [{"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]} for name, spec in READ_TOOLS.items()] + OTHER_TOOLS


def mcp_result(value: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}]}


def mcp_error(message: str) -> Dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def api_drift(live_names: List[str]) -> Dict[str, Any]:
    try:
        with open(API_INDEX_PATH, "r", encoding="utf-8") as handle:
            index = json.load(handle)
    except (OSError, ValueError):
        return {"error": "api_index.json is missing; run tools/sync_api.py"}
    indexed = {"fn:" + n for n in index.get("fn", [])} | {"obj:" + n for n in index.get("obj", [])}
    live = set(live_names)
    return {"only_live": sorted(live - indexed), "only_in_index": sorted(indexed - live), "index_source": index.get("source")}


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name in READ_TOOLS:
        request = dict(arguments, op=READ_TOOLS[name]["op"])
        drift = bool(request.pop("drift", False)) if name == "ma3_api" else False
        result = plugin_request(request)
        if drift:
            names = plugin_request({"op": "api", "kind": "all", "names_only": True}).get("names", [])
            result["drift"] = api_drift(names)
        return mcp_result(result)
    if name == "ma3_mvr_load":
        scene = mvr.parse_mvr(str(arguments["path"]))
        STATE["scene"], STATE["mvr_stage"], STATE["alignment"] = scene, None, None
        summary = mvr.summarize(scene)
        summary["next"] = "call ma3_mvr_stage to find the stage deck, then ma3_mvr_align to tie the file to the patch"
        return mcp_result(summary)
    if name == "ma3_mvr_stage":
        scene = STATE.get("scene")
        if not scene:
            return mcp_error("load an MVR first with ma3_mvr_load")
        stage = mvr.infer_stage(scene, arguments.get("stage_names"), arguments.get("audience"), arguments.get("deck_height_m"))
        if stage.get("found"):
            STATE["mvr_stage"] = stage
            saved = stage_in_ma3(stage, STATE.get("alignment"))
            patterns.save(STAGE_PATH, saved)
            stage = dict(stage, patch_frame=saved if saved is not stage else "not aligned yet: run ma3_mvr_align to convert to patch coordinates")
        return mcp_result(stage)
    if name == "ma3_mvr_fixtures":
        scene = STATE.get("scene")
        if not scene:
            return mcp_error("load an MVR first with ma3_mvr_load")
        stage = STATE.get("mvr_stage")
        rows = []
        for f in scene["fixtures"]:
            if arguments.get("layer") and arguments["layer"].lower() not in f["layer"].lower():
                continue
            if arguments.get("spec_contains") and arguments["spec_contains"].lower() not in (f.get("gdtf_spec") or "").lower():
                continue
            row = dict(f)
            if stage and stage.get("found"):
                row["stage_frame"] = mvr.stage_coordinates(f["position_m"], stage)
            rows.append(row)
        offset = int(arguments.get("offset", 0))
        limit = max(1, min(int(arguments.get("limit", 100)), 1000))
        return mcp_result({"total": len(rows), "offset": offset, "items": rows[offset:offset + limit]})
    if name == "ma3_mvr_align":
        scene = STATE.get("scene")
        if not scene:
            return mcp_error("load an MVR first with ma3_mvr_load")
        rows, offset = [], 0
        while True:
            page = plugin_request({"op": "fixtures", "scope": "all", "fields": ["FID", "POSX", "POSY", "POSZ"], "limit": 1000, "offset": offset})
            for item in page["items"]:
                f = item.get("fields", {})
                try:
                    rows.append({"fid": int(str(f.get("FID"))), "x": float(f["POSX"]), "y": float(f["POSY"]), "z": float(f["POSZ"])})
                except (TypeError, ValueError, KeyError):
                    continue
            offset += len(page["items"])
            if not page.get("truncated") or not page["items"]:
                break
        alignment = mvr.align_with_ma3(scene, rows)
        STATE["alignment"] = alignment
        if alignment.get("ok") and STATE.get("mvr_stage"):
            patterns.save(STAGE_PATH, stage_in_ma3(STATE["mvr_stage"], alignment))
            alignment["stage_saved_in_patch_frame"] = True
        return mcp_result(alignment)
    if name == "ma3_patterns_extract":
        numbers = [int(p) for p in arguments["presets"]]
        groups = [int(g) for g in arguments["groups"]]
        directory = os.path.join(SNAPSHOT_DIR, "patterns")
        os.makedirs(directory, exist_ok=True)
        scanned = scan_ma3(groups, numbers, directory)
        fixtures = {int(f["fid"]): f for f in scanned["fixtures"]}
        presets: Dict[int, Dict[str, Any]] = {}
        missing = []
        for item in scanned["exported"]:
            path = os.path.join(directory, "pos_%d.xml" % item["preset"])
            if not item.get("ok") or not os.path.exists(path):
                missing.append(item["preset"])
                continue
            with open(path, "r", encoding="utf-8-sig") as handle:
                text = handle.read()
            presets[item["preset"]] = {"name": item.get("name") or patterns.preset_name_from_xml(text), "values": patterns.parse_preset_xml(text)}
        stage = current_stage()
        built = patterns.build_patterns(presets, fixtures, stage)
        patterns.save(PATTERNS_PATH, built)
        summary = [{"preset": p["preset"], "name": p["name"], "group": p["group"], "type": p["fixture_type"], "fixtures": p["fixtures"],
                    "rows": [(r["grid_row"], r["tilt_mean_deg"], r["landing_v_m"]) for r in p["rows"]], "lateral_slope": p["lateral"]["landing_u_slope"]}
                   for p in built["patterns"]]
        return mcp_result({"stored": len(built["patterns"]), "stage_used": stage, "presets_missing": missing, "summary": summary})
    if name == "ma3_patterns":
        data = patterns.load(PATTERNS_PATH)
        if not data:
            return mcp_error("no patterns stored yet: run ma3_patterns_extract after the user finishes the hand-made positions")
        items = [p for p in data["patterns"] if ("preset" not in arguments or p["preset"] == int(arguments["preset"]))
                 and ("group" not in arguments or p["group"] == int(arguments["group"]))]
        return mcp_result({"stage": data["stage"], "count": len(items), "patterns": items})
    if name == "ma3_patterns_generate":
        data = patterns.load(PATTERNS_PATH)
        if not data:
            return mcp_error("no patterns stored yet: run ma3_patterns_extract first")
        match = [p for p in data["patterns"] if p["preset"] == int(arguments["preset"]) and p["group"] == int(arguments["source_group"])]
        if not match:
            return mcp_error("no stored pattern for preset %s learned from group %s" % (arguments["preset"], arguments["source_group"]))
        scanned = scan_ma3([int(arguments["target_group"])], [], "")
        fixtures = {int(f["fid"]): f for f in scanned["fixtures"]}
        values = patterns.generate(match[0], fixtures, data["stage"])
        return mcp_result({"preset": arguments["preset"], "pattern_group": arguments["source_group"], "target_group": arguments["target_group"],
                           "stage": data["stage"], "values": {str(k): v for k, v in sorted(values.items())},
                           "note": "nothing was changed on the console; store these per fixture with /merge /noconfirm"})
    if name == "ma3_notes":
        added = str(arguments.get("add", ""))
        notes = add_note(added) if added.strip() else load_notes()
        return mcp_result({"base": BASE_KNOWLEDGE, "learned": notes})
    if name == "ma3_workflow_save":
        WORKFLOWS[str(arguments.get("name", ""))] = {"actions": arguments.get("actions", [])}
        return mcp_result({"saved": True, "name": arguments.get("name", "")})
    if name == "ma3_journal":
        limit = max(1, int(arguments.get("limit", 50)))
        entries = load_journal()
        return mcp_result({"total": len(entries), "entries": [journal_summary(e) for e in entries[-limit:]]})
    if name == "ma3_rollback":
        return mcp_result(do_rollback(arguments))
    if name == "ma3_lua":
        request = {"op": "lua", "code": arguments.get("code", ""), "label": arguments.get("label", "ma3_lua"), "touches": arguments.get("touches"), "timeout_s": arguments.get("timeout_s")}
        if arguments.get("touches"):
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            request["snapshot_dir"] = SNAPSHOT_DIR
        result = plugin_request(request)
        entry = record_change("ma3_lua", str(request["label"]), dict(request, inverse=arguments.get("inverse")), result)
        result.pop("ok", None)
        return mcp_result(dict(result, journal_id=entry["id"], rollback_strategy=entry["strategy"]))
    if name in ("ma3_execute", "ma3_workflow_run"):
        if name == "ma3_workflow_run":
            workflow = WORKFLOWS.get(str(arguments.get("name", "")))
            if not workflow:
                return mcp_error("Unknown workflow: " + str(arguments.get("name", "")))
            request = {"op": "workflow_execute", "actions": workflow.get("actions", []), "confirm": arguments.get("confirm", False)}
            label = "workflow " + str(arguments.get("name"))
        else:
            request = {"op": "execute", "actions": arguments.get("actions", []), "confirm": arguments.get("confirm", False)}
            label = "ma3_execute"
        result = plugin_request(request)
        if request["confirm"] and result.get("undo_id"):
            entry = record_change(name, label, request, result)
            result = dict(result, journal_id=entry["id"], rollback_strategy=entry["strategy"])
        return mcp_result(result)
    teach = {"ma3_teach_start": lambda a: {"op": "teach_start", "name": a.get("name", "")},
             "ma3_teach_record": lambda a: {"op": "teach_record", "name": a.get("name", ""), "command": a.get("command", "")},
             "ma3_teach_stop": lambda a: {"op": "teach_stop", "name": a.get("name", "")}}.get(name)
    if teach:
        return mcp_result(plugin_request(teach(arguments)))
    return mcp_error("Unknown tool: " + str(name))


def handle(message: Dict[str, Any]) -> Dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "grandma3-onpc-bridge", "version": "2.0.0"}, "instructions": instructions()}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params", {})
        try:
            result = call_tool(params.get("name"), params.get("arguments", {}) or {})
        except Exception as exc:
            result = mcp_error(str(exc))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is not None:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found: " + str(method)}}
    return None


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = handle(json.loads(line))
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
