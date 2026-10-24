-- Host-side tests: the console API is stubbed so the plugin logic can run under a plain Lua 5.4.
-- Run from this directory: lua test_grandma3_ai_assistant.lua

local methods = {}
local HMT = {__index = methods}
local registry = {}
local oops_presses = 0
local undo_open = 0
local cmd_log = {}

local function handle(class, props, kids, name)
    local h = setmetatable({__ma3_handle = true, class = class, props = props or {}, kids = kids or {}}, HMT)
    h.props.Name = h.props.Name or name
    registry["H#" .. tostring(#registry + 1)] = h
    h.id = "H#" .. tostring(#registry)
    for _, kid in ipairs(h.kids) do kid.parent = h end
    return h
end

function methods.Get(h, name) return h.props[name] end
function methods.Set(h, name, value) h.props[name] = value end
function methods.GetClass(h) return h.class end
function methods.Children(h) return h.kids end
function methods.Count(h) return #h.kids end
function methods.Parent(h) return h.parent end
function methods.Index(h) return 1 end
function methods.GetChildClass(h) return "Child" end
function methods.PropertyCount(h) local n = 0; for _ in pairs(h.props) do n = n + 1 end; return n end
function methods.PropertyName(h, i)
    local names = {}
    for k in pairs(h.props) do names[#names + 1] = k end
    table.sort(names)
    return names[i]
end
function methods.Append(h, class, undo)
    local kid = handle(class or "Child", {}, {})
    kid.parent = h
    h.kids[#h.kids + 1] = kid
    h.last_undo = undo
    return kid
end
function methods.FindParent(h, class) return h.parent end
function methods.Dump(h) return "dump of " .. h.class end

local fixture_a = handle("Fixture", {FID = "101", PosX = "1.5", PosY = "2", PosZ = "3"}, {}, "Spot 1")
local fixture_b = handle("Fixture", {FID = "102", PosX = "-4", PosY = "2", PosZ = "3"}, {}, "Spot 2")
local sequence = handle("Sequence", {No = "1"}, {handle("Cue", {No = "1"}, {}, "Intro"), handle("Cue", {No = "2"}, {}, "Verse")}, "Opening")
local root = handle("Root", {}, {sequence}, "Root")

function Root() return root end
function Patch() return handle("Patch", {}, {fixture_a, fixture_b}, "Patch") end
function DataPool() return {Sequences = handle("Sequences", {}, {sequence}, "Sequences")} end
function SelectedSequence() return sequence end
function CompareHandle(a, b) return a == b end
function GetCurrentCue() return sequence.kids[1] end
function HandleToStr(h) return h.id end
function StrToHandle(s) return registry[s] end
function ToAddr(h) return h.class .. " " .. tostring(h.props.No or h.props.FID or "") end
function GetObject(address)
    if address == "Sequence 1" then return sequence end
    return nil
end
function ObjectList(pattern) if pattern == "Cue *" then return sequence.kids end return {} end
function FromAddr() return nil end
function Version() return "2.5.1.0" end
function SelectionCount() return 2 end
function SelectionFirst() return 11, 1, 0, 0 end
function SelectionNext(i) if i == 11 then return 12, 2, 0, 0 end return nil end
function GetSubfixture(i) return i == 11 and fixture_a or fixture_b end
function GetApiDescriptor() return {{function_name = "Cmd", arguments = "string", return_values = "string"}, {function_name = "Printf", arguments = "string", return_values = ""}} end
function GetObjApiDescriptor() return {{"Children", "handle", "table"}} end
function CreateUndo() undo_open = undo_open + 1; return {undo = true} end
function CloseUndo() undo_open = undo_open - 1; return true end
function UndoEnable() end
function Cmd(command, undo) cmd_log[#cmd_log + 1] = {command = command, undo = undo}; if command == "Oops" then oops_presses = oops_presses + 1 end; return "OK" end
function CmdIndirectWait(command, undo) cmd_log[#cmd_log + 1] = {command = command, undo = undo}; return "OK" end
function Printf() end
function ErrPrintf() end

GMA3_AI_TEST = true
local plugin = assert(loadfile("../grandma3_ai_assistant.lua"))()
local request = plugin.handle_request

local function check(condition, message) if not condition then error(message, 2) end end
local passed = 0
local function test(name, fn) fn(); passed = passed + 1; print("ok - " .. name) end

test("json encodes control characters, nan and empty tables", function()
    check(plugin.json_encode("a\n\"b\"\1") == '"a\\n\\"b\\"\\u0001"', "escaping")
    check(plugin.json_encode(0 / 0) == "null", "nan")
    check(plugin.json_encode({}) == "[]", "empty table")
    check(plugin.json_encode({1, 2}) == "[1,2]", "array")
end)

test("json decode handles unicode escapes", function()
    local value = plugin.json_decode('{"a":"\\u00e9\\n","b":[1,2.5,true,null]}')
    check(value.a == "\u{e9}\n", "unicode")
    check(value.b[2] == 2.5 and value.b[3] == true, "array values")
end)

test("handles encode as descriptors", function()
    local text = plugin.json_encode(sequence)
    check(text:find('"class":"Sequence"') and text:find('"_h":"H#'), text)
end)

test("status reports plugin version", function()
    local r = request({op = "status"})
    check(r.ok and r.plugin_version and r.version == "2.5.1.0", "status")
end)

test("unknown op is an error", function()
    check(request({op = "nope"}).ok == false, "unknown")
end)

test("inspect returns properties and paged children", function()
    local r = request({op = "inspect", target = "Sequence 1", limit = 1})
    check(r.ok and r.properties.Name == "Opening", "properties")
    check(#r.children.items == 1 and r.children.total == 2 and r.children.truncated, "paging")
end)

test("list by pattern returns fields", function()
    local r = request({op = "list", pattern = "Cue *", fields = {"Name"}})
    check(r.total == 2 and r.items[2].fields.Name == "Verse", "list")
end)

test("fixtures selection carries grid coordinates", function()
    local r = request({op = "fixtures", scope = "selection", fields = {"PosX"}})
    check(#r.items == 2 and r.items[1].grid.x == 1 and r.items[2].grid.x == 2, "grid")
    check(r.items[2].fields.PosX == "-4", "position")
end)

test("fixtures all walks the patch by class", function()
    local r = request({op = "fixtures", scope = "all"})
    check(r.total == 2, "all")
end)

test("sequences marks the selected one", function()
    local r = request({op = "sequences"})
    check(r.items[1].is_selected and r.selected_sequence.no == "1" and r.items[1].cue_count == 2, "selected")
end)

test("api search filters by name", function()
    local r = request({op = "api", query = "print"})
    check(r.total == 1 and r.items[1].name == "Printf", "api")
end)

test("lua returns a value and journals property changes", function()
    local r = request({op = "lua", label = "rename", code = 'local s = ma3.h("Sequence 1"); s:Set("Name", "Closing"); return s:Get("Name")'})
    check(r.completed and r.result == "Closing", "result")
    check(sequence.props.Name == "Closing", "set applied")
    check(#r.set_log == 1 and r.set_log[1].old == "Opening", "old value recorded")
    check(undo_open == 0, "undo closed")
end)

test("lua injects the undo handle into structural calls", function()
    local r = request({op = "lua", code = 'ma3.h("Sequence 1"):Append("Cue")'})
    check(r.completed and r.undo_used and r.oops_steps == 1, "undo used")
    check(sequence.last_undo and sequence.last_undo.undo == true, "undo injected")
end)

test("lua runs commands inside the undo group", function()
    cmd_log = {}
    local r = request({op = "lua", code = 'Cmd("Store Cue 9")'})
    check(r.oops_steps == 1 and cmd_log[1].undo and cmd_log[1].undo.undo, "cmd undo")
end)

test("lua errors still close undo and restore the metatable", function()
    local set_before = methods.Set
    local r = request({op = "lua", code = 'ma3.h("Sequence 1"):Set("Name", "X"); error("boom")'})
    check(r.completed == false and r.lua_error:find("boom"), "error reported")
    check(undo_open == 0, "undo closed after error")
    check(methods.Set == set_before, "instrumentation restored")
    check(#r.set_log == 1, "partial change still journaled")
end)

test("lua blocks UndoDisable", function()
    local r = request({op = "lua", code = "UndoDisable()"})
    check(r.completed == false and r.lua_error:find("blocked"), "blocked")
end)

test("lua flags calls Oops cannot undo", function()
    local r = request({op = "lua", code = 'SetVar(nil, "a", 1)'})
    local found = false
    for _, flag in ipairs(r.flags) do if flag == "SetVar" then found = true end end
    check(found, "flag")
end)

test("lua compile errors are reported", function()
    local r = request({op = "lua", code = "this is not lua"})
    check(r.completed == false and r.lua_error, "compile error")
    check(undo_open == 0, "undo closed")
end)

test("rollback restores recorded properties", function()
    sequence.props.Name = "Opening"
    local r = request({op = "lua", code = 'ma3.h("Sequence 1"):Set("Name", "Changed")'})
    check(sequence.props.Name == "Changed", "changed")
    local back = request({op = "rollback", undo_id = r.undo_id, oops_steps = 0, set_log = r.set_log})
    check(back.properties.restored == 1 and sequence.props.Name == "Opening", "restored")
end)

test("rollback presses Oops only for the newest undo group", function()
    local first = request({op = "lua", code = 'Cmd("Store Cue 1")'})
    local second = request({op = "lua", code = 'Cmd("Store Cue 2")'})
    oops_presses = 0
    local skipped = request({op = "rollback", undo_id = first.undo_id, oops_steps = 1})
    check(skipped.oops.skipped and oops_presses == 0, "older entry skipped")
    local ok = request({op = "rollback", undo_id = second.undo_id, oops_steps = 1})
    check(ok.oops.done == 1 and oops_presses == 1, "newest rolled back")
    local forced = request({op = "rollback", undo_id = first.undo_id, oops_steps = 1, force_oops = true})
    check(forced.oops.done == 1, "forced")
end)

test("rollback runs the inverse script", function()
    local r = request({op = "rollback", undo_id = "x", inverse_code = 'ma3.h("Sequence 1"):Set("Name", "FromInverse")'})
    check(r.inverse.completed and sequence.props.Name == "FromInverse", "inverse")
end)

test("execute previews then runs with an undo group", function()
    local preview = request({op = "execute", actions = {{type = "cmd", command = "Go+"}}, confirm = false})
    check(preview.requires_confirmation and preview.preview[1] == "Go+", "preview")
    cmd_log = {}
    local done = request({op = "execute", actions = {{type = "cmd", command = "Go+"}}, confirm = true})
    check(done.completed and done.undo_id and cmd_log[1].undo, "executed")
    check(plugin.oops_log[#plugin.oops_log] == done.undo_id, "tracked for rollback")
end)

test("execute rejects unsafe commands", function()
    check(request({op = "execute", actions = {{type = "cmd", command = "a\nb"}}, confirm = true}).ok == false, "unsafe")
end)

test("oversized replies become errors", function()
    local big = plugin.encode_reply({ok = true, data = string.rep("x", 1000000)})
    check(big:find("response too large"), "too large")
end)

print(string.format("%d tests passed", passed))
