# Corresponding source for the binaries lpbridge distributes

The lpbridge installer contains a Linux print engine built from Ubuntu 24.04. That
engine is made of free software, most of it under the GPL, the LGPL, or (for
Ghostscript) the AGPL. Those licences allow us to hand you the compiled
result, on the condition that the **source for exactly those binaries** is
available to you. This file is how that condition is met.

Nothing in the engine is proprietary. No vendor driver blob is bundled: the
Canon and Brother packages in `agent/data/vendor-drivers.json` are downloaded
by *your* machine, from the vendor, when you choose a printer that needs one.

## Where the source is

| What | Where |
|---|---|
| lpbridge itself (agent, container recipes, packaging, build scripts) | this repository |
| Every Ubuntu package in the engine | `lpbridge-engine-sources-<snapshot>.tar`, attached to the [release](https://github.com/karol-domosud/lpbridge/releases) the installer came from |
| The same packages, upstream | `https://snapshot.ubuntu.com/ubuntu/<snapshot>/` (one archive: every architecture, `noble`, `noble-updates`, `noble-security`) |
| FEX (MIT; in the engine) | https://github.com/FEX-Emu/FEX — the `.deb` the image was built from is named in `container/Containerfile.vm` (`FEX_URL`) |
| vfkit | https://github.com/crc-org/vfkit |
| CPython | https://github.com/astral-sh/python-build-standalone and https://www.python.org/ |

`<snapshot>` is the timestamp the engine was built against. Read it from a
running engine (`cat /etc/lpbridge-snapshot`), from `container/Containerfile`
(`ARG SNAPSHOT=`), or from the tarball's file name.

If a release asset is ever missing, or you would rather have the source on
physical media, open an issue — we will get it to you at no charge beyond the
cost of the medium. This offer stands for at least three years after the
release in question.

## Why apt is pinned to a snapshot

`archive.ubuntu.com` only carries the *current* version of each package. A
month after a release, `apt-get source` there can no longer produce the source
that matches a binary we shipped — which would leave the obligation above
unsatisfiable. `snapshot.ubuntu.com` archives every version permanently, so
`container/Containerfile` points apt at one frozen timestamp. That also makes
the engine reproducible: the same recipe builds the same engine next year.

Bumping `ARG SNAPSHOT` is therefore a release-sized decision, not a routine
one. Bump it, rebuild the engine and the VM, regenerate the source tarball,
and attach the new tarball to the new release. The timestamp must be later
than the pinned base image's build date (`podman image inspect --format
'{{.Created}}' ubuntu:24.04`): apt never downgrades, so a snapshot older than
the base would leave the base's newer packages in place — outside the
snapshot, and blocking their amd64/i386 twins.

## Regenerating the tarball

```sh
make container                 # build the engine at the pinned snapshot
tools/collect-sources.sh       # -> build/lpbridge-engine-sources-<snapshot>.tar
```

It downloads the `.dsc`, `.orig.tar.*` and `.debian.tar.*` for every source
package the image contains — about **0.65 GB, ~520 files** — and prints a
SHA-256 to record next to the release.

The download comes from snapshot.ubuntu.com: expect 10–20 minutes. Nothing else in the release process depends on it, so start it
early and let it run.

## Licence of lpbridge itself

lpbridge's own code is GPL-3.0-or-later ([LICENSE](LICENSE)); every source
file carries an SPDX header saying so.

The engine image is an aggregate of many separately
licensed works; each package keeps its own copyright and licence text in
`/usr/share/doc/<package>/copyright` inside the image, and
[THIRD-PARTY.md](packaging/THIRD-PARTY.md) summarises the major ones.
