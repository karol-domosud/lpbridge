#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

# lpbridge engine entrypoint.
#
# The engine is a stock CUPS server (cupsd + cups-filters + every open driver
# family Ubuntu ships). The lpbridge agent on the Mac talks to it over IPP for
# queues, jobs and job state, and uses these modes for the few things IPP
# cannot do:
#
#   --serve                       run cupsd in the foreground (podman runtime;
#                                 the VM runs it as a systemd service)
#   --exec CMD [ARG...]           run a command in the engine (lpadmin, lpinfo,
#                                 dpkg ...); stdout/stderr/exit pass through
#   --dump-capabilities --ppd P   JSON: the PPD's option groups (PPD -> IPP map)
#   --install-packages DIR        install .deb files the Mac already verified
#
# Exit codes: 0 ok, 10 bad/missing PPD, 20 package/plan missing, else the
# command's own.
set -u

PPDCAPS=/usr/local/lib/lpbridge/ppdcaps.awk

case "${1:-}" in
    --install-packages) exec /usr/local/lib/lpbridge/driver-packages.sh install "$2" ;;
    --exec)             shift; exec "$@" ;;
    --serve|"")
        mkdir -p /run/cups /var/spool/cups/tmp /var/log/cups /var/cache/cups
        chown -R lp:lp /var/spool/cups /var/cache/cups 2>/dev/null || true
        # keep cupsd running the way systemd does in the VM: a vendor
        # package's postinst may restart it, and cupsd has been seen to crash
        # (a vendor postinst's `/etc/init.d/cups restart` signals every cupsd
        # it finds and then daemonises its own; this loop must survive that)
        trap '' TERM INT
        while :; do
            if pidof cupsd >/dev/null 2>&1; then sleep 2; continue; fi
            /usr/sbin/cupsd -f || echo "cupsd exited ($?), restarting" >&2
            sleep 1
        done ;;
    --dump-capabilities)
        shift
        PPD=""
        while [ $# -gt 0 ]; do
            case "$1" in --ppd) PPD="$2"; shift 2 ;; *) shift ;; esac
        done
        [ -n "$PPD" ] || { echo "entrypoint: --ppd required" >&2; exit 10; }
        if [ -f "$PPD" ]; then
            case "$PPD" in
                *.gz) t=$(mktemp /tmp/lpbridge-ppd.XXXXXX.ppd); zcat "$PPD" > "$t" || exit 10; PPD="$t" ;;
            esac
        elif [ -f "$PPD.gz" ]; then
            t=$(mktemp /tmp/lpbridge-ppd.XXXXXX.ppd); zcat "$PPD.gz" > "$t" || exit 10; PPD="$t"
        else
            echo "entrypoint: PPD not found: $PPD" >&2; exit 10
        fi
        exec gawk -f "$PPDCAPS" -v ppd_path="$PPD" "$PPD" ;;
    *)  echo "entrypoint: unknown mode: $1" >&2; exit 2 ;;
esac
