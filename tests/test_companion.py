import json
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
import companion_server


class McpServerTests(unittest.TestCase):
    def setUp(self):
        companion_server.WORKFLOWS.clear()

    def test_initialize(self):
        response = companion_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(response["result"]["serverInfo"]["name"], "grandma3-onpc-bridge")

    def test_tools_list_exposes_show_operations(self):
        response = companion_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("ma3_context", names)
        self.assertIn("ma3_execute", names)
        self.assertIn("ma3_teach_stop", names)

    def test_workflow_save_is_local_to_mcp_process(self):
        response = companion_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ma3_workflow_save", "arguments": {"name": "demo", "actions": [{"type": "cmd", "command": "Go+"}]}}})
        self.assertIn("saved", response["result"]["content"][0]["text"])
        self.assertEqual(companion_server.WORKFLOWS["demo"]["actions"][0]["command"], "Go+")

    def test_tool_call_forwards_to_plugin(self):
        original = companion_server.plugin_request
        companion_server.plugin_request = lambda request: {"ok": True, "request": request, "running": True}
        try:
            response = companion_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "ma3_status", "arguments": {}}})
        finally:
            companion_server.plugin_request = original
        self.assertIn("running", response["result"]["content"][0]["text"])

    def test_initialize_carries_console_knowledge(self):
        response = companion_server.handle({"jsonrpc": "2.0", "id": 5, "method": "initialize"})
        self.assertIn("Store Cue 1 Thru 5", response["result"]["instructions"])

    def test_notes_persist(self):
        import tempfile, os
        original = companion_server.NOTES_PATH
        with tempfile.TemporaryDirectory() as tmp:
            companion_server.NOTES_PATH = os.path.join(tmp, "notes.json")
            try:
                response = companion_server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "ma3_notes", "arguments": {"add": "quirk"}}})
                self.assertIn("quirk", response["result"]["content"][0]["text"])
                self.assertIn("quirk", companion_server.instructions())
            finally:
                companion_server.NOTES_PATH = original


class JournalTests(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (companion_server.JOURNAL_PATH, companion_server.SNAPSHOT_DIR, companion_server.plugin_request)
        companion_server.JOURNAL_PATH = os.path.join(self.tmp.name, "journal.json")
        companion_server.SNAPSHOT_DIR = os.path.join(self.tmp.name, "snaps")
        self.sent = []

    def tearDown(self):
        companion_server.JOURNAL_PATH, companion_server.SNAPSHOT_DIR, companion_server.plugin_request = self.saved
        self.tmp.cleanup()

    def call(self, name, arguments):
        response = companion_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return response["result"]

    def fake_plugin(self, replies):
        def fake(request):
            self.sent.append(request)
            reply = replies.get(request["op"], {"ok": True})
            if isinstance(reply, Exception):
                raise reply
            return dict(reply)
        companion_server.plugin_request = fake

    def test_read_tools_forward_arguments(self):
        self.fake_plugin({"inspect": {"ok": True, "properties": {}}})
        self.call("ma3_inspect", {"target": "Group 1", "limit": 5})
        self.assertEqual(self.sent[0], {"target": "Group 1", "limit": 5, "op": "inspect"})

    def test_tool_list_has_new_read_and_lua_tools(self):
        names = {t["name"] for t in companion_server.TOOLS}
        for expected in ("ma3_api", "ma3_inspect", "ma3_list", "ma3_fixtures", "ma3_sequences", "ma3_presets", "ma3_lua", "ma3_journal", "ma3_rollback"):
            self.assertIn(expected, names)
        self.assertEqual(len(names), len(companion_server.TOOLS))

    def test_lua_is_journaled_with_strategy(self):
        self.fake_plugin({"lua": {"ok": True, "undo_id": "u1", "oops_steps": 1, "set_log": [{"h": "H#1", "prop": "Name", "old": "A"}], "snapshots": [], "flags": [], "completed": True}})
        result = self.call("ma3_lua", {"code": "return 1", "label": "demo"})
        body = json.loads(result["content"][0]["text"])
        self.assertEqual(body["journal_id"], 1)
        self.assertEqual(body["rollback_strategy"], "oops+property-restore")
        self.assertEqual(companion_server.load_journal()[0]["code"], "return 1")

    def test_lua_with_touches_requests_snapshots(self):
        self.fake_plugin({"lua": {"ok": True, "undo_id": "u1", "oops_steps": 0, "snapshots": [{"ok": True, "target": "Group 1"}], "completed": True}})
        self.call("ma3_lua", {"code": "return 1", "touches": ["Group 1"]})
        self.assertEqual(self.sent[0]["snapshot_dir"], companion_server.SNAPSHOT_DIR)
        self.assertEqual(companion_server.load_journal()[0]["strategy"], "snapshot")

    def test_execute_confirm_is_journaled_but_preview_is_not(self):
        self.fake_plugin({"execute": {"ok": True, "completed": True, "undo_id": "u2", "oops_steps": 1}})
        self.call("ma3_execute", {"actions": [{"type": "cmd", "command": "Go+"}], "confirm": False})
        self.assertEqual(companion_server.load_journal(), [])
        self.call("ma3_execute", {"actions": [{"type": "cmd", "command": "Go+"}], "confirm": True})
        self.assertEqual(len(companion_server.load_journal()), 1)

    def test_rollback_newest_first_and_marks_entries(self):
        self.fake_plugin({"lua": {"ok": True, "undo_id": "u1", "oops_steps": 1, "completed": True},
                          "rollback": {"ok": True, "oops": {"steps": 1, "done": 1}}})
        self.call("ma3_lua", {"code": "a=1"})
        self.fake_plugin({"lua": {"ok": True, "undo_id": "u2", "oops_steps": 1, "completed": True},
                          "rollback": {"ok": True, "oops": {"steps": 1, "done": 1}}})
        self.call("ma3_lua", {"code": "b=1"})
        result = self.call("ma3_rollback", {"last": 2})
        reports = json.loads(result["content"][0]["text"])["rolled_back"]
        self.assertEqual([r["id"] for r in reports], [2, 1])
        self.assertTrue(all(e["rolled_back"] for e in companion_server.load_journal()))

    def test_partial_rollback_is_not_reported_clean(self):
        self.fake_plugin({"lua": {"ok": True, "undo_id": "u1", "oops_steps": 1, "completed": True},
                          "rollback": {"ok": True, "oops": {"skipped": "newer undo groups exist"}}})
        self.call("ma3_lua", {"code": "a=1"})
        result = self.call("ma3_rollback", {"id": 1})
        report = json.loads(result["content"][0]["text"])["rolled_back"][0]
        self.assertFalse(report["clean"])
        self.assertFalse(companion_server.load_journal()[0]["rolled_back"])

    def test_rollback_with_nothing_recorded_says_so(self):
        self.fake_plugin({"lua": {"ok": True, "undo_id": "u1", "oops_steps": 0, "completed": True}})
        self.call("ma3_lua", {"code": "return 1"})
        report = json.loads(self.call("ma3_rollback", {"id": 1})["content"][0]["text"])["rolled_back"][0]
        self.assertEqual(report["strategy"], "none")
        self.assertEqual([r["op"] for r in self.sent if r["op"] == "rollback"], [])

    def test_unknown_operation_hint(self):
        original = companion_server.socket.create_connection
        class Fake:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def sendall(self, data): pass
            def recv(self, n): return b'{"ok":false,"error":"unknown operation: api"}\n'
        companion_server.socket.create_connection = lambda *a, **k: Fake()
        try:
            result = self.call("ma3_api", {"query": "Cmd"})
        finally:
            companion_server.socket.create_connection = original
        self.assertTrue(result["isError"])
        self.assertIn("ReloadAllPlugins", result["content"][0]["text"])

    def test_api_drift_compares_with_index(self):
        import os
        index = os.path.join(self.tmp.name, "api.json")
        with open(index, "w") as handle:
            json.dump({"fn": ["Cmd", "OldFn"], "obj": [], "source": "test"}, handle)
        saved = companion_server.API_INDEX_PATH
        companion_server.API_INDEX_PATH = index
        try:
            drift = companion_server.api_drift(["fn:Cmd", "fn:NewFn"])
        finally:
            companion_server.API_INDEX_PATH = saved
        self.assertEqual(drift["only_live"], ["fn:NewFn"])
        self.assertEqual(drift["only_in_index"], ["fn:OldFn"])


if __name__ == "__main__":
    unittest.main()
