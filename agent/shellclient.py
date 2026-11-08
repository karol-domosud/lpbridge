#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud
"""Open a root shell in the lpbridge print engine.

    python3 -m agent.shellclient            interactive shell
    python3 -m agent.shellclient CMD...     run one command and exit

`tools/lpbridge-shell` (and `Contents/Resources/bin/lpbridge-shell` in the
app, behind "Open Engine Terminal…") is the launcher for this. It talks to
the running lpbridge agent on 127.0.0.1:8631, which boots the engine if
needed and keeps it up for as long as this shell lives (renewed every 30 s;
if this process dies the agent notices within 90 s and lets the engine idle
out as usual).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:%s" % os.environ.get("LPBRIDGE_PORT", "8631")
HEADERS = {"X-lpbridge": "1", "Content-Type": "application/json"}


def call(method, path, timeout=200):
    request = urllib.request.Request(BASE + path, method=method, headers=HEADERS, data=b"{}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def main(argv):
    try:
        info = call("POST", "/api/shell")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("The running lpbridge service is older than this tool and has no engine shell.\n"
                  "Install the current lpbridge app (or restart the service from this checkout).", file=sys.stderr)
        elif exc.code == 403:
            print("Not opened: the shell was not allowed in the dialog on this Mac.", file=sys.stderr)
        else:
            print("The lpbridge service refused: HTTP %d" % exc.code, file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print("lpbridge's print service is not running (%s).\n"
              "Open the lpbridge app once, or run: python3 -m agent" % exc.reason, file=sys.stderr)
        return 2
    if not info.get("ok"):
        print(info.get("error", "could not open a shell"), file=sys.stderr)
        return 1
    token = info["token"]
    stop = threading.Event()

    def renew():
        while not stop.wait(30):
            try:
                call("POST", "/api/shell/%s/renew" % token, timeout=10)
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=renew, daemon=True).start()

    if not argv:
        print("Engine shell. Your transfer folder:\n  %s  ->  %s in the engine\n"
              % (info["transfer"], info["guest_transfer"]))
    command = list(info["argv"]) + (["--"] + argv if argv and info["argv"][0].endswith("ssh") else argv)
    try:
        code = subprocess.call(command)
    except KeyboardInterrupt:
        code = 130
    finally:
        stop.set()
        try:
            call("DELETE", "/api/shell/%s" % token, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    return code


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main(sys.argv[1:]))
