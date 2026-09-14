"""Minimal MCP stdio client for the official Roblox Studio MCP server.

Speaks JSON-RPC 2.0 (newline-delimited) with the server Roblox Studio exposes
via %LOCALAPPDATA%\\Roblox\\mcp.bat. Enable it in Studio under
Assistant > ... > Manage MCP Servers > Enable Studio as MCP server.

Reference: https://create.roblox.com/docs/studio/mcp
"""
import json
import repo_config
import os
import queue
import re
import subprocess
import threading
import time


class StudioMCP:
    def __init__(self, bat_path=None, timeout=180.0):
        self.bat = bat_path or repo_config.mcp_launcher() or os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Roblox", "mcp.bat")
        if not os.path.isfile(self.bat):
            raise FileNotFoundError(
                "Roblox Studio MCP launcher not found: %s\n"
                "In Studio: Assistant > ... > Manage MCP Servers > "
                "'Enable Studio as MCP server'." % self.bat)
        self.timeout = timeout
        self.proc = None
        self._id = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        self.proc = subprocess.Popen(
            ["cmd.exe", "/c", self.bat],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self._lines = queue.Queue()
        self._broken = False
        # A cmd.exe child can keep stdout open after its parent is killed.
        # Read off-thread so the RPC deadline never depends on pipe EOF.
        self._reader = threading.Thread(
            target=self._read_output, args=(self.proc.stdout, self._lines),
            daemon=True)
        self._reader.start()
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "roblox-studio-sync", "version": "1.0"},
        }, 30)
        self._notify("notifications/initialized")

    def call(self, name, arguments, timeout=None):
        """Invoke a tool; returns the concatenated text content."""
        return _content_text(self.call_result(name, arguments, timeout))

    def call_result(self, name, arguments, timeout=None):
        """Invoke a tool and preserve all content, including inline images."""
        result = self._rpc("tools/call",
                           {"name": name, "arguments": arguments},
                           self.timeout if timeout is None else timeout)
        if result.get("isError"):
            raise RuntimeError("%s tool error: %s" % (name, _content_text(result)))
        return result

    def list_studios(self):
        return parse_studio_list(self.call("list_roblox_studios", {}, timeout=30))

    def wait_for_studios(self, timeout=10.0, poll=0.5):
        """Poll list_roblox_studios until at least one Studio instance is
        attached to this bridge, then return the list (possibly empty on
        timeout). A freshly spawned StudioMCP.exe needs about a second
        before the running Studio discovers it; calls made before that fail
        with 'Unable to reach Roblox Studio right now' or return an empty
        instance list - both are retried here."""
        deadline = time.time() + timeout
        studios = []
        while True:
            try:
                studios = self.list_studios()
                if studios:
                    return studios
            except Exception:
                pass  # bridge not attached yet; keep polling
            if time.time() >= deadline:
                return studios
            time.sleep(poll)

    # -- protocol plumbing ------------------------------------------------

    def _notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    @staticmethod
    def _read_output(stream, lines):
        try:
            for line in iter(stream.readline, b""):
                lines.put(line)
        finally:
            lines.put(None)

    def _rpc(self, method, params, timeout):
        if self._broken:
            raise ConnectionError("MCP connection failed; create a new client")
        self._id += 1
        rid = self._id
        deadline = time.monotonic() + timeout
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                self._broken = True
                raise TimeoutError("%s timed out after %.0fs; Studio execution may still be suspended"
                                   % (method, timeout)) from None
            if line is None:
                self._broken = True
                raise ConnectionError("MCP server closed stdout")
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # skip non-JSON noise
            if isinstance(msg, dict) and msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError("%s RPC error: %s" % (method, msg["error"]))
                return msg["result"]

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           capture_output=True, timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


def _content_text(result):
    return "\n".join(p.get("text", "") for p in result.get("content", [])
                     if isinstance(p, dict) and p.get("type") == "text")


def parse_studio_list(text):
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except ValueError:
        pass
    studios = []
    for chunk in re.findall(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and "id" in obj:
            studios.append(obj)
    return studios
