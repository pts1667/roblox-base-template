"""Deadline and fail-fast checks without a live Studio process."""
import contextlib
import io
import json
import os
import queue
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from rbx_studio import StudioMCP
from test_runner import run_tests


class StudioDeadlineTests(unittest.TestCase):
    def client(self):
        client = object.__new__(StudioMCP)
        client._id = 0
        client._broken = False
        client._lines = queue.Queue()
        client._send = Mock()
        return client

    def test_no_eof_still_times_out_and_connection_is_not_reused(self):
        client = self.client()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            client._rpc("tools/call", {}, 0.02)
        self.assertLess(time.monotonic() - start, 1)
        client._lines.put(json.dumps({"id": 1, "result": {}}).encode())
        with self.assertRaises(ConnectionError):
            client._rpc("tools/call", {}, 1)
        self.assertEqual(client._send.call_count, 1)

    def test_eof_is_distinct_from_timeout(self):
        client = self.client()
        client._lines.put(None)
        with self.assertRaisesRegex(ConnectionError, "closed stdout"):
            client._rpc("tools/call", {}, 1)

    def test_notifications_and_noise_do_not_hide_response(self):
        client = self.client()
        for line in (b"noise", b"[]", b'{"method":"notice"}',
                     b'{"id":1,"result":{"content":[]}}'):
            client._lines.put(line)
        self.assertEqual(client._rpc("tools/call", {}, 1), {"content": []})

    def test_suite_stops_on_error_and_flushes_progress(self):
        client = Mock()
        client.call.side_effect = TimeoutError("Studio suspended")
        with tempfile.TemporaryDirectory() as directory:
            fixture = os.path.join(directory, "fixture.luau")
            with open(fixture, "w", encoding="utf-8") as handle:
                handle.write("return 'ok'")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(run_tests(client, "id", [fixture, fixture], timeout=0.1, datamodel="Edit"), 1)
            self.assertEqual(client.call.call_count, 1)
            self.assertIn("1 not run", output.getvalue())
            code = client.call.call_args.args[1]["code"]
            self.assertNotIn("assert(testOk", code)
            self.assertIn("pcall(cleanupTestModules)", code)

    def test_server_fixture_checks_mode_before_execution(self):
        client = Mock()
        client.call.return_value = "Current Studio Mode: Edit"
        with tempfile.TemporaryDirectory() as directory:
            fixture = os.path.join(directory, "fixture.luau")
            with open(fixture, "w", encoding="utf-8") as handle:
                handle.write("-- Test VM: Server\nreturn 'ok'")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_tests(client, "id", [fixture]), 1)
            self.assertEqual(client.call.call_count, 1)
            self.assertEqual(client.call.call_args.args[0], "get_studio_state")
            client.call.side_effect = ["Current Studio Mode: Play", "ok"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_tests(client, "id", [fixture]), 0)
            self.assertEqual(client.call.call_args.args[1]["datamodel_type"], "Server")


if __name__ == "__main__":
    unittest.main()
