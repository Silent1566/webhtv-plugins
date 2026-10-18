#!/usr/bin/env python3
"""Build a WebHTV runtime module APK.

A module APK is a minimal, signed Android package whose only payload is the native
library (plus a ``runtime.json`` manifest describing it). The host downloads it,
verifies the catalog digest, hands it to ``PackageInstaller``, and then loads the
library straight out of the installed module's ``nativeLibraryDir``.

Two properties of the output matter and are easy to get wrong:

* ``lib/<abi>/*.so`` must be stored **uncompressed**. Android only exposes
  ``nativeLibraryDir`` for libraries it can mmap directly; a deflated entry would
  force extraction elsewhere and break the load path.
* The APK must be signed. ``PackageInstaller`` rejects unsigned packages, and the
  host cross-checks the installed package name against the catalog entry.

Usage:
    python scripts/build_module.py --abi arm64-v8a
    python scripts/build_module.py --abi arm64-v8a --native /path/to/libsherpa-onnx-jni.so
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_WEBHTV = Path(os.environ.get("WEBHTV_DIR", "F:/Workspace/webhtv"))
DEFAULT_SDK = Path(os.environ.get("ANDROID_SDK", "C:/Users/Maple/AppData/Local/Android/Sdk"))
DEBUG_KEYSTORE = Path.home() / ".android" / "debug.keystore"

# Manifest of each module: what it is, what it needs, and where the libraries come from.
MODULES = {
    "sherpa": {
        "module": "sherpa",
        "packageName": "com.silent.runtime.sherpa",
        "versionCode": 1,
        "versionName": "1.0.0",
        "compatibilityId": "sherpa-onnx-jni-v1",
        "libraries": ["libsherpa-onnx-jni.so"],
        # Verified with readelf: the library depends only on libandroid/liblog/libm/libdl/libc,
        # so no host-provided shared library is involved. This is why sherpa was the pilot module.
        "hostLibraries": {},
        "source": "jniLibs",
    },
    "mpv": {
        "module": "mpv",
        "packageName": "com.silent.runtime.mpv",
        "versionCode": 1,
        "versionName": "1.0.0",
        "compatibilityId": "mpv-jni-ffmpeg-shared-v1",
        # The host's own LOAD_ORDER, minus c++_shared (which the host provides) and minus the
        # .so the module does not ship. Order matters: the dynamic linker resolves each
        # DT_NEEDED against what is already loaded.
        "libraries": [
            "libmvutil.so",
            "libmwresample.so",
            "libmwscale.so",
            "libmvcodec.so",
            "libmvformat.so",
            "libmvfilter.so",
            "libmvdevice.so",
            "libmpv.so",
            "libplayer.so",
        ],
        # libmvcodec/libmpv/libplayer all DT_NEEDED libc++_shared.so. The host already ships one
        # copy (app/src/main/jniLibs) and the module must NOT bundle another: two copies collide
        # on the same soname and the later one fails to dlopen with a missing-symbol error.
        #
        # The digest is per-ABI (the two host copies differ), so it is derived from the checkout
        # at build time rather than hardcoded — a hardcoded value silently breaks the other ABI,
        # and the host would reject the module at load time with a digest mismatch.
        "hostLibraries": {
            "libc++_shared.so": "@derive:app/src/main/jniLibs/{abi}/libc++_shared.so",
        },
        "source": "assets",
    },
    "ijk": {
        "module": "ijk",
        "packageName": "com.silent.runtime.ijk",
        "versionCode": 1,
        "versionName": "1.0.0",
        "compatibilityId": "ijk-jni-v1",
        # IjkMediaPlayer asks for these by bare name ("ijkffmpeg") via System.loadLibrary, so the
        # host installs a resolver mapping the bare name to the module's file. Order matters:
        # libijksdl and libijkplayer both DT_NEEDED libijkffmpeg.
        "libraries": [
            "libijkffmpeg.so",
            "libijksdl.so",
            "libijkplayer.so",
        ],
        # Verified with readelf: the three libraries depend only on system libraries and each
        # other, so no host-provided shared library is involved.
        "hostLibraries": {},
        "source": "jniLibs",
    },
}

# Modules whose classes must already exist in the host APK; a module only ships .so files.
HOST_PROVIDES_CLASSES = True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_native_libraries(module: str, abi: str, webhtv: Path, explicit: Path | None) -> dict[str, Path]:
    """Locate every library the module ships."""
    spec = MODULES[module]
    found: dict[str, Path] = {}
    for name in spec["libraries"]:
        if explicit is not None and len(spec["libraries"]) == 1:
            candidates = [explicit]
        elif spec.get("source") == "assets":
            candidates = [webhtv / "app" / "src" / f"{abi.replace('-', '_')}" / "assets" / "mpv-libs" / abi / name]
        else:
            candidates = [webhtv / "app" / "src" / "main" / "jniLibs" / abi / name]
        for candidate in candidates:
            if candidate.is_file():
                found[name] = candidate
                break
        else:
            sys.exit(
                f"error: could not find {name} for {abi} ({module}).\nlooked in:\n"
                + "\n".join(f"  {c}" for c in candidates)
            )
    return found


def find_native_library(module: str, abi: str, webhtv: Path, explicit: Path | None) -> Path:
    """Locate the native library to package."""
    name = MODULES[module]["library"]
    if explicit is not None:
        if not explicit.is_file():
            sys.exit(f"error: --native not found: {explicit}")
        return explicit
    candidates = [
        webhtv / "app" / "src" / "main" / "jniLibs" / abi / name,
        webhtv / "app" / "src" / "arm64_v8a" / "assets" / "mpv-libs" / abi / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    sys.exit(
        f"error: could not find {name} for {abi}.\n"
        f"looked in:\n" + "\n".join(f"  {c}" for c in candidates)
    )


def tool(sdk: Path, name: str) -> Path:
    build_tools = sorted((sdk / "build-tools").glob("*"))
    if not build_tools:
        sys.exit(f"error: no build-tools under {sdk}")
    for build_tool in reversed(build_tools):
        for candidate in (build_tool / name, build_tool / f"{name}.exe", build_tool / f"{name}.bat"):
            if candidate.is_file():
                return candidate
    sys.exit(f"error: {name} not found under {sdk / 'build-tools'}")


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"error: {' '.join(str(c) for c in command)}\n{result.stdout}\n{result.stderr}")


def resolve_host_libraries(spec: dict, abi: str, webhtv: Path) -> dict[str, str]:
    """Resolve the module's hostLibraries digests for one ABI.

    A value of ``@derive:<path>`` is replaced by the SHA-256 of that file inside the WebHTV
    checkout, with ``{abi}`` substituted. Deriving beats hardcoding because the host ships a
    different ``libc++_shared.so`` per ABI; a hardcoded digest would silently describe only one
    of them and the host would reject the other at load time.
    """
    resolved: dict[str, str] = {}
    for name, value in spec.get("hostLibraries", {}).items():
        if value.startswith("@derive:"):
            rel = value[len("@derive:"):].format(abi=abi)
            path = webhtv / rel
            if not path.is_file():
                sys.exit(f"error: cannot derive host library digest, missing {path}")
            resolved[name] = sha256_file(path)
        else:
            resolved[name] = value
    return resolved


def write_manifest(dest: Path, module: str, abi: str, entry_rel: str, load_order: list[str],
                   host_libraries: dict[str, str], files: list[dict]) -> None:
    spec = MODULES[module]
    payload = {
        "module": spec["module"],
        "packageName": spec["packageName"],
        "versionCode": spec["versionCode"],
        "versionName": spec["versionName"],
        "compatibilityId": spec["compatibilityId"],
        "abi": abi,
        "library": entry_rel,
        "loadOrder": load_order,
        "hostLibraries": host_libraries,
        "files": files,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=False), encoding="utf-8")


def write_android_manifest(dest: Path, module: str) -> None:
    spec = MODULES[module]
    # A module declares no activity and ships no dex: it exists purely to publish native
    # libraries. ``android:hasCode="false"`` is required, otherwise the platform refuses the
    # install with INSTALL_FAILED_INVALID_APK "code is missing".
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
        f'    package="{spec["packageName"]}"\n'
        f'    android:versionCode="{spec["versionCode"]}"\n'
        f'    android:versionName="{spec["versionName"]}">\n'
        '    <application android:hasCode="false" android:extractNativeLibs="true" />\n'
        '</manifest>\n',
        encoding="utf-8",
    )


def build_module(module: str, abi: str, webhtv: Path, sdk: Path, out_dir: Path, native: Path | None) -> Path:
    spec = MODULES[module]
    sources = find_native_libraries(module, abi, webhtv, native)
    out_dir.mkdir(parents=True, exist_ok=True)
    unsigned = out_dir / f"runtime-{module}-{spec['versionName']}-{abi}-unsigned.apk"
    aligned = out_dir / f"runtime-{module}-{spec['versionName']}-{abi}-aligned.apk"
    final = out_dir / f"runtime-{module}-{spec['versionName']}-{abi}.apk"

    load_order = [f"lib/{abi}/{name}" for name in spec["libraries"]]
    # The entry point is the last library: it depends on the rest, which are loaded first.
    entry_rel = load_order[-1]

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        lib_dir = staging / "lib" / abi
        lib_dir.mkdir(parents=True, exist_ok=True)

        files = []
        for name, source in sources.items():
            shutil.copy2(source, lib_dir / name)
            files.append({
                "path": f"lib/{abi}/{name}",
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
            })

        write_manifest(staging / "assets" / "runtime.json", module, abi, entry_rel, load_order,
                       resolve_host_libraries(spec, abi, webhtv), files)
        write_android_manifest(staging / "AndroidManifest.xml", module)

        aapt2 = tool(sdk, "aapt2")
        zipalign = tool(sdk, "zipalign")
        apksigner = tool(sdk, "apksigner")

        # aapt2 links the manifest and binary XML; the payload is added afterwards so the
        # .so entries can be forced to ZIP_STORED.
        android_jar = sdk / "platforms" / "android-36" / "android.jar"
        if not android_jar.is_file():
            platforms = sorted((sdk / "platforms").glob("android-*"))
            if not platforms:
                sys.exit(f"error: no platforms under {sdk}")
            android_jar = platforms[-1] / "android.jar"

        run([str(aapt2), "link",
             "-o", str(unsigned),
             "-I", str(android_jar),
             "--manifest", str(staging / "AndroidManifest.xml"),
             "--min-sdk-version", "24",
             "--target-sdk-version", "28"])

        with zipfile.ZipFile(unsigned, "a") as archive:
            for name in spec["libraries"]:
                rel = f"lib/{abi}/{name}"
                info = zipfile.ZipInfo(rel, date_time=(1981, 1, 1, 1, 1, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o644 << 16
                with (lib_dir / name).open("rb") as handle:
                    archive.writestr(info, handle.read())
            assets_info = zipfile.ZipInfo("assets/runtime.json", date_time=(1981, 1, 1, 1, 1, 0))
            assets_info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(assets_info, (staging / "assets" / "runtime.json").read_bytes())

    if final.exists():
        final.unlink()
    run([str(zipalign), "-f", "-p", "4", str(unsigned), str(aligned)])

    sign_args = [str(apksigner), "sign",
                 "--ks", str(DEBUG_KEYSTORE),
                 "--ks-pass", "pass:android",
                 "--key-pass", "pass:android",
                 "--ks-key-alias", "androiddebugkey",
                 "--v2-signing-enabled", "true",
                 "--out", str(final), str(aligned)]
    run(sign_args)

    unsigned.unlink(missing_ok=True)
    aligned.unlink(missing_ok=True)
    return final


def read_zip_entry(apk: Path, name: str) -> bytes:
    with zipfile.ZipFile(apk) as archive:
        return archive.read(name)


def entry_is_stored(apk: Path, name: str) -> bool:
    with zipfile.ZipFile(apk) as archive:
        return archive.getinfo(name).compress_type == zipfile.ZIP_STORED


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a WebHTV runtime module APK")
    parser.add_argument("--module", default="sherpa", choices=sorted(MODULES))
    parser.add_argument("--abi", default="arm64-v8a")
    parser.add_argument("--webhtv", type=Path, default=DEFAULT_WEBHTV)
    parser.add_argument("--sdk", type=Path, default=DEFAULT_SDK)
    parser.add_argument("--native", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPO / "dist")
    args = parser.parse_args()

    if not args.webhtv.is_dir():
        sys.exit(f"error: webhtv checkout not found: {args.webhtv}")

    apk = build_module(args.module, args.abi, args.webhtv, args.sdk, args.out, args.native)

    spec = MODULES[args.module]
    # Every shipped library must be stored, not just the entry point: the platform only exposes
    # nativeLibraryDir for entries it can map directly, and a deflated dependency would still be
    # needed at load time.
    for name in spec["libraries"]:
        rel = f"lib/{args.abi}/{name}"
        if not entry_is_stored(apk, rel):
            sys.exit(f"error: {rel} is compressed; nativeLibraryDir would not expose it")

    payload = json.loads(read_zip_entry(apk, "assets/runtime.json"))
    total = sum(entry["size"] for entry in payload["files"])

    print(f"module      {payload['module']}")
    print(f"package     {payload['packageName']}")
    print(f"abi         {payload['abi']}")
    print(f"apk         {apk}  ({apk.stat().st_size} bytes)")
    print(f"libraries   {len(payload['files'])} stored ({total} bytes)")
    print(f"entry       {payload['library']}")
    if payload.get("hostLibraries"):
        for host, digest in payload["hostLibraries"].items():
            print(f"host lib    {host}  {digest[:16]}…")
    print(f"sha256      {sha256_file(apk)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
