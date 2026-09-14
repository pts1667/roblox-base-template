#!/usr/bin/env python3
"""Save the current Roblox Studio viewport via MCP screen_capture.

Examples:
  python -X utf8 scripts/capture_studio.py
  python -X utf8 scripts/capture_studio.py --output artifacts/ui-refinement/before.jpg
  python -X utf8 scripts/capture_studio.py --camera-position 80 60 80 --look-at-position 0 0 0

Uses the config.json's studio.place_name and the same stale-session resolver as sync.
Captures the current viewport without starting/stopping Play or sending input.
The Studio tool controls which viewport is captured; inspect the saved image.
Camera arguments temporarily change the capture viewpoint through MCP.
Images are saved in their original format (currently JPEG), without recompression.
"""
import argparse
import base64
import binascii
from datetime import datetime, timezone
import json
import repo_config
from pathlib import Path
import sys

from rbx_studio import StudioMCP
import sync_to_studio as syncmod


IMAGE_FORMATS = {
    "image/jpeg": (".jpg", (".jpg", ".jpeg"), b"\xff\xd8\xff"),
    "image/png": (".png", (".png",), b"\x89PNG\r\n\x1a\n"),
    "image/webp": (".webp", (".webp",), b"RIFF"),
}


def decode_image(result):
    """Validate the single inline image returned by Studio's capture tool."""
    images = [part for part in result.get("content", [])
              if isinstance(part, dict) and part.get("type") == "image"]
    if len(images) != 1:
        details = "\n".join(part.get("text", "") for part in result.get("content", [])
                            if isinstance(part, dict) and part.get("type") == "text")
        raise ValueError("Expected one inline screenshot, received %d. %s"
                         % (len(images), details[:1000]))
    image = images[0]
    mime_type = image.get("mimeType")
    if mime_type not in IMAGE_FORMATS:
        raise ValueError("Unsupported screenshot MIME type: %r" % mime_type)
    try:
        data = base64.b64decode(image.get("data", ""), validate=True)
    except (ValueError, TypeError, binascii.Error) as error:
        raise ValueError("Screenshot contains invalid base64 image data") from error
    signature = IMAGE_FORMATS[mime_type][2]
    if not data.startswith(signature):
        raise ValueError("Screenshot data does not match %s" % mime_type)
    if mime_type == "image/webp" and data[8:12] != b"WEBP":
        raise ValueError("Screenshot data is not a WebP image")
    return data, mime_type


def save_image(result, output, capture_id):
    data, mime_type = decode_image(result)
    extension, accepted, _ = IMAGE_FORMATS[mime_type]
    path = Path(output) if output else Path(syncmod.ROOT) / "artifacts" / "captures" / (capture_id + extension)
    if not path.suffix:
        path = path.with_suffix(extension)
    if path.suffix.lower() not in accepted:
        raise ValueError("Studio returned %s; use an output ending in %s (or omit the extension)"
                         % (mime_type, extension))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path.resolve(), mime_type, len(data)


def parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output", help="output image path; default artifacts/captures/<capture-id>.jpg")
    ap.add_argument("--capture-id", help="MCP capture identifier; default UTC timestamp")
    ap.add_argument("--studio-id", help="override the Studio instance id")
    ap.add_argument("--camera-position", type=float, nargs=3, metavar=("X", "Y", "Z"))
    ap.add_argument("--look-at-position", type=float, nargs=3, metavar=("X", "Y", "Z"))
    ap.add_argument("--timeout", type=float, default=90.0, help="capture timeout in seconds (default 90)")
    ap.add_argument("--json", action="store_true", help="print machine-readable capture metadata")
    return ap


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    if (args.camera_position is None) != (args.look_at_position is None):
        ap.error("--camera-position and --look-at-position must be supplied together")
    if args.timeout <= 0:
        ap.error("--timeout must be positive")
    capture_id = args.capture_id or datetime.now(timezone.utc).strftime("ScreenCapture_%Y%m%dT%H%M%S_%fZ")
    if not capture_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in capture_id):
        ap.error("--capture-id may contain only letters, numbers, underscores and hyphens")
    place_name = repo_config.place_name()
    client = StudioMCP()
    try:
        client.start()
        sid, how = syncmod.resolve_studio(client, place_name, override=args.studio_id)
        arguments = {"studio_id": sid, "capture_id": capture_id}
        if args.camera_position is not None:
            arguments.update(camera_position=args.camera_position, look_at_position=args.look_at_position)
        result = client.call_result("screen_capture", arguments, timeout=args.timeout)
        path, mime_type, size = save_image(result, args.output, capture_id)
        if args.json:
            print(json.dumps(dict(path=str(path), mime_type=mime_type, bytes=size,
                                  capture_id=capture_id, studio_id=sid, place_name=place_name)))
        else:
            print("Studio: %s (%s)" % (sid, how))
            print("Saved %s (%s, %d bytes)" % (path, mime_type, size))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print("Capture failed: %s" % error, file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
