#!/usr/bin/env python3
"""Run a Luau snippet in the running Roblox Studio session via the MCP server
(`execute_luau`) and print the result - the generic live-testing runner.

Usage:
  python scripts/luau_run.py snippet.luau               # play Server VM
  python scripts/luau_run.py --client snippet.luau      # play Client VM
  python scripts/luau_run.py --edit snippet.luau        # edit datamodel
  python scripts/luau_run.py --code "return 1 + 1"
  python scripts/luau_run.py --console 50 snippet.luau  # also tail console

Notes:
- Return a string (`return table.concat(t, '\\n')`); it is printed as-is.
- Prefer the Client VM for live state: requiring game modules in the MCP VM
  yields a private copy (separate require cache), not the play session's
  modules. Drive real flows through your game's remotes from the Client VM.
- Play-created instances revert on stop; DataStore writes may persist.
  Destroy temporary instances and clean up persistent fixtures explicitly.
"""
import argparse
import repo_config
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rbx_studio import StudioMCP
import sync_to_studio as syncmod


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--client", action="store_true", help="run in the play Client VM")
    g.add_argument("--edit", action="store_true", help="run in the Edit datamodel")
    ap.add_argument("--code", default=None, help="Luau snippet instead of a file")
    ap.add_argument("--console", type=int, default=0, metavar="N",
                    help="also print the last N Studio console lines")
    ap.add_argument("--studio-id", help="override the Studio instance id")
    ap.add_argument("file", nargs="?", help=".luau file with the snippet")
    args = ap.parse_args()

    if args.code is not None:
        code = args.code
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            code = f.read()
    else:
        ap.error("give a snippet file or --code")

    dm = "Edit" if args.edit else ("Client" if args.client else "Server")
    client = StudioMCP()
    client.start()
    try:
        sid, _ = syncmod.resolve_studio(client, repo_config.place_name(), override=args.studio_id)
        res = client.call("execute_luau",
                          {"studio_id": sid, "datamodel_type": dm, "code": code},
                          timeout=120)
        text = res if isinstance(res, str) else str(res)
        print(text if text != "" else "(no return value)")
        if args.console:
            con = client.call("get_console_output",
                              {"studio_id": sid, "limit": args.console}, timeout=60)
            print("--- console (last %d lines) ---" % args.console)
            print(con.strip())
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
