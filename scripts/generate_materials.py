#!/usr/bin/env python3
"""Generate custom MaterialVariants in Studio via the MCP generate_material tool.

Drives Studio's AI Material Generator (Window > 3D > Material Generator) the
same way capture_studio.py drives screen_capture: connect to the open Studio
place (config.json studio.place_name, stale-session safe) and call the generate_material
MCP tool. Each call registers a MaterialVariant in the open place and returns
the BaseMaterial + variant Name pair needed to apply it to BaseParts
(Material = base material, MaterialVariant = generated name).

Usage:
  python -X utf8 scripts/generate_materials.py --name CobbleRoad \\
      --description "weathered gray cobblestone road, top down, seamless" \\
      --base-material Cobblestone
  python -X utf8 scripts/generate_materials.py --name MossyStone \\
      --description "mossy castle stone, photorealistic, close up" \\
      --base-material Rock --pattern Organic \\
      --apply-to game.Workspace.Dungeon
  python -X utf8 scripts/generate_materials.py --batch artifacts/materials.json

Batch file format (JSON array; "pattern" optional, defaults to --pattern):
  [
    {"name": "CobbleRoad", "description": "...", "baseMaterial": "Cobblestone"},
    {"name": "Moss", "description": "...", "baseMaterial": "Grass",
     "pattern": "Organic", "applyTo": "game.Workspace.Park"}
  ]

Prints one JSON result line per material. Generated variants live in the open
place under MaterialService (AssistantMaterials folders) and are saved with
the place file - they are not uploaded assets, so there is no journal.
Re-generating an existing materialId does not replace the old variant: Studio
registers the new generation under a numbered name (X, X1, X2, ...).
"""
import argparse
import json
import repo_config
import sys

from rbx_studio import StudioMCP
import sync_to_studio as syncmod

# Base materials accepted by the generate_material MCP tool (Enum.Material subset).
BASE_MATERIALS = (
    "Plastic", "SmoothPlastic", "Wood", "WoodPlanks", "Marble", "Basalt",
    "Slate", "CrackedLava", "Concrete", "Limestone", "Granite", "Pavement",
    "Brick", "Pebble", "Cobblestone", "Rock", "Sandstone", "CorrodedMetal",
    "DiamondPlate", "Foil", "Metal", "Grass", "LeafyGrass", "Sand", "Fabric",
    "Snow", "Mud", "Ground", "Asphalt", "Salt", "Ice", "Glacier", "Cardboard",
    "Carpet", "CeramicTiles", "ClayRoofTiles", "RoofShingles", "Leather",
    "Plaster", "Rubber",
)
PATTERNS = ("Regular", "Organic")

FIND_VARIANT_LUAU = """local wanted = __NAME__
local svc = game:GetService('MaterialService')
local hits = {}
for _, d in ipairs(svc:GetDescendants()) do
\tif d:IsA('MaterialVariant') and d.Name == wanted then
\t\ttable.insert(hits, d:GetFullName() .. ' (base ' .. d.BaseMaterial.Name .. ')')
\tend
end
if #hits == 0 then
\treturn 'MISSING'
end
return table.concat(hits, '\\n')
"""

APPLY_VARIANT_LUAU = """local pathParts = string.split(__PATH__, '/')
local rootName = table.remove(pathParts, 1)
local target
local ok = pcall(function() target = game:GetService(rootName) end)
if not ok or not target then target = game:FindFirstChild(rootName) end
assert(target, 'cannot resolve root instance: ' .. rootName)
for i = 1, #pathParts do
\ttarget = target:FindFirstChild(pathParts[i])
\tassert(target, 'missing instance segment: ' .. pathParts[i])
end
local baseMaterial = '__BASE__'
local variantName = __NAME__
local parts = {}
if target:IsA('BasePart') then
\ttable.insert(parts, target)
end
for _, d in ipairs(target:GetDescendants()) do
\tif d:IsA('BasePart') then
\t\ttable.insert(parts, d)
\tend
end
for _, p in ipairs(parts) do
\tp.Material = Enum.Material[baseMaterial]
\tp.MaterialVariant = variantName
end
return tostring(#parts) .. ' BasePart(s) now use ' .. baseMaterial .. '/' .. variantName .. ' under ' .. target:GetFullName()
"""


def apply_path(request_path):
    """Accept MCP-style dot paths (game.Workspace.Road) and slash paths
    (Workspace/Road); returns the slash form used by the apply Luau."""
    stripped = str(request_path).strip()
    for prefix in ("game.", "game/"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    if "/" not in stripped:
        stripped = stripped.replace(".", "/")
    if not stripped.split("/")[0]:
        raise ValueError("applyTo path has no root instance")
    return stripped


def normalize_request(entry, defaults, seen_names):
    """Validate one generation request; returns the normalized dict."""
    if not isinstance(entry, dict):
        raise ValueError("batch entries must be JSON objects")
    request = {
        "name": str(entry.get("name") or "").strip(),
        "description": str(entry.get("description") or "").strip(),
        "baseMaterial": str(entry.get("baseMaterial") or "").strip(),
        "pattern": str(entry.get("pattern") or defaults["pattern"]).strip(),
        "applyTo": str(entry.get("applyTo") or defaults["applyTo"] or "").strip(),
    }
    if not request["name"]:
        raise ValueError("material name is empty")
    if request["name"] in seen_names:
        raise ValueError("duplicate material name: " + request["name"])
    if not request["description"]:
        raise ValueError("material description is empty for " + request["name"])
    if request["baseMaterial"] not in BASE_MATERIALS:
        raise ValueError("baseMaterial %r is not one of: %s"
                         % (request["baseMaterial"], ", ".join(BASE_MATERIALS)))
    if request["pattern"] not in PATTERNS:
        raise ValueError("pattern %r must be one of: %s" % (request["pattern"], ", ".join(PATTERNS)))
    if "'" in request["applyTo"]:
        raise ValueError("applyTo path must not contain single quotes")
    seen_names.add(request["name"])
    return request


def generate_one(client, sid, request, timeout, retries):
    """Call generate_material and verify the variant landed under MaterialService.

    Studio's tool intermittently fails with Luau errors of its own
    (MaterialGenTool assert failures); failed attempts are retried up to
    --retries times since generation has no side effect on success."""
    arguments = {
        "materialPattern": request["pattern"],
        "materialId": request["name"],
        "baseMaterial": request["baseMaterial"],
        "materialDescription": request["description"],
        "studio_id": sid,
    }
    result = {"name": request["name"], "baseMaterial": request["baseMaterial"],
              "pattern": request["pattern"], "status": "failed", "variant": None,
              "instance": None, "applied": None, "error": None}
    verify_code = FIND_VARIANT_LUAU.replace("__NAME__", syncmod.luau_quote(request["name"].encode("utf-8")))
    error = None
    for attempt in range(1 + max(0, retries)):
        try:
            raw = client.call("generate_material", arguments, timeout=timeout)
            try:
                variant = json.loads(raw)
                result["variant"] = {"BaseMaterial": variant.get("BaseMaterial"),
                                     "Name": variant.get("Name")}
            except ValueError:
                result["variant"] = {"raw": raw}
            found = client.call("execute_luau",
                                {"code": verify_code, "datamodel_type": "Edit",
                                 "studio_id": sid}, timeout=60).strip()
            result["instance"] = None if found == "MISSING" else found
            if found != "MISSING":
                result["status"] = "ok"
                break
            error = "variant not found under MaterialService"
        except (RuntimeError, OSError, ValueError, TimeoutError) as err:
            error = str(err).strip()[:500]
            result["variant"] = None
    result["error"] = None if result["status"] == "ok" else error
    if request["applyTo"] and result["status"] == "ok":
        apply_code = (APPLY_VARIANT_LUAU
                      .replace("__PATH__", syncmod.luau_quote(apply_path(request["applyTo"]).encode("utf-8")))
                      .replace("__BASE__", request["baseMaterial"])
                      .replace("__NAME__", syncmod.luau_quote(request["name"].encode("utf-8"))))
        result["applied"] = client.call("execute_luau",
                                        {"code": apply_code, "datamodel_type": "Edit",
                                         "studio_id": sid}, timeout=60).strip()
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="MaterialVariant name (tool's materialId)")
    ap.add_argument("--description", help="text prompt describing the surface")
    ap.add_argument("--base-material", help="Enum.Material base; one of: %s" % ", ".join(BASE_MATERIALS))
    ap.add_argument("--pattern", default="Regular",
                    help="Regular (tiled) or Organic (less repetitive); default Regular")
    ap.add_argument("--apply-to",
                    help="apply the variant to all BaseParts under this dot-notation "
                         "instance path (e.g. game.Workspace.Road)")
    ap.add_argument("--batch", help="JSON file with an array of {name, description, "
                                    "baseMaterial, pattern, applyTo} requests")
    ap.add_argument("--studio-id", help="target this Studio instance explicitly")
    ap.add_argument("--timeout", type=float, default=240.0,
                    help="per-material generation timeout in seconds (default 240)")
    ap.add_argument("--retries", type=int, default=1,
                    help="retry a failed generation this many times (default 1)")
    args = ap.parse_args(argv)

    defaults = {"pattern": args.pattern, "applyTo": args.apply_to}
    seen_names = set()
    if args.batch:
        if any((args.name, args.description, args.base_material)):
            ap.error("--batch cannot be combined with --name/--description/--base-material")
        try:
            with open(args.batch, "r", encoding="utf-8-sig") as f:
                spec = json.load(f)
        except (OSError, ValueError) as err:
            ap.error("cannot read batch file %s: %s" % (args.batch, err))
        if not isinstance(spec, list) or not spec:
            ap.error("batch file must contain a non-empty JSON array")
        try:
            requests = [normalize_request(entry, defaults, seen_names) for entry in spec]
        except ValueError as err:
            ap.error(str(err))
    else:
        try:
            requests = [normalize_request({"name": args.name, "description": args.description,
                                           "baseMaterial": args.base_material},
                                          defaults, seen_names)]
        except ValueError as err:
            ap.error(str(err))

    place_name = repo_config.place_name()
    client = StudioMCP()
    results = []
    try:
        client.start()
        sid, how = syncmod.resolve_studio(client, place_name, override=args.studio_id)
        print("Studio: %s (%s)" % (sid, how), file=sys.stderr)
        for request in requests:
            try:
                result = generate_one(client, sid, request, args.timeout, args.retries)
            except (RuntimeError, OSError, ValueError, TimeoutError) as err:
                result = {"name": request["name"], "baseMaterial": request["baseMaterial"],
                          "pattern": request["pattern"], "status": "failed", "variant": None,
                          "instance": None, "applied": None,
                          "error": str(err).strip()[:500]}
            results.append(result)
            print(json.dumps(result))
        failed = [r for r in results if r["status"] != "ok"]
        print("Generated %d/%d material(s)%s" % (
            len(results) - len(failed), len(results),
            ("; %d failed" % len(failed)) if failed else ""))
        return 1 if failed else 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())