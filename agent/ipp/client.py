#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""A small IPP client for talking to the engine's cupsd (stdlib only).

The engine is a stock CUPS server; everything the agent needs from it --
listing drivers, adding queues, printing, job and printer state -- is a
standard IPP or CUPS operation. Requests reuse the codec: an IPP request has
the same wire layout as a response (version, 16-bit op/status, request id).
"""

import http.client
import itertools
import socket
import urllib.parse

from . import codec

# CUPS private operations (cups/ipp.h)
CUPS_GET_PRINTERS = 0x4002
CUPS_ADD_MODIFY_PRINTER = 0x4003
CUPS_DELETE_PRINTER = 0x4004
CUPS_GET_PPDS = 0x400C
CUPS_GET_PPD = 0x400F

_request_ids = itertools.count(1)


class IppError(Exception):
    def __init__(self, status, message=None, response=None):
        self.status = status
        self.response = response
        Exception.__init__(self, message or "IPP status 0x%04x" % status)


def _attr_list(attributes):
    """``{name: (tag, [values])}`` or ``[(name, tag, [values])]`` -> list."""
    if isinstance(attributes, dict):
        return [(name, tag, list(values)) for name, (tag, values) in attributes.items()]
    return list(attributes)


def build_request(operation, groups, request_id=None, version=(2, 0)):
    request_id = request_id or next(_request_ids)
    return codec.build_response(operation, request_id, groups, version=version), request_id


def operation_attributes(printer_uri=None, extra=None, user="lpbridge"):
    attrs = [("attributes-charset", codec.TAG_CHARSET, ["utf-8"]),
             ("attributes-natural-language", codec.TAG_LANGUAGE, ["en"])]
    if printer_uri:
        attrs.append(("printer-uri", codec.TAG_URI, [printer_uri]))
    attrs.append(("requesting-user-name", codec.TAG_NAME, [user]))
    if extra:
        attrs.extend(_attr_list(extra))
    return attrs


class Response(object):
    def __init__(self, status, request_id, groups):
        self.status = status
        self.request_id = request_id
        self.groups = groups

    def group(self, delimiter):
        return codec.group_dict(self.groups, delimiter)

    def groups_of(self, delimiter):
        return [attrs for tag, attrs in self.groups if tag == delimiter]

    @property
    def status_message(self):
        return codec.first(self.group(codec.TAG_OPERATION), "status-message")


def call(url, operation, groups, document=None, timeout=60, user="lpbridge"):
    """POST one IPP request to ``url`` (``http://host:631/...``).

    ``groups`` is ``[(delimiter, attributes)]`` where attributes are either a
    decoded dict or a list of ``(name, tag, values)``; the operation group's
    charset/language/printer-uri are added by the caller via
    :func:`operation_attributes`. Raises :class:`IppError` for any status
    outside the successful-ok range.
    """
    encoded = [(delimiter, _attr_list(attrs)) for delimiter, attrs in groups]
    body, request_id = build_request(operation, encoded)
    if document:
        body += document
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 631, timeout=timeout)
    try:
        conn.request("POST", parts.path or "/", body=body,
                     headers={"Content-Type": "application/ipp",
                              "Content-Length": str(len(body))})
        http_response = conn.getresponse()
        data = http_response.read()
    except (socket.error, http.client.HTTPException) as exc:
        raise IppError(codec.ERR_DEVICE_ERROR, "engine unreachable at %s: %s" % (url, exc))
    finally:
        conn.close()
    if http_response.status != 200:
        raise IppError(codec.ERR_DEVICE_ERROR, "engine answered HTTP %d for %s"
                       % (http_response.status, url))
    try:
        _version, status, rid, parsed, _offset = codec.parse_request(data)
    except codec.IppParseError as exc:
        raise IppError(codec.ERR_INTERNAL, "bad IPP reply from engine: %s" % exc)
    response = Response(status, rid, parsed)
    if status >= 0x0400:
        raise IppError(status, "%s (0x%04x)" % (response.status_message or "IPP error", status),
                       response=response)
    return response


def fetch(url, timeout=30):
    """Plain HTTP GET (e.g. ``/printers/<name>.ppd``)."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 631, timeout=timeout)
    try:
        conn.request("GET", parts.path or "/")
        response = conn.getresponse()
        data = response.read()
    except (socket.error, http.client.HTTPException) as exc:
        raise IppError(codec.ERR_DEVICE_ERROR, "engine unreachable at %s: %s" % (url, exc))
    finally:
        conn.close()
    if response.status != 200:
        raise IppError(codec.ERR_NOT_FOUND, "HTTP %d for %s" % (response.status, url))
    return data
