# lpbridge interfaces

lpbridge has three interfaces. Two are protocols other software speaks to
the agent; one is the agent speaking to its own engine.

| Interface | Who uses it | Where |
|---|---|---|
| [Control API](#control-api) — JSON over HTTP on `127.0.0.1:8631` | the setup app, the menubar app, `curl` | `agent/control.py` |
| [IPP virtual printers](#ipp-virtual-printers) — IPP Everywhere on the same port | macOS CUPS (the Print dialog) | `agent/ipp/server.py` |
| [Engine contract](#engine-contract) — IPP + a file RPC into the guest | the agent only | `agent/engine.py`, `container/entrypoint.sh` |

Everything binds to loopback (invariant 5). There is no authentication and
no TLS: anything running as the logged-in user can drive the agent, and
nothing else can reach it.

---

## Control API

Base URL: `http://127.0.0.1:8631` (`--port` changes it; the app reads the
same value). All responses are `application/json` with `Cache-Control:
no-store`. Bodies are UTF-8.

### The `X-lpbridge` header

Every **mutating** request (`POST`, `DELETE`) must carry `X-lpbridge: 1`, or
it is refused with `403 {"error": "missing X-lpbridge header"}`. This is not
authentication — it is a CSRF guard. A web page on another origin cannot add
a custom header to a `fetch()` without a CORS preflight, and the agent never
answers preflights, so a random site cannot add or remove printers. `GET`s
need no header.

### Errors

Errors are JSON with an `error` string and a 4xx status; `POST /api/printers`
and `DELETE` use `{"ok": false, "message": ...}` instead so the app can show
the message verbatim.

| Status | Meaning |
|---|---|
| 400 | malformed request, missing field, or the operation itself failed (`ok: false`) |
| 403 | mutating call without `X-lpbridge` |
| 404 | unknown route, printer, or device token |

### `GET /`

`text/plain` pointer for anyone who opens the port in a browser. Not an API.

### `GET /api/health`

Liveness and identity. The app polls this to decide whether the service is
up and whether it is the version the app expects.

```json
{
  "ok": true,
  "printers": 2,
  "app_protocol": 1,
  "service_build": "6c0341e8…",
  "started_at": 1789678034.55,
  "managed": true,
  "runtime": "vfkit"
}
```

| Field | |
|---|---|
| `printers` | number of printers currently served |
| `app_protocol` | bumped when this API changes incompatibly; the app refuses to drive an older service |
| `service_build` | SHA-256 over the agent's own sources (`agent/build_id.py`) — compare, do not parse |
| `started_at` | Unix time the agent process started |
| `managed` | `true` when started with `--managed`: only printers saved in `printers.json` are restored; nothing is auto-added |
| `runtime` | `vfkit` or `podman` |

### `GET /api/printers`

Every printer the agent is serving, with live job state.

```json
{"printers": [ <Printer>, … ]}
```

**Printer**

| Field | Type | |
|---|---|---|
| `id` | string | stable id; also the macOS queue name, the engine queue name and the IPP path segment |
| `name` | string | human name as shown in the Print dialog |
| `driver` | string | label of the driver in use (`"splix"`, `"Epson ESC/P-R"`, `"IPP Everywhere (driverless)"`, `"Brother HL3040CN CUPS (your file)"`, …) |
| `ppd_name` | string | the CUPS `ppd-name` the engine queue was created with, or `"everywhere"` |
| `candidates` | [Candidate] | the ranked alternatives the resolver offered (see below); empty when the driver was pinned by a profile |
| `device_uri` | string | the engine-side device URI: `socket://`, `ipp://`, `lpd://` for network printers, `lpbridge:///<id>` for USB and dry-run (relay) |
| `make_and_model` | string | the PPD's NickName |
| `transport` | string | how the last leg is done, for display (`"usb-raw → usb://…"`, `"engine -> socket://…"`, `"dryrun -> /path"`) |
| `endpoint` | string | the IPP URI macOS prints to: `ipp://localhost:8631/ipp/print/<id>` |
| `auto` | bool | discovered and added automatically (vs. pinned by a YAML profile) |
| `offline` | bool | USB printer currently unplugged; jobs queue |
| `fault` | [reason, message] or null | set after repeated failures; the reason is an IPP `printer-state-reason` macOS also shows |
| `queued` | int | jobs not yet completed, canceled or aborted |
| `jobs` | [Job] | the ten most recent jobs, newest first |

**Job**

| Field | | |
|---|---|---|
| `id` | int | lpbridge's job id (not the engine's) |
| `name` | string | the document title, or `"Untitled"` |
| `state` | int | RFC 8011 `job-state`: 3 pending, 4 held, 5 processing, 6 stopped, 7 canceled, 8 aborted, 9 completed |
| `reasons` | [string] | `job-state-reasons`, e.g. `["job-printing", "connecting-to-device"]` while the engine retries an unreachable printer |
| `created` | int | Unix time |
| `error` | string or null | why it aborted, in the engine's words |

**Candidate**

| Field | | |
|---|---|---|
| `ppd_name` | string | a CUPS ppd-name (`drv:///…`, `foomatic:…`, `escpr:…`, `lsb/usr/lpbridge-user/…`) or `"everywhere"` |
| `driver` | string | label |
| `make_and_model` | string | the PPD's own description |
| `source` | string | why it was offered: `device-id` (exact 1284 match), `make-and-model` (closest spelling), `driverless` (IPP Everywhere), `generic` (by the languages the printer reported), `profile`, `user` |
| `generic` | bool | present and `true` for generic PPDs |

### `GET /api/discover`

Printers the agent can see right now — USB (`lpinfo`), Bonjour, and the
SNMP sweep for older network printers — with a driver hint for each.

Never blocks. A scan takes several seconds and runs in the background; this
returns the last completed snapshot. Driver hints are computed lazily on
separate threads: the first call for a new printer returns `available: null`
and later calls fill it in, so poll until `available` is non-null before
enabling an Add button.

Query: `?force=1` requests a fresh scan and drops cached hints.

```json
{
  "devices": [ <Device>, … ],
  "scanning": false,
  "scanned_at": 1789678050.1
}
```

**Device**

| Field | | |
|---|---|---|
| `id` | string | the printer id this device *would* get when added — use it as the `device` token below |
| `name`, `make`, `model` | string | as reported |
| `uri` | string | `usb://…`, `socket://host:9100`, `ipp://…`, `lpd://…` |
| `transport` | string | `usb-raw`, `jetdirect`, `ipp`, `lpd` |
| `device_id` | string | IEEE 1284 device id (`MFG:…;MDL:…;CMD:…;`); for network printers synthesised from Bonjour `usb_MFG`/`usb_MDL`/`pdl` |
| `pdl` | string or null | Bonjour `pdl=` value, e.g. `image/urf,application/pdf` |
| `serving` | bool | already added |
| `available` | bool or null | `null` = hint not computed yet; `false` = no driver found |
| `driver`, `ppd_name`, `source`, `note` | | the recommended candidate (see Candidate) and a one-line reason |
| `candidates` | [Candidate] | the full ranked list, for the user to choose from |

Hints are computed with `install_vendor=False`: a vendor package that a
printer needs is *not* downloaded just to show a hint. It is downloaded when
the printer is added.

### `POST /api/printers`

Add a printer and start serving it: resolve the driver (downloading and
installing a vendor package if the catalog has one), create the engine
queue, read the PPD back, create the macOS queue, persist the selection when
`--managed`.

Request:

```json
{"device": "<Device.id>", "ppd": "<optional Candidate.ppd_name>"}
```

`device` may also be the device's `uri`. `ppd` overrides the recommended
candidate — it can be any name from `candidates`, or any ppd-name the engine
knows. Omit it to take the first candidate.

Response: `200 {"ok": true, "message": "ok"}`, or `400 {"ok": false,
"message": "<why>"}` — the message is user-facing (e.g. *"could not read the
printer's IPP capabilities at ipp://…; make sure it is switched on"*). This
call blocks for the whole setup (up to a few minutes when a vendor package
is downloaded); the app uses a 900 s timeout.

Adding a printer never hands it to macOS instead (invariant 9): if a driver
candidate exists, the printer is bridged.

### `DELETE /api/printers/<id>`

Stop serving, remove the macOS queue and its hidden raw queue, remove the
engine queue, forget the cached driver choice and the saved selection.
`200 {"ok": true}` or `404 {"ok": false, "message": "not serving <id>"}`.

### `POST /api/printers/<id>/identify`

Reserved. Currently only logs the request; `200 {"ok": true}`.

### `POST /api/drivers`

Install a driver file the user supplied. The body is the **raw file**
(`application/octet-stream`); the name comes in a header because there is no
multipart parsing:

```
POST /api/drivers
X-lpbridge: 1
X-lpbridge-filename: brother_hl3040cn_printer_en.ppd
Content-Type: application/octet-stream
Content-Length: …

<bytes>
```

Accepted: `.ppd`, `.ppd.gz` (unpacked first), `.deb` (installed into the
engine with its Ubuntu dependencies). Limit 256 MB. The file is kept under
`~/Library/Application Support/lpbridge/user-drivers/` and re-installed into
the engine on every agent start, so it survives an engine update. Cached
driver rankings are dropped so the next `/api/discover` re-matches.

```json
{"ok": true, "kind": "ppd", "detail": "lsb/usr/lpbridge-user/brother_hl3040cn_printer_en.ppd"}
{"ok": true, "kind": "deb", "detail": "installed cnrdrvcups-ufr2-uk_6.30-1.07_arm64.deb into the engine"}
```

`detail` for a PPD is the ppd-name it can now be selected by.

### `POST /api/shell`

Open a root shell in the engine: **asks the person at the Mac first** (a
native dialog from the agent, in the login session; refused after 60 s
without an answer), **then requires them to authenticate** as the account
owner -- Touch ID, Apple Watch or the account password, via the bundled
`Contents/Resources/bin/lpbridge-authorize` (LocalAuthentication,
`deviceOwnerAuthentication`); a checkout without the helper gets macOS's
administrator-password prompt instead -- then boots the engine if needed, takes a *hold* so it
does not idle out, and returns how to connect. A refusal is
`403 {"ok": false, "error": "…"}` and nothing was booted or started.

The dialog exists because the control API is loopback-only with no auth:
without it any program running as the user could open a root shell into
the engine silently. It is not a boundary against a same-user process --
the file RPC is root in the guest and lives in the user's own directory --
but it makes the shell something a person consents to, not something a
script gets for free.

```json
{"ok": true, "token": "…", "argv": ["/usr/bin/ssh", "-i", "…/ssh/id_ed25519", "…", "root@192.168.64.3"],
 "ttl": 90, "transfer": "…/lpbridge/vm-spool/transfer", "guest_transfer": "/spool/transfer"}
```

`argv` is the exact command to exec (an `ssh` invocation for the VM; `podman
exec -it` in development). The hold expires `ttl` seconds after the last
renew — a client that dies simply stops renewing and the engine goes back to
its idle countdown. `tools/lpbridge-shell` (bundled in the app as
`Contents/Resources/bin/lpbridge-shell`) is the reference client: it renews every
30 s and closes the hold on exit.

- `POST /api/shell/<token>/renew` → `{"ok": true|false}`
- `DELETE /api/shell/<token>` → `{"ok": true|false}`

The ssh key pair lives in `~/Library/Application Support/lpbridge/ssh/`
(`0700`/`0600`); the public half is written to `<share>/.ssh-authorized-keys`,
which the guest agent installs for root at every boot with
`from="<NAT gateway>",no-port-forwarding,no-agent-forwarding,no-X11-forwarding`
-- the gateway address is the Mac, so other guests on the same vmnet (podman
machine, UTM) are refused even with the key. sshd is not enabled in the
image: the agent starts `ssh.service` (via the file RPC) when the first hold
opens and stops it when the last one closes, so the engine has no listening
service outside a shell session. The engine's host key is not trusted on
first use: after starting sshd the agent reads
`/etc/ssh/ssh_host_ed25519_key.pub` over the file RPC (the channel it already
trusts with root) and writes `…/lpbridge/ssh/known_hosts` from it, so ssh
runs with `StrictHostKeyChecking=yes` and an engine update's fresh host keys
never produce a stale pin. `container/vm/sshd-lpbridge.conf` refuses
every kind of forwarding and tunnelling (`AllowTcpForwarding no`,
`PermitTunnel no`, …), so the engine cannot be used as a relay.

The trust model, stated plainly: root in the engine was never protected from
the Mac -- anything that can write to the share can run `--exec` as root --
so ssh adds no new capability for a local process. What root there reaches
is the share, the LAN through NAT, and CPU; not the Mac's files, USB or
keychain. The MOTD says so.

### `GET /api/log?n=200`

The agent's in-memory log ring, oldest first. `n` is clamped to 1–500.

```json
{"lines": ["02:19:19  job 1: engine job 3 on queue brother-…", …]}
```

### Example session

```sh
curl -s localhost:8631/api/discover | jq '.devices[] | {id, name, driver, available}'
curl -s -X POST -H 'X-lpbridge: 1' -H 'Content-Type: application/json' \
     -d '{"device":"epson-l3260-series-146806b4"}' localhost:8631/api/printers
curl -s localhost:8631/api/printers | jq '.printers[].jobs'
curl -s -X POST -H 'X-lpbridge: 1' -H 'X-lpbridge-filename: driver.deb' \
     --data-binary @driver.deb localhost:8631/api/drivers
curl -s -X DELETE -H 'X-lpbridge: 1' localhost:8631/api/printers/epson-l3260-series-146806b4
```

---

## IPP virtual printers

Each served printer is an IPP Everywhere printer at
`ipp://localhost:8631/ipp/print/<id>` (when exactly one printer is served,
`/ipp/print` also resolves to it). macOS is pointed at it with
`lpadmin -m everywhere`; the agent does that itself.

Supported operations (RFC 8011): `Print-Job`, `Validate-Job`, `Create-Job`,
`Send-Document`, `Close-Job`, `Cancel-Job`, `Get-Job-Attributes`,
`Get-Jobs`, `Get-Printer-Attributes`, `Identify-Printer`. Documents are
accepted as `application/pdf` (what macOS sends) and `application/octet-stream`.

Printer attributes (`media-supported`, `sides-supported`,
`print-color-mode-supported`, `printer-resolution-supported`,
`media-source-supported`, `media-type-supported`, `output-bin-supported`, …)
are derived from the queue's PPD by `agent/ipp/attributes.py`, so the Print
dialog shows the printer's real trays and options. Job attributes the client
sends are forwarded to the engine's cupsd verbatim, plus the PPD options the
same module maps for vendor-specific choices.

**Job state is honest** (invariant 2). A job is `pending` with
`resources-are-not-ready` while the engine boots, `processing` while the
engine's cupsd has it — with cupsd's own printer-state reasons mirrored
(`connecting-to-device`, `media-empty`, …) — and `completed` only after the
engine reports it done and, for USB printers, the bytes have been handed to
Apple's `usb://` backend. Failures come back as `aborted` with the engine's
reason.

With `--advertise`, each printer is also announced over Bonjour
(`_ipp._tcp,_universal`) so other machines on the LAN can find it; off by
default.

---

## Engine contract

The engine is a stock CUPS server in the guest (vfkit VM, or a podman
container in development). The agent is its only client.

### Over IPP

`agent/ipp/client.py` speaks to cupsd at the address the guest reports
(`<share>/.vm-ip`, port 631; `127.0.0.1:6631` under podman):

| Operation | Used for |
|---|---|
| `CUPS-Get-PPDs` (0x400C) | `rank_drivers()` — cups-driverd's own scoring by `ppd-device-id` and `ppd-make-and-model` |
| `CUPS-Get-PPD` (0x400F) | checking a ppd-name exists |
| `CUPS-Get-Printers` (0x4002) | listing queues |
| `Get-Printer-Attributes` | queue state and device URI |
| `Print-Job` | submitting the PDF with the client's job attributes |
| `Get-Job-Attributes` | polling `job-state`, `job-state-reasons`, `job-printer-state-message` |
| `Cancel-Job` | when the macOS user cancels |
| `GET /printers/<id>.ppd` | reading back the PPD cupsd installed (driverless PPDs are generated by cupsd) |

cupsd's setup calls are serialised inside `Engine` (cupsd 2.4.2 crashed with heap corruption under concurrent setup traffic).

### Over the file RPC

What IPP cannot carry goes through one shared directory (virtio-fs `/spool`
in the VM, a bind mount under podman). The agent writes
`<share>/inbox/<id>.req` — one `entrypoint.sh` argument per line — and the
guest agent (`container/vm/lpbridge-vm-agent`) runs it and writes
`<share>/outbox/<id>/{stdout,stderr,exit}` then `.complete`. Under podman
the same arguments go through `podman exec`.

```
--serve                          run cupsd in the foreground (podman); the VM uses systemd
--exec CMD [ARG…]                lpadmin, lpinfo, dpkg, sh -c … inside the engine
--dump-capabilities --ppd PATH   JSON: the PPD's option groups (legacy: the agent parses the PPD on the Mac now, `agent/ipp/ppd.py`)
--install-packages DIR           dpkg -i every .deb in DIR; dependencies from the engine's apt
```

Exit codes: 0 ok, 10 bad/missing PPD, 20 no `.deb` files or package
failure, otherwise the command's own. stderr is logged verbatim.

### Markers the guest writes

| File in `<share>` | Meaning |
|---|---|
| `.vm-ip` | the guest's IPv4 on the NAT interface; rewritten if it changes |
| `.vm-ready` | the guest agent is up and cupsd answered on 631 |
| `.vm-x86-64` | an x86-64 interpreter is registered; the file holds its name, `FEX-x86_64` or `qemu-x86_64` |
| `.vm-i386` | qemu-i386 is registered as the 32-bit x86 interpreter |
| `.ssh-authorized-keys` | written by the Mac; the guest installs it as root's `authorized_keys` at boot, restricted to the NAT gateway and with forwarding refused |
| `transfer/` | the user's transfer folder, `/spool/transfer` in the guest |
| `outbound/<id>/<cups-job-id>.prn` | a finished device stream from the `lpbridge` backend (USB relay); the agent pushes it and deletes it |

### Queues

Engine queue name = lpbridge printer id. Device URIs: the printer's own
`socket://`/`ipp://`/`lpd://` (by IP address — the guest has no mDNS), or
`lpbridge:///<id>` for the USB relay. Queues persist on the guest disk
(`DirtyCleanInterval 0`); the agent re-creates them on demand if they vanish.

User-supplied PPDs live in `/usr/share/ppd/lpbridge-user/` and appear as
`lsb/usr/lpbridge-user/<file>`.
