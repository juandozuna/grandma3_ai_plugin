"""Enforce console /noconfirm at the MCP transport boundary."""

import re
from copy import deepcopy


def console_command(command: str) -> str:
    """Normalize each unquoted semicolon/newline-delimited command."""
    def finish(text):
        # Protect quoted labels, Lua and macro command text from option parsing.
        masked = []
        quote = None
        escaped = False
        for char in text:
            if quote:
                masked.append(' ')
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
                masked.append(' ')
            else:
                masked.append(char)
        visible = ''.join(masked)
        # An explicit /confirm must not override the enforced policy.
        for match in reversed(list(re.finditer(r'(?i)(?<!\S)/confirm(?=\s|$)', visible))):
            text = text[:match.start()] + text[match.end():]
        text = text.rstrip()
        if not text.strip() or re.search(r'(?i)(?<!\S)/noconfirm(?=\s|$)', visible):
            return text
        return text + ' /noconfirm'

    result, current = [], []
    quote = None
    escaped = False
    for char in command:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
            current.append(char)
        elif char in ';\n':
            result.extend((finish(''.join(current)), char))
            current = []
        else:
            current.append(char)
    result.append(finish(''.join(current)))
    return ''.join(result)


# The plugin executes snippets in a per-request environment whose _G points to
# that environment. Wrapping it preserves the plugin's existing undo wrappers
# and does not modify the console's actual global Cmd functions.
LUA_PREFIX = r'''-- MCP console /noconfirm policy
do
    local function normalize(command)
        if type(command) ~= 'string' then return command end
        local output, part, quote, escaped = {}, {}, nil, false
        local function finish()
            local text = table.concat(part)
            local visible, q, esc = {}, nil, false
            for i = 1, #text do
                local c = text:sub(i,i)
                if q then
                    visible[#visible+1] = ' '
                    if esc then esc = false
                    elseif c == '\\' then esc = true
                    elseif c == q then q = nil end
                elseif c == '"' or c == "'" then q = c; visible[#visible+1] = ' '
                else visible[#visible+1] = c end
            end
            local mask = table.concat(visible):lower()
            local positions = {}
            for start, token, stop in mask:gmatch('()(/[%a]+)()') do
                local before = start == 1 or mask:sub(start-1,start-1):match('%s')
                local after = stop > #mask or mask:sub(stop,stop):match('%s')
                if before and after and token == '/confirm' then
                    positions[#positions+1] = {start,stop}
                end
            end
            for i = #positions,1,-1 do
                local p = positions[i]; text = text:sub(1,p[1]-1)..text:sub(p[2])
            end
            text = text:gsub('%s+$','')
            local has_option = (' '..mask..' '):find('%s/noconfirm%s')
            if text:match('%S') and not has_option then text = text..' /noconfirm' end
            output[#output+1] = text; part = {}
        end
        for i = 1,#command do
            local c = command:sub(i,i)
            if quote then
                part[#part+1] = c
                if escaped then escaped = false
                elseif c == '\\' then escaped = true
                elseif c == quote then quote = nil end
            elseif c == '"' or c == "'" then quote = c; part[#part+1] = c
            elseif c == ';' or c == '\n' then finish(); output[#output+1] = c
            else part[#part+1] = c end
        end
        finish()
        return table.concat(output)
    end
    for _, name in ipairs({'Cmd','CmdIndirect','CmdIndirectWait'}) do
        local original = _G[name]
        if type(original) == 'function' then
            _G[name] = function(command, ...) return original(normalize(command), ...) end
        end
    end
end
'''


def lua_code(code: str) -> str:
    if not code or code.startswith(LUA_PREFIX):
        return code
    return LUA_PREFIX + code


def prepare_request(request):
    """Copy requests so workflow definitions and caller arguments stay intact."""
    prepared = deepcopy(request)
    for action in prepared.get('actions', []):
        if action.get('type') == 'cmd' and isinstance(action.get('command'), str):
            action['command'] = console_command(action['command'])
    if prepared.get('op') == 'lua' and isinstance(prepared.get('code'), str):
        prepared['code'] = lua_code(prepared['code'])
    if isinstance(prepared.get('inverse_code'), str):
        prepared['inverse_code'] = lua_code(prepared['inverse_code'])
    if prepared.get('op') == 'teach_record' and isinstance(prepared.get('command'), str):
        prepared['command'] = console_command(prepared['command'])
    return prepared
