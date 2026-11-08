#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud
"""A shell into the engine, for the driver installers a file drop cannot run.

Samsung's ULD, HP's plugin and their kind are interactive installer scripts.
For those the user gets root in the guest over ssh: the agent makes one
ed25519 key pair on the Mac, the guest agent installs the public half at
every boot (accepted only from the NAT gateway address -- the Mac -- with
every kind of forwarding refused), and sshd runs only from the first shell
opening to the last one closing: the rest of the time the engine has no
listening service at all.

Opening one is a decision the person at the Mac makes, not a process: the
agent puts up a native dialog (osascript, in the login session) naming what
is about to happen, and then the person must authenticate as the account
owner -- Touch ID, Apple Watch or the account password, through the bundled
`lpbridge-authorize` helper (LocalAuthentication); a checkout without the
helper falls back to macOS's administrator password prompt. The
control API is loopback-only and unauthenticated, so without this any
program running as the user -- a `curl | sh`, a stray script -- could open a
root shell into the engine silently. A same-user process can still write to
the file RPC directly (see the trust model in docs/API.md), so the dialog is
not a security boundary between the user and their own processes; it is the
boundary between what the user meant and what something did in their name.

While a shell is open the engine must not idle out, so a shell takes a
*hold* on the lifecycle. Holds carry a TTL and the wrapper renews them; a
wrapper that dies (closed Terminal window, kill -9) simply stops renewing
and the engine goes back to its normal idle countdown. Nothing leaks.

    hold = ShellHolds(runtime, lifecycle)
    info = hold.open()            # boots the engine, returns {token, argv, ...}
    hold.renew(info["token"])     # every ~30 s while the shell runs
    hold.close(info["token"])
"""

import os
import socket
import subprocess
import threading
import time
import uuid

from .ipp.server import log

SUPPORT = os.path.expanduser("~/Library/Application Support/lpbridge")
HOLD_TTL = 90          # seconds without a renew before a hold is dropped
KEY_NAME = "id_ed25519"


def key_paths():
    directory = os.path.join(SUPPORT, "ssh")
    return os.path.join(directory, KEY_NAME), os.path.join(directory, KEY_NAME + ".pub")


def ensure_keypair():
    """The user's engine key; made once, kept on the Mac only."""
    private, public = key_paths()
    directory = os.path.dirname(private)
    if os.path.exists(private) and os.path.exists(public):
        # keep the permissions honest even if something else relaxed them
        os.chmod(directory, 0o700)
        os.chmod(private, 0o600)
        return private, public
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lpbridge engine shell",
                    "-f", private], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.chmod(private, 0o600)
    log("generated the engine shell key at %s" % private)
    return private, public


def publish_key(runtime):
    """Put the public key where the guest agent installs it at boot."""
    share = getattr(runtime, "share_root", lambda: None)()
    if not share:
        return
    _private, public = ensure_keypair()
    target = os.path.join(share, ".ssh-authorized-keys")
    with open(public) as source, open(target + ".tmp", "w") as sink:
        sink.write(source.read())
    os.replace(target + ".tmp", target)


class ShellRefused(Exception):
    """The person at the Mac did not agree to open a shell."""


CONFIRM_TIMEOUT = 60

_CONFIRM_TEXT = ("A program on this Mac is asking to open a root shell in the lpbridge "
                 "print engine (the Linux machine that drives your printers).\n\n"
                 "This is normal if you just chose \u201cOpen Engine Terminal\u2026\u201d or ran "
                 "lpbridge-shell yourself, to run a driver installer.\n\n"
                 "Anything run there has root on the engine and can reach your network. "
                 "If you did not ask for this, click Don\u2019t Allow. "
                 "You will be asked to authenticate next.")


def _applescript_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def confirm_with_dialog(timeout=CONFIRM_TIMEOUT):
    """Ask the person at the Mac; True only on an explicit Allow."""
    script = ("display dialog %s with title \"lpbridge\" with icon caution "
              "buttons {\"Don't Allow\", \"Allow\"} default button \"Don't Allow\" "
              "cancel button \"Don't Allow\" giving up after %d"
              % (_applescript_string(_CONFIRM_TEXT), timeout))
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                text=True, timeout=timeout + 15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("engine shell: could not ask the user (%s); refusing" % exc)
        return False
    # "button returned:Allow, gave up:false"; a click on Don't Allow is the
    # cancel button, which makes osascript exit 1 ("User canceled")
    out = result.stdout.strip()
    return result.returncode == 0 and "button returned:Allow" in out and "gave up:true" not in out


AUTH_REASON = "open a root shell in the lpbridge print engine"


def _authorize_helper():
    """The bundled LocalAuthentication helper, if this is an installed app or a built checkout."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (os.path.join(root, "bin", "lpbridge-authorize"),
                      os.path.join(root, "build", "lpbridge-authorize")):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def authenticate_user(timeout=150):
    """True only when the person proved they own this account."""
    helper = _authorize_helper()
    if helper:
        try:
            result = subprocess.run([helper, AUTH_REASON], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log("engine shell: authentication helper failed (%s); refusing" % exc)
            return False
        if result.returncode == 0:
            return True
        if result.returncode != 2:          # 2: no Touch ID / password policy; fall back
            log("engine shell: authentication declined (%s)" % result.stderr.strip())
            return False
    # No helper (a bare checkout): the standard administrator prompt. The
    # command it runs as root is /usr/bin/true; only the prompt matters.
    script = ('do shell script "/usr/bin/true" with prompt %s with administrator privileges'
              % _applescript_string("lpbridge wants to " + AUTH_REASON + "."))
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("engine shell: could not authenticate the user (%s); refusing" % exc)
        return False
    if result.returncode != 0:
        log("engine shell: authentication declined (%s)" % result.stderr.strip())
    return result.returncode == 0


def confirm_and_authenticate():
    """The default gate: explain in a dialog, then prove it is the account owner."""
    return confirm_with_dialog() and authenticate_user()


def _wait_for_port(host, port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("the engine's ssh did not come up within %ds" % timeout)


def transfer_dir(runtime):
    share = getattr(runtime, "share_root", lambda: None)()
    path = os.path.join(share, "transfer") if share else os.path.join(SUPPORT, "transfer")
    os.makedirs(path, exist_ok=True)
    return path


class ShellHolds(object):
    def __init__(self, runtime, lifecycle, confirm=confirm_and_authenticate):
        self.runtime = runtime
        self.lifecycle = lifecycle
        self.confirm = confirm
        self._holds = {}            # token -> last renew time
        self._lock = threading.Lock()
        threading.Thread(target=self._reaper, name="shell-holds", daemon=True).start()

    def open(self, boot_timeout=90):
        """Ask the user, boot the engine (if needed), take a hold, return how to connect."""
        if not self.confirm():
            log("engine shell refused by the user")
            raise ShellRefused("the user did not allow opening an engine shell")
        token = uuid.uuid4().hex
        self.lifecycle.job_started(setup=True)
        with self._lock:
            self._holds[token] = time.time()
        try:
            publish_key(self.runtime)
            self.runtime.ensure_up(timeout=boot_timeout)
            self.runtime.shell_start()
            argv = self.runtime.shell_command(key_paths()[0])
            # cupsd answering is what "ready" means; sshd comes up a moment
            # later (it waits for the key to be installed), so wait for it
            if argv and argv[0].endswith("ssh"):
                _wait_for_port(argv[-1].rsplit("@", 1)[-1], 22, timeout=30)
        except Exception:
            self.close(token)
            raise
        log("engine shell opened (%s)" % token[:8])
        return {"token": token, "argv": argv, "ttl": HOLD_TTL,
                "transfer": transfer_dir(self.runtime),
                "guest_transfer": "/spool/transfer"}

    def renew(self, token):
        with self._lock:
            if token not in self._holds:
                return False
            self._holds[token] = time.time()
            return True

    def close(self, token):
        with self._lock:
            if self._holds.pop(token, None) is None:
                return False
            last = not self._holds
        if last:
            # nobody is connected any more: no reason to keep a listener up
            try:
                self.runtime.shell_stop()
            except Exception as exc:  # noqa: BLE001
                log("engine shell listener not stopped: %s" % exc)
        self.lifecycle.job_finished()
        log("engine shell closed (%s)" % token[:8])
        return True

    def _reaper(self):
        while True:
            time.sleep(HOLD_TTL / 3)
            now = time.time()
            with self._lock:
                stale = [t for t, seen in self._holds.items() if now - seen > HOLD_TTL]
            for token in stale:
                log("engine shell %s stopped renewing; releasing its hold" % token[:8])
                self.close(token)
