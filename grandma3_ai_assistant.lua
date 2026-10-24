-- grandMA3 AI Assistant (onPC only)
-- Companion protocol: one JSON request and one JSON response per localhost TCP connection.
--
-- Sections: JSON codec, handle helpers, read ops (api/inspect/list/fixtures/sequences/presets),
-- command ops (execute/teach), the open ma3_lua executor with undo + journaling, rollback, bridge loop.

local PLUGIN_NAME = "grandMA3 AI Assistant"
local PLUGIN_VERSION = "2.0.0"
local HOST = "127.0.0.1"
local PORT = 17844
local MAX_COMMAND = 512
local MAX_ACTIONS = 32
local MAX_REPLY = 900000
local DEFAULT_PAGE = 100
local MAX_PAGE = 1000
local component_handle = select(4, ...)
local teaching = {}
local bridge_running = false
local bridge_socket = nil
local undo_counter = 0
local oops_log = {} -- ids of undo groups this plugin created, newest last

---------------------------------------------------------------------------
-- JSON codec
---------------------------------------------------------------------------

local describe_handle -- forward declaration, defined with the handle helpers

local function is_handle(value)
    local kind = type(value)
    return kind == "userdata" or (kind == "table" and rawget(value, "__ma3_handle") == true)
end

local ESCAPES = {['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t', ['\b'] = '\\b', ['\f'] = '\\f'}

local function json_escape(s)
    return (tostring(s):gsub('[%c"\\]', function(c)
        return ESCAPES[c] or string.format("\\u%04x", c:byte())
    end))
end

local function json_encode(value, depth)
    depth = depth or 0
    if depth > 16 then return '"<depth limit>"' end
    local kind = type(value)
    if kind == "nil" then return "null" end
    if kind == "boolean" then return value and "true" or "false" end
    if kind == "number" then
        if value ~= value or value == math.huge or value == -math.huge then return "null" end
        if math.type(value) == "integer" then return string.format("%d", value) end
        return string.format("%.14g", value)
    end
    if kind == "string" then return '"' .. json_escape(value) .. '"' end
    if is_handle(value) then return json_encode(describe_handle(value), depth + 1) end
    if kind == "table" then
        local count, max_index, array = 0, 0, true
        for k in pairs(value) do
            count = count + 1
            if math.type(k) == "integer" and k >= 1 then
                if k > max_index then max_index = k end
            else
                array = false
            end
        end
        if count == 0 then return "[]" end
        if array and max_index == count then
            local out = {}
            for i = 1, max_index do out[i] = json_encode(value[i], depth + 1) end
            return "[" .. table.concat(out, ",") .. "]"
        end
        local out = {}
        for k, v in pairs(value) do
            out[#out + 1] = '"' .. json_escape(k) .. '":' .. json_encode(v, depth + 1)
        end
        return "{" .. table.concat(out, ",") .. "}"
    end
    return '"<' .. kind .. '>"'
end

local function json_decode(text)
    local i = 1
    local function ws() while text:sub(i, i):match("%s") do i = i + 1 end end
    local parse
    local function string_value()
        i = i + 1
        local out = {}
        local map = {n = "\n", r = "\r", t = "\t", b = "\b", f = "\f", ['"'] = '"', ['\\'] = "\\", ['/'] = "/"}
        while i <= #text do
            local c = text:sub(i, i)
            if c == '"' then i = i + 1; return table.concat(out) end
            if c == "\\" then
                local e = text:sub(i + 1, i + 1)
                if e == "u" then
                    local code = tonumber(text:sub(i + 2, i + 5), 16) or 63
                    i = i + 6
                    -- surrogate pair
                    if code >= 0xD800 and code <= 0xDBFF and text:sub(i, i + 1) == "\\u" then
                        local low = tonumber(text:sub(i + 2, i + 5), 16)
                        if low and low >= 0xDC00 and low <= 0xDFFF then
                            code = 0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00)
                            i = i + 6
                        end
                    end
                    out[#out + 1] = utf8.char(code)
                else
                    out[#out + 1] = map[e] or e; i = i + 2
                end
            else out[#out + 1] = c; i = i + 1 end
        end
        error("unterminated JSON string")
    end
    local function number_value()
        local start = i
        while text:sub(i, i):match("[%d%+%-%.eE]") do i = i + 1 end
        local n = tonumber(text:sub(start, i - 1))
        if n == nil then error("invalid JSON number") end
        return n
    end
    local function array_value()
        i = i + 1; local out = {}; ws()
        if text:sub(i, i) == "]" then i = i + 1; return out end
        while true do
            out[#out + 1] = parse(); ws()
            if text:sub(i, i) == "]" then i = i + 1; return out end
            if text:sub(i, i) ~= "," then error("expected comma") end
            i = i + 1; ws()
        end
    end
    local function object_value()
        i = i + 1; local out = {}; ws()
        if text:sub(i, i) == "}" then i = i + 1; return out end
        while true do
            if text:sub(i, i) ~= '"' then error("expected object key") end
            local key = string_value(); ws()
            if text:sub(i, i) ~= ":" then error("expected colon") end
            i = i + 1; ws(); out[key] = parse(); ws()
            if text:sub(i, i) == "}" then i = i + 1; return out end
            if text:sub(i, i) ~= "," then error("expected comma") end
            i = i + 1; ws()
        end
    end
    parse = function()
        ws(); local c = text:sub(i, i)
        if c == '"' then return string_value() end
        if c == "{" then return object_value() end
        if c == "[" then return array_value() end
        if text:sub(i, i + 3) == "true" then i = i + 4; return true end
        if text:sub(i, i + 4) == "false" then i = i + 5; return false end
        if text:sub(i, i + 3) == "null" then i = i + 4; return nil end
        return number_value()
    end
    local result = parse(); ws(); return result
end

---------------------------------------------------------------------------
-- Handle helpers
---------------------------------------------------------------------------

-- Call f(...) and return its first result, or nil when it raises. Console API calls may throw on
-- objects of the wrong class, so every optional read goes through here.
local function safe(f, ...)
    local ok, a = pcall(f, ...)
    if ok then return a end
    return nil
end

local function handle_to_str(h)
    return safe(HandleToStr, h)
end

local function handle_name(h)
    local name = safe(function() return h:Get("Name") end)
    if type(name) == "string" or type(name) == "number" then return tostring(name) end
    return nil
end

describe_handle = function(h)
    return {
        _h = handle_to_str(h),
        class = safe(function() return h:GetClass() end),
        addr = safe(ToAddr, h, false),
        name = handle_name(h),
    }
end

local function resolve(target)
    if is_handle(target) then return target end
    if type(target) ~= "string" or target == "" then return nil end
    if target:match("^H?#") then
        -- the console prints handles as "#00015DE4"; the docs call the format "H#..."
        return safe(StrToHandle, target) or safe(StrToHandle, "H" .. target:gsub("^H", ""))
    end
    local h = safe(GetObject, target)
    if h then return h end
    local list = safe(ObjectList, target)
    if type(list) == "table" and list[1] then return list[1] end
    return safe(FromAddr, target)
end

local function value_text(v, max_len)
    local kind = type(v)
    if is_handle(v) then return describe_handle(v) end
    if kind == "string" then
        if max_len and #v > max_len then return v:sub(1, max_len) .. "..." end
        return v
    end
    if kind == "number" or kind == "boolean" then return v end
    if v == nil then return nil end
    return tostring(v)
end

local function read_props(h, max_len, only)
    local props = {}
    if only then
        for _, name in ipairs(only) do
            props[name] = value_text(safe(function() return h:Get(name) end), max_len)
        end
        return props
    end
    local count = safe(function() return h:PropertyCount() end) or 0
    for i = 1, count do
        local name = safe(function() return h:PropertyName(i) end)
        if name then
            props[name] = value_text(safe(function() return h:Get(name) end), max_len)
        end
    end
    return props
end

local function page_bounds(req)
    local offset = math.max(0, math.floor(tonumber(req.offset) or 0))
    local limit = math.floor(tonumber(req.limit) or DEFAULT_PAGE)
    if limit < 1 then limit = 1 end
    if limit > MAX_PAGE then limit = MAX_PAGE end
    return offset, limit
end

local function slice(list, offset, limit)
    local out = {}
    for i = offset + 1, math.min(#list, offset + limit) do out[#out + 1] = list[i] end
    return out
end

local function paged(list, req)
    local offset, limit = page_bounds(req)
    local items = slice(list, offset, limit)
    return {items = items, total = #list, offset = offset, limit = limit, truncated = offset + #items < #list}
end

local function children_of(h)
    return safe(function() return h:Children() end) or {}
end

-- Depth-first walk collecting handles whose class equals `class` (or all when class is nil).
local function walk(root, class, cap)
    local found, stack = {}, {root}
    cap = cap or 5000
    while #stack > 0 and #found < cap do
        local h = table.remove(stack)
        if class == nil or safe(function() return h:GetClass() end) == class then found[#found + 1] = h end
        local kids = children_of(h)
        for i = #kids, 1, -1 do stack[#stack + 1] = kids[i] end
    end
    return found
end

---------------------------------------------------------------------------
-- Context
---------------------------------------------------------------------------

local function selected_sequence_info()
    if type(SelectedSequence) ~= "function" then return nil end
    local sequence = safe(SelectedSequence)
    if not sequence then return nil end
    local info = describe_handle(sequence)
    info.no = safe(function() return sequence:Get("No") end)
    return info
end

local function selected_context()
    local context = {version = "unknown", selection_count = 0, selected = {}}
    if type(Version) == "function" then pcall(function() context.version = Version() end) end
    if type(SelectionCount) == "function" then pcall(function() context.selection_count = SelectionCount() end) end
    if type(SelectionFirst) == "function" and context.selection_count > 0 then
        local ok, index, x, y, z = pcall(SelectionFirst)
        local guard = 0
        while ok and index and guard < 64 do
            context.selected[#context.selected + 1] = {subfixture_index = index, grid = {x = x, y = y, z = z}}
            guard = guard + 1
            ok, index, x, y, z = pcall(SelectionNext, index)
        end
        context.selected_truncated = context.selection_count > #context.selected
    end
    context.selected_sequence = selected_sequence_info()
    return context
end

---------------------------------------------------------------------------
-- Command safety (used by ma3_execute and the teach ops)
---------------------------------------------------------------------------

local function safe_command(command)
    return type(command) == "string" and #command > 0 and #command <= MAX_COMMAND and not command:find("[\r\n]")
end

local function safe_atom(value)
    return type(value) == "string" and #value > 0 and #value <= 128
        and not value:find('["\r\n;]')
end

local function new_undo_id()
    undo_counter = undo_counter + 1
    return "u" .. undo_counter
end

local function execute_actions(actions)
    if type(actions) ~= "table" then return false, "AI returned no action list" end
    if #actions > MAX_ACTIONS then return false, "action batch exceeds safety limit" end
    local undo = CreateUndo(PLUGIN_NAME)
    local undo_id = new_undo_id()
    local results = {}
    local success = true
    for _, action in ipairs(actions) do
        local command
        if action.type == "cmd" then
            command = action.command
        elseif action.type == "set" and safe_atom(action.address) and safe_atom(action.property) and safe_atom(action.value) then
            command = string.format('Set %s Property "%s" "%s"', action.address, (action.property:gsub('"', '')), (action.value:gsub('"', '')))
        elseif action.type == "wait" then
            local seconds = tonumber(action.seconds) or 0
            if seconds >= 0 and seconds <= 30 then coroutine.yield(seconds) else success = false; results[#results + 1] = "invalid wait" end
        else
            success = false; results[#results + 1] = "unsupported action"
        end
        if command then
            if not safe_command(command) then success = false; results[#results + 1] = "unsafe command" else
                local ok, feedback = pcall(CmdIndirectWait, command, undo)
                results[#results + 1] = tostring(feedback or "OK")
                if not ok then success = false end
            end
        end
    end
    if undo then pcall(CloseUndo, undo) end
    oops_log[#oops_log + 1] = undo_id
    return success, table.concat(results, " | "), undo_id
end

local function teach_start(name)
    if not name or name == "" then ErrPrintf(PLUGIN_NAME .. ": TeachStart requires a workflow name"); return false end
    teaching[name] = {name = name, version = selected_context().version, before = selected_context(), events = {}, actions = {}, hooks = {}}
    if type(HookObjectChange) == "function" and type(DataPool) == "function" and component_handle then
        local pool = DataPool()
        if pool then
            local function changed(object)
                local current = teaching[name]
                if not current or #current.events >= 64 then return end
                local address, dump = "", ""
                pcall(function() address = object:ToAddr() end)
                pcall(function() dump = object:Dump() end)
                current.events[#current.events + 1] = {address = address, dump = tostring(dump):sub(1, 4000)}
            end
            local ok, hook = pcall(HookObjectChange, changed, pool, component_handle)
            if ok and hook then teaching[name].hooks[#teaching[name].hooks + 1] = hook end
        end
    end
    Printf(PLUGIN_NAME .. ": teaching started for " .. name)
    return true
end

local function teach_record(name, command)
    local workflow = teaching[name]
    if not workflow then ErrPrintf(PLUGIN_NAME .. ": workflow is not being taught"); return false end
    if not safe_command(command) then ErrPrintf(PLUGIN_NAME .. ": unsafe or empty command"); return false end
    workflow.actions[#workflow.actions + 1] = {type = "cmd", command = command}
    Printf(PLUGIN_NAME .. ": recorded step " .. tostring(#workflow.actions))
    return true
end

local function teach_stop(name)
    local workflow = teaching[name]
    if not workflow then ErrPrintf(PLUGIN_NAME .. ": workflow is not being taught"); return end
    for _, hook in ipairs(workflow.hooks or {}) do if type(Unhook) == "function" then pcall(Unhook, hook) end end
    workflow.hooks = nil
    workflow.after = selected_context()
    teaching[name] = nil
    return workflow
end

local function preview_actions(actions)
    local preview = {}
    if type(actions) ~= "table" or #actions > MAX_ACTIONS then return nil, "invalid action list" end
    for _, action in ipairs(actions) do
        if action.type == "cmd" and safe_command(action.command) then preview[#preview + 1] = action.command
        elseif action.type == "set" and safe_atom(action.address) and safe_atom(action.property) and safe_atom(action.value) then preview[#preview + 1] = string.format('Set %s Property "%s" "%s"', action.address, action.property, action.value)
        elseif action.type == "wait" and tonumber(action.seconds) and tonumber(action.seconds) >= 0 and tonumber(action.seconds) <= 30 then preview[#preview + 1] = "wait " .. tostring(action.seconds)
        else return nil, "unsafe or unsupported action" end
    end
    return preview
end

---------------------------------------------------------------------------
-- Read ops
---------------------------------------------------------------------------

local OPS = {}

local function descriptor_entries(kind, source)
    local entries = {}
    local list = safe(source)
    if type(list) ~= "table" then return entries end
    for _, e in pairs(list) do
        if type(e) == "table" then
            entries[#entries + 1] = {
                kind = kind,
                name = e.function_name or e[1],
                arguments = e.arguments or e[2],
                returns = e.return_values or e[3],
            }
        end
    end
    return entries
end

OPS.api = function(req)
    local kind = req.kind or "all"
    if kind == "enums" then
        if type(Enums) ~= "table" then return {error = "Enums table is not available on this build"} end
        if req.enum then
            local enum = Enums[req.enum]
            if type(enum) ~= "table" then return {error = "unknown enum: " .. tostring(req.enum)} end
            local values = {}
            for k, v in pairs(enum) do values[#values + 1] = {name = tostring(k), value = value_text(v)} end
            table.sort(values, function(a, b) return a.name < b.name end)
            return {enum = req.enum, values = values}
        end
        local names = {}
        for k in pairs(Enums) do names[#names + 1] = tostring(k) end
        table.sort(names)
        return {enums = names}
    end
    local entries = {}
    if kind == "all" or kind == "fn" then
        for _, e in ipairs(descriptor_entries("fn", GetApiDescriptor)) do entries[#entries + 1] = e end
    end
    if kind == "all" or kind == "obj" then
        for _, e in ipairs(descriptor_entries("obj", GetObjApiDescriptor)) do entries[#entries + 1] = e end
    end
    local query = req.query and tostring(req.query):lower() or nil
    local filtered = {}
    for _, e in ipairs(entries) do
        local name = tostring(e.name or "")
        if not query or name:lower():find(query, 1, true)
            or (req.in_args and tostring(e.arguments or ""):lower():find(query, 1, true)) then
            filtered[#filtered + 1] = e
        end
    end
    table.sort(filtered, function(a, b) return tostring(a.name) < tostring(b.name) end)
    if req.names_only then
        local names = {}
        for _, e in ipairs(filtered) do names[#names + 1] = e.kind .. ":" .. tostring(e.name) end
        return {names = names, total = #names}
    end
    return paged(filtered, req)
end

local function inspect_handle(h, req)
    local offset, limit = page_bounds(req)
    local out = {handle = describe_handle(h)}
    out.parent = safe(function() local p = h:Parent(); return p and describe_handle(p) end)
    out.child_count = safe(function() return h:Count() end)
    out.child_class = safe(function() return h:GetChildClass() end)
    out.numeric_addr = safe(function() return Addr(h) end)
    if req.props ~= false then out.properties = read_props(h, tonumber(req.max_value_len) or 200, req.fields) end
    if req.children ~= false then
        local kids = children_of(h)
        local rows = {}
        for _, child in ipairs(slice(kids, offset, limit)) do
            local row = describe_handle(child)
            row.index = safe(function() return child:Index() end)
            row.no = safe(function() return child:Get("No") end)
            if req.child_fields then row.fields = read_props(child, 120, req.child_fields) end
            rows[#rows + 1] = row
        end
        out.children = {items = rows, total = #kids, offset = offset, limit = limit, truncated = offset + #rows < #kids}
    end
    if req.dump then out.dump = safe(function() return h:Dump() end) end
    return out
end

OPS.inspect = function(req)
    local h = resolve(req.target)
    if not h then return {error = "cannot resolve target: " .. tostring(req.target)} end
    return inspect_handle(h, req)
end

OPS.list = function(req)
    local handles
    if req.pattern then
        handles = safe(ObjectList, req.pattern) or {}
    elseif req.base then
        local base = resolve(req.base)
        if not base then return {error = "cannot resolve base: " .. tostring(req.base)} end
        if req.recursive then handles = walk(base, req.class, tonumber(req.cap) or 5000)
        else handles = children_of(base) end
    else
        return {error = "list needs 'pattern' (ObjectList syntax) or 'base' (address / H# id)"}
    end
    local result = paged(handles, req)
    local rows = {}
    for _, h in ipairs(result.items) do
        local row = describe_handle(h)
        row.index = safe(function() return h:Index() end)
        row.no = safe(function() return h:Get("No") end)
        if req.fields then row.fields = read_props(h, 200, req.fields) end
        rows[#rows + 1] = row
    end
    result.items = rows
    return result
end

local function grid_for_selection()
    local order, grid = {}, {}
    if type(SelectionFirst) ~= "function" then return order, grid end
    local ok, index, x, y, z = pcall(SelectionFirst)
    local guard, limit = 0, (safe(SelectionCount) or 0) + 8
    while ok and index and guard < limit do
        order[#order + 1] = index
        grid[index] = {x = x, y = y, z = z}
        guard = guard + 1
        ok, index, x, y, z = pcall(SelectionNext, index)
    end
    return order, grid
end

local function fixture_row(h, grid, req)
    local fixture = safe(function() return h:FindParent("Fixture") end) or h
    local row = {handle = describe_handle(fixture), grid = grid}
    if req.fields then row.fields = read_props(fixture, 120, req.fields)
    else row.properties = read_props(fixture, 80) end
    return row
end

OPS.fixtures = function(req)
    local scope = req.scope or "selection"
    local source, grids = {}, {}
    if scope == "selection" then
        local order, grid = grid_for_selection()
        for _, index in ipairs(order) do
            local sub = safe(GetSubfixture, index)
            if sub then
                source[#source + 1] = sub
                grids[#source] = {subfixture_index = index, x = grid[index].x, y = grid[index].y, z = grid[index].z}
            end
        end
    elseif scope == "all" then
        local root = resolve(req.root) or safe(Patch)
        if not root then return {error = "Patch() is not available"} end
        source = walk(root, req.class or "Fixture", tonumber(req.cap) or 5000)
    elseif scope:match("^group:") then
        local group = resolve("Group " .. scope:sub(7))
        if not group then return {error = "cannot resolve " .. scope} end
        for _, item in ipairs(children_of(group)) do
            source[#source + 1] = safe(function() return item:GetAssignedObj() end) or item
        end
    else
        return {error = "scope must be selection, all or group:<n>"}
    end
    local offset, limit = page_bounds(req)
    local rows = {}
    for i = offset + 1, math.min(#source, offset + limit) do
        rows[#rows + 1] = fixture_row(source[i], grids[i], req)
    end
    return {items = rows, total = #source, offset = offset, limit = limit, truncated = offset + #rows < #source, scope = scope}
end

OPS.sequences = function(req)
    local selected = safe(SelectedSequence)
    if req.target then
        local h = resolve(req.target)
        if not h then return {error = "cannot resolve target: " .. tostring(req.target)} end
        local out = inspect_handle(h, req)
        out.is_selected = selected ~= nil and safe(CompareHandle, h, selected) == true
        return out
    end
    local pool = safe(function() return DataPool().Sequences end)
    if not pool then return {error = "DataPool().Sequences is not available", selected_sequence = selected_sequence_info()} end
    local rows = {}
    for _, seq in ipairs(children_of(pool)) do
        local row = describe_handle(seq)
        row.no = safe(function() return seq:Get("No") end)
        row.cue_count = safe(function() return seq:Count() end)
        row.is_selected = selected ~= nil and safe(CompareHandle, seq, selected) == true
        rows[#rows + 1] = row
    end
    local result = paged(rows, req)
    result.selected_sequence = selected_sequence_info()
    result.current_cue = safe(function() local c = GetCurrentCue(); return c and describe_handle(c) end)
    return result
end

OPS.presets = function(req)
    local pools = safe(function() return DataPool().PresetPools end)
    if req.target then
        local h = resolve(req.target)
        if not h then return {error = "cannot resolve target: " .. tostring(req.target)} end
        -- GetPresetDataFast crashed the grandMA3 onPC 2.5.1.0 engine, so it is never called from here.
        local ok, data = pcall(GetPresetData, h, req.phasers_only == true, req.by_fixtures ~= false)
        if not ok then return {error = tostring(data)} end
        return {preset = describe_handle(h), data = data}
    end
    if req.ui_channel then
        local ok, data = pcall(GetProgPhaser, math.floor(tonumber(req.ui_channel)), req.phaser_only ~= false)
        if not ok then return {error = tostring(data)} end
        return {ui_channel = req.ui_channel, phaser = data}
    end
    if not pools then return {error = "DataPool().PresetPools is not available"} end
    if req.pool then
        local pool = resolve("PresetPool " .. tostring(req.pool))
        if not pool then
            for _, p in ipairs(children_of(pools)) do
                if handle_name(p) == tostring(req.pool) or tostring(safe(function() return p:Get("No") end)) == tostring(req.pool) then pool = p end
            end
        end
        if not pool then return {error = "cannot resolve preset pool: " .. tostring(req.pool)} end
        local rows = {}
        for _, preset in ipairs(children_of(pool)) do
            local row = describe_handle(preset)
            row.no = safe(function() return preset:Get("No") end)
            rows[#rows + 1] = row
        end
        return paged(rows, req)
    end
    local rows = {}
    for _, p in ipairs(children_of(pools)) do
        local row = describe_handle(p)
        row.no = safe(function() return p:Get("No") end)
        row.preset_count = safe(function() return p:Count() end)
        rows[#rows + 1] = row
    end
    return paged(rows, req)
end

---------------------------------------------------------------------------
-- ma3_lua: open Lua executor with undo + journaling
---------------------------------------------------------------------------

-- Names that cannot be undone by Oops. A snippet that mentions one is flagged so rollback is honest.
local NON_UNDOABLE = {
    "Keyboard", "Mouse", "Touch", "SetBlockInput", "RemoteCommand", "SendLuaMessage", "Export", "ExportJson",
    "ExportCSV", "CopyFile", "Save", "SetVar", "DelVar", "SetLED", "Import", "Load", "CreateDirectoryRecursive",
}

local function scan_flags(code)
    local flags = {}
    for _, name in ipairs(NON_UNDOABLE) do
        if code:find("%f[%w_]" .. name .. "%f[^%w_]") then flags[#flags + 1] = name end
    end
    return flags
end

local PROXY_UNDO_POSITION = {Create = 4, Append = 3, Insert = 4, Delete = 3, Remove = 3, Copy = 3, Acquire = 3, Aquire = 3}

-- Temporarily wrap the shared handle metatable so Set is journaled (old value recorded) and the
-- structural calls get the undo handle injected. Returns a restore function and whether it worked.
local function instrument(ctx)
    local sample = safe(Root)
    local mt = sample and getmetatable(sample)
    local index = mt and mt.__index
    if type(index) ~= "table" then return function() end, false end
    local restores = {}
    local function replace(name, factory)
        local original = rawget(index, name)
        if type(original) ~= "function" then return end
        index[name] = factory(original)
        restores[#restores + 1] = function() index[name] = original end
    end
    local get = rawget(index, "Get")
    replace("Set", function(original)
        return function(h, prop, value, ...)
            if type(get) == "function" then
                local ok, old = pcall(get, h, prop)
                if ok and (type(old) == "string" or type(old) == "number") then
                    ctx.set_log[#ctx.set_log + 1] = {h = handle_to_str(h), addr = safe(ToAddr, h, false), prop = prop, old = tostring(old)}
                end
            end
            return original(h, prop, value, ...)
        end
    end)
    for name, position in pairs(PROXY_UNDO_POSITION) do
        replace(name, function(original)
            return function(...)
                local packed = table.pack(...)
                if packed[position] == nil then packed[position] = ctx.undo; ctx.undo_used = true end
                return original(table.unpack(packed, 1, math.max(packed.n, position)))
            end
        end)
    end
    return function() for i = #restores, 1, -1 do pcall(restores[i]) end end, true
end

local function build_helpers(ctx)
    local ma3 = {}
    ma3.h = resolve
    ma3.safe = safe
    function ma3.cmd(command) return ctx.env.Cmd(command) end
    function ma3.set(h, prop, value) return h:Set(prop, tostring(value)) end
    function ma3.props(h, fields) return read_props(h, 200, fields) end
    function ma3.children(h) return children_of(resolve(h)) end
    function ma3.dump(h) return safe(function() return resolve(h):Dump() end) end
    function ma3.find(pattern) return safe(ObjectList, pattern) or {} end
    function ma3.walk(root, class, cap) return walk(resolve(root), class, cap) end
    function ma3.describe(h) return describe_handle(resolve(h)) end
    function ma3.fixtures(opts) return OPS.fixtures(opts or {}) end
    function ma3.log(message) ctx.output[#ctx.output + 1] = tostring(message) end
    -- Vector from `from` to `to` ({x,y,z}); azimuth is atan2(dx, dy) and elevation is relative to the XY plane, in degrees.
    -- Mapping these to Pan/Tilt depends on the fixture's mounting and rotation, so verify the axes on the show first.
    function ma3.aim(from, to)
        local dx, dy, dz = to.x - from.x, to.y - from.y, to.z - from.z
        local flat = math.sqrt(dx * dx + dy * dy)
        return {
            dx = dx, dy = dy, dz = dz,
            distance = math.sqrt(flat * flat + dz * dz),
            azimuth_deg = math.deg(math.atan(dx, dy)),
            elevation_deg = math.deg(math.atan(dz, flat)),
        }
    end
    -- Split items into `parts` groups of near-equal size ordered by key function (e.g. x position, left to right).
    function ma3.split(items, key, parts)
        local sorted = {}
        for i, item in ipairs(items) do sorted[i] = item end
        table.sort(sorted, function(a, b) return key(a) < key(b) end)
        local groups, size = {}, math.ceil(#sorted / math.max(1, parts))
        for i, item in ipairs(sorted) do
            local g = math.floor((i - 1) / size) + 1
            groups[g] = groups[g] or {}
            groups[g][#groups[g] + 1] = item
        end
        return groups
    end
    return ma3
end

local function run_lua(req)
    local code = req.code
    if type(code) ~= "string" or code == "" then return {error = "ma3_lua needs 'code'"} end
    local label = tostring(req.label or "ma3_lua"):sub(1, 60)
    local ctx = {set_log = {}, output = {}, undo_used = false, cmds = 0}
    ctx.undo = CreateUndo(PLUGIN_NAME .. ": " .. label)
    local undo_id = new_undo_id()

    local used_undo_name = code:find("%f[%w_]undo%f[^%w_]") ~= nil
    local env = setmetatable({}, {__index = _G})
    env._G = env
    env.undo = ctx.undo
    ctx.env = env
    env.ma3 = build_helpers(ctx)
    env.print = function(...)
        local parts = {}
        for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
        ctx.output[#ctx.output + 1] = table.concat(parts, "\t")
    end
    env.Cmd = function(command, u, ...) ctx.cmds = ctx.cmds + 1; ctx.undo_used = true; return Cmd(command, u or ctx.undo, ...) end
    env.CmdIndirect = function(command, u, target) ctx.cmds = ctx.cmds + 1; ctx.undo_used = true; return CmdIndirect(command, u or ctx.undo, target) end
    env.CmdIndirectWait = function(command, u, target) ctx.cmds = ctx.cmds + 1; ctx.undo_used = true; return CmdIndirectWait(command, u or ctx.undo, target) end
    env.UndoDisable = function() error("UndoDisable is blocked in ma3_lua so every change stays rollbackable") end

    local snapshots = {}
    if type(req.touches) == "table" and req.snapshot_dir then
        for i, address in ipairs(req.touches) do
            local h = resolve(address)
            local name = string.format("%s_%d", undo_id, i)
            local ok = h ~= nil and safe(function() return h:Export(req.snapshot_dir, name) end)
            snapshots[#snapshots + 1] = {target = address, h = h and handle_to_str(h) or nil, dir = req.snapshot_dir, name = name, ok = ok == true}
        end
    end

    local restore, instrumented = instrument(ctx)
    local timeout = math.min(tonumber(req.timeout_s) or 20, 120)
    local deadline = os.clock() + timeout
    local hook_ok = type(debug) == "table" and type(debug.sethook) == "function"
    if hook_ok then
        pcall(debug.sethook, function()
            if os.clock() > deadline then error("ma3_lua timed out after " .. timeout .. "s") end
        end, "", 1000000)
    end

    local chunk, load_err = load(code, "=ma3_lua", "t", env)
    local packed
    if chunk then packed = table.pack(pcall(chunk)) else packed = table.pack(false, load_err) end

    if hook_ok then pcall(debug.sethook) end
    restore()
    pcall(UndoEnable)
    local closed = pcall(CloseUndo, ctx.undo)
    local used_undo = ctx.undo_used or used_undo_name
    if used_undo then oops_log[#oops_log + 1] = undo_id end

    local response = {
        undo_id = undo_id,
        undo_used = used_undo,
        oops_steps = used_undo and 1 or 0,
        undo_closed = closed,
        instrumented = instrumented,
        flags = scan_flags(code),
        set_log = ctx.set_log,
        snapshots = snapshots,
        output = ctx.output,
        commands_run = ctx.cmds,
    }
    if packed[1] then
        local results = {}
        for i = 2, packed.n do results[#results + 1] = packed[i] end
        response.result = #results == 1 and results[1] or results
        response.completed = true
    else
        response.completed = false
        response.lua_error = tostring(packed[2])
    end
    return response
end

OPS.lua = run_lua

-- Undo what a journaled entry did: Oops (only when it is the newest plugin undo group), then
-- recorded old property values, then snapshots, then an AI-supplied inverse script.
OPS.rollback = function(req)
    local report = {}
    local steps = math.floor(tonumber(req.oops_steps) or 0)
    if steps > 0 then
        if oops_log[#oops_log] == req.undo_id or req.force_oops == true then
            local done = 0
            for _ = 1, steps do
                local ok = pcall(Cmd, "Oops")
                if ok then done = done + 1 end
            end
            if oops_log[#oops_log] == req.undo_id then table.remove(oops_log) end
            report.oops = {steps = steps, done = done}
        else
            report.oops = {skipped = "newer undo groups exist after " .. tostring(req.undo_id) .. "; roll back newer entries first or pass force_oops"}
        end
    end
    if type(req.set_log) == "table" then
        local restored, missing = 0, 0
        for i = #req.set_log, 1, -1 do
            local entry = req.set_log[i]
            local h = resolve(entry.h) or resolve(entry.addr)
            if h and safe(function() h:Set(entry.prop, entry.old); return true end) then restored = restored + 1 else missing = missing + 1 end
        end
        report.properties = {restored = restored, unresolved = missing}
    end
    if type(req.snapshots) == "table" then
        local imported, failed = 0, 0
        for _, snap in ipairs(req.snapshots) do
            local h = resolve(snap.h) or resolve(snap.target)
            if snap.ok and h and safe(function() return h:Import(snap.dir, snap.name) end) == true then imported = imported + 1 else failed = failed + 1 end
        end
        report.snapshots = {imported = imported, failed = failed}
    end
    if type(req.inverse_code) == "string" and req.inverse_code ~= "" then
        local inverse = run_lua({code = req.inverse_code, label = "rollback " .. tostring(req.undo_id)})
        report.inverse = {completed = inverse.completed, error = inverse.lua_error, undo_id = inverse.undo_id}
    end
    return report
end

---------------------------------------------------------------------------
-- Request dispatch
---------------------------------------------------------------------------

OPS.status = function()
    return {running = bridge_running, version = type(Version) == "function" and Version() or "unknown", plugin_version = PLUGIN_VERSION}
end

OPS.context = function() return selected_context() end

local function execute_op(req)
    local preview, err = preview_actions(req.actions)
    if not preview then return {ok = false, error = err} end
    if not req.confirm then return {preview = preview, requires_confirmation = true} end
    local ok, detail, undo_id = execute_actions(req.actions)
    return {completed = ok, detail = detail, undo_id = undo_id, oops_steps = 1}
end

OPS.execute = execute_op
OPS.workflow_execute = execute_op

OPS.teach_start = function(req)
    if not teach_start(req.name) then return {ok = false, error = "could not start teaching"} end
    return {name = req.name}
end

OPS.teach_record = function(req)
    if not teach_record(req.name, req.command) then return {ok = false, error = "could not record step"} end
    return {name = req.name}
end

OPS.teach_stop = function(req)
    local workflow = teach_stop(req.name)
    if not workflow then return {ok = false, error = "workflow is not being taught"} end
    return {workflow = workflow}
end

local function handle_request(request)
    if type(request) ~= "table" then return {ok = false, error = "request must be a JSON object"} end
    local op = OPS[request.op]
    if not op then return {ok = false, error = "unknown operation: " .. tostring(request.op)} end
    local ok, result = xpcall(op, function(e) return tostring(e) end, request)
    if not ok then return {ok = false, error = result} end
    if type(result) ~= "table" then return {ok = false, error = "operation returned no result"} end
    if result.ok == nil then result.ok = result.error == nil end
    return result
end

local function encode_reply(response)
    local ok, text = pcall(json_encode, response)
    if not ok then return json_encode({ok = false, error = "cannot encode response: " .. tostring(text)}) end
    if #text > MAX_REPLY then
        return json_encode({ok = false, error = "response too large (" .. #text .. " bytes); use limit/offset, fields or a narrower target"})
    end
    return text
end

local function send_all(client, data)
    local sent = 0
    while sent < #data do
        local last, err, partial = client:send(data, sent + 1)
        if last then sent = last
        elseif partial and partial > sent then sent = partial
        else return false, err end
    end
    return true
end

local function bridge_server()
    local ok_socket, socket = pcall(require, "socket")
    if not ok_socket then ErrPrintf(PLUGIN_NAME .. ": LuaSocket is unavailable"); return end
    local listener, err = socket.bind(HOST, PORT)
    if not listener then ErrPrintf(PLUGIN_NAME .. ": cannot bind bridge: " .. tostring(err)); return end
    listener:settimeout(0)
    bridge_socket = listener
    bridge_running = true
    Printf(PLUGIN_NAME .. ": MCP bridge " .. PLUGIN_VERSION .. " listening on " .. HOST .. ":" .. PORT)
    while bridge_running do
        local client = listener:accept()
        if client then
            client:settimeout(5)
            local line = client:receive("*l")
            if line then
                local ok, request = pcall(json_decode, line)
                local response = ok and handle_request(request) or {ok = false, error = "invalid JSON request"}
                send_all(client, encode_reply(response) .. "\n")
            end
            client:close()
        end
        coroutine.yield(0.05)
    end
    listener:close()
    bridge_socket = nil
    Printf(PLUGIN_NAME .. ": MCP bridge stopped")
end

local STOP_MACRO_NAME = "Stop AI Assistant"

-- Creates a macro that runs this plugin's Stop operation, unless one with that name already exists.
local function ensure_stop_macro()
    local ok, err = pcall(function()
        local macros = DataPool().Macros
        local used, slot = {}, 1
        for _, macro in ipairs(macros:Children()) do
            if macro.name == STOP_MACRO_NAME then return end
            used[tonumber(macro.no) or 0] = true
        end
        while used[slot] do slot = slot + 1 end
        local macro = macros:Create(slot)
        macro.name = STOP_MACRO_NAME
        local line = macro:Append()
        line.command = 'Plugin "' .. PLUGIN_NAME .. '" "Stop"'
        Printf(PLUGIN_NAME .. ": created macro " .. slot .. " '" .. STOP_MACRO_NAME .. "'")
    end)
    if not ok then ErrPrintf(PLUGIN_NAME .. ": could not create stop macro: " .. tostring(err)) end
end

local function main(_, argument)
    local command = tostring(argument or "Start")
    local op = command:match("^(%S+)")
    op = (op or "Start"):lower()
    if op == "start" then if not bridge_running then ensure_stop_macro(); return bridge_server() end
    elseif op == "stop" then bridge_running = false
    else ErrPrintf(PLUGIN_NAME .. ": unknown operation " .. op) end
end

-- Host-side tests load this file with GMA3_AI_TEST set and drive the pieces directly.
if rawget(_G, "GMA3_AI_TEST") then
    return {main = main, handle_request = handle_request, json_encode = json_encode, json_decode = json_decode,
            oops_log = oops_log, encode_reply = encode_reply}
end

return main
