# Working on lpbridge

Everything below runs on an Apple-silicon Mac with the Command Line Tools and
Podman installed. Podman is a *developer* dependency only — it builds the
engine image and runs the golden tests; users never see it.

```sh
brew install podman
CONTAINERS_MACHINE_PROVIDER=applehv podman machine init --now   # once
```

`tools/bootstrap.sh` does all of the below in one go — tools, Podman, the
engine VM, the app, install — and is what the README points non-developers
at. `--only <stage>` runs one step, `--rebuild` redoes the VM and the app,
`--no-install` stops after building, `--dmg` also builds the installer. It is
also the reference for the exact order things have to happen in.

`make help` lists every target.

---

## The two halves

**The agent** (`agent/`) is stdlib Python 3 that runs on macOS. Change it and
just run it — no build step:

```sh
python3 -m agent --runtime podman --transport dryrun --verbose
```

That serves every detected printer, converting jobs in a throwaway engine
container and writing the finished device bytes to
`~/Library/Application Support/lpbridge/out/` instead of a printer. There is
no web page on the port -- the setup app is the front end -- but the JSON it
uses is one `curl` away (`curl localhost:8631/api/printers`,
`/api/discover`, `/api/log`). Drop `--transport dryrun` to print for real. `--runtime vfkit` uses the bundled VM instead of Podman.

**The engine** (`container/`) is the Linux side: an Ubuntu 24.04 CUPS server with
every open driver family. Two artefacts are built from the same recipe —
a container image for development and tests, and a bootable disk image for
the shipped VM.

```sh
make image      # -> lpbridge:latest        (2-6 min, needs network)
make vm         # -> build/lpbridge-vm-arm64.img + -vmlinuz + -initrd (10-20 min)
make vm-install # same, then copies it where a local agent will find it
```

`make vm` builds the container first, so it covers both. Touch anything under
`container/` and you need at least `make image`; touch `Containerfile.vm`,
`container/vm/` or the driver list and you need `make vm` before `make app`.

---

## Tests

```sh
make test-unit        # ~60 tests, no Podman, a couple of seconds
make test             # + golden: real engine, byte-exact device stream
python3 tests/run.py vfkit    # boots the built VM and prints through it
```

`tools/lpbridge-shell` gives you a root shell in whichever engine the running
agent uses (ssh into the VM, `podman exec` for the container). `tests/run.py <name>` runs one module (`engine`, `pipeline`, `vendor`, …).
The golden test is the contract: `tests/golden/samsung-ml-2580n/` holds a PDF,
the job options, and the exact bytes the driver must produce. Adding a printer
you own means adding a directory like it.

---

## Building the app and the installer

```sh
make app                      # -> build/lpbridge.app
python3 -m pip install --target build/packaging-tools ds_store==1.3.1 mac_alias==2.2.2
make dmg                      # -> build/lpbridge-<version>-arm64.dmg
```

`make app` needs the three `build/lpbridge-vm-arm64*` files from `make vm`,
plus vfkit (found at `/opt/podman/bin/vfkit`; override with `VFKIT=`). It
downloads the pinned portable CPython once and caches it. The bundle is signed
locally, inside out; there is no notarization step, so first launch needs
right-click → Open. The previous build is kept as `build/lpbridge.previous.app`.

The version lives in `packaging/build_app.py` (`VERSION`). Bump it there and in
`packaging/build_dmg.py`, `README.md`, and `packaging/README.md`.

### Artwork

The app icon and the DMG background are checked in as finished files:

```
packaging/assets/AppIcon.icns          -> lpbridge.app/Contents/Resources/
packaging/assets/dmg-background.tiff   -> .background/background.tiff in the volume
```

Replace them with your own and rebuild; nothing generates them. Two things the
replacements have to respect:

* the background is a TIFF with a 1x and a 2x representation at **660x420
  points** (660x420 and 1320x840 pixels). Finder draws it at the window's point
  size, so a single-resolution image looks soft on a Retina display. Keep the
  point size equal to `WindowBounds` in `build_dmg.py`;
* icon positions live in `build_dmg.py` (`Iloc`) and are measured from the
  **top-left**, while most image editors and AppKit work from the bottom-left.

### Trying a build without installing it

```sh
open build/lpbridge.app                 # runs from build/, offers to install
```

The app installs a LaunchAgent (`~/Library/LaunchAgents/com.lpbridge.app.agent.plist`)
pointing at the copy in `/Applications`. While developing, stop it so it does
not fight your foreground agent over port 8631 and the VM share:

```sh
launchctl bootout gui/$(id -u)/com.lpbridge.app.agent      # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lpbridge.app.agent.plist   # start
```

---

## Releasing

1. `make vm && make app && make dmg`
2. `tools/collect-sources.sh` — produces
   `build/lpbridge-engine-sources-<snapshot>.tar`. **Required**: it is the
   corresponding source for the GPL/AGPL binaries inside the DMG. See
   [SOURCES.md](SOURCES.md).
3. Tag, push, and attach **both** the DMG and the source tarball:

```sh
git tag -a v0.7.2 -m "v0.7.2"
git push origin main --tags
gh release create v0.7.2 \
  build/lpbridge-0.7.2-arm64.dmg \
  build/lpbridge-engine-sources-*.tar \
  --title "lpbridge 0.7.2" --notes-file docs/release-notes-0.7.2.md
```

Keep the release (and the repository) reachable for at least three years — the
source obligation outlives the download.

### Bumping the Ubuntu snapshot

`ARG SNAPSHOT` in `container/Containerfile` pins every package to one moment of
the Ubuntu archive, which is what makes the engine reproducible and the source
retrievable. Changing it means: `make vm`, re-run the tests, regenerate the
source tarball, and ship both in a new release. Do not bump it casually, and
keep it later than the base image's `Created` date (see `SOURCES.md`) — when
you bump the `ubuntu:24.04` digest, bump the snapshot with it.

---

## Day-to-day git

The work happens on a branch; `main` is what gets released.

```sh
git checkout -b my-change
# ... edit, then:
make test-unit
git add -A && git commit
git push -u origin my-change
gh pr create --base main --fill
```

After review, `gh pr merge --squash` (or merge on the web). To ship from
`main`, follow *Releasing* above.

New source files start with the two-line header every existing one has:

```
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud
```

(`//` in Swift, `*%` in a PPD — and in a PPD it goes *after* the
`*PPD-Adobe:` line, which must stay first.)

---

## Where things are

| You want to change… | Look at |
|---|---|
| How a driver is chosen | `agent/engine.py` (`rank_drivers`) |
| What happens when a printer is added | `agent/manager.py`, `agent/provision.py` |
| Job handling and state | `agent/pipeline.py`, `agent/ipp/server.py` |
| Which drivers exist at all | `container/Containerfile` |
| The VM itself (kernel, network, FEX, qemu) | `container/Containerfile.vm`, `container/vm/`, `tools/build-vm.sh` |
| Vendor driver downloads | `agent/data/vendor-drivers.json`, `agent/vendor.py` |
| The setup app | `packaging/app/*.swift` |
| The control API the apps talk to | `agent/control.py` |
| The engine shell (ssh, holds) | `agent/shell.py`, `tools/lpbridge-shell`, `container/vm/sshd-lpbridge.conf` |
| The app icon and DMG background | `packaging/assets/` |
| The DMG window layout | `packaging/build_dmg.py` (`WindowBounds`, `Iloc` positions) |

`docs/API.md` documents every interface — the
control API the apps use, the IPP surface macOS sees, and the engine
contract — and must be updated with any change to them (bump `app_protocol`
in `agent/control.py` when the control API changes incompatibly).
