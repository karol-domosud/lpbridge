#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

# Build lpbridge from source and install it, on a Mac that has nothing yet
# but this checkout.
#
#   tools/bootstrap.sh                # everything: tools -> engine VM -> app -> install
#   tools/bootstrap.sh --dmg          # build build/lpbridge-<version>-arm64.dmg instead of installing
#   tools/bootstrap.sh --rebuild      # redo the engine VM and the app even if built
#   tools/bootstrap.sh --no-install   # stop after building
#   tools/bootstrap.sh --keep-machine # leave the Podman machine running afterwards
#   tools/bootstrap.sh --only vm      # one stage: preflight tools podman machine vm app install
#
# It runs from the repository it is part of and fetches no lpbridge code of
# its own: what you read is what gets built. Every stage is idempotent;
# rerun after a failure and it picks up where it stopped. Everything it does
# is logged to build/bootstrap.log.
#
# Podman is a build dependency only (a Linux to run apt in); its machine is a
# 2 GiB, 4-CPU VM that would otherwise sit resident for good and be mistaken
# for lpbridge idling. If this script started it, it stops it at the end.
#
# Why build it yourself? The app is signed locally ("ad-hoc"), and an app
# built on your own Mac is not quarantined, so it opens like any other --
# no notarization, no right-click-Open dance. The engine is bit-for-bit the
# same as a release build: every package comes from one pinned Ubuntu
# snapshot (container/Containerfile, ARG SNAPSHOT), Splix and the 18,900
# open-driver PPDs included. What you need for that is a Linux to run apt in,
# and Podman provides one -- it is the only heavyweight dependency, and it is
# a build dependency only: the installed app never uses it.
#
# Two steps cannot be silent: the Xcode Command Line Tools installer and the
# Podman installer (when Homebrew is absent) each open a window of their own
# and this script waits for you to finish it. Neither the build nor the
# install runs anything as root.
set -eu

started_machine=0   # set by stage_machine when it booted the Podman VM

# -- options ---------------------------------------------------------------
want_dmg=0; rebuild=0; install=1; keep_machine=0; only=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dmg) want_dmg=1; install=0 ;;   # the DMG is the deliverable; install from it
        --rebuild) rebuild=1 ;;
        --no-install) install=0 ;;
        --keep-machine) keep_machine=1 ;;
        --only) shift; only="${1:-}" ;;
        -h|--help) sed -n '5,36p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '\033[1m== %s ==\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Wait for a condition, with a stable message. The user is doing something
# in another window; do not scroll their terminal while they do.
wait_for() {  # wait_for "message" command...
    msg="$1"; shift
    if "$@" >/dev/null 2>&1; then return 0; fi
    note "$msg"
    while ! "$@" >/dev/null 2>&1; do sleep 3; done
}

# -- the checkout this script lives in ----------------------------------------
here="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || here=""
if [ -n "$here" ] && [ -f "$here/../container/Containerfile" ] && [ -d "$here/../agent" ]; then
    REPO="$(cd "$here/.." && pwd)"
else
    die "run this from a checkout of lpbridge (tools/bootstrap.sh); it does not fetch the code itself"
fi

# -- stages -------------------------------------------------------------------
stage_preflight() {
    say "checking this Mac"
    [ "$(uname -s)" = Darwin ] || die "this is a macOS project"
    [ "$(uname -m)" = arm64 ] || die "lpbridge builds for Apple silicon only (this is $(uname -m))"
    major=$(sw_vers -productVersion | cut -d. -f1)
    [ "$major" -ge 13 ] || die "macOS 13 or newer is required (this is $(sw_vers -productVersion))"
    avail_gb=$(df -g "$HOME" | awk 'NR==2 { print $4 }')
    [ "$avail_gb" -ge 12 ] || die "about 12 GB of free space is needed (Podman's machine, the engine build, the app); $avail_gb GB free"
    curl -sS -m 15 -o /dev/null https://snapshot.ubuntu.com/ubuntu/ \
        || die "cannot reach snapshot.ubuntu.com -- the engine build downloads every package from there"
    note "macOS $(sw_vers -productVersion), $avail_gb GB free, network OK"
}

stage_tools() {
    say "Xcode Command Line Tools (git, swiftc, python3)"
    # /usr/bin/swiftc always exists (a shim); only `xcode-select -p` says
    # whether there is anything behind it
    if ! xcode-select -p >/dev/null 2>&1; then
        note "opening Apple's installer; click Install and let it finish"
        xcode-select --install 2>/dev/null || true
        wait_for "waiting for the Command Line Tools to finish installing..." \
            sh -c 'xcode-select -p >/dev/null 2>&1 && swiftc -version >/dev/null 2>&1'
    fi
    swiftc -version >/dev/null 2>&1 || die "swiftc does not run; try: sudo xcode-select --reset"
    note "$(swiftc -version 2>&1 | head -1)"

    cd "$REPO"
    mkdir -p build
}

podman_bin() {
    for candidate in "$(command -v podman 2>/dev/null)" /opt/podman/bin/podman /opt/homebrew/bin/podman; do
        [ -n "$candidate" ] && [ -x "$candidate" ] && { echo "$candidate"; return 0; }
    done
    return 1
}

vfkit_bin() {
    for candidate in "${VFKIT:-}" "$(command -v vfkit 2>/dev/null)" /opt/podman/bin/vfkit /opt/homebrew/bin/vfkit; do
        [ -n "$candidate" ] && [ -x "$candidate" ] && { echo "$candidate"; return 0; }
    done
    return 1
}

stage_podman() {
    say "Podman (build dependency: a Linux to run apt in)"
    if ! podman_bin >/dev/null; then
        if command -v brew >/dev/null 2>&1; then
            note "brew install podman"
            brew install podman
        else
            # Podman's own installer .pkg. Its release page publishes a
            # shasums file next to the assets; verify against it. The pkg
            # wants an administrator password -- Apple's installer asks,
            # not this script.
            note "no Homebrew; fetching Podman's macOS installer"
            api="https://api.github.com/repos/containers/podman/releases/latest"
            release=$(curl -fsSL -m 30 "$api") || die "could not query Podman's releases"
            pkg_url=$(printf '%s' "$release" | grep -o '"browser_download_url": *"[^"]*podman-installer-macos-arm64.pkg"' | head -1 | sed 's/.*"\(http[^"]*\)"/\1/')
            sum_url=$(printf '%s' "$release" | grep -o '"browser_download_url": *"[^"]*shasums"' | head -1 | sed 's/.*"\(http[^"]*\)"/\1/')
            [ -n "$pkg_url" ] && [ -n "$sum_url" ] || die "Podman's latest release has no arm64 installer; install Podman from https://podman.io and rerun"
            pkg="build/$(basename "$pkg_url")"
            curl -fL -o "$pkg" "$pkg_url"
            curl -fsSL -o "$pkg.shasums" "$sum_url"
            want=$(grep "$(basename "$pkg_url")" "$pkg.shasums" | awk '{ print $1 }' | head -1)
            have=$(shasum -a 256 "$pkg" | awk '{ print $1 }')
            [ -n "$want" ] && [ "$want" = "$have" ] || die "checksum mismatch for $pkg"
            note "opening the installer; complete it, then this continues"
            open "$pkg"
            wait_for "waiting for Podman to be installed..." test -x /opt/podman/bin/podman
        fi
    fi
    PODMAN=$(podman_bin) || die "podman not found after installation"
    note "$("$PODMAN" --version)"
    VFKIT=$(vfkit_bin) || die "vfkit not found (it comes with Podman; expected /opt/podman/bin/vfkit)"
    export VFKIT
    note "vfkit at $VFKIT"
}

stage_machine() {
    say "Podman machine"
    PODMAN=$(podman_bin) || die "podman not found -- run the podman stage first"
    export CONTAINERS_MACHINE_PROVIDER="${CONTAINERS_MACHINE_PROVIDER:-applehv}"
    if ! "$PODMAN" machine inspect >/dev/null 2>&1; then
        note "creating it (downloads a ~1 GB Fedora image once)"
        "$PODMAN" machine init --now
        started_machine=1
    elif [ "$("$PODMAN" machine inspect --format '{{.State}}' 2>/dev/null)" != running ]; then
        "$PODMAN" machine start
        started_machine=1
    fi
    "$PODMAN" info --format '{{.Host.Arch}}' >/dev/null || die "podman cannot talk to its machine"
    note "running"
}

stage_vm() {
    say "engine VM (Ubuntu 24.04 + CUPS + every open driver; 10-20 min the first time)"
    cd "$REPO"
    if [ "$rebuild" = 0 ] && [ -s build/lpbridge-vm-arm64.img ] \
            && [ -s build/lpbridge-vm-arm64-vmlinuz ] && [ -s build/lpbridge-vm-arm64-initrd ]; then
        note "already built (--rebuild to redo)"
        return
    fi
    verify_base_image
    tools/build-vm.sh "$REPO/build"
}

# The engine's base is pinned by content digest in container/Containerfile.
# podman refuses an image whose manifest does not hash to the digest asked
# for, so a pull by digest is the verification -- but do it as its own step
# and say so, rather than leaving it implicit inside the build.
verify_base_image() {
    PODMAN=$(podman_bin) || die "podman not found"
    image=$(sed -n 's/^ARG BASE_IMAGE=\(.*\)$/\1/p' container/Containerfile | head -1)
    digest="${image##*@}"
    case "$digest" in
        sha256:[0-9a-f]*) ;;
        *) die "container/Containerfile does not pin BASE_IMAGE by sha256 digest ($image)" ;;
    esac
    note "engine base image: $image"
    "$PODMAN" pull -q --platform linux/arm64 "$image" >/dev/null || die "could not pull the engine base image"
    if ! "$PODMAN" image inspect --format '{{range .RepoDigests}}{{.}} {{end}}' "$image" 2>/dev/null \
            | tr ' ' '\n' | grep -q "@$digest\$"; then
        die "the pulled base image does not carry the pinned digest $digest"
    fi
    note "base image digest verified: $digest"
}

stage_app() {
    say "the app"
    cd "$REPO"
    VFKIT=$(vfkit_bin) || die "vfkit not found"
    export VFKIT
    if [ "$rebuild" = 0 ] && [ -d build/lpbridge.app ] \
            && [ build/lpbridge.app -nt build/lpbridge-vm-arm64.img ] \
            && ! find agent packaging tools container -newer build/lpbridge.app -type f 2>/dev/null | grep -q .; then
        note "already built and up to date (--rebuild to redo)"
    else
        /usr/bin/python3 packaging/build_app.py
    fi
    codesign --verify --deep --strict build/lpbridge.app
    if [ "$want_dmg" = 1 ]; then
        say "the DMG"
        [ -d build/packaging-tools ] || /usr/bin/python3 -m pip install --quiet \
            --target build/packaging-tools ds_store==1.3.1 mac_alias==2.2.2
        /usr/bin/python3 packaging/build_dmg.py
    fi
}

stage_install() {
    if [ "$install" = 1 ]; then :
    elif [ "$want_dmg" = 1 ]; then
        note "not installing: open build/lpbridge-*.dmg and drag the app to Applications"; return
    else
        note "not installing (--no-install)"; return
    fi
    say "installing"
    cd "$REPO"
    [ -d build/lpbridge.app ] || die "build/lpbridge.app is missing -- run the app stage first"
    if [ -w /Applications ]; then dest=/Applications; else dest="$HOME/Applications"; mkdir -p "$dest"; fi
    # the app's own LaunchAgent and any running instance must let go first
    launchctl bootout "gui/$(id -u)/com.lpbridge.app.agent" 2>/dev/null || true
    osascript -e 'tell application "lpbridge" to quit' 2>/dev/null || true
    sleep 1
    rm -rf "$dest/lpbridge.app"
    cp -R build/lpbridge.app "$dest/lpbridge.app"
    note "installed $dest/lpbridge.app ($(defaults read "$dest/lpbridge.app/Contents/Info.plist" CFBundleShortVersionString))"
    note "opening it -- the first run unpacks the engine and starts the service (no password needed)"
    open "$dest/lpbridge.app"
}

# -- run ----------------------------------------------------------------------
all="preflight tools podman machine vm app install"
if [ -n "$only" ]; then
    case " $all " in *" $only "*) ;; *) die "unknown stage '$only' (one of: $all)" ;; esac
    stages="$only"
else
    stages="$all"
fi

# everything is logged
log="$REPO/build/bootstrap.log"; mkdir -p "$REPO/build"
printf '\n---- %s ----\n' "$(date)" >> "$log"
exec > >(tee -a "$log") 2>&1

for stage in $stages; do
    "stage_$stage"
done
if [ "$started_machine" = 1 ] && [ -z "$only" ]; then
    if [ "$keep_machine" = 1 ]; then
        note "leaving the Podman machine running (--keep-machine); 'podman machine stop' frees its 2 GiB"
    else
        say "stopping the Podman machine (build dependency only; it holds 2 GiB while up)"
        "$(podman_bin)" machine stop >/dev/null 2>&1 || note "could not stop it; run: podman machine stop"
    fi
fi
if [ -z "$only" ]; then
    say "done"
    [ "$install" = 1 ] && note "lpbridge is in your Applications folder; the log is $REPO/build/bootstrap.log"
fi
exit 0
