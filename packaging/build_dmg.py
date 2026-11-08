#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Create a Finder drag-to-Applications disk image from the finished bundle."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BUILD = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "build"
ASSETS = Path(__file__).resolve().parent / "assets"


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


sys.path.insert(0, str(BUILD / "packaging-tools"))
try:
    from ds_store import DSStore
    from mac_alias import Alias
except ImportError:
    raise SystemExit("Install build-only layout helpers: python3 -m pip install --target build/packaging-tools ds_store==1.3.1 mac_alias==2.2.2")

# macOS 26 deprecates hdiutil's create/attach/detach/convert in favour of
# `diskutil image`; older build hosts only have hdiutil. Same steps either way.
MODERN = subprocess.run(["diskutil", "image", "create", "from", "--help"],
                        capture_output=True).returncode == 0


def make_writable(volume, image):
    if MODERN:
        # a sparse bundle is the writable format diskutil offers; APFS inside
        run("diskutil", "image", "create", "from", "--format", "UDSB", "--volumeName", "lpbridge", volume, image)
    else:
        run("hdiutil", "create", "-volname", "lpbridge", "-srcfolder", volume, "-format", "UDRW", "-fs", "HFS+", image)


def attach(image, mount):
    if MODERN:
        run("diskutil", "image", "attach", "--nobrowse", "--mountPoint", mount, image)
    else:
        run("hdiutil", "attach", "-readwrite", "-noverify", "-noautoopen", "-mountpoint", mount, image)


def detach(mount):
    run("diskutil", "eject", mount) if MODERN else run("hdiutil", "detach", mount)


def compress(image, output):
    if MODERN:
        run("diskutil", "image", "create", "from", "--format", "UDZO", image, output)
    else:
        run("hdiutil", "convert", image, "-format", "UDZO", "-imagekey", "zlib-level=9", "-o", output)


with tempfile.TemporaryDirectory(prefix="dmg-stage-", dir=BUILD) as temporary:
    run("codesign", "--verify", "--deep", "--strict", BUILD / "lpbridge.app")
    staging = Path(temporary)
    volume = staging / "volume"
    volume.mkdir()
    shutil.copytree(BUILD / "lpbridge.app", volume / "lpbridge.app", symlinks=True)
    (volume / "Applications").symlink_to("/Applications")
    (volume / ".background").mkdir()
    shutil.copy2(ASSETS / "dmg-background.tiff", volume / ".background/background.tiff")
    writable = staging / ("installer.sparsebundle" if MODERN else "installer.dmg")
    make_writable(volume, writable)
    mount = staging / "mounted"
    attach(writable, mount)
    try:
        with DSStore.open(str(mount / ".DS_Store"), "w+") as layout:
            layout["."]["bwsp"] = {"ShowToolbar": False, "ShowStatusBar": False,
                                    "ShowSidebar": False, "ShowPathbar": False,
                                    "ContainerShowSidebar": False, "PreviewPaneVisibility": False,
                                    "ShowTabView": False, "SidebarWidth": 0,
                                    "WindowBounds": "{{180, 160}, {660, 420}}"}
            layout["."]["icvp"] = {"viewOptionsVersion": 1, "backgroundType": 2,
                                    "backgroundImageAlias": Alias.for_file(str(mount / ".background/background.tiff")).to_bytes(),
                                    "iconSize": 96.0, "textSize": 13.0,
                                    "gridSpacing": 100.0, "gridOffsetX": 0.0, "gridOffsetY": 0.0,
                                    "scrollPositionX": 0.0, "scrollPositionY": 0.0,
                                    "backgroundColorRed": 1.0, "backgroundColorGreen": 1.0, "backgroundColorBlue": 1.0,
                                    "arrangeBy": "none", "labelOnBottom": True,
                                    "showItemInfo": False, "showIconPreview": True}
            layout["."]["vSrn"] = ("long", 1)
            layout["."]["icvl"] = ("type", b"icnv")
            layout["lpbridge.app"]["Iloc"] = (180, 210)
            layout["Applications"]["Iloc"] = (480, 210)
    finally:
        detach(mount)
    output = BUILD / "lpbridge-0.7.3-arm64.dmg"
    compress(writable, staging / "release.dmg")
    (staging / "release.dmg").replace(output)
    run("hdiutil", "verify", output)
    print("Built %s" % output)
