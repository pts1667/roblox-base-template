"""Inventory local asset sources against approved IDs; never uploads or reads credentials."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import repo_config
from pathlib import Path
import re
import struct
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
MODELS = {".gltf", ".glb", ".fbx", ".obj", ".dae", ".rbxm", ".rbxmx"}
FORMATS = {"gltf", "glb", "fbx", "fbx(unity)", "obj", "dae"}
PREFERENCE = {".gltf": 0, ".glb": 1, ".fbx": 2, ".rbxm": 3, ".rbxmx": 4, ".obj": 5, ".dae": 6}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def model_structure(path: Path) -> dict:
    """Inspect glTF/GLB rig requirements before permitting automatic uploads."""
    raw = path.read_bytes()
    if path.suffix.lower() == '.glb':
        if len(raw) < 20:
            raise ValueError('Truncated GLB header')
        magic, version, length, chunk_size, chunk_type = struct.unpack_from('<4sIIII', raw)
        if magic != b'glTF' or version != 2 or length != len(raw) or chunk_type != 0x4e4f534a or chunk_size > len(raw) - 20:
            raise ValueError('Invalid GLB JSON chunk')
        raw = raw[20:20 + chunk_size]
    data = json.loads(raw.decode('utf-8-sig'))
    nodes = data.get('nodes', [])
    return {'meshes': len(data.get('meshes', [])), 'skins': len(data.get('skins', [])),
            'animations': len(data.get('animations', [])),
            'bones': sorted({nodes[i].get('name', str(i)) for skin in data.get('skins', []) for i in skin.get('joints', [])})}


def approved_ids(document: str) -> dict[tuple[str, str], dict]:
    """Scope identical piece names to their pack; first documented ID is canonical."""
    section = ""
    result = {}
    for line in document.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        match = re.match(r"\|\s*`([^`]+)`\s*\|\s*`(\d+)`(.*?)\|", line)
        if match:
            name, asset_id, alternate_text = match.groups()
            result[section, name] = {
                "asset_id": asset_id,
                "alternate_ids": re.findall(r"`(\d+)`", alternate_text),
            }
    return result


def group_key(relative: Path) -> str:
    return "/".join(part for part in relative.with_suffix("").parts if part.lower() not in FORMATS)


def gltf_dependencies(path: Path, pack_root: Path) -> tuple[list[Path], str | None]:
    """No remote URLs or paths outside the pack. Uploaders must embed these bytes."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        dependencies = []
        for entry in data.get("buffers", []) + data.get("images", []):
            uri = entry.get("uri")
            if not uri or uri.startswith("data:"):
                continue
            parsed = urlsplit(uri)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                return [], "External or unsupported glTF dependency URI"
            target = (path.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(pack_root.resolve()) or not target.is_file():
                return [], "Missing glTF dependency or path outside source pack"
            dependencies.append(target)
        return sorted(set(dependencies)), None
    except (ValueError, OSError, TypeError, AttributeError):
        return [], "Malformed or unreadable glTF source"


def inventory(source_root: Path, document_path: Path) -> dict:
    source_root = source_root.resolve()
    packs = repo_config.asset_packs()
    known = approved_ids(document_path.read_text(encoding="utf-8"))
    paths = sorted(p for p in source_root.rglob("*") if p.is_file())
    files, groups, licences = {}, defaultdict(list), {}
    for path in paths:
        relative = path.relative_to(source_root)
        pack = relative.parts[0]
        section, usage, priority = packs.get(pack, (pack, "Review editor/world use", 3))
        key = relative.as_posix()
        files[key] = {"path": "unimported/" + key, "sha256": digest(path), "bytes": path.stat().st_size,
                      "pack": pack, "status": "excluded", "reason": "Pack metadata or auxiliary source file"}
        if "license" in path.name.lower() and path.suffix.lower() == ".txt":
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if "CC0" in text or "Creative Commons Zero" in text:
                licences[pack] = {"license": "CC0-1.0", "evidence": files[key]["path"], "sha256": files[key]["sha256"]}
        if path.suffix.lower() in MODELS:
            groups[group_key(relative)].append(path)
        elif path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            approved = known.get((section, path.stem))
            if approved:
                files[key].update(status="reused", reason="Approved image ID; reuse across source resolutions", **approved)
            elif any(token in key.lower() for token in ("texture", "sample", "preview", "contents", "blueprint", "map_empty")):
                files[key]["reason"] = "Texture dependency, reference or pack preview; not a separate model upload"
            else:
                files[key].update(status="blocked", reason="Standalone image needs license/use review before upload", intended_use=usage)
        elif path.suffix.lower() == ".zip":
            files[key]["reason"] = "Archive; inspect expanded sources instead of uploading the archive"
    names = Counter((key.split("/")[0], members[0].stem) for key, members in groups.items())
    embedded = {}
    for members in groups.values():
        for path in members:
            if path.suffix.lower() != '.gltf':
                continue
            pack = path.relative_to(source_root).parts[0]
            section = packs.get(pack, (pack, '', 3))[0]
            approved = known.get((section, path.stem))
            if not approved:
                continue
            try:
                nodes = json.loads(path.read_text(encoding='utf-8-sig')).get('nodes', [])
                for node in nodes:
                    name = node.get('name')
                    if isinstance(name, str) and name != path.stem:
                        for alias in (name, path.stem + '_' + name):
                            embedded[pack, alias] = {**approved, 'parent_model': path.stem}
            except (ValueError, OSError, TypeError, AttributeError):
                pass  # The canonical group's dependency check reports malformed glTF.
    assets = []
    for key, members in sorted(groups.items()):
        members.sort(key=lambda path: (PREFERENCE[path.suffix.lower()], path.as_posix()))
        canonical = members[0]
        pack = canonical.relative_to(source_root).parts[0]
        section, usage, priority = packs.get(pack, (pack, "Review editor/world use", 3))
        approved = known.get((section, canonical.stem)) if names[pack, canonical.stem] == 1 else None
        root_file = files[canonical.relative_to(source_root).as_posix()]
        license_info = licences.get(pack, {"license": "unverified", "evidence": None})
        asset = {"key": key, "name": canonical.stem, "source": root_file["path"], "pack": pack,
                 "priority": priority, "intended_use": usage, **license_info,
                 "status": "queued", "reason": "Free source awaiting upload and in-experience verification",
                 "dependencies": [], "fingerprint": root_file["sha256"]}
        if approved:
            asset.update(status="reused", reason="Approved Model ID from ROBLOX_ASSETS.md; do not upload again", **approved)
            if pack == 'animations':
                asset.update(asset_type='Animation', reason='Existing runtime Animation ID matched by published name; reuse, not a static Model upload')
        elif names[pack, canonical.stem] > 1:
            asset.update(status="blocked", reason="Repeated model name in different folders; resolve identity before upload")
        elif license_info["license"] == "unverified":
            asset.update(status="blocked", reason="No verified free redistribution license in source pack")
        elif canonical.suffix.lower() in {".obj", ".dae"}:
            asset.update(status="blocked", reason="No directly supported Open Cloud source; convert or use Studio importer")
        elif canonical.suffix.lower() in {".rbxm", ".rbxmx"}:
            asset.update(status="blocked", reason="Inspect Roblox instances/scripts and determine Model versus Animation before upload")
        if not approved and (pack, canonical.stem) in embedded:
            parent = embedded[pack, canonical.stem]
            asset.update(status='excluded', reason='Separate OBJ component is already a node in approved parent model', **parent)
        if not approved and canonical.suffix.lower() in {'.gltf', '.glb'}:
            try:
                asset['structure'] = model_structure(canonical)
                if asset['structure']['skins'] or asset['structure']['animations']:
                    asset.update(status='blocked', reason='Skinned mesh/animation requires rig and clip adaptation; not compatible with automatic static Model integration')
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                asset.update(status='blocked', reason='Cannot inspect glTF structure: ' + str(exc)[:200])
        if canonical.suffix.lower() == ".gltf":
            dependencies, failure = gltf_dependencies(canonical, source_root / pack)
            if failure and not approved:
                asset.update(status="blocked", reason=failure)
            if failure:
                asset["source_warning"] = failure
            fingerprint = hashlib.sha256(root_file["sha256"].encode())
            for dependency in dependencies:
                dep_key = dependency.relative_to(source_root).as_posix()
                dep_file = files[dep_key]
                asset["dependencies"].append(dep_file["path"])
                fingerprint.update(dep_key.encode())
                fingerprint.update(dep_file["sha256"].encode())
                dep_file.setdefault("used_by", []).append(key)
                if dep_file["status"] != "reused":
                    dep_file.update(status="excluded", reason="Referenced model dependency; bundle with canonical source")
            asset["fingerprint"] = fingerprint.hexdigest()
        for member in members:
            file = files[member.relative_to(source_root).as_posix()]
            file["asset_key"] = key
            if member == canonical:
                file.update(status=asset["status"], reason=asset["reason"])
                if approved:
                    file.update(approved)
            else:
                file.update(status="excluded", reason="Alternate export of canonical source " + asset["source"])
        assets.append(asset)
    return {"version": 1, "source_root": "unimported", "approved_document": "docs/ROBLOX_ASSETS.md",
            "approved_document_sha256": digest(document_path), "asset_groups": assets,
            "files": list(files.values()), "summary": {
                "file_count": len(files), "model_groups": len(assets),
                "files_by_status": dict(sorted(Counter(f["status"] for f in files.values()).items())),
                "models_by_status": dict(sorted(Counter(a["status"] for a in assets).items()))}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "docs/ASSET_IMPORT_INVENTORY.json")
    parser.add_argument("--check", action="store_true", help="Compare discovery with the saved inventory without writing")
    args = parser.parse_args()
    result = inventory(ROOT / "unimported", ROOT / "docs/ROBLOX_ASSETS.md")
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        matches = args.out.exists() and args.out.read_text(encoding="utf-8") == encoded
        print(json.dumps({**result["summary"], "inventory_matches": matches}))
        return 0 if matches else 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(args.out)
    print(json.dumps(result["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
