#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""RFC 8010 IPP message encode/decode.

Lifted from the standalone prototype and kept deliberately small: the agent
only needs the operations an IPP Everywhere client actually sends. No third
party library, stdlib only (invariant 4).

The decoder returns groups as a list of ``(delimiter, {name: (tag, [values])})``
so that ``media-col`` and other nested collections round-trip. The encoder takes
the mirror image: ``[(delimiter, [(name, tag, [values]), ...])]``.
"""

import datetime
import struct

# --- delimiter tags --------------------------------------------------------
TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04
TAG_UNSUPPORTED_GROUP = 0x05

# --- value tags ----------------------------------------------------------
TAG_UNSUPPORTED = 0x10
TAG_UNKNOWN = 0x12
TAG_NO_VALUE = 0x13
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_OCTET = 0x30
TAG_DATETIME = 0x31
TAG_RESOLUTION = 0x32
TAG_RANGE = 0x33
TAG_BEG_COLLECTION = 0x34
TAG_TEXT_LANG = 0x35
TAG_NAME_LANG = 0x36
TAG_END_COLLECTION = 0x37
TAG_TEXT = 0x41
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_URI_SCHEME = 0x46
TAG_CHARSET = 0x47
TAG_LANGUAGE = 0x48
TAG_MIME = 0x49
TAG_MEMBER_NAME = 0x4A

# --- operations --------------------------------------------------------
OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CREATE_JOB = 0x0005
OP_SEND_DOCUMENT = 0x0006
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B
OP_CLOSE_JOB = 0x003B
OP_IDENTIFY_PRINTER = 0x003C

# --- status codes --------------------------------------------------------
OK = 0x0000
OK_IGNORED_ATTRS = 0x0001
ERR_BAD_REQUEST = 0x0400
ERR_FORBIDDEN = 0x0401
ERR_NOT_FOUND = 0x0406
ERR_NOT_POSSIBLE = 0x0409
ERR_TIMEOUT = 0x040C
ERR_FORMAT_NOT_SUPPORTED = 0x040A
ERR_ATTRS_NOT_SUPPORTED = 0x040B
ERR_INTERNAL = 0x0500
ERR_OP_NOT_SUPPORTED = 0x0501
ERR_DEVICE_ERROR = 0x0502
ERR_NOT_ACCEPTING = 0x0503
ERR_BUSY = 0x0507

# --- job-state enum (RFC 8011 s5.3.7) -----------------------------------
JOB_PENDING = 3
JOB_HELD = 4
JOB_PROCESSING = 5
JOB_STOPPED = 6
JOB_CANCELED = 7
JOB_ABORTED = 8
JOB_COMPLETED = 9


class IppParseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
def _encode_value(tag, value):
    if tag in (TAG_INTEGER, TAG_ENUM):
        return struct.pack(">i", int(value))
    if tag == TAG_BOOLEAN:
        return struct.pack(">b", 1 if value else 0)
    if tag == TAG_RESOLUTION:
        return struct.pack(">iiB", int(value[0]), int(value[1]), int(value[2]))
    if tag == TAG_RANGE:
        return struct.pack(">ii", int(value[0]), int(value[1]))
    if tag in (TAG_NO_VALUE, TAG_UNKNOWN, TAG_UNSUPPORTED):
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _encode_attribute(out, name, tag, values):
    """One attribute; extra values repeat the tag with a zero-length name."""
    for index, value in enumerate(values):
        label = name.encode("utf-8") if index == 0 else b""
        if tag == TAG_BEG_COLLECTION:
            out.append(struct.pack(">BH", TAG_BEG_COLLECTION, len(label)))
            out.append(label)
            out.append(struct.pack(">H", 0))
            for member, (member_tag, member_values) in value.items():
                key = member.encode("utf-8")
                out.append(struct.pack(">BHH", TAG_MEMBER_NAME, 0, len(key)))
                out.append(key)
                _encode_attribute(out, "", member_tag, member_values)
            out.append(struct.pack(">BHH", TAG_END_COLLECTION, 0, 0))
        else:
            encoded = _encode_value(tag, value)
            out.append(struct.pack(">BH", tag, len(label)))
            out.append(label)
            out.append(struct.pack(">H", len(encoded)))
            out.append(encoded)


def build_response(status, request_id, groups, version=(1, 1)):
    """groups: list of (delimiter_tag, [(name, tag, [values]), ...])."""
    out = [struct.pack(">BBHI", version[0], version[1], status, request_id)]
    for delimiter, attributes in groups:
        out.append(struct.pack(">B", delimiter))
        for name, tag, values in attributes:
            _encode_attribute(out, name, tag, values)
    out.append(struct.pack(">B", TAG_END))
    return b"".join(out)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
def _decode_value(tag, raw):
    if tag in (TAG_INTEGER, TAG_ENUM):
        return struct.unpack(">i", raw)[0] if len(raw) == 4 else 0
    if tag == TAG_BOOLEAN:
        return bool(raw[0]) if raw else False
    if tag == TAG_RESOLUTION:
        if len(raw) == 9:
            x, y, units = struct.unpack(">iiB", raw)
            return (x, y, units)
        return (0, 0, 3)
    if tag == TAG_RANGE:
        return tuple(struct.unpack(">ii", raw)) if len(raw) == 8 else (0, 0)
    if tag in (TAG_TEXT_LANG, TAG_NAME_LANG):
        if len(raw) >= 4:
            lang_len = struct.unpack(">H", raw[0:2])[0]
            offset = 2 + lang_len
            text_len = struct.unpack(">H", raw[offset:offset + 2])[0]
            return raw[offset + 2:offset + 2 + text_len].decode("utf-8", "replace")
        return ""
    if tag in (TAG_NO_VALUE, TAG_UNKNOWN, TAG_UNSUPPORTED):
        return None
    return raw.decode("utf-8", "replace")


def parse_request(data):
    """Return ``(version, operation, request_id, groups, document_offset)``.

    ``groups`` is a list of ``(delimiter, {name: (tag, [values])})``.
    """
    if len(data) < 9:
        raise IppParseError("message too short")
    major, minor, operation, request_id = struct.unpack(">BBHI", data[:8])
    pos = 8
    groups = []
    current_name = None
    current = None
    collection_stack = []
    pending_member = None

    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == TAG_END:
            break
        if tag < 0x10:  # delimiter
            current = {}
            groups.append((tag, current))
            current_name = None
            continue
        if current is None:
            raise IppParseError("attribute before any group delimiter")

        name_len = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        name = data[pos:pos + name_len].decode("utf-8", "replace")
        pos += name_len
        value_len = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        raw = data[pos:pos + value_len]
        pos += value_len

        if tag == TAG_BEG_COLLECTION:
            collection = {}
            if collection_stack:
                target_name = pending_member
                pending_member = None
            else:
                target_name = name if name_len else current_name
            collection_stack.append((target_name, collection, name_len != 0))
            continue
        if tag == TAG_MEMBER_NAME:
            pending_member = raw.decode("utf-8", "replace")
            continue
        if tag == TAG_END_COLLECTION:
            if not collection_stack:
                continue
            target_name, collection, is_new = collection_stack.pop()
            container = collection_stack[-1][1] if collection_stack else current
            if collection_stack:
                container[target_name] = (TAG_BEG_COLLECTION, [collection])
            else:
                if is_new or target_name not in container:
                    container[target_name] = (TAG_BEG_COLLECTION, [collection])
                else:
                    container[target_name][1].append(collection)
                current_name = target_name
            pending_member = None
            continue

        value = _decode_value(tag, raw)
        if collection_stack:
            key = pending_member if name_len == 0 else name
            collection = collection_stack[-1][1]
            if key in collection:
                collection[key][1].append(value)
            else:
                collection[key] = (tag, [value])
            pending_member = None
        elif name_len == 0 and current_name:
            current[current_name][1].append(value)
        else:
            current[name] = (tag, [value])
            current_name = name

    return (major, minor), operation, request_id, groups, pos


# ---------------------------------------------------------------------------
# Small helpers shared by server / attributes
# ---------------------------------------------------------------------------
def group_dict(groups, delimiter):
    merged = {}
    for tag, attributes in groups:
        if tag == delimiter:
            merged.update(attributes)
    return merged


def first(attributes, name, fallback=None):
    entry = attributes.get(name)
    if not entry or not entry[1]:
        return fallback
    return entry[1][0]


def format_resolution(value):
    x, y, units = value
    suffix = "dpi" if units == 3 else "dpcm"
    return "%d%s" % (x, suffix) if x == y else "%dx%d%s" % (x, y, suffix)


def json_safe(attributes):
    """Turn a decoded attribute dict into something ``json.dump`` can handle."""
    out = {}
    for name, (tag, values) in attributes.items():
        converted = []
        for value in values:
            if isinstance(value, dict):
                converted.append(json_safe(value))
            elif isinstance(value, tuple) and tag == TAG_RESOLUTION:
                converted.append(format_resolution(value))
            elif isinstance(value, tuple):
                converted.append(list(value))
            elif isinstance(value, bytes):
                converted.append(value.decode("utf-8", "replace"))
            else:
                converted.append(value)
        out[name] = converted[0] if len(converted) == 1 else converted
    return out


def ipp_datetime(when=None):
    """RFC 2579 DateAndTime, 11 octets."""
    when = when or datetime.datetime.now(datetime.timezone.utc).astimezone()
    offset = when.utcoffset() or datetime.timedelta(0)
    total = int(offset.total_seconds())
    sign = b"+" if total >= 0 else b"-"
    total = abs(total)
    return struct.pack(">HBBBBBB", when.year, when.month, when.day, when.hour,
                       when.minute, when.second, when.microsecond // 100000) \
        + sign + struct.pack(">BB", total // 3600, (total % 3600) // 60)
