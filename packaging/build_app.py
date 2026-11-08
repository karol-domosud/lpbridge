#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Build-time tooling only. End users need no Python, compiler or container tool."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BUILD = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build"
VERSION = "0.7.3"


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    cache = BUILD / "packaging-cache"
    cache.mkdir(exist_ok=True)
    lock = json.loads((ROOT / "packaging/dependencies.json").read_text())
    python = lock["python"]
    archive = cache / "python.tar.gz"
    supplied = os.environ.get("PYTHON_ARCHIVE")
    if supplied:
        shutil.copyfile(supplied, archive)
    if not archive.exists():
        print("Downloading the pinned portable Python runtime…", flush=True)
        temporary = archive.with_suffix(".partial")
        urllib.request.urlretrieve(python["url"], temporary)
        temporary.rename(archive)
    if digest(archive) != python["sha256"]:
        raise SystemExit("Python archive checksum mismatch. Remove the cached archive and retry.")
    vfkit = Path(os.environ.get("VFKIT", "/opt/podman/bin/vfkit"))
    assets = [BUILD / name for name in ("lpbridge-vm-arm64.img", "lpbridge-vm-arm64-vmlinuz", "lpbridge-vm-arm64-initrd")]
    for asset in [vfkit] + assets:
        if not asset.is_file():
            raise SystemExit("Missing build input: %s. Build the VM with make vm first." % asset)
    with tempfile.TemporaryDirectory(prefix="app-stage-", dir=BUILD) as temporary:
        app = Path(temporary) / "lpbridge.app"
        contents = app / "Contents"
        resources = contents / "Resources"
        binaries = contents / "MacOS"
        binaries.mkdir(parents=True)
        resources.mkdir()
        for directory in ("agent", "profiles", "container"):
            shutil.copytree(ROOT / directory, resources / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        sys.path.insert(0, str(ROOT))
        from agent.build_id import source_build_id
        (resources / "service-build.txt").write_text(source_build_id(resources / "agent") + "\n")
        (resources / "packaging").mkdir()
        shutil.copy2(ROOT / "packaging/raw.ppd", resources / "packaging/raw.ppd")
        print("Bundling portable Python and the print engine…", flush=True)
        with tarfile.open(archive) as source:
            # The archive is pinned and verified above; reject escapes regardless.
            for member in source.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise ValueError("Unsafe Python archive path")
            source.extractall(resources)
        (resources / "bin").mkdir()
        shutil.copy2(vfkit, resources / "bin/vfkit")
        payload = resources / "payload"
        payload.mkdir()
        manifest = {"assets": []}
        for asset in assets:
            sha = digest(asset)
            packed = cache / (sha + ".gz")
            if not packed.exists():
                print("Compressing %s…" % asset.name, flush=True)
                staging = packed.with_suffix(".partial")
                with asset.open("rb") as source, staging.open("wb") as target:
                    with gzip.GzipFile(filename="", fileobj=target, mode="wb", compresslevel=6, mtime=0) as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                staging.rename(packed)
            shutil.copy2(packed, payload / (asset.name + ".gz"))
            manifest["assets"].append({"name": asset.name, "sha256": sha,
                                       "size": asset.stat().st_size, "compressed": True})
        manifest["version"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
        (payload / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (resources / "dependencies.json").write_text(json.dumps({**lock, "vfkit_sha256": digest(vfkit)}, indent=2))
        shutil.copy2(ROOT / "packaging/THIRD-PARTY.md", resources / "THIRD-PARTY.md")
        shutil.copy2(ROOT / "packaging/vfkit-LICENSE", resources / "vfkit-LICENSE")
        print("Compiling the native app…", flush=True)
        run("swiftc", "-O", "-swift-version", "5", "-target", "arm64-apple-macosx13.0",
            "-module-cache-path", cache / "swift-modules",
            *sorted((ROOT / "packaging/app").glob("*.swift")), "-o", binaries / "lpbridge",
            "-framework", "AppKit", "-framework", "SwiftUI", "-framework", "CryptoKit")
        run("swiftc", "-O", "-swift-version", "5", "-target", "arm64-apple-macosx13.0",
            "-module-cache-path", cache / "swift-modules",
            ROOT / "packaging/authorize/main.swift", "-o", resources / "bin/lpbridge-authorize",
            "-framework", "LocalAuthentication")
        shutil.copy2(ROOT / "packaging/assets/AppIcon.icns", resources / "AppIcon.icns")
        shutil.copy2(ROOT / "tools/lpbridge-shell", resources / "bin/lpbridge-shell")
        os.chmod(resources / "bin/lpbridge-shell", 0o755)
        info = {"CFBundleName": "lpbridge", "CFBundleDisplayName": "lpbridge",
                "CFBundleIdentifier": "com.lpbridge.app", "CFBundleVersion": "6",
                "CFBundleShortVersionString": VERSION, "CFBundlePackageType": "APPL",
                "CFBundleExecutable": "lpbridge", "CFBundleIconFile": "AppIcon",
                "NSHumanReadableCopyright": "Karol Domosud 2026 GPLv3",
                "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
                "NSLocalNetworkUsageDescription": "lpbridge finds printers on your local network so you can add them to your Mac.",
                "NSBonjourServices": ["_ipp._tcp", "_ipps._tcp", "_printer._tcp", "_pdl-datastream._tcp"]}
        with (contents / "Info.plist").open("wb") as out:
            plistlib.dump(info, out)
        # Sign nested Mach-O libraries first. Ad-hoc signatures work on Apple
        # Silicon without notarization; no developer identity is required.
        magic = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
        print("Applying local signatures…", flush=True)
        for item in (resources / "python").rglob("*"):
            if item.is_file() and not item.is_symlink():
                with item.open("rb") as stream:
                    is_macho = stream.read(4) in magic
                if is_macho:
                    subprocess.run(["codesign", "--force", "--sign", "-", str(item)], check=True, capture_output=True)
        run("codesign", "--force", "--sign", "-", "--entitlements",
            ROOT / "packaging/vfkit.entitlements.plist", resources / "bin/vfkit")
        run("codesign", "--force", "--sign", "-", app)
        run("codesign", "--verify", "--deep", "--strict", app)
        destination = BUILD / "lpbridge.app"
        if destination.exists():
            backup = BUILD / "lpbridge.previous.app"
            if backup.exists():
                shutil.rmtree(backup)
            destination.rename(backup)
        app.rename(destination)
    print("Built %s" % destination)


if __name__ == "__main__":
    main()
