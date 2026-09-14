"""Verify journaled Models inside the intended Studio experience, without credentials or save writes.

Run in Play Server (default) for runtime evidence; --edit is a preliminary load check.
The tester may run this script. It writes a separate report, never the import journal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import repo_config
from pathlib import Path
import uuid

from asset_import import ROOT, now, read_json, write_json
from rbx_studio import StudioMCP
import sync_to_studio as syncmod


def input_code(entries, destination):
    # JSON is embedded in a delimiter that cannot occur in the serialized payload.
    payload = json.dumps({"entries": entries, "destination": destination, "id": uuid.uuid4().hex}, ensure_ascii=True)
    delimiter = "="
    while "]" + delimiter + "]" in payload:
        delimiter += "="
    return "local input = game:GetService('HttpService'):JSONDecode([" + delimiter + "[" + payload + "]" + delimiter + "])\n"


def snippet(entries, destination):
    return input_code(entries, destination) + r'''
local expected = input.destination
assert(tostring(game.PlaceId) == expected.place_id, 'Wrong place')
assert(tostring(game.GameId) == expected.universe_id, 'Wrong universe')
assert(tostring(game.CreatorId) == tostring(expected.creator.userId or expected.creator.groupId), 'Wrong creator')
assert(game.CreatorType.Name == (expected.creator.userId and 'User' or 'Group'), 'Wrong creator type')
local results = {}
for _, entry in ipairs(input.entries) do
    local row = {key=entry.key, asset_id=entry.asset_id, fingerprint=entry.fingerprint, passed=false}
    local done, expired, loaded = false, false, nil
    task.spawn(function()
        local ok, failure = pcall(function()
            loaded = game:GetService('InsertService'):LoadAsset(tonumber(entry.asset_id))
            if expired then loaded:Destroy(); loaded=nil; return end
            assert(loaded and loaded:IsA('Model'), 'LoadAsset did not return a Model')
            local scripts, parts, meshes, bones, content = 0, 0, {}, 0, {}
            local function add(id)
                if type(id) == 'string' and id ~= '' then content[id] = true end
            end
            for _, node in ipairs(loaded:GetDescendants()) do
                if node:IsA('LuaSourceContainer') then scripts += 1 end
                if node:IsA('BasePart') then parts += 1 end
                if node:IsA('Bone') then bones += 1 end
                if node:IsA('MeshPart') then
                    meshes[node.MeshId] = true
                    add(node.MeshId); add(node.TextureID)
                elseif node:IsA('SpecialMesh') then
                    add(node.MeshId); add(node.TextureId)
                elseif node:IsA('SurfaceAppearance') then
                    add(node.ColorMap); add(node.MetalnessMap); add(node.NormalMap); add(node.RoughnessMap)
                elseif node:IsA('Decal') or node:IsA('Texture') then
                    add(node.Texture)
                end
            end
            row.parts, row.scripts, row.bones = parts, scripts, bones
            assert(scripts == 0, 'Unexpected scripts in uploaded asset')
            assert(parts > 0, 'Model has no geometry')
            local frame, size = loaded:GetBoundingBox()
            row.size = {size.X, size.Y, size.Z}
            row.pivot = {loaded:GetPivot():GetComponents()}
            row.bounds = {frame:GetComponents()}
            assert(size.Magnitude > 0 and size.Magnitude < 10000, 'Invalid model dimensions')
            row.mesh_ids = {}
            for id in pairs(meshes) do table.insert(row.mesh_ids, id) end
            table.sort(row.mesh_ids)
            row.content_ids = {}
            for id in pairs(content) do table.insert(row.content_ids, id) end
            table.sort(row.content_ids)
            row.passed = true
        end)
        if not ok then row.error = tostring(failure):sub(1, 500) end
        if loaded then loaded:Destroy(); loaded=nil end
        done = true
    end)
    local deadline = os.clock() + 12
    repeat task.wait(0.05) until done or os.clock() >= deadline
    if not done then
        expired = true
        if loaded then loaded:Destroy(); loaded=nil end
        row.passed, row.error = false, 'Model load deadline exceeded; late result is destroyed'
    end
    table.insert(results, row)
end
return game:GetService('HttpService'):JSONEncode({
    place_id=tostring(game.PlaceId), universe_id=tostring(game.GameId),
    creator_id=tostring(game.CreatorId), running=game:GetService('RunService'):IsRunning(), results=results
})
'''


def client_snippet(entries, destination):
    return input_code(entries, destination) + r'''
local http = game:GetService('HttpService')
local gui = game:GetService('Players').LocalPlayer:WaitForChild('PlayerGui', 5)
local fixture = gui and gui:WaitForChild('_RobloxAssetVerify', 5)
assert(fixture, 'Install the gameplay client fixture in Edit before starting Play')
fixture.Request.Value = http:JSONEncode(input)
local deadline = os.clock() + 100
repeat
    task.wait(0.05)
    if fixture.Response.Value ~= '' then
        local result = http:JSONDecode(fixture.Response.Value)
        assert(not result.error, result.error)
        if result.id == input.id then return http:JSONEncode(result.rows) end
    end
until os.clock() >= deadline
error('Gameplay client fixture response deadline exceeded')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=ROOT / "docs/ASSET_IMPORT_RESULTS.json")
    parser.add_argument("--destination", type=Path, help="Override config.json open_cloud with a destination JSON file")
    parser.add_argument("--pack")
    parser.add_argument("--key")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--edit", action="store_true")
    fixture_mode = parser.add_mutually_exclusive_group()
    fixture_mode.add_argument("--install-client", action="store_true", help="Install temporary LocalScript in Edit before Play")
    fixture_mode.add_argument("--remove-client", action="store_true", help="Remove temporary LocalScript in Edit after Play")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.install_client or args.remove_client:
        fixture = (ROOT / "scripts/fixtures/asset_content_client.luau").read_text(encoding="utf-8")
        encoded = json.dumps(fixture, ensure_ascii=True)
        # Decode JSON through HttpService to preserve exact source bytes/newlines.
        delimiter = "="
        while "]" + delimiter + "]" in encoded:
            delimiter += "="
        code = "local source = game:GetService('HttpService'):JSONDecode([" + delimiter + "[" + encoded + "]" + delimiter + "])\n"
        code += "assert(not game:GetService('RunService'):IsRunning(), 'Fixture changes require Edit')\n"
        code += "local parent = game:GetService('StarterPlayer').StarterPlayerScripts\nlocal existing = parent:FindFirstChild('_RobloxAssetVerify')\n"
        code += "assert(not existing or existing:GetAttribute('AssetVerificationFixture'), 'Unowned fixture name collision')\n"
        if args.install_client:
            code += "assert(not existing or existing.Source == source, 'Different fixture already installed; remove it first')\n"
            code += "local fixture = existing or Instance.new('LocalScript')\nfixture.Name='_RobloxAssetVerify'\nfixture.Source=source\nfixture:SetAttribute('AssetVerificationFixture', true)\nfixture.Parent=parent\nassert(fixture.Source == source, 'Fixture readback mismatch')\nreturn 'ok: client fixture installed and verified'"
        else:
            code += "if existing then existing:Destroy() end\nreturn 'ok: client fixture removed'"
        with StudioMCP() as client:
            sid, _ = syncmod.resolve_studio(client, repo_config.place_name())
            result = client.call("execute_luau", {"studio_id": sid, "datamodel_type": "Edit", "code": code}, timeout=30)
        if not result.startswith("ok:"):
            raise ValueError("Fixture setup/cleanup failed: " + result)
        print(result)
        return 0
    if not args.out:
        parser.error("Verification requires --out")
    if not 1 <= args.limit <= 8 or args.offset < 0:
        parser.error("limit must be 1-8 and offset nonnegative (bounded MCP deadline)")
    if args.out.exists():
        parser.error("Report already exists; choose a new evidence path")
    journal_bytes = args.journal.read_bytes()
    journal = json.loads(journal_bytes)
    entries = sorted((e for e in journal["entries"].values() if e.get("asset_id") and
                      e["state"] in ("uploaded", "verified") and
                      (not args.pack or e["pack"] == args.pack) and (not args.key or e["key"] == args.key)),
                     key=lambda e: e["key"])[args.offset:args.offset + args.limit]
    if not entries:
        parser.error("No uploaded assets selected")
    destination = repo_config.open_cloud_destination(args.destination)
    code = snippet([{k: e[k] for k in ("key", "asset_id", "fingerprint")} for e in entries], destination)
    with StudioMCP() as client:
        sid, _ = syncmod.resolve_studio(client, repo_config.place_name())
        result = json.loads(client.call("execute_luau", {"studio_id": sid,
                           "datamodel_type": "Edit" if args.edit else "Server", "code": code}, timeout=115))
        if not args.edit:
            loaded = [row for row in result["results"] if row["passed"]]
            content = json.loads(client.call("execute_luau", {"studio_id": sid, "datamodel_type": "Client",
                                 "code": client_snippet(loaded, destination)}, timeout=115)) if loaded else []
            by_key = {row["key"]: row for row in content}
            for row in result["results"]:
                row["model_passed"] = row["passed"]
                loaded_content = by_key.get(row["key"], {})
                row["passed"] = row["passed"] and loaded_content.get("passed") is True
                row["content_failures"] = loaded_content.get("content_failures", [])
                row["client_content"] = loaded_content
                if loaded_content.get("error"):
                    row["error"] = loaded_content["error"]
    if len(result["results"]) != len(entries):
        raise ValueError("Verification returned an incomplete batch")
    result.update(checked_at=now(), journal_sha256=hashlib.sha256(journal_bytes).hexdigest(),
                  mode="Edit" if args.edit else "Server", scripts_destroy_owned_instances=True)
    write_json(args.out, result)
    passed = sum(bool(row["passed"]) for row in result["results"])
    print(json.dumps({"passed": passed, "total": len(entries), "report": str(args.out)}))
    return 0 if passed == len(entries) and (args.edit or result["running"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
