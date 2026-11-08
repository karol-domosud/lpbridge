# Building the macOS distribution

End users install the DMG. The commands below are for developers only.

```
make vm        # only when the engine needs rebuilding; requires Podman
make app       # bundles the VM, pinned portable Python, vfkit, and native UI
python3 -m pip install --target build/packaging-tools ds_store==1.3.1 mac_alias==2.2.2
make dmg
```

`make app` requires the three `build/lpbridge-vm-arm64*` files (raw disk,
kernel, initrd), macOS Command Line Tools, and vfkit at `/opt/podman/bin/vfkit`.
Override `VFKIT` to use another copy. The Python archive is pinned by SHA-256
in `dependencies.json`, downloaded only on the build machine, and cached.
`PYTHON_ARCHIVE` can point to an already-downloaded copy with the same checksum.
The app builder recompresses the actual disk input, so an old `.img.zst`
cannot accidentally ship instead of a newer disk. VM assets are fingerprinted
in the generated manifest. The previous app build is retained as
`build/lpbridge.previous.app`.

Artifacts: `build/lpbridge.app` and `build/lpbridge-0.7.3-arm64.dmg`.
The bundle is signed locally, inside out, including Python's libraries and
vfkit's virtualization entitlement. There is no notarization step.
The DMG has a custom background, icon layout, and Applications shortcut.
The background and the app icon are checked in under `assets/`. The background
is a two-representation TIFF (660x420 points at 1x and 2x) so it stays sharp on
Retina displays; its point size must keep matching `WindowBounds` in
`build_dmg.py`.
Its Finder metadata is generated directly; no Apple Events permission is needed.

## First run and service ownership

Opening a copy outside Applications offers setup and copies it into the user's
Applications folder before starting any persistent service. It will not
overwrite a pre-existing app there. The app extracts its payload into
`~/Library/Application Support/lpbridge/vm/<payload-id>/`, verifies each file,
and only then writes an installation marker. Interrupted extraction is retried;
partial files are cleaned under an exclusive setup lock. Older engines remain
intact in their own directories.

The login service is `~/Library/LaunchAgents/com.lpbridge.app.agent.plist`.
It launches the **bundled** Python with `--managed --runtime vfkit` and restores
`~/Library/Application Support/lpbridge/printers.json`. The app closes without
stopping printing. Quit closes the frontend; the service continues at login.
Logs are under the same Application Support directory, in `logs/`, and are
accessible with **Open Diagnostics**. Kernel/VM logs are in the engine directory.

The managed agent does not automatically install newly discovered printers.
Choices are explicit and persisted atomically. Device URIs distinguish identical
printer models. Background driver hints are cached rather than continually
waking the VM. A single process lock protects the service and spool.

The older development service can be replaced from the setup screen, after
checking that it has no queued jobs. Only a same-user Python agent listening on
the lpbridge port can be stopped. Old queues are retained, and printers can be
chosen again in the new app. Existing Podman machines are never stopped.

## Validation

```
python3 tests/run.py codec attributes profiles pipeline multiprinter setup
swiftc -swift-version 5 packaging/app/Bootstrap.swift tests/BootstrapTests.swift \
  -o build/bootstrap-tests -framework AppKit -framework CryptoKit
build/bootstrap-tests
codesign --verify --deep --strict build/lpbridge.app
```

The bootstrap tests exercise real gzip extraction, checksum validation, byte
progress, reuse, corruption rejection, and atomic cleanup. To verify the full
release payload in an isolated directory without installing a LaunchAgent:

```
build/bootstrap-tests build/lpbridge.app/Contents/Resources build/installer-smoke
```

For native visual QA, launch the built executable with `--preview` followed by
`welcome`, `preparing`, `discovery`, `library`, `success`, or `error`.
Preview mode is visibly labeled and performs no printer or service operations.
Sample printer names are UI fixtures only; no device profiles are shipped.

Before distributing a release, check installation and printing on a clean Mac,
including an ordinary (non-admin) account, login restoration, USB disconnection,
network discovery permissions, and Gatekeeper's first-open flow. Automated
byte-output tests cannot certify actual paper output on every printer.

When invoking the bundled Python directly for developer smoke tests, always
pass `-B` (or set `PYTHONDONTWRITEBYTECODE=1`). Writing Python caches into a
signed bundle invalidates its signature. The LaunchAgent sets this automatically;
the DMG builder rejects a bundle whose signature has changed.
