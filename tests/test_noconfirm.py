import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import companion_server
import noconfirm


class NoConfirmTests(unittest.TestCase):
    def test_batches_quotes_and_existing_options(self):
        cases = {
            'Store Preset 4.1 /merge': 'Store Preset 4.1 /merge /noconfirm',
            'Copy Group 1 At 2; Delete Group 3': 'Copy Group 1 At 2 /noconfirm; Delete Group 3 /noconfirm',
            'Store Cue 1\nStore Cue 2': 'Store Cue 1 /noconfirm\nStore Cue 2 /noconfirm',
            'Label Group 1 "A; /noconfirm"': 'Label Group 1 "A; /noconfirm" /noconfirm',
            'Label Group 1 "A\\\";B"': 'Label Group 1 "A\\\";B" /noconfirm',
            'Store Cue 1 /NoConfirm': 'Store Cue 1 /NoConfirm',
            'Store Cue 1 /confirm /merge': 'Store Cue 1  /merge /noconfirm',
            'Store Cue 1 /confirm /noconfirm': 'Store Cue 1  /noconfirm',
            ' ;\n': ';\n',
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(noconfirm.console_command(command), expected)
                self.assertEqual(noconfirm.console_command(expected), expected)

    def test_transport_normalizes_without_changing_confirm_or_arguments(self):
        class Socket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def sendall(self, data): self.request = json.loads(data)
            def recv(self, count): return b'{"ok":true}\n'
        client = Socket()
        for op in ('execute', 'workflow_execute'):
            for confirm in (False, True):
                request = {'op': op, 'confirm': confirm, 'actions': [
                    {'type': 'cmd', 'command': 'Store Preset 4.1 /merge; Label Preset 4.1 "Red"'},
                    {'type': 'wait', 'seconds': 0.1}]}
                with patch.object(companion_server.socket, 'create_connection', return_value=client):
                    companion_server.plugin_request(request)
                self.assertEqual(client.request['confirm'], confirm)
                self.assertEqual(client.request['actions'][0]['command'].count('/noconfirm'), 2)
                self.assertNotIn('/noconfirm', request['actions'][0]['command'])
                self.assertEqual(client.request['actions'][1], request['actions'][1])

    def test_lua_runtime_handles_dynamic_commands_and_helpers(self):
        lua = shutil.which('lua')
        self.assertIsNotNone(lua, 'Lua is required to verify command wrappers')
        commands = ['Store Cue 1; Store Cue 2', 'Label Group 1 "A; /noconfirm"',
                    'Label Group 1 "A\\\";B"', 'Store Cue 1 /confirm',
                    'Store Cue 1 /NoConfirm', 'Store Cue 1\nStore Cue 2']
        harness = '''
local sent={}
local function capture(command, undo, target)
    sent[#sent+1]=command
    assert(undo=='UNDO' and target=='TARGET')
    return 'RESULT'
end
local env=setmetatable({Cmd=capture,CmdIndirect=capture,CmdIndirectWait=capture},{__index=_G})
env._G=env
env.ma3={cmd=function(command,u,t) return env.Cmd(command,u,t) end}
'''
        # JSON strings are valid Lua literals for these command characters.
        code = 'local commands={' + ','.join(json.dumps(c) for c in commands) + '}\n'
        code += '''for _,c in ipairs(commands) do
for _,f in ipairs({Cmd,CmdIndirect,CmdIndirectWait,ma3.cmd}) do
assert(f(c,'UNDO','TARGET')=='RESULT')
end end'''
        source = noconfirm.lua_code(code)
        delimiter = '[====[' + source + ']====]'
        harness += f"assert(load({delimiter},'test','t',env))()\n"
        harness += "for _,s in ipairs(sent) do io.write(s:gsub('\\n','<NL>'),'\\n') end\n"
        result = subprocess.run([lua, '-'], input=harness, text=True, capture_output=True, check=True)
        expected = [noconfirm.console_command(c).replace('\n','<NL>') for c in commands for _ in range(4)]
        self.assertEqual(result.stdout.splitlines(), expected)
        self.assertEqual(noconfirm.lua_code(source), source)

    def test_inverse_lua_and_teaching_are_normalized(self):
        inverse = noconfirm.prepare_request({'op': 'rollback', 'inverse_code': 'Cmd("Delete Group 9")'})
        self.assertTrue(inverse['inverse_code'].startswith(noconfirm.LUA_PREFIX))
        self.assertEqual(noconfirm.prepare_request({'op':'teach_record','command':'Copy Group 1 At 2'})['command'],
                         'Copy Group 1 At 2 /noconfirm')


if __name__ == '__main__':
    unittest.main()
