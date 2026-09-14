#!/usr/bin/env python3
"""Static analysis over src/.

Preferred backend: luau-lsp analyze with the Roblox definitions file
(scripts/globalTypes.d.lua) - full Roblox API typing, no noise.
Fallback (no defs file): plain luau-analyze, filtering the 'Unknown global
CFrame'-style noise that a Roblox-less typechecker reports for every Roblox
built-in; the signal that remains is the real lint set (undefined project
globals - the pre-declaration nil-call crash class, unused vars, shadowing).

Usage: python scripts/analyze.py [--defs=FILE] [--strict] [files...]
  --defs=FILE  definitions file override (default: scripts/globalTypes.d.lua,
               env ROBLOX_LUAU_DEFS)
  --strict     exit 1 on any remaining finding (default: report only)
"""
import json
import repo_config
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
MANIFEST = os.path.join(SRC, 'manifest.json')
SOURCEMAP = os.path.join(ROOT, 'scripts', '.luau-sourcemap.json')

SERVICE_CLASSES = {
    'ReplicatedStorage': 'ReplicatedStorage',
    'ServerScriptService': 'ServerScriptService',
    'ServerStorage': 'ServerStorage',
    'StarterPlayer': 'StarterPlayer',
    'StarterPlayerScripts': 'StarterPlayerScripts',
    'StarterGui': 'StarterGui',
    'Workspace': 'Workspace',
    'Lighting': 'Lighting',
    'Players': 'Players',
}


def build_sourcemap():
    """Rojo-style DataModel sourcemap from src/manifest.json so luau-lsp can
    resolve script.X / WaitForChild requires back to src files."""
    with open(MANIFEST, encoding='utf-8') as fh:
        manifest = json.load(fh)
    root = {'name': repo_config.place_name() or 'RobloxProject', 'className': 'DataModel', 'children': []}
    for file, spec in manifest['files'].items():
        node = root
        parts = spec['path'].split('/')
        for i, part in enumerate(parts):
            children = node.setdefault('children', [])
            child = next((c for c in children if c['name'] == part), None)
            if child is None:
                last = i == len(parts) - 1
                child = {
                    'name': part,
                    'className': spec['class'] if last
                    else SERVICE_CLASSES.get(part, 'Folder'),
                }
                if last:
                    child['filePaths'] = ['src/' + file.replace('\\', '/')]
                children.append(child)
            node = child
    with open(SOURCEMAP, 'w', encoding='utf-8') as fh:
        json.dump(root, fh)
    return SOURCEMAP

# Roblox/Luau built-ins a Roblox-less luau-analyze cannot know (fallback only)
ROBLOX_GLOBALS = """
game workspace script shared plugin Instance Vector2 Vector3 CFrame UDim UDim2
Ray Region3 Rect Faces Axes Color3 ColorSequence ColorSequenceKeypoint
NumberRange NumberSequence NumberSequenceKeypoint TweenInfo Random DateTime
Font Content buffer bit32 utf8 task coroutine Enum tick wait spawn delay
elapsedTime time typeof SharedTable Players RunService ReplicatedStorage
ServerStorage ServerScriptService Lighting TweenService HttpService
UserInputService ContextActionService GuiService AssetService InsertService
DataStoreService RaycastParams newproxy loadstring
""".split()

UNKNOWN_GLOBAL = re.compile(r"Unknown global '(\w+)'")
ROBLOX_TYPES = {'Vector3', 'Vector2', 'CFrame', 'Color3', 'UDim', 'UDim2',
                'Instance', 'Enum', 'Random', 'TweenInfo', 'NumberRange',
                'NumberSequence', 'ColorSequence', 'buffer', 'Ray',
                'RaycastParams', 'Content'}


def find_defs(explicit):
    candidates = [explicit, os.environ.get('ROBLOX_LUAU_DEFS'),
                  os.path.join(ROOT, 'scripts', 'globalTypes.d.lua'),
                  os.path.join(ROOT, 'scripts', 'globalTypes.d.luau')]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def collect_files(args):
    files = args
    if not files:
        for root, _dirs, names in os.walk(SRC):
            for n in sorted(names):
                if n.endswith(('.lua', '.luau')):
                    files.append(os.path.join(root, n).replace('\\', '/'))
    return files


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return (proc.stdout + proc.stderr).splitlines()


def filter_roblox_noise(lines):
    kept = []
    for line in lines:
        m = UNKNOWN_GLOBAL.search(line)
        if m and m.group(1) in ROBLOX_GLOBALS:
            continue
        if any(("Unknown type '%s'" % t) in line for t in ROBLOX_TYPES):
            continue
        if 'TypeError: Type inference failed to complete' in line:
            continue
        if line.startswith('[INFO]') or line.startswith('[WARN]'):
            continue  # luau-lsp's own log lines, not findings
        kept.append(line)
    return kept


def main():
    defs_arg = strict = None
    files_arg = []
    for a in sys.argv[1:]:
        if a in ('-h', '--help'):
            print(__doc__)
            return
        if a == '--strict':
            strict = True
        elif a.startswith('--defs='):
            defs_arg = a[len('--defs='):]
        else:
            files_arg.append(a)

    files = collect_files(files_arg)
    if not files:
        build_sourcemap()
        print('analyze: 0 findings in 0 files (src/ is empty)')
        return
    defs = find_defs(defs_arg)

    if defs:
        smap = build_sourcemap()
        lines = run(['luau-lsp', 'analyze',
                     '--definitions=' + defs,
                     '--platform=roblox',
                     '--sourcemap=' + smap] + files)
        backend = 'luau-lsp (--definitions=%s, sourcemap from manifest.json)' \
            % os.path.relpath(defs, ROOT)
        kept = filter_roblox_noise(lines)  # safety net only; defs should cover it
    else:
        lines = run(['luau-analyze'] + files)
        backend = 'luau-analyze (no definitions file)'
        kept = filter_roblox_noise(lines)
        print('hint: drop the Roblox definitions at scripts/globalTypes.d.lua '
              'for full Roblox typing (luau-lsp analyze --definitions=...)')

    for line in kept:
        print(line)
    print('analyze[%s]: %d finding(s) in %d file(s)' % (backend, len(kept), len(files)))
    sys.exit(1 if (strict and kept) else 0)


if __name__ == '__main__':
    main()
