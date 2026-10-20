"""Build an immutable Registry v1 ZIP plus its SHA-256 checksum.

Run: python scripts/build_registry_artifact.py 0.1.0-beta.1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
ROOT = Path(__file__).resolve().parents[1]
MAX_SIZE = 100 * 1024 * 1024


def build(version: str, *, output_dir: Path | None = None) -> tuple[Path, Path]:
    if not SEMVER.fullmatch(version):
        raise ValueError(f"Version must be strict SemVer: {version}")
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    plugin_id = manifest.get("id")
    if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
        raise ValueError("Invalid plugin.json id")
    base = str(manifest.get("version", "")).split("-", 1)[0].split("+", 1)[0]
    if version.split("-", 1)[0].split("+", 1)[0] != base:
        raise ValueError(f"Release {version} must match manifest base version {base}")
    if manifest.get("api_version") != 1:
        raise ValueError("Template supports Plugin API v1 only")
    if manifest.get("execution") != "in_process" or manifest.get("entry") != "plugin.py":
        raise ValueError("Template requires in_process and plugin.py")
    if manifest.get("route_prefix") is not None or manifest.get("rpc_api_version") is not None:
        raise ValueError("In-process plugins may not declare RPC fields")
    if manifest.get("rpc_permissions") != []:
        raise ValueError("In-process plugins must have empty rpc_permissions")
    if manifest.get("requires") != ["core_ui"]:
        raise ValueError("Update build script when changing plugin dependencies")
    source = ROOT / "plugin.py"
    if not source.is_file() or source.is_symlink():
        raise ValueError("plugin.py is missing or a symlink")
    compile(source.read_text(encoding="utf-8"), str(source), "exec")
    manifest["version"] = version
    target = output_dir or ROOT / "dist"
    target.mkdir(parents=True, exist_ok=True)
    artifact = target / f"{plugin_id}-{version}.zip"
    checksum = target / f"{artifact.name}.sha256"
    # Only the two reviewed files enter the archive. Add assets explicitly if needed.
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        zf.write(source, "plugin.py")
    if not 0 < artifact.stat().st_size <= MAX_SIZE:
        raise ValueError("Registry ZIP must be between 1 and 100 MiB")
    with zipfile.ZipFile(artifact) as zf:
        if set(zf.namelist()) != {"plugin.json", "plugin.py"}:
            raise ValueError("Unexpected ZIP layout")
        packaged = json.loads(zf.read("plugin.json"))
        if packaged["id"] != plugin_id or packaged["version"] != version:
            raise ValueError("Packaged plugin identity differs from requested release")
    sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksum.write_text(f"{sha256}  {artifact.name}\n", encoding="utf-8")
    return artifact, checksum


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="SemVer, e.g. 0.1.0-beta.1")
    args = parser.parse_args()
    archive, sha = build(args.version)
    print(archive)
    print(sha)
