#!/usr/bin/env python3
"""Regenerate ``catalog.json`` from the built module APKs in ``dist/``.

The catalog is the host's only view of what modules exist. Every artifact carries a
SHA-256 digest and size; the host verifies both before handing anything to
``PackageInstaller``, so this file must never be hand-edited without rebuilding.

Usage:
    python scripts/gen_catalog.py
    python scripts/gen_catalog.py --version-name 1.0.0 --release-base https://...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
CATALOG = REPO / "catalog.json"

# Module id -> (package name, compatibility contract). Kept in sync with scripts/build_module.py.
PACKAGES = {
    "sherpa": ("com.silent.runtime.sherpa", "sherpa-onnx-jni-v1"),
    "mpv": ("com.silent.runtime.mpv", "mpv-jni-ffmpeg-shared-v1"),
    "ijk": ("com.silent.runtime.ijk", "ijk-jni-v1"),
}

ARTIFACT_NAME = re.compile(r"^runtime-(?P<module>[a-z0-9]+)-(?P<version>\d+\.\d+\.\d+)-(?P<abi>[a-z0-9_-]+)\.apk$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(apk: Path) -> dict:
    with zipfile.ZipFile(apk) as archive:
        return json.loads(archive.read("assets/runtime.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate catalog.json from dist/")
    parser.add_argument("--release-base", default="https://github.com/Silent1566/webhtv-plugins/releases/download",
                        help="Base URL the host downloads artifacts from")
    parser.add_argument("--tag", default="modules", help="Release tag holding the artifacts")
    parser.add_argument("--min-sdk", type=int, default=24)
    args = parser.parse_args()

    if not DIST.is_dir():
        sys.exit(f"error: {DIST} not found; run scripts/build_module.py first")

    modules: dict[str, dict] = {}
    for apk in sorted(DIST.glob("runtime-*-*.apk")):
        match = ARTIFACT_NAME.match(apk.name)
        if not match:
            print(f"skip (unrecognised name): {apk.name}")
            continue
        module_id = match.group("module")
        abi = match.group("abi")

        manifest = read_manifest(apk)
        if manifest.get("module") != module_id:
            sys.exit(f"error: {apk.name} declares module {manifest.get('module')!r}")
        if manifest.get("abi") != abi:
            sys.exit(f"error: {apk.name} declares abi {manifest.get('abi')!r}")

        package_name, compatibility = PACKAGES.get(module_id, (manifest.get("packageName"), manifest.get("compatibilityId")))
        if manifest.get("packageName") != package_name:
            sys.exit(f"error: {apk.name} declares package {manifest.get('packageName')!r}, expected {package_name!r}")
        if manifest.get("compatibilityId") != compatibility:
            sys.exit(f"error: {apk.name} declares compatibility {manifest.get('compatibilityId')!r}, expected {compatibility!r}")

        entry = modules.setdefault(module_id, {
            "id": module_id,
            "packageName": package_name,
            "compatibilityId": compatibility,
            "versionCode": manifest["versionCode"],
            "versionName": manifest["versionName"],
            "artifacts": [],
        })
        if entry["versionCode"] != manifest["versionCode"]:
            sys.exit(f"error: {apk.name} versionCode disagrees with other artifacts of {module_id}")

        entry["artifacts"].append({
            "abi": abi,
            "minSdk": args.min_sdk,
            "size": apk.stat().st_size,
            "sha256": sha256_file(apk),
            "url": f"{args.release_base.rstrip('/')}/{args.tag}/{apk.name}",
        })

    if not modules:
        sys.exit("error: no module APKs found in dist/")

    for entry in modules.values():
        entry["artifacts"].sort(key=lambda artifact: artifact["abi"])

    catalog = {"schema": 1, "modules": [modules[key] for key in sorted(modules)]}
    CATALOG.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    total = sum(len(entry["artifacts"]) for entry in modules.values())
    print(f"wrote {CATALOG} ({len(modules)} module(s), {total} artifact(s))")
    for entry in catalog["modules"]:
        print(f"  {entry['id']} {entry['versionName']} ({entry['compatibilityId']})")
        for artifact in entry["artifacts"]:
            print(f"    {artifact['abi']:<14} {artifact['size']:>10} bytes  {artifact['sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
