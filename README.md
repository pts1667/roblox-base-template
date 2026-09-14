# Roblox base template

An empty source repository with Python tools for Roblox Studio MCP and the
Roblox Open Cloud asset API, adapted from MMO Tycoon. Author Luau locally and
sync it into an open Studio place through a manually maintained manifest.

## Setup

1. Install Python 3.11 or newer. The Python scripts use only the standard library.
2. On Windows, open your experience in Roblox Studio and enable Studio as an MCP
   server in Assistant's MCP settings. The default launcher is
   `%LOCALAPPDATA%\Roblox\mcp.bat`; the wrapper requires this Windows launcher.
3. Set `studio.place_name` in [config.json](config.json) to the name reported by
   `python -X utf8 scripts/sync_to_studio.py --list` (omit a trailing
   ` (placeId: ...)` annotation). An empty name selects the sole connected
   instance, or a still-connected cached session. Set a name for reliable targeting.
4. Install `luau-lsp` on PATH before analyzing source files. Roblox type definitions
   are included. For formatting, run `python -X utf8 scripts/format.py --install`
   to download the pinned StyLua 2.5.0 Windows binary, or provide that version
   on PATH / through the `STYLUA` environment variable.

Run commands below from the repository root. Studio MCP commands require Studio
to be open with its server enabled; offline analysis and asset planning do not.

## Repository configuration

`config.json` holds settings shared across the repository. Credentials stay in
an environment variable or local `.env` (see [.env.example](.env.example)).

| Setting | Purpose |
| --- | --- |
| `studio.place_name` | Target Studio experience name; initially empty. |
| `studio.mcp_launcher` | Optional launcher path, absolute or relative to this repo. Empty uses the Windows default. |
| `open_cloud.creator` | Asset owner, e.g. `{"userId": "YOUR_USER_ID"}` or `{"groupId": "YOUR_GROUP_ID"}`. |
| `open_cloud.place_id` | Numeric place ID stored as a string, used for asset verification. |
| `open_cloud.universe_id` | Numeric universe ID stored as a string, used for verification and asset permissions. |
| `open_cloud.verification_asset_id` | Existing asset ID owned by the destination creator, used to verify ownership before authenticated operations. |
| `assets.packs` | Optional per-folder asset catalog title, intended use and import priority. |

The empty Open Cloud settings must be filled before using the import workflow.
CLI `--studio-id` overrides Studio selection where supported; asset commands
accept `--destination FILE` to override the `open_cloud` object for one run.
Session IDs and installed-script tracking are local, ignored files.

For optional pack settings, use a folder name under `unimported/` as the key:

```json
{
  "MyPack": {
    "title": "My Pack",
    "intended_use": "World scenery",
    "priority": 1
  }
}
```

Place that object inside `assets.packs`. Without overrides, the folder name is
the asset-document heading and priority is 3. Lower numbers are processed first.

## Add and sync source

[src/manifest.json](src/manifest.json) starts with `{"files": {}}`. Add source
files under `src/` and register each file relative to that directory:

```json
{
  "files": {
    "Main.server.luau": {
      "path": "ServerScriptService/Main",
      "class": "Script"
    },
    "shared/Config.luau": {
      "path": "ReplicatedStorage/Config",
      "class": "ModuleScript"
    },
    "Main.client.luau": {
      "path": "StarterPlayer/StarterPlayerScripts/Main",
      "class": "LocalScript"
    }
  }
}
```

This is an example; create the corresponding files before adding those entries.
Paths start with a Studio service, use `/`, and omit `game`. Intermediate folders
are created during sync. The local folder layout need not match the Studio tree.

```powershell
python -X utf8 scripts/analyze.py --strict
python -X utf8 scripts/format.py --check
python -X utf8 scripts/sync_to_studio.py --check
python -X utf8 scripts/sync_to_studio.py
```

Sync in Edit mode and save the place in Studio afterward. Sync verifies source
readback and skips unchanged files. Use `--file Main.server.luau` for one file.
`--check` prints drift; its exit code alone does not indicate whether drift exists.
After removing a source and manifest entry, `--prune` removes its tracked Studio
instance. Normal sync leaves unlisted scripts alone.

## Studio tools

| Script | Purpose / example |
| --- | --- |
| `sync_to_studio.py` | Sync, inspect drift, list instances and prune removed sources. |
| `studio_play.py` | `start`, `stop`, `status`, `console`, or `run --time 30`. |
| `luau_run.py` | Execute a file or `--code "return game.Name"`; select `--edit`, `--client`, or default Play Server. |
| `capture_studio.py` | Save the current viewport in `artifacts/captures/`; optional output and camera arguments. |
| `generate_materials.py` | Generate MaterialVariants via `--name`, `--description` and `--base-material`, or `--batch FILE`. |
| `rbx_studio.py` | Shared MCP client with deadlines and image-result support. |
| `test_runner.py` | Optional live runner for user-requested `scripts/test/*.luau` fixtures; none are included. |
| `analyze.py` / `format.py` | Local source analysis and formatting. Analysis supports `.lua` and `.luau` and succeeds on empty `src/`. |

Use `--help` on a command script for its options. Live test fixtures return a
string starting with `ok`; `_prelude.luau` provides cleanup and a fresh module
helper for direct modules in selected folders. Adapt its dependency selection
to your game's module graph before relying on it for cross-folder requires.
Play-created instances revert on stop, but DataStore writes may persist.

## Open Cloud asset workflow

1. Put source packs and their license evidence under `unimported/<pack>/`.
2. Record any existing approved IDs in [docs/ROBLOX_ASSETS.md](docs/ROBLOX_ASSETS.md)
   to reuse them. Configure `open_cloud` in `config.json`.
3. Run the offline inventory and review the import plan:

   ```powershell
   python -X utf8 scripts/asset_inventory.py
   python -X utf8 scripts/asset_import.py plan
   ```

4. For authenticated commands, set `ROBLOX_STUDIO_API_KEY` in your environment
   or copy `.env.example` to `.env` and fill it locally. The key needs access to
   the operations you invoke. An authorized upload uses
   `python -X utf8 scripts/asset_import.py upload --pack MyPack --limit 5`.

The importer handles free static Model uploads from supported glTF/GLB/FBX
sources, checks licensing and dependencies, and journals operations for resume
and reconciliation. It requests a zero price. Its automatic pre-upload inventory
reconciliation currently supports user creators; group uploads need adaptation.
Skinned models and animations are blocked by this static-model workflow.

`asset_verify.py --edit --out artifacts/assets-edit.json` checks uploaded models
in Studio. For gameplay-client content verification, install its temporary client
fixture using `--install-client` in Edit, start Play, then run with `--out` and
without `--edit`. Stop Play and use `--remove-client` afterward. Verification
reports are separate from the journal; use `asset_import.py record-models` for
Edit reports or `record-verification` for gameplay reports, with `--report FILE`.
Use `--help` for batch selection, resume, metadata and permission commands.

## Tooling checks and template scope

Run the copied offline wrapper checks without connecting to Studio or Open Cloud:

```powershell
python -X utf8 -m unittest discover -s scripts -p "test_*.py"
```

See [AGENTS.md](AGENTS.md) for agent authoring and verification rules. This template
includes no gameplay code, imported assets, credentials, cached Studio sessions,
animation converter or rig assets. MMO Tycoon's live-agent diagnostics, building
exporter and historical asset-report generator depend on that game and are omitted.
