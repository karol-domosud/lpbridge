#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""The print engine: a stock CUPS server in the guest, driven over IPP.

Everything the distro does for a printer -- driver matching (``cups-driverd``),
driverless setup (``lpadmin -m everywhere``), the filter chain, the network
backends -- happens inside cupsd. This module is the agent's view of it:

    engine = Engine(runtime)
    drivers = engine.rank_drivers(device)           # what CUPS would pick
    engine.add_queue("epson-l3260", "socket://192.168.1.110:9100",
                     drivers[0]["ppd_name"], info="EPSON L3260")
    job_id = engine.print_job("epson-l3260", pdf_bytes, job_attributes)
    engine.job_state(job_id)                        # honest, from cupsd

Queue names are the lpbridge printer ids. USB printers get the device URI
``lpbridge:///<id>``: the guest cannot see USB, so that backend hands the
finished bytes back to the Mac (see container/cups/lpbridge-backend).
"""

import itertools
import os
import re
import shlex
import shutil
import threading
import time
import uuid

from .ipp import client, codec
from .ipp.server import log

# Generic PPDs by printer language, in the order a distro's "Generic" list
# offers them. Only names present in the engine are offered (see
# :meth:`Engine.generic_candidates`).
GENERIC_BY_LANGUAGE = [
    ("postscript", ["drv:///sample.drv/generic.ppd",
                    "foomatic:Generic-PostScript_Printer-Postscript.ppd"]),
    ("pdf", ["lsb/usr/cupsfilters/Generic-PDF_Printer-PDF.ppd"]),
    ("pclxl", ["foomatic:Generic-PCL_6_PCL_XL_Printer-pxlmono.ppd",
               "lsb/usr/cupsfilters/pxlmono.ppd"]),
    ("pcl", ["drv:///sample.drv/generpcl.ppd",
             "foomatic:Generic-PCL_5e_Printer-ljet4.ppd",
             "foomatic:Generic-PCL_5c_Printer-cljet5.ppd"]),
    ("escp", ["foomatic:Generic-ESC_P_Dot_Matrix_Printer-epson.ppd"]),
    ("ibmppr", ["foomatic:Generic-IBM-Compatible_Dot_Matrix_Printer-ibmpro.ppd"]),
    ("urf", ["everywhere"]),
    ("pwg", ["everywhere"]),
]

_LANGUAGE_PATTERNS = [
    ("postscript", r"\b(postscript|ps|psx|br-?script)\b|application/postscript"),
    ("pdf", r"\bpdf\b|application/pdf"),
    ("pclxl", r"\bpcl ?xl\b|\bpcl6\b|pclxl"),
    ("pcl", r"\bpcl\b|\bpcl ?[345]\w*\b|application/vnd\.hp-pcl"),
    ("escp", r"\besc/?p\b|\besc/?p2\b|\bescpl?2?\b|escp|\bepson\b"),
    ("ibmppr", r"\bibmp?pr\b|\bibm ?pro ?printer\b|\bibm\b|\bmicroline\b"),
    ("urf", r"image/urf|\burf\b|airprint"),
    ("pwg", r"image/pwg-raster"),
    ("spl", r"\bspl\b|\bqpdl\b|\bsplc?\b"),
]

# What language a *generic-ish* Ghostscript/foomatic driver emits, from its
# name. Purpose-built vendor drivers (hpcups, escpr, gutenprint, foo2zjs ...)
# are deliberately absent: they know their printers better than a CMD: field.
_DRIVER_LANGUAGE = [
    ("postscript", r"foomatic/postscript\b|-postscript\.ppd|/generic\.ppd"),
    ("pcl", r"foomatic/(lj[0-9]\w*|ljet\w*|laserjet|pxl\w*|cljet\w*|hpdj|cdj\w*|pcl3)\b"
            r"|/pxlmono\.ppd|/pxlcolor\.ppd|generpcl|openprinting/[^/]+/pxl/"),
    ("escp", r"foomatic/(epson\w*|eps9\w*|lq850|stcolor|st800)\b"),
    ("ibmppr", r"foomatic/(ibmpro|okiibm)\b"),
    ("spl", r"splix|qpdl"),
]


def candidate_language(candidate):
    text = "%s %s" % (candidate.get("ppd_name") or "", candidate.get("make_and_model") or "")
    text = text.lower()
    for language, pattern in _DRIVER_LANGUAGE:
        if re.search(pattern, text):
            return language
    return None


def normalize_name(value):
    """A CUPS queue name from a printer id / model (letters, digits, - and _)."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", value or "").strip("-")
    return (name or "printer")[:120]


def device_languages(device):
    """Printer languages from a 1284 ``CMD:`` field and/or Bonjour ``pdl=``."""
    text = " ".join(filter(None, [
        _devid_field(device.get("device_id"), "CMD"),
        _devid_field(device.get("device_id"), "COMMAND SET"),
        device.get("pdl") or "",
    ])).lower()
    found = []
    for language, pattern in _LANGUAGE_PATTERNS:
        if re.search(pattern, text) and language not in found:
            found.append(language)
    return found


def _devid_field(device_id, key):
    if not device_id:
        return ""
    for part in device_id.split(";"):
        name, _sep, value = part.partition(":")
        if name.strip().upper() == key:
            return value.strip()
    return ""


class Engine(object):
    def __init__(self, runtime):
        self.runtime = runtime
        # cupsd 2.4.2 (the former Debian base) was seen to corrupt its heap under
        # concurrent CUPS-Get-PPDs / lpadmin traffic; setup calls are
        # serialised (jobs and state queries are not -- they are cheap and
        # cupsd handles them all day).
        self._setup_lock = threading.RLock()

    # -- plumbing --------------------------------------------------
    @property
    def url(self):
        return self.runtime.engine_url()

    def printer_uri(self, name):
        return "ipp://localhost/printers/%s" % name

    def _call(self, operation, groups, document=None, timeout=60):
        self.runtime.ensure_up()
        return client.call(self.url + "/", operation, groups, document=document,
                           timeout=timeout)

    def exec(self, argv, timeout=120):
        """Run a command in the engine (``lpadmin`` etc.) via the runtime."""
        self.runtime.ensure_up()
        return self.runtime.exec(argv, timeout=timeout)

    # -- drivers ------------------------------------------------------
    def get_ppds(self, device_id=None, make_and_model=None, limit=40):
        """CUPS-Get-PPDs: what ``lpinfo -m --device-id / --make-and-model`` shows,
        already scored and ordered by cups-driverd."""
        extra = [("requested-attributes", codec.TAG_KEYWORD,
                  ["ppd-name", "ppd-make", "ppd-make-and-model", "ppd-device-id",
                   "ppd-natural-language", "ppd-type"]),
                 ("limit", codec.TAG_INTEGER, [limit])]
        if device_id:
            extra.append(("ppd-device-id", codec.TAG_TEXT, [device_id]))
        if make_and_model:
            extra.append(("ppd-make-and-model", codec.TAG_TEXT, [make_and_model]))
        try:
            with self._setup_lock:
                response = self._call(client.CUPS_GET_PPDS, [(codec.TAG_OPERATION, client.operation_attributes(
                    "ipp://localhost/", extra=extra))])
        except client.IppError as exc:
            if exc.status == codec.ERR_NOT_FOUND:
                return []  # cupsd's way of saying "no PPD matches"
            raise
        out = []
        for attrs in response.groups_of(codec.TAG_PRINTER):
            out.append({
                "ppd_name": codec.first(attrs, "ppd-name"),
                "make": codec.first(attrs, "ppd-make"),
                "make_and_model": codec.first(attrs, "ppd-make-and-model"),
                "device_id": codec.first(attrs, "ppd-device-id"),
                "language": codec.first(attrs, "ppd-natural-language"),
                "type": codec.first(attrs, "ppd-type"),
            })
        return out

    def ppd_exists(self, ppd_name):
        if ppd_name == "everywhere":
            return True
        try:
            response = self._call(client.CUPS_GET_PPD, [(codec.TAG_OPERATION, client.operation_attributes(
                "ipp://localhost/", extra=[("ppd-name", codec.TAG_NAME, [ppd_name])]))])
        except client.IppError:
            return False
        return response.status < 0x0400

    def generic_candidates(self, device):
        """Generic PPDs for the languages the printer says it speaks."""
        out = []
        for language in device_languages(device):
            for _lang, names in GENERIC_BY_LANGUAGE:
                if _lang != language:
                    continue
                for name in names:
                    if name == "everywhere" and device.get("transport") == "usb-raw":
                        continue  # cupsd needs to query the printer over IP
                    if any(c["ppd_name"] == name for c in out):
                        continue
                    out.append({"ppd_name": name, "make_and_model": _generic_label(name, language),
                                "generic": True, "language": language})
        return out

    def rank_drivers(self, device, limit=12):
        """The candidate list a distro's printer tool would offer, best first.

        1. cups-driverd's device-id matches (its own scoring), then its
           make-and-model matches;
        2. ``everywhere`` for IPP Everywhere / AirPrint network printers;
        3. generic PPDs for the languages the printer advertises.
        Nothing is ever ruled out because macOS "should" handle the printer.
        """
        seen = set()
        ranked = []

        def add(candidate, source):
            name = candidate.get("ppd_name")
            if not name:
                return
            # cups-driverd lists a vendor PPD once per directory it lives in
            # (Brother/x.ppd under cups/model, lsb/usr/Brother/x.ppd under
            # share/ppd); same file, same driver -- show it once
            key = (name.rsplit("/", 1)[-1].lower(),
                   (candidate.get("make_and_model") or "").strip().lower())
            if name in seen or key in seen:
                return
            seen.update((name, key))
            entry = dict(candidate)
            entry["source"] = source
            entry["driver"] = driver_label(entry)
            ranked.append(entry)

        device_id = device.get("device_id") or ""
        make = _devid_field(device_id, "MFG") or _devid_field(device_id, "MANUFACTURER") \
            or device.get("make") or ""
        model = _devid_field(device_id, "MDL") or _devid_field(device_id, "MODEL") \
            or device.get("model") or ""
        exact = []
        if make and model:
            try:
                exact = self.get_ppds(device_id="MFG:%s;MDL:%s;" % (make, model), limit=limit)
            except client.IppError as exc:
                log("driver lookup by device-id failed: %s" % exc)
        for candidate in exact:
            add(candidate, "device-id")

        languages = device_languages(device)
        if device.get("transport") != "usb-raw" and any(l in languages for l in ("urf", "pwg", "pdf", "postscript")):
            add({"ppd_name": "everywhere", "make_and_model": "IPP Everywhere / AirPrint (driverless)"},
                "driverless")

        # Looser make-and-model searches until one of them finds something,
        # the way system-config-printer walks back a model number. The 1284
        # DESCRIPTION and COMPATIBLE ID fields (what Windows matches on) are
        # extra spellings of the same printer.
        queries = []
        per_spelling = [_make_model_queries(make, spelling)
                        for spelling in _model_spellings(device_id, make, model)]
        # exact spellings of every source first, then walk them all back
        for round_ in itertools.zip_longest(*per_spelling):
            for query in round_:
                if query and query not in queries:
                    queries.append(query)
        for query in queries:
            if len(ranked) >= 3:
                break
            try:
                found = self.get_ppds(make_and_model=query, limit=limit)
            except client.IppError as exc:
                log("driver lookup for '%s' failed: %s" % (query, exc))
                break
            for candidate in found:
                add(candidate, "make-and-model")
            if found:
                break
        for candidate in self.generic_candidates(device):
            add(candidate, "generic")
        return rerank_by_language(ranked, languages)[:limit]

    # -- user-supplied drivers ------------------------------------------
    USER_PPD_DIR = "/usr/share/ppd/lpbridge-user"

    def install_ppd(self, name, data):
        """Put a PPD the user supplied where cups-driverd lists it
        (``lsb/usr/lpbridge-user/<name>``). Returns the ppd-name."""
        if not data.lstrip().startswith(b"*PPD-Adobe"):
            raise EngineError("%s is not a PPD file" % name)
        safe = re.sub(r"[^A-Za-z0-9_.+-]+", "_", name)
        if not safe.lower().endswith(".ppd"):
            safe += ".ppd"
        work = os.path.join(self.runtime.job_spool_root(), "ppd-%s" % uuid.uuid4().hex)
        os.makedirs(work)
        try:
            local = os.path.join(work, safe)
            with open(local, "wb") as handle:
                handle.write(data)
            with self._setup_lock:
                result = self.exec(["sh", "-c", "mkdir -p %s && cp %s %s/ && chmod 644 %s/%s" % (
                    self.USER_PPD_DIR, shlex.quote(self.runtime.guest_path(local)),
                    self.USER_PPD_DIR, self.USER_PPD_DIR, shlex.quote(safe))])
            if result["exit_code"] != 0:
                raise EngineError("could not install %s: %s" % (safe, result["stderr"].strip()))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return "lsb/usr/lpbridge-user/%s" % safe

    def install_deb(self, path):
        """A vendor package the user supplied; dependencies via the engine's apt."""
        with self._setup_lock:
            return self.runtime.install_packages([path], timeout=1200)

    # -- queues --------------------------------------------------------
    def add_queue(self, name, device_uri, ppd_name, info=None, location="lpbridge", ipp_uri=None):
        with self._setup_lock:
            return self._add_queue(name, device_uri, ppd_name, info, location, ipp_uri)

    def _add_queue(self, name, device_uri, ppd_name, info, location, ipp_uri):
        """``lpadmin`` in the engine; idempotent, so it is safe on every start.

        ``everywhere`` makes cupsd query the printer over the network for its
        IPP attributes and generate the PPD -- exactly the distro's driverless
        setup, now behind the lpbridge virtual printer. lpadmin insists on an
        ``ipp://`` device URI for that, so a relay queue (USB / dry run) is
        created against ``ipp_uri`` and re-pointed at ``device_uri`` after.
        """
        argv = ["lpadmin", "-p", name, "-E", "-v", device_uri,
                "-o", "printer-is-shared=true", "-o", "printer-error-policy=retry-current-job",
                "-L", location]
        if info:
            argv += ["-D", info]
        if ppd_name == "everywhere" and not device_uri.startswith(("ipp://", "ipps://")):
            # cupsd regenerates an everywhere PPD whenever the device URI
            # changes, so a relay queue gets a *copy* of the PPD generated
            # through the printer's real IPP endpoint.
            if not ipp_uri:
                raise EngineError("driverless setup needs the printer's IPP address")
            probe = "%s-probe" % name[:100]
            script = ("set -e; lpadmin -p %(probe)s -E -v %(ipp)s -m everywhere; "
                      "for i in $(seq 1 50); do test -s /etc/cups/ppd/%(probe)s.ppd && break; sleep 0.2; done; "
                      "test -s /etc/cups/ppd/%(probe)s.ppd || { lpadmin -x %(probe)s; "
                      "echo 'the printer did not answer the IPP capability query' >&2; exit 4; }; "
                      "cp /etc/cups/ppd/%(probe)s.ppd /tmp/%(probe)s.ppd; lpadmin -x %(probe)s"
                      % {"probe": shlex.quote(probe), "ipp": shlex.quote(ipp_uri)})
            result = self.exec(["sh", "-c", script], timeout=180)
            if result["exit_code"] != 0:
                raise EngineError("driverless setup failed for %s: %s" % (
                    name, (result["stderr"] or result["stdout"]).strip()))
            argv += ["-P", "/tmp/%s.ppd" % probe]
        else:
            argv += ["-m", ppd_name]
        result = self.exec(argv, timeout=180)
        if result["exit_code"] != 0:
            raise EngineError("lpadmin failed for %s: %s" % (
                name, (result["stderr"] or result["stdout"]).strip() or "exit %d" % result["exit_code"]))
        # `-E` before `-p` enables encryption in lpadmin; after `-p` it means
        # "enable and accept". Make sure of it regardless of argument order.
        self.exec(["cupsenable", name])
        self.exec(["cupsaccept", name])

    def remove_queue(self, name):
        with self._setup_lock:
            result = self.exec(["lpadmin", "-x", name])
        return result["exit_code"] == 0

    def has_queue(self, name):
        try:
            self.printer_attributes(name, requested=["printer-name"])
            return True
        except client.IppError as exc:
            if exc.status == codec.ERR_NOT_FOUND:
                return False
            raise

    def queue_ppd(self, name, attempts=15):
        # cupsd answers the add before the PPD file is on disk; give it a moment
        for attempt in range(attempts):
            try:
                return client.fetch("%s/printers/%s.ppd" % (self.url, name))
            except client.IppError as exc:
                if exc.status != codec.ERR_NOT_FOUND or attempt == attempts - 1:
                    raise
                time.sleep(0.3)

    def printer_attributes(self, name, requested=None):
        extra = [("requested-attributes", codec.TAG_KEYWORD, requested or [
            "printer-state", "printer-state-reasons", "printer-state-message",
            "printer-is-accepting-jobs", "device-uri", "printer-make-and-model"])]
        response = self._call(codec.OP_GET_PRINTER_ATTRIBUTES, [(codec.TAG_OPERATION, client.operation_attributes(
            self.printer_uri(name), extra=extra))])
        return codec.json_safe(response.group(codec.TAG_PRINTER))

    def list_queues(self):
        try:
            response = self._call(client.CUPS_GET_PRINTERS, [(codec.TAG_OPERATION, client.operation_attributes(
                "ipp://localhost/", extra=[("requested-attributes", codec.TAG_KEYWORD, ["printer-name", "device-uri", "printer-state"])]))])
        except client.IppError as exc:
            if exc.status == codec.ERR_NOT_FOUND:
                return []
            raise
        return [codec.json_safe(attrs) for attrs in response.groups_of(codec.TAG_PRINTER)]

    # -- jobs ------------------------------------------------------------
    def print_job(self, name, document, job_attributes=None, document_format="application/pdf",
                  title="lpbridge", user="lpbridge"):
        """Print-Job with the client's own job attributes passed through, so
        cupsd applies media/sides/quality/vendor options exactly as it would
        for a Linux desktop client."""
        operation = client.operation_attributes(self.printer_uri(name), extra=[
            ("job-name", codec.TAG_NAME, [title]),
            ("document-format", codec.TAG_MIME, [document_format]),
        ], user=user)
        groups = [(codec.TAG_OPERATION, operation)]
        if job_attributes:
            groups.append((codec.TAG_JOB, job_attributes))
        response = self._call(codec.OP_PRINT_JOB, groups, document=document, timeout=300)
        job_id = codec.first(response.group(codec.TAG_JOB), "job-id")
        if not job_id:
            raise EngineError("engine accepted the job but returned no job-id")
        return int(job_id)

    def job_state(self, job_id):
        extra = [("requested-attributes", codec.TAG_KEYWORD, [
            "job-state", "job-state-reasons", "job-printer-state-message",
            "job-printer-state-reasons", "job-impressions-completed", "job-media-sheets-completed"])]
        response = self._call(codec.OP_GET_JOB_ATTRIBUTES, [(codec.TAG_OPERATION, client.operation_attributes(
            extra=[("job-uri", codec.TAG_URI, ["ipp://localhost/jobs/%d" % job_id])] + extra))])
        attrs = codec.json_safe(response.group(codec.TAG_JOB))
        reasons = attrs.get("job-state-reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        printer_reasons = attrs.get("job-printer-state-reasons") or []
        if isinstance(printer_reasons, str):
            printer_reasons = [printer_reasons]
        return {
            "state": int(attrs.get("job-state") or 0),
            "reasons": reasons,
            "message": attrs.get("job-printer-state-message") or "",
            "printer_reasons": printer_reasons,
            "impressions": int(attrs.get("job-impressions-completed") or 0),
        }

    def cancel_job(self, job_id):
        try:
            self._call(codec.OP_CANCEL_JOB, [(codec.TAG_OPERATION, client.operation_attributes(
                extra=[("job-uri", codec.TAG_URI, ["ipp://localhost/jobs/%d" % job_id])]))])
        except client.IppError as exc:
            log("cancel of engine job %d: %s" % (job_id, exc))


def rerank_by_language(ranked, languages):
    """A driver that emits a language the printer says it does not speak goes
    behind the ones that fit (stable otherwise). Only applies to drivers
    whose output language is obvious from their name, and only when the
    printer reported its languages at all (CMD: / pdl=)."""
    if not languages:
        return ranked
    spoken = set(languages)
    if "pclxl" in spoken:
        spoken.add("pcl")

    def key(candidate):
        language = candidate_language(candidate)
        return 1 if language and language not in spoken else 0

    return sorted(ranked, key=key)


class EngineError(Exception):
    pass


def driver_label(candidate):
    """Human label for a PPD candidate: 'Epson ESC/P-R', 'Gutenprint', 'splix' ..."""
    name = candidate.get("ppd_name") or ""
    mm = candidate.get("make_and_model") or ""
    if name == "everywhere":
        return "IPP Everywhere (driverless)"
    if name.startswith("lsb/usr/lpbridge-user/"):
        return "%s (your file)" % (mm or name.rsplit("/", 1)[-1])
    if candidate.get("generic"):
        return mm
    match = re.search(r"Foomatic/(\S+)", mm)
    if match:
        return "foomatic %s" % match.group(1)
    for tag, label in (("gutenprint", "Gutenprint"), ("foo2", "foo2zjs"), ("splix", "splix"),
                       ("hpcups", "HPLIP hpcups"), ("hpijs", "HPLIP hpijs"), ("escpr", "Epson ESC/P-R"),
                       ("brlaser", "brlaser"), ("ptouch", "ptouch"), ("dymo", "Dymo"),
                       ("cupsfilters", "cups-filters"), ("sample.drv", "CUPS generic"),
                       ("postscript-hp", "HP PostScript")):
        if tag in name.lower() or tag in mm.lower():
            return label
    if " - " in mm:
        return mm.split(" - ")[-1]
    return mm or name.split("/")[-1].rsplit(".ppd", 1)[0]


def _generic_label(name, language):
    labels = {"postscript": "Generic PostScript printer", "pdf": "Generic PDF printer",
              "pclxl": "Generic PCL 6 / PCL XL printer", "pcl": "Generic PCL 5 laser printer",
              "escp": "Generic ESC/P dot-matrix printer", "ibmppr": "Generic IBM 9-pin dot-matrix printer",
              "urf": "IPP Everywhere / AirPrint (driverless)", "pwg": "IPP Everywhere (driverless)"}
    label = labels.get(language, "Generic printer")
    if "cljet5" in name:
        label = "Generic PCL 5c colour laser printer"
    elif "ljet4" in name:
        label = "Generic PCL 5e laser printer"
    return label


def _model_spellings(device_id, make, model):
    """MDL, then DESCRIPTION, then COMPATIBLE ID -- each without the make."""
    out = []
    for value in (model, _devid_field(device_id, "DESCRIPTION"),
                  _devid_field(device_id, "COMPATIBLE ID"), _devid_field(device_id, "CID")):
        value = (value or "").replace("_", " ")
        value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
        value = _strip_make(make, value)
        if value and value.lower() not in [v.lower() for v in out]:
            out.append(value)
    return out


def _strip_make(make, model):
    """'Oki_ML3321' with make 'Oki Data Corporation' -> 'ML3321'."""
    canonical = _canonical_make(make)
    for prefix in {make, canonical, make.split()[0] if make.split() else ""}:
        prefix = (prefix or "").strip()
        if prefix and model.lower().startswith(prefix.lower() + " "):
            return model[len(prefix):].strip()
        if prefix and model.lower().startswith(prefix.lower()) and len(model) > len(prefix) \
                and not model[len(prefix)].isalnum():
            return model[len(prefix) + 1:].strip()
    return model


def _make_model_queries(make, model):
    """Progressively looser make-and-model searches, the way system-config-printer
    retries: 'Oki ML 321 Elite' -> 'Oki ML 321' -> 'Oki ML 32' -> 'Oki ML 3' ->
    'Oki ML'. Trailing words go first, then the digits of the model number.
    cups-driverd matches these as case-insensitive prefixes."""
    model = re.sub(r"\s*\bseries\b\s*$", "", model or "", flags=re.I).strip()
    make = _canonical_make(make)
    queries = []

    def push(query):
        query = re.sub(r"\s+", " ", query).strip()
        if query and query.lower() not in [q.lower() for q in queries]:
            queries.append(query)

    if not model:
        return []
    push("%s %s" % (make, model))
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", model.replace("_", " "))
    push("%s %s" % (make, spaced))
    tokens = spaced.split()
    # drop trailing words until the last token carries a digit
    while len(tokens) > 1 and not re.search(r"\d", tokens[-1]):
        tokens.pop()
        push("%s %s" % (make, " ".join(tokens)))
    # then shorten that model number one character at a time
    while len(tokens) > 1 and len(tokens[-1]) > 1:
        tokens[-1] = tokens[-1][:-1]
        push("%s %s" % (make, " ".join(tokens)))
    if len(tokens) > 1:
        push("%s %s" % (make, " ".join(tokens[:-1])))
    return queries[:12]


def _canonical_make(make):
    make = (make or "").strip()
    lowered = re.sub(r"[^a-z0-9]", "", make.lower())
    table = {
        "hewlettpackard": "HP", "hewlettpackardcompany": "HP", "hp": "HP",
        "okidata": "Oki", "okidatacorp": "Oki", "okidatacorporation": "Oki", "oki": "Oki",
        "seikoepson": "Epson", "seikoepsoncorp": "Epson", "epson": "Epson",
        "brotherindustries": "Brother", "brotherindustriesltd": "Brother",
        "canoninc": "Canon", "lexmarkinternational": "Lexmark", "lexmarkinternationalinc": "Lexmark",
        "samsungelectronics": "Samsung", "samsungelectronicscoltd": "Samsung",
        "kyoceradocumentsolutions": "Kyocera", "kyoceramita": "Kyocera",
        "xeroxcorporation": "Xerox", "fujixerox": "Fuji Xerox", "konicaminolta": "Konica Minolta",
        "ricohcompanyltd": "Ricoh", "starmicronics": "Star", "zebratechnologies": "Zebra",
    }
    return table.get(lowered, make)


def shell_join(argv):
    return " ".join(shlex.quote(a) for a in argv)
