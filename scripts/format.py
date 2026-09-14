"""Format durable Luau sources with pinned StyLua; downloads only with --install."""
import argparse
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = "2.5.0"
WINDOWS_SHA256 = "4ae761026d971d798cafe933d0e5765adb9891f7e59d781796b9d11a8e213852"
LOCAL_BINARY = ROOT / "artifacts" / "tools" / "stylua" / VERSION / "stylua.exe"


def install():
    if os.name != "nt":
        raise SystemExit("Install StyLua 2.5.0 for this platform and set STYLUA to its path.")
    url = f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{VERSION}/stylua-windows-x86_64.zip"
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != WINDOWS_SHA256:
        raise SystemExit("StyLua archive checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        binary = package.read("stylua.exe")
    LOCAL_BINARY.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_BINARY.write_bytes(binary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report formatting drift without changing sources")
    parser.add_argument("--install", action="store_true", help="Download the pinned Windows formatter into artifacts/tools")
    args = parser.parse_args()
    if args.install:
        install()
    if not any(path.suffix in {".lua", ".luau"} for path in (ROOT / "src").rglob("*")):
        print("format: no Luau source files in src/")
        return 0
    binary = os.environ.get("STYLUA") or (str(LOCAL_BINARY) if LOCAL_BINARY.exists() else shutil.which("stylua"))
    if not binary:
        raise SystemExit("StyLua unavailable: run scripts/format.py --install or set STYLUA.")
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    if version != f"stylua {VERSION}":
        raise SystemExit(f"Expected StyLua {VERSION}; found {version}")
    command = [binary, "--verify", "--config-path", str(ROOT / ".stylua.toml")]
    if args.check:
        command.append("--check")
    command.append("src")
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
