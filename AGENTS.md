# Roblox Studio project

This repository starts as an empty Roblox project. Add project-specific context
here as the experience develops. Read [README.md](README.md) for setup and commands.

## Repository and source of truth

- `src/`: durable Luau sources; no gameplay code is included initially.
- `src/manifest.json`: maps source files to Studio instance paths and classes.
  Source folders are independent of the Studio tree and runtime requires.
- `config.json`: shared, non-secret Studio target, Open Cloud destination and
  asset-pack settings. Keep persistent repository parameters here.
- `scripts/`: Studio MCP wrappers, Open Cloud asset tooling, analysis, formatting
  and existing tooling checks.
- `docs/ROBLOX_ASSETS.md`: approved asset IDs and their provenance.
- `unimported/`: local source assets; ignored by Git.
- `artifacts/`: local output, captures and downloaded tools; ignored by Git.

Preserve unrelated working changes. Do not copy credentials, generated session
state, another experience's IDs or game-specific scripts into this template.

## Source changes and Studio installation

- Author durable changes in `src/` and add manifest entries for new scripts.
  Use explicit `path` and `class` fields; classes are `Script`, `LocalScript`
  or `ModuleScript`.
- Run `python -X utf8 scripts/analyze.py --strict` after source changes and keep
  findings at zero. Use luau-lsp with the bundled `scripts/globalTypes.d.lua`
  and generated sourcemap; do not substitute syntax-only parsers.
- Use `python -X utf8 scripts/format.py --check` to check source formatting.
  Run without `--check` to format; `--install` installs the pinned formatter.
- In Edit mode, run `python -X utf8 scripts/sync_to_studio.py`. Writes are read
  back and verified. `--check` reports drift: inspect output as well as exit code.
  `--file NAME` limits scope; ordinary sync skips unchanged sources.
- After intentionally removing a source and its manifest entry, use `--prune`
  to delete its tracked installed instance. Plain sync does not delete scripts.
- Mirror existing Studio-only scripts into `src/` and the manifest before
  treating changes to them as durable. Direct `Script.Source` edits are not a
  replacement for repository sources. Save the Studio place after syncing.
- Transfer source using escaped or long-bracket Luau strings. Do not use
  `HttpService:Base64Decode` or round-trip source through truncated tool output.

## Studio interaction and verification

- Use `config.json`'s `studio.place_name` to select the experience. List active
  instances with `sync_to_studio.py --list`; use `--studio-id` where supported
  for a temporary override. Never commit session UUIDs.
- Use `python -X utf8` for wrappers so Unicode output works on Windows.
- MCP `require` uses a private cache, separate from gameplay modules. For live
  state, use the game's remotes from the Client VM. For isolated checks, clone
  modules before requiring changed source; a Source assignment alone does not
  clear an existing cache.
- Keep Luau snippets bounded, return short summaries, and use deadlines and
  `task.wait` for live waits. Clean up temporary instances explicitly.
- Rely on the user for interactive playtesting. A scripted check or capture
  does not establish that the user has verified appearance or gameplay.
- Play-created instances revert on stop; DataStore/profile writes may persist.
  Do not reset persistent data to hide failures. Report persistent mutations.
- Do not write unit tests, test fixtures or verification scripts unless the
  user specifically asks. Existing offline tooling checks may be run when
  changing the wrappers. Use the optional live test runner only when requested.

## Assets, UI and credentials

You should rely on the user for manual testing/verification, unless instructed
otherwise. This means no using `capture_studio.py`, or in-game testing, unless
necessary. Static analysis should still be ran after code changes.

- Use `capture_studio.py` when a reference screenshot is needed. For substantial
  UI changes, use the current UI as a reference for an image-generated design
  when available, then implement it. Keep interface text concise.
- Keep approved asset IDs, source licensing and verification evidence in
  `docs/ROBLOX_ASSETS.md`; retain the import journal after uploads.
- Inspect imported models for unwanted scripts before using them in gameplay.
- Open Cloud credentials belong in the `ROBLOX_STUDIO_API_KEY` environment
  variable or local `.env`, never in `config.json`, source, logs or commits.
  Only the main agent may use the key; subagents must not read or use it.
- Do not purchase anything through Open Cloud. Preserve the upload tool's
  zero-price restriction and creator checks. Review the offline import plan
  before running an authorized upload.
