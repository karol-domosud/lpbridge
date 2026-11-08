#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

# Install vendor .deb files the Mac downloaded and verified.
#
#   driver-packages.sh install DIR
#
# The engine has its own (NAT) network, so dependencies the vendor package
# declares come from Ubuntu's signed archive like on any Linux box.
# Dependencies the archive cannot satisfy are
# skipped and the package is installed with --force-depends: the SONAMEs
# resolve against the turbo build. Nothing here touches hardware.
set -eu
export DEBIAN_FRONTEND=noninteractive

case "${1:-}" in
    install)
        dir="${2:-}"
        [ -d "$dir" ] || exit 20
        set -- "$dir"/*.deb
        [ -e "$1" ] || { echo "no .deb files in $dir" >&2; exit 20; }
        apt-get update -qq >&2 || echo "apt-get update failed; installing offline" >&2
        want=""
        for deb; do
            deps=$(dpkg-deb -f "$deb" Depends 2>/dev/null | tr ',' '\n' \
                   | sed 's/(.*//; s/^ *//' | awk -F'|' '{print $1}' | tr -d ' ')
            for d in $deps; do
                [ -n "$d" ] || continue
                dpkg -s "$d" >/dev/null 2>&1 && continue
                if apt-cache show "$d" >/dev/null 2>&1; then want="$want $d"
                else echo "dependency not in the archive, skipped: $d" >&2; fi
            done
        done
        # shellcheck disable=SC2086
        [ -z "$want" ] || apt-get install -y --no-install-recommends $want >&2 \
            || echo "some dependencies could not be installed" >&2
        dpkg -i --force-depends "$@" >&2
        dpkg --configure -a >&2 || true
        rm -rf /var/lib/apt/lists/*
        # Newly installed drivers may supply .drv sources or compressed PPDs;
        # cups-driverd picks those up by itself on the next listing.
        echo "installed: $*"
        ;;
    *) echo "usage: driver-packages.sh install DIR" >&2; exit 20 ;;
esac
