#!/usr/bin/env python3
"""Run the pure-logic unit tests in scripts/test/ against the live place.

Each scripts/test/*.luau is a self-contained fixture executed through Studio
MCP. Auto mode selects Edit when stopped, or Server in Play. Fixtures marked
'-- Test VM: Server' require Play for scheduled tasks. A test passes when it
returns a string starting with 'ok'; any Luau error fails it. Fixtures use
private module graphs and isolated state; they must clean up their resources.

Remember: don't use this unless the user specifically requests for it!

Usage:
  python scripts/test_runner.py              # run every scripts/test/*.luau
  python scripts/test_runner.py growth       # suffix-match one file
  python scripts/test_runner.py -v           # print each snippet's return
Stops on the first failure: Studio may suspend execution when an error occurs.
Exit code 0 when all pass, 1 otherwise (harness friendly).
"""
import argparse
import repo_config
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_to_studio as syncmod
from rbx_studio import StudioMCP
from studio_play import is_running, load_mode_text

TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")
PRELUDE = os.path.join(TEST_DIR, "_prelude.luau")


def collect_tests(pattern=None):
    # files starting with '_' are helpers (e.g. _prelude.luau), not tests
    names = sorted(n for n in os.listdir(TEST_DIR)
                   if n.endswith(".luau") and not n.startswith("_"))
    if pattern:
        names = [n for n in names if pattern in n]
    return [os.path.join(TEST_DIR, n) for n in names]


def run_tests(client, sid, files, verbose=False, timeout=60, datamodel="auto"):
    with open(PRELUDE, "r", encoding="utf-8") as fh:
        prelude = fh.read()
    failures = 0
    attempted = 0
    server_checked = False
    automatic_vm = None
    for path in files:
        name = os.path.basename(path)
        attempted += 1
        print("RUN  %s" % name, flush=True)
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
            needs_server = "-- Test VM: Server" in source.splitlines()[:5]
            vm = datamodel
            code = (prelude + "\nlocal testOk, testResult = pcall(function()\n"
                    + source + "\nend)\n"
                    + "local cleanOk, cleanError = pcall(cleanupTestModules)\n"
                    + "if not cleanOk then return 'FAIL cleanup: ' .. tostring(cleanError) end\n"
                    + "if not testOk then return 'FAIL test: ' .. tostring(testResult) end\n"
                    + "return testResult")
        try:
            if vm == "auto":
                if automatic_vm is None:
                    state = client.call("get_studio_state", {"studio_id": sid}, timeout=10)
                    mode = load_mode_text(state)
                    if mode is None:
                        raise RuntimeError("Could not determine the current Studio mode")
                    automatic_vm = "Server" if is_running(mode) else "Edit"
                    server_checked = automatic_vm == "Server"
                vm = automatic_vm
            if needs_server and vm == "Edit":
                raise RuntimeError("This fixture requires the running Server VM; use --datamodel Server in Play")
            if vm == "Server" and not server_checked:
                state = client.call("get_studio_state", {"studio_id": sid}, timeout=10)
                if not is_running(load_mode_text(state)):
                    raise RuntimeError("Server fixtures require Play; start Studio before running this suite")
                server_checked = True
            res = client.call("execute_luau",
                              {"studio_id": sid, "datamodel_type": vm,
                               "code": code}, timeout=timeout)
            text = (res if isinstance(res, str) else str(res)).strip()
        except Exception as err:
            text = "error: %s" % str(err).strip()
        if text.startswith("ok"):
            print("PASS %s%s" % (name, ("  -> " + text) if verbose else ""), flush=True)
        else:
            failures += 1
            print("FAIL %s\n     %s" % (name, text.replace("\n", "\n     ")), flush=True)
            print("Stopped after failure; verify Studio is not paused and clean up "
                  "the failed fixture before retrying.", flush=True)
            break
    total = len(files)
    print("%d/%d test file(s) passed; %d not run" %
          (attempted - failures, total, total - attempted), flush=True)
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pattern", nargs="?", default=None,
                    help="substring match on test file names")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print each snippet's return value")
    ap.add_argument("--studio-id", default=None,
                    help="override the Studio instance id")
    ap.add_argument("--timeout", type=float, default=60,
                    help="per-test RPC deadline in seconds (default: 60)")
    ap.add_argument("--datamodel", choices=("auto", "Edit", "Server"), default="auto",
                    help="auto selects Edit when stopped or Server in Play; server-only fixture markers are enforced")
    args = ap.parse_args()
    if not 0 < args.timeout <= 600:
        ap.error("--timeout must be greater than zero and at most 600")

    files = collect_tests(args.pattern)
    if not files:
        print("no test files matched %r in scripts/test/" % args.pattern)
        return 1

    client = StudioMCP()
    client.start()
    try:
        sid, _ = syncmod.resolve_studio(
            client, repo_config.place_name(),
            override=args.studio_id)
        return run_tests(client, sid, files, verbose=args.verbose, timeout=args.timeout,
                         datamodel=args.datamodel)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
