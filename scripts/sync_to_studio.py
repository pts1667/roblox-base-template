#!/usr/bin/env python3
"""Sync src/*.lua|*.luau scripts into the open Roblox Studio place.

Driven by src/manifest.json (manually maintained): each file maps to an
instance path in the Studio datamodel. Uses the official Roblox Studio MCP
server (stdio, %LOCALAPPDATA%\\Roblox\\mcp.bat - see rbx_studio.py).

If Studio has been (re)started since the last sync, the cached studio_id goes
stale; the script detects that, re-lists connected Studio instances and
reconnects to the one matching the config.json's studio.place_name.

Usage:
  python scripts/sync_to_studio.py              # sync all manifest files
  python scripts/sync_to_studio.py --check      # report drift, write nothing
  python scripts/sync_to_studio.py --file Main.server.luau
  python scripts/sync_to_studio.py --show-unchanged
  python scripts/sync_to_studio.py --list       # show connected Studio instances
  python scripts/sync_to_studio.py --studio-id <uuid>
  python scripts/sync_to_studio.py --prune      # sync, then destroy Studio
                           # instances this tool installed that are no longer
                           # in the manifest (never touches untracked scripts)

Every successful install is recorded in scripts/.studio-session.json (per
Studio instance); --prune only ever removes instances recorded there.
"""
import argparse
import base64
import json
import repo_config
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rbx_studio import StudioMCP

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(ROOT, "src")
STATE_PATH = os.path.join(HERE, ".studio-session.json")

WRITE_LUAU = """local pathParts = string.split('__PATH__', '/')
local rootName = table.remove(pathParts, 1)
local parent
local ok = pcall(function() parent = game:GetService(rootName) end)
if not ok or not parent then parent = game:FindFirstChild(rootName) end
assert(parent, 'cannot resolve root instance: ' .. rootName)
for i = 1, #pathParts - 1 do
\tlocal seg = pathParts[i]
\tlocal child = parent:FindFirstChild(seg)
\tif not child then
\t\tchild = Instance.new('Folder')
\t\tchild.Name = seg
\t\tchild.Parent = parent
\tend
\tparent = child
end
local name = pathParts[#pathParts]
local src = __SRC__
assert(#src == __LENGTH__, 'payload truncated: got ' .. #src .. ' expected __LENGTH__')
local existing = parent:FindFirstChild(name)
if existing and (not existing:IsA('LuaSourceContainer') or existing.ClassName ~= '__CLASS__') then
\texisting:Destroy()
\texisting = nil
end
local action
if existing then
\taction = 'updated'
else
\texisting = Instance.new('__CLASS__')
\taction = 'created'
end
existing.Name = name
existing.Source = src
existing.Parent = parent
return action .. ' ' .. parent:GetFullName() .. '/' .. name .. ' (' .. #src .. ' chars)'
"""


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def default_class(file_name):
    if ".server." in file_name:
        return "Script"
    if ".client." in file_name:
        return "LocalScript"
    return "ModuleScript"


def game_path(instance_path):
    return "game." + instance_path.replace("/", ".")


def resolve_studio(client, place_name, override=None, retries=8, delay=1.5):
    """Return (studio_id, note). A freshly spawned StudioMCP.exe bridge needs
    ~1 s before the running Studio attaches to it (calls before that fail
    with 'Unable to reach Roblox Studio right now' or an empty instance
    list), so the handshake is waited out first. The cached studio_id is
    then verified against the attached instances - it goes stale when Studio
    was restarted - and otherwise reconnects by place name."""
    studios = client.wait_for_studios(timeout=10, poll=0.5)
    if not studios:
        raise SystemExit(
            "No Roblox Studio instances connected to the MCP bridge.\n"
            "Open Studio, enable the MCP server (Assistant > ... >\n"
            "Manage MCP Servers > Enable Studio as MCP server), then retry.\n"
            "If the toggle was just enabled, restart Studio once.")

    cache = load_json(STATE_PATH, {})
    candidates = []
    if override:
        candidates.append((override, "--studio-id"))
    elif cache.get("studio_id") and (not place_name or any(
            s.get("id") == cache["studio_id"] and re.sub(
                r" \(placeId: [0-9]+\)\Z", "", s.get("name") or "") == place_name
            for s in studios)):
        candidates.append((cache["studio_id"], "cached session"))

    attached = {s.get("id") for s in studios}
    for sid, source in candidates:
        if sid not in attached:
            if override:
                raise SystemExit("Requested --studio-id is not connected: %s" % override)
            continue  # stale: Studio restarted since the last sync
        try:
            client.call("get_studio_state", {"studio_id": sid}, timeout=20)
            return sid, source
        except Exception as err:
            if override:
                raise SystemExit("Requested --studio-id is not responding: %s" % override) from err
            print("cached studio_id %s not responding (%s); re-listing..."
                  % (sid, str(err).strip()[:100]))

    last_err = None
    for _ in range(retries):
        try:
            studios = client.list_studios()
        except Exception as err:
            last_err = err
            time.sleep(delay)
            continue
        if not studios:
            last_err = RuntimeError("no Studio instances attached yet")
            time.sleep(delay)
            continue
        exact = [s for s in studios if s.get("name") == place_name]
        if not exact:
            # Published places include this metadata in Studio's display name.
            exact = [s for s in studios if re.sub(
                r" \(placeId: [0-9]+\)\Z", "", s.get("name") or "") == place_name]
        if len(exact) == 1:
            set_studio(exact[0]["id"], exact[0].get("name", ""))
            return exact[0]["id"], "reconnected by place name %r" % place_name
        if len(studios) == 1 and not place_name:
            set_studio(studios[0]["id"], studios[0].get("name", ""))
            return studios[0]["id"], "only connected instance"
        raise SystemExit(
            "Cannot select a unique Studio instance for configured place name %r.\n"
            "Set studio.place_name in config.json or use --studio-id:\n%s"
            % (place_name, "\n".join(
                "  %s  %s" % (s.get("id"), s.get("name")) for s in studios)))
    raise SystemExit("Roblox Studio MCP unreachable (%s).\n"
                     "If Studio was (re)started recently, give it a few seconds "
                     "and retry." % last_err)


def set_studio(studio_id, name):
    state = load_json(STATE_PATH, {})
    state["studio_id"] = studio_id
    state["name"] = name
    save_state(state)


def record_installed(studio_id, instance_path, file_name):
    state = load_json(STATE_PATH, {})
    per_studio = state.setdefault("installed", {}).setdefault(studio_id, {})
    per_studio[instance_path] = file_name
    save_state(state)


def drop_installed(studio_id, instance_paths):
    state = load_json(STATE_PATH, {})
    per_studio = state.get("installed", {}).get(studio_id, {})
    for path in instance_paths:
        per_studio.pop(path, None)
    save_state(state)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def build_write_code(instance_path, cls, data):
    if "'" in instance_path or "'" in cls:
        raise ValueError("path/class must not contain single quotes")
    code = WRITE_LUAU
    code = code.replace("__PATH__", instance_path)
    code = code.replace("__CLASS__", cls)
    code = code.replace("__SRC__", luau_quote(data))
    code = code.replace("__LENGTH__", str(len(data)))
    return code
def luau_quote(data):
    """Encode bytes as a Luau string literal (no API calls needed in Studio)."""
    out = ["'"]
    for b in data:
        c = chr(b)
        if c == "'":
            out.append("\\'")
        elif c == "\\":
            out.append("\\\\")
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif 0x20 <= b < 0x7F:
            out.append(c)
        else:
            out.append("\\%03d" % b)
    out.append("'")
    return "".join(out)


def parse_script_read(text):
    """Parse 'LINE<arrow>CONTENT' output into a 1-indexed line list."""
    hits = {}
    for m in re.finditer(r"(?m)^\s*(\d+)→(.*)$", text):
        val = m.group(2)
        if val.endswith("\r"):
            val = val[:-1]
        hits[int(m.group(1))] = val
    if not hits:
        return None
    top = max(hits)
    lines = [hits.get(i, "") for i in range(1, top + 1)]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def local_lines(data):
    lines = data.decode("utf-8").replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def first_diff(studio_lines, src_lines):
    for i, (a, b) in enumerate(zip(studio_lines, src_lines), 1):
        if a != b:
            return i, a, b
    if len(studio_lines) != len(src_lines):
        n = min(len(studio_lines), len(src_lines))
        a = studio_lines[n] if n < len(studio_lines) else "<end>"
        b = src_lines[n] if n < len(src_lines) else "<end>"
        return n + 1, a, b
    return None


def read_studio_source(client, sid, instance_path):
    text = client.call("script_read", {"target_file": game_path(instance_path)}, timeout=60)
    return parse_script_read(text)


def check_file(client, sid, name, instance_path, data):
    try:
        studio_lines = read_studio_source(client, sid, instance_path)
    except Exception as err:
        if re.search(r"not find|not exist|not found|could not", str(err), re.I):
            return "NEW FILE (would create on sync)"
        raise
    diff = first_diff(studio_lines, local_lines(data))
    if diff is None:
        return "in sync (%d lines)" % len(local_lines(data))
    line, studio_val, src_val = diff
    return "DRIFT at line %d\n    studio: %s\n    src:    %s" % (
        line, studio_val[:120], src_val[:120])


def sync_file(client, sid, name, instance_path, cls, data, force=False,
              show_unchanged=False):
    try:
        studio_lines = read_studio_source(client, sid, instance_path)
    except Exception as err:
        if not re.search(r"not find|not exist|not found|could not", str(err), re.I):
            raise
        change = "NEW FILE"
    else:
        if first_diff(studio_lines, local_lines(data)) is None:
            if not force:
                if show_unchanged:
                    return "UNCHANGED %s (already in sync; --force to rewrite)" % name
                return None
            change = "UNCHANGED (rewritten via --force)"
        else:
            change = "MODIFIED"
    code = build_write_code(instance_path, cls, data)
    client.call("execute_luau",
                {"code": code, "datamodel_type": "Edit", "studio_id": sid},
                timeout=120)
    # verify: read back and compare line-for-line
    studio_lines = read_studio_source(client, sid, instance_path)
    diff = first_diff(studio_lines, local_lines(data))
    if diff is not None:
        raise RuntimeError("post-sync verify failed at line %d: studio=%r src=%r"
                           % (diff[0], diff[1][:80], diff[2][:80]))
    record_installed(sid, instance_path, name)
    return "%s %s -> %s; verified (%d lines)" % (
        change, name, instance_path, len(local_lines(data)))


DESTROY_LUAU = """local pathParts = string.split('__PATH__', '/')
local rootName = table.remove(pathParts, 1)
local parent
local ok = pcall(function() parent = game:GetService(rootName) end)
if not ok or not parent then parent = game:FindFirstChild(rootName) end
assert(parent, 'cannot resolve root instance: ' .. rootName)
for i = 1, #pathParts - 1 do
\tlocal child = parent:FindFirstChild(pathParts[i])
\tif not child then
\t\treturn 'already gone (parent missing)'
\tend
\tparent = child
end
local inst = parent:FindFirstChild(pathParts[#pathParts])
if not inst then
\treturn 'already gone'
end
local desc = inst.ClassName .. ' ' .. inst.Name
inst:Destroy()
return 'destroyed ' .. desc
"""


def prune_stale(client, sid, manifest_paths):
    """Destroy Studio instances this tool installed that are no longer in the
    manifest. Only instances recorded in the state file are ever touched."""
    installed = load_json(STATE_PATH, {}).get("installed", {}).get(sid, {})
    stale = sorted((path, file_name) for path, file_name in installed.items()
                   if path not in manifest_paths)
    if not stale:
        print("nothing to prune")
        return
    for path, file_name in stale:
        code = DESTROY_LUAU.replace("__PATH__", path)
        try:
            result = client.call("execute_luau",
                                 {"code": code, "datamodel_type": "Edit",
                                  "studio_id": sid}, timeout=60).strip()
        except Exception as err:
            print("%-40s PRUNE FAILED: %s" % (path, str(err).strip()[:160]))
            continue
        print("PRUNED %s -> %s (%s)" % (file_name, path, result))
        drop_installed(sid, [path])


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=os.path.join(SRC_DIR, "manifest.json"))
    ap.add_argument("--file", action="append",
                    help="sync only this source file (repeatable)")
    ap.add_argument("--check", action="store_true",
                    help="compare src against Studio without writing")
    ap.add_argument("--list", action="store_true",
                    help="list connected Studio instances and exit")
    ap.add_argument("--studio-id", help="target this Studio instance explicitly")
    ap.add_argument("--prune", action="store_true",
                    help="after syncing, destroy Studio instances installed by "
                         "this tool that are no longer in the manifest")
    ap.add_argument("--force", action="store_true",
                    help="rewrite even when the Studio copy already matches")
    ap.add_argument("--show-unchanged", action="store_true",
                    help="show files that already match Studio")
    args = ap.parse_args()

    client = None
    for attempt in range(3):
        try:
            client = StudioMCP()
            client.start()
            break
        except Exception as err:
            if client:
                client.close()
            client = None
            if attempt == 2:
                raise SystemExit("Could not start Roblox Studio MCP server: %s" % err)
            time.sleep(2)

    if args.list:
        try:
            studios = client.wait_for_studios(timeout=10, poll=0.5)
            if not studios:
                raise SystemExit("No Roblox Studio instances attached.\n"
                                 "(keep Studio open with the MCP server enabled, "
                                 "then retry)")
            for s in studios:
                print("%s  %s" % (s.get("id"), s.get("name")))
            return 0
        finally:
            client.close()

    try:
        manifest = load_json(args.manifest, None)
        if not manifest or "files" not in manifest:
            raise SystemExit("manifest not found or invalid: %s" % args.manifest)
        place_name = repo_config.place_name()

        sid, how = resolve_studio(client, place_name, override=args.studio_id)
        print("Studio: %s (%s)" % (sid, how))

        if args.check and args.prune:
            raise SystemExit("--prune performs deletions and cannot be combined "
                             "with --check")
        if args.prune:
            manifest_paths = {e.get("path") for e in manifest["files"].values()
                              if e.get("path")}
            prune_stale(client, sid, manifest_paths)

        wanted = set(args.file or [])
        rc = 0
        matched = set()
        for name in sorted(manifest["files"]):
            if wanted and not any(name == w or name.endswith("/" + w)
                                  for w in wanted):
                continue
            matched.update(w for w in wanted
                           if name == w or name.endswith("/" + w))
            entry = manifest["files"][name]
            local_path = os.path.join(SRC_DIR, name)
            if not os.path.isfile(local_path):
                print("%-24s MISSING local file" % name)
                rc = 1
                continue
            with open(local_path, "rb") as f:
                data = f.read()
            instance_path = entry["path"]
            cls = entry.get("class") or default_class(name)
            try:
                if args.check:
                    status = check_file(client, sid, name, instance_path, data)
                    print("%-24s %s" % (name, status))
                else:
                    status = sync_file(client, sid, name, instance_path, cls, data,
                                       force=args.force,
                                       show_unchanged=args.show_unchanged)
                    if status is not None:
                        print(status)
            except Exception as err:
                print("%-24s FAILED: %s" % (name, str(err).strip()))
                rc = 1
        for w in sorted(wanted - matched):
            print("warning: --file %s matched no manifest entry" % w)
            rc = 1
        return rc
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
