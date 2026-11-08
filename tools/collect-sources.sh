#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

# Collect the corresponding source for everything the engine image ships.
#
#   tools/collect-sources.sh [out-dir]
#
# We redistribute compiled GPL/AGPL software inside the engine image, so the
# source for those exact versions has to be available to whoever receives it
# (see SOURCES.md). This produces one tarball to attach to the GitHub release:
#
#   <out>/lpbridge-engine-sources-<snapshot>.tar
#
# The engine's apt is pinned to a snapshot.ubuntu.com timestamp, so this stays
# reproducible years later.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO/build}"
IMAGE="${IMAGE:-lpbridge:latest}"
export CONTAINERS_MACHINE_PROVIDER="${CONTAINERS_MACHINE_PROVIDER:-applehv}"

podman image exists "$IMAGE" || { echo "build the engine first: make container" >&2; exit 1; }
SNAPSHOT=$(podman run --rm --entrypoint cat "$IMAGE" /etc/lpbridge-snapshot)
mkdir -p "$OUT"
TAR="$OUT/lpbridge-engine-sources-$SNAPSHOT.tar"

echo "== collecting source for $IMAGE (snapshot $SNAPSHOT)"
# apt-get source needs a non-root user for no good reason; run as root with
# --allow-unauthenticated off and let dpkg-source skip the extraction.
podman run --rm --entrypoint /bin/sh "$IMAGE" -c '
    set -eu
    mkdir -p /tmp/src && cd /tmp/src
    apt-get -qq update >/dev/null
    packages=$(dpkg-query -W -f="\${source:Package}\n" | sort -u | tr "\n" " ")
    # shellcheck disable=SC2086
    apt-get source --download-only $packages >/dev/null 2>&1 || \
        apt-get source --download-only $packages >&2
    cd /tmp && tar cf - src
' > "$TAR.part"

mv -f "$TAR.part" "$TAR"
SIZE=$(du -h "$TAR" | cut -f1)
COUNT=$(tar tf "$TAR" | grep -c "^src/[^/]*$" || true)
shasum -a 256 "$TAR" | tee "$TAR.sha256"
echo
echo "built: $TAR ($SIZE, $COUNT files)"
echo "attach it to the GitHub release; SOURCES.md points recipients at it."
