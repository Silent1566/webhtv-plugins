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
import io
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
    "dv5": {
        "module": "dv5",
        "packageName": "com.silent.runtime.dv5",
        "versionCode": 1,
        "versionName": "1.0.0",
        "compatibilityId": "exo-dv5-renderer-v1",
        # ExoDv5Native asks for this by bare name via System.loadLibrary, so the host installs a
        # resolver mapping the bare name to the module's file.
        "libraries": ["libexo_dovi_renderer.so"],
        # Verified with readelf: the library depends only on system libraries
        # (libandroid/liblog/libmediandk/libvulkan/libdl/libm/libc) and carries a soname, so no
        # host-provided shared library is involved.
        "hostLibraries": {},
        # Unlike the other modules this library is not checked into the WebHTV tree: it is built by
        # CMake from app/src/main/cpp/exo_dovi_renderer.cpp against the static libraries under
        # third_party/exo-dv5-native/prebuilt. "cmake" tells find_native_libraries to build it.
        "source": "cmake",
        "cmake": {
            "sourceDir": "app/src/main/cpp",
            "target": "exo_dovi_renderer",
            "platform": "android-26",
        },
    },
    "python": {
        "module": "python",
        "packageName": "com.silent.runtime.python",
        "versionCode": 1,
        "versionName": "1.0.0",
        "compatibilityId": "chaquopy-python310-v1",
        # Topological order, measured in S12, not Chaquopy's own bare-name sequence.
        #
        # Platform.loadNativeLibs() loads crypto_chaquopy, ssl_chaquopy, sqlite3_chaquopy,
        # python3.10, chaquopy_java - but readelf shows libcrypto_chaquopy depends on
        # libcrypto_python, libssl_chaquopy on libssl_python, libsqlite3_chaquopy on
        # libsqlite3_python, and libssl_python on libcrypto_python. Bare names survive that
        # because the linker falls back to searching the namespace's library paths; a module's
        # directory is not on that path, so each dependency must already be mapped.
        "libraries": [
            "libcrypto_python.so",
            "libpython3.10.so",
            "libsqlite3_python.so",
            "libssl_python.so",
            "libcrypto_chaquopy.so",
            "libssl_chaquopy.so",
            "libsqlite3_chaquopy.so",
            "libchaquopy_java.so",
        ],
        # Verified with readelf: the eight libraries depend only on system libraries and each
        # other, so no host-provided shared library is involved. libpython3.10.so carries no
        # soname, which S10 measured is still satisfiable by preloading it by absolute path.
        "hostLibraries": {},
        # Chaquopy's payload is not only native libraries: the interpreter's stdlib, requirements
        # and bootstrap live in a tree of assets the runtime reads through an AssetManager. Both
        # halves are build products of the WebHTV checkout, produced by the Chaquopy Gradle plugin.
        "source": "chaquopy",
        "payload": {
            "from": "chaquo/build/intermediates/assets/{abi_flavor}Debug/merge{abi_variant}DebugAssets/chaquopy",
            "to": "assets/chaquopy",
        },
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


def normalize_imy_bytes(data: bytes) -> bytes:
    """Returns a byte-for-byte reproducible form of a Chaquopy ``.imy`` archive.

    A ``.imy`` is a zip of ``.pyc`` files, and two things in it depend on *when* the build ran
    rather than on the sources:

    * Chaquopy compiles with ``python -m compileall`` and never passes ``--invalidation-mode``, so
      the bytecode uses the default timestamp mode and each ``.pyc`` header embeds the source
      file's mtime. A ``git clone`` sets mtime to the clone time, so every CI build differs.
    * The zip entries carry the build's own timestamps.

    Both are normalized here. Measured before this: a clean checkout and a local build produced
    identical package versions yet different module digests, with all 349 ``.pyc`` headers
    differing.

    Normalizing the header is safe because the payload ships ``.pyc`` files with no ``.py``
    alongside them, and Chaquopy loads them through ``SourcelessAssetLoader`` (verified in
    ``importer.py``), which never consults the header: it was exercised with the mtime and size
    fields overwritten and the code still loaded.

    One thing this cannot do is reorder the frozenset constants CPython's peephole optimizer folds
    out of ``x in {...}``: their order follows the hash seed. That is why the build also pins
    ``PYTHONHASHSEED``; with it set, two clean builds were byte-identical.

    Entries are re-emitted in sorted order with a fixed timestamp and no compression, so the
    result depends only on the file contents.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        entries = sorted(source.infolist(), key=lambda i: i.filename)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as target:
            for entry in entries:
                content = bytearray(source.read(entry.filename))
                if entry.filename.endswith(".pyc") and len(content) >= 16:
                    flags = struct.unpack_from("<I", content, 4)[0]
                    if flags == 0:
                        # Timestamp mode: zero the source mtime and size so the header stops
                        # recording when the checkout happened.
                        struct.pack_into("<I", content, 8, 0)
                        struct.pack_into("<I", content, 12, 0)
                info = zipfile.ZipInfo(entry.filename, date_time=(1981, 1, 1, 1, 1, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = entry.external_attr
                target.writestr(info, bytes(content))
    return out.getvalue()


def collect_payload(source: Path, dest_prefix: str) -> list[tuple[str, bytes]]:
    """Returns the whole payload tree as packaged bytes, normalized where needed.

    This runs in two passes because one file depends on the others. ``build.json`` records a
    digest for every asset, and the ``.imy`` digests change when their bytecode headers are
    normalized — so the digests have to be recomputed first and written back, otherwise
    ``build.json`` would still describe the un-normalized archives and the module digest would keep
    drifting even though every archive was stable.

    ``build.json`` itself is only used by Chaquopy to decide whether an already-extracted asset is
    current (``extractAsset`` compares the recorded digest with what it stored in preferences), so
    rewriting it with the normalized digests keeps it self-consistent.
    """
    contents: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source).as_posix()
        contents[rel] = path.read_bytes()

    # Pass one: normalize the archives and remember their new digests.
    digests: dict[str, str] = {}
    for rel, data in list(contents.items()):
        if rel.endswith(".imy"):
            normalized = normalize_imy_bytes(data)
            contents[rel] = normalized
            digests[rel] = hashlib.sha256(normalized).hexdigest()

    # Pass two: point build.json at the normalized digests.
    if "build.json" in contents and digests:
        manifest = json.loads(contents["build.json"])
        assets = manifest.get("assets")
        if isinstance(assets, dict):
            updated = 0
            for rel, digest in digests.items():
                if rel in assets:
                    assets[rel] = digest
                    updated += 1
            if updated != len(digests):
                missing = sorted(set(digests) - set(assets))
                sys.exit(f"error: build.json does not describe every archive: {missing}")
            contents["build.json"] = json.dumps(
                manifest, indent=4, ensure_ascii=False).encode("utf-8")

    return [(f"{dest_prefix}/{rel}", contents[rel]) for rel in sorted(contents)]


def sdk_tool(sdk: Path, *parts: str) -> str:
    """Locate an executable under the SDK root, preferring the newest version directory."""
    exe = parts[-1] + (".exe" if os.name == "nt" else "")
    roots = sorted((sdk / parts[0]).iterdir(), reverse=True) if (sdk / parts[0]).is_dir() else []
    for root in roots:
        candidate = root.joinpath(*parts[1:-1], exe)
        if candidate.is_file():
            return str(candidate)
    sys.exit(f"error: {'/'.join(parts)} not found under {sdk}")


def build_with_cmake(module: str, abi: str, webhtv: Path, sdk: Path, name: str) -> Path:
    """Builds a library that is not checked in, using the project's own CMake target.

    Most modules ship libraries that live in the WebHTV tree, so a checkout is enough to package
    them. The DV5 renderer is different: it is compiled from app/src/main/cpp against the static
    libraries under third_party/exo-dv5-native/prebuilt, and no built .so is tracked. Building it
    here keeps the module derived from the same source as the host rather than from a checked-in
    binary that could drift.

    The result is stripped, because CMake's default output carries debug info and is more than
    twice the size (21.3 MiB vs 9.1 MiB for arm64).
    """
    spec = MODULES[module]["cmake"]
    source_dir = webhtv / spec["sourceDir"]
    if not source_dir.is_dir():
        sys.exit(f"error: cmake source dir not found: {source_dir}")

    ndk_root = sdk / "ndk"
    if not ndk_root.is_dir():
        sys.exit(f"error: no NDK under {sdk}")
    toolchain = None
    ndk_version = None
    for version in sorted(ndk_root.iterdir(), reverse=True):
        candidate = version / "build" / "cmake" / "android.toolchain.cmake"
        if candidate.is_file():
            toolchain = candidate
            ndk_version = version
            break
    if toolchain is None:
        sys.exit(f"error: no android.toolchain.cmake under {ndk_root}")

    cmake = sdk_tool(sdk, "cmake", "bin", "cmake")
    ninja = sdk_tool(sdk, "cmake", "bin", "ninja")
    host_tag = "windows-x86_64" if os.name == "nt" else "linux-x86_64"
    strip = ndk_version / "toolchains" / "llvm" / "prebuilt" / host_tag / "bin" / \
        ("llvm-strip.exe" if os.name == "nt" else "llvm-strip")
    if not strip.is_file():
        sys.exit(f"error: llvm-strip not found: {strip}")

    build_dir = Path(tempfile.mkdtemp(prefix=f"cmake-{module}-{abi}-"))
    run([cmake, "-S", str(source_dir), "-B", str(build_dir), "-G", "Ninja",
         f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
         f"-DANDROID_ABI={abi}",
         f"-DANDROID_PLATFORM={spec['platform']}",
         f"-DCMAKE_MAKE_PROGRAM={ninja}",
         "-DCMAKE_BUILD_TYPE=Release",
         # Build-id off, so the artifact is reproducible.
         #
         # The catalog pins a SHA-256 per artifact, and the host refuses a download whose digest
         # does not match. A build-id is derived from the linked output, which records the build
         # directory, so with it enabled two builds of the same source in different directories
         # produce different digests - measured: 9,584,504 bytes either way, but only
         # .note.gnu.build-id differed (20 of its 36 bytes). With the flag the same two builds are
         # byte-identical. Nothing here needs a build-id: Android loads the library by name, and
         # these artifacts are stripped release payloads with no separate debug symbols to match.
         "-DCMAKE_SHARED_LINKER_FLAGS=-Wl,--build-id=none"])
    run([cmake, "--build", str(build_dir), "--target", spec["target"]])

    built = build_dir / name
    if not built.is_file():
        # CMake may place the output in a subdirectory depending on the generator.
        matches = list(build_dir.rglob(name))
        if not matches:
            sys.exit(f"error: cmake did not produce {name} under {build_dir}")
        built = matches[0]

    stripped = build_dir / f"stripped-{name}"
    shutil.copy2(built, stripped)
    run([str(strip), "--strip-unneeded", str(stripped)])
    return stripped


def gradle_variant(abi: str) -> tuple[str, str]:
    """Returns the two spellings Gradle uses for an ABI, as ``(ablavor, Variant)``.

    The build directory is ``<abi_flavor>Debug`` (``arm64_v8aDebug``) but the task output
    directories capitalize the first letter (``copyArm64_v8aDebugJniLibsProjectOnly``,
    ``mergeArm64_v8aDebugAssets``). Getting this wrong works on Windows, where paths are
    case-insensitive, and fails on the Linux CI runner - so both spellings are derived in one
    place rather than repeated at each call site.
    """
    flavor = abi.replace("-", "_")
    return flavor, flavor[:1].upper() + flavor[1:]


def find_native_libraries(module: str, abi: str, webhtv: Path, sdk: Path,
                          explicit: Path | None) -> dict[str, Path]:
    """Locate every library the module ships."""
    spec = MODULES[module]
    found: dict[str, Path] = {}
    for name in spec["libraries"]:
        if explicit is not None and len(spec["libraries"]) == 1:
            candidates = [explicit]
        elif spec.get("source") == "cmake":
            found[name] = build_with_cmake(module, abi, webhtv, sdk, name)
            continue
        elif spec.get("source") == "chaquopy":
            # Chaquopy's own Gradle plugin produces these, so they live in the chaquo module's build
            # output rather than in a checked-in directory. The host checkout must have been built
            # for this ABI first, which is what the CI step does before calling this script.
            flavor, variant = gradle_variant(abi)
            candidates = [webhtv / "chaquo" / "build" / "intermediates" / "library_jni"
                          / f"{flavor}Debug" / f"copy{variant}DebugJniLibsProjectOnly"
                          / "jni" / abi / name]
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


def find_payload(module: str, abi: str, webhtv: Path) -> Path | None:
    """Locate an extra payload tree the module ships besides its native libraries.

    Returns the source directory, or None when the module declares no payload. The Python module
    needs this: Chaquopy's interpreter reads its stdlib, requirements and bootstrap from a tree of
    assets inside the APK, so those files have to travel in the module for the runtime to start.
    """
    spec = MODULES[module]
    payload = spec.get("payload")
    if not payload:
        return None
    flavor, variant = gradle_variant(abi)
    source = webhtv / payload["from"].format(abi_flavor=flavor, abi_variant=variant)
    if not source.is_dir():
        sys.exit(
            f"error: payload directory not found for {module} ({abi}): {source}\n"
            "build the WebHTV checkout for this ABI first"
        )
    return source


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
    sources = find_native_libraries(module, abi, webhtv, sdk, native)
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

        # An extra payload tree, when the module declares one. Every file is listed in the manifest
        # with its digest, so the host verifies the whole tree before the runtime reads it.
        #
        # The digests are taken over the bytes that will actually be packaged (see collect_payload),
        # not over the files on disk: the archives are normalized to remove build timestamps and
        # build.json is rewritten to match, so hashing the raw files would record digests the host
        # could never match.
        payload_source = find_payload(module, abi, webhtv)
        payload_entries: list[tuple[str, bytes]] = []
        if payload_source is not None:
            payload_entries = collect_payload(payload_source, spec["payload"]["to"])
            for declared, data in payload_entries:
                files.append({
                    "path": declared,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
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
            # Payload files are deflated: unlike the .so entries they are not mapped, and the
            # interpreter reads them through an AssetManager, which decompresses transparently.
            for declared, data in payload_entries:
                info = zipfile.ZipInfo(declared, date_time=(1981, 1, 1, 1, 1, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, data)

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
    parser.add_argument("--all", action="store_true",
                        help="build every module for every ABI (used by CI)")
    parser.add_argument("--webhtv", type=Path, default=DEFAULT_WEBHTV)
    parser.add_argument("--sdk", type=Path, default=DEFAULT_SDK)
    parser.add_argument("--native", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPO / "dist")
    args = parser.parse_args()

    if not args.webhtv.is_dir():
        sys.exit(f"error: webhtv checkout not found: {args.webhtv}")

    if args.all:
        # CI builds everything it ships, so a module cannot be added to the catalog without a
        # reproducible way to rebuild it. Building only the default module previously left mpv and
        # ijk artifacts in the catalog that CI could not reproduce.
        for module in sorted(MODULES):
            for abi in ("arm64-v8a", "armeabi-v7a"):
                build_module(module, abi, args.webhtv, args.sdk, args.out, None)
        return 0

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
