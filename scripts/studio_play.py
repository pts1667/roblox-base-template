#!/usr/bin/env python3
"""Drive play mode in the open Roblox Studio place (test helper).

Wraps the Studio MCP tools (start_stop_play / get_studio_state /
get_console_output) on top of rbx_studio.py so a test cycle needs no MCP
client tooling - just this script. Studio connection reuses sync_to_studio's
resolver (cached studio_id, stale-session re-listing by place name).

Usage:
  python scripts/studio_play.py                    # start, wait 30s, stop
  python scripts/studio_play.py run --time 60      # start, wait 60s, stop
  python scripts/studio_play.py start              # start the game
  python scripts/studio_play.py stop               # stop the game
  python scripts/studio_play.py status             # is the game running?
  python scripts/studio_play.py console            # dump Studio console output
  python scripts/studio_play.py console --limit 200
  python scripts/studio_play.py ... --studio-id <uuid>   # override instance

Exit code 0 on success, 1 on failure (harness friendly).
"""
import argparse
import repo_config
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_to_studio as syncmod
from rbx_studio import StudioMCP

ROOT = syncmod.ROOT


def load_mode_text(text):
    """Extract the mode token from get_studio_state output (or None)."""
    m = re.search(r"Current Studio Mode:\s*([^\n]+)", text or "")
    return m.group(1).strip() if m else None


def is_running(mode):
    return bool(mode) and mode.lower().startswith("play")


def wait_for_mode(client, sid, want_running, timeout, poll=1.0):
    """Poll get_studio_state until the mode matches; returns (ok, mode)."""
    deadline = time.time() + timeout
    mode = None
    while time.time() < deadline:
        try:
            mode = load_mode_text(client.call(
                "get_studio_state", {"studio_id": sid}, timeout=20))
            if mode is not None and is_running(mode) == want_running:
                return True, mode
        except Exception:
            pass  # transient MCP hiccups during the transition; keep polling
        time.sleep(poll)
    return False, mode


def cmd_start(client, sid, args):
    try:
        client.call("start_stop_play",
                    {"studio_id": sid, "is_start": True}, timeout=60)
    except Exception as err:
        print("start call failed (%s); polling state anyway..." % str(err).strip())
    ok, mode = wait_for_mode(client, sid, True, args.timeout)
    if ok:
        print("game running (mode: %s)" % mode)
        return 0
    print("game did not enter Play within %ss (mode: %s); "
          "check Studio for a modal dialog and start Play manually"
          % (args.timeout, mode or "unknown"))
    return 1


def cmd_stop(client, sid, args):
    try:
        client.call("start_stop_play",
                    {"studio_id": sid, "is_start": False}, timeout=60)
    except Exception as err:
        print("stop call failed (%s); polling state anyway..." % str(err).strip())
    ok, mode = wait_for_mode(client, sid, False, args.timeout)
    if ok:
        print("game stopped (mode: %s)" % mode)
        return 0
    print("game still running after %ss (mode: %s)" % (args.timeout, mode or "unknown"))
    return 1


def cmd_status(client, sid, args):
    mode = load_mode_text(client.call("get_studio_state", {"studio_id": sid},
                                      timeout=20))
    if mode is None:
        print("could not read Studio state")
        return 1
    print("running" if is_running(mode) else "stopped")
    return 0


def cmd_run(client, sid, args):
    print("== starting game ==")
    rc = cmd_start(client, sid, args)
    if rc:
        return rc
    print("== running for %ss ==" % args.time)
    time.sleep(args.time)
    print("== stopping game ==")
    return cmd_stop(client, sid, args)


def cmd_console(client, sid, args):
    text = client.call("get_console_output",
                       {"studio_id": sid, "limit": args.limit}, timeout=60)
    print(text.strip())
    return 0


def cmd_tests(client, sid, args):
    """Run the pure-logic unit tests (scripts/test/) in the Edit datamodel."""
    import test_runner
    files = test_runner.collect_tests(args.pattern)
    if not files:
        print("no test files matched %r" % args.pattern)
        return 1
    return test_runner.run_tests(client, sid, files, verbose=args.verbose)


COMMANDS = {
    "run": cmd_run,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "console": cmd_console,
    "tests": cmd_tests,
}


def main():
    ap = argparse.ArgumentParser(
        description="Start/wait/stop Roblox Studio play mode via the MCP server.")
    ap.add_argument("command", nargs="?", default="run",
                    choices=sorted(COMMANDS),
                    help="run = start, wait --time, stop (default)")
    ap.add_argument("--time", type=float, default=30.0,
                    help="seconds to run before stopping (run only, default 30)")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="seconds to wait for start/stop transitions")
    ap.add_argument("--limit", type=int, default=100,
                    help="console lines to print (console only, default 100)")
    ap.add_argument("--pattern", default=None,
                    help="substring match on test file names (tests only)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print test return values (tests only)")
    ap.add_argument("--studio-id", default=None,
                    help="override the Studio instance id")
    args = ap.parse_args()

    place_name = repo_config.place_name()

    client = StudioMCP()
    client.start()
    try:
        sid, how = syncmod.resolve_studio(client, place_name,
                                          override=args.studio_id)
        print("Studio: %s (%s)" % (sid, how))
        return COMMANDS[args.command](client, sid, args)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
