"""Shared, non-secret repository settings from root config.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


def section(name):
    with CONFIG_PATH.open(encoding="utf-8-sig") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object")
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config.json: {name} must be an object")
    return value


def place_name():
    value = section("studio").get("place_name", "")
    if not isinstance(value, str):
        raise ValueError("config.json: studio.place_name must be a string")
    return value


def mcp_launcher():
    value = section("studio").get("mcp_launcher", "")
    if not isinstance(value, str):
        raise ValueError("config.json: studio.mcp_launcher must be a string")
    if not value:
        return None
    path = Path(value)
    return str(path if path.is_absolute() else ROOT / path)


def open_cloud_destination(override=None):
    if override:
        with Path(override).open(encoding="utf-8-sig") as handle:
            value = json.load(handle)
    else:
        value = section("open_cloud")
    if not isinstance(value, dict) or not isinstance(value.get("creator"), dict):
        raise ValueError("Configure open_cloud.creator in config.json (or --destination)")
    return value


def asset_packs():
    packs = section("assets").get("packs", {})
    if not isinstance(packs, dict):
        raise ValueError("config.json: assets.packs must be an object")
    result = {}
    for name, spec in packs.items():
        if not isinstance(spec, dict):
            raise ValueError(f"config.json: assets.packs.{name} must be an object")
        title = spec.get("title", name)
        usage = spec.get("intended_use", "Review editor/world use")
        priority = spec.get("priority", 3)
        if not isinstance(title, str) or not isinstance(usage, str) or type(priority) is not int:
            raise ValueError(f"config.json: invalid title, intended_use or priority for pack {name}")
        result[name] = (title, usage, priority)
    return result
