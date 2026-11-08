#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""A tiny YAML reader for driver profiles.

Invariant 4 forbids pip dependencies in the agent, and PyYAML is not in the
macOS system Python. Profiles only need a small, well-behaved subset:

* block mappings and block sequences, nested by indentation
* flow sequences ``[a, b]`` and flow mappings ``{k: v}``
* ``#`` comments, single/double quoted scalars
* ``true``/``false``/``null``/integers/floats, else string

Anything fancier (anchors, multi-line scalars, tags) is deliberately not
supported; ``lint-profiles`` rejects files this cannot round-trip.
"""

import re

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)$")


class YamlError(Exception):
    pass


def load(text):
    lines = _significant_lines(text)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise YamlError("trailing content at line %d" % (lines[index][2]))
    return value


def load_path(path):
    with open(path, "r", encoding="utf-8") as handle:
        return load(handle.read())


# ---------------------------------------------------------------------------
def _significant_lines(text):
    out = []
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        lead = stripped[:len(stripped) - len(stripped.lstrip())]
        if "\t" in lead:
            raise YamlError("tab indentation at line %d" % number)
        indent = len(stripped) - len(stripped.lstrip(" "))
        out.append((indent, stripped.strip(), number))
    return out


def _strip_comment(line):
    out = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in (" ", "\t")):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _parse_block(lines, index, indent):
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_sequence(lines, index, indent):
    items = []
    while index < len(lines):
        cur_indent, content, number = lines[index]
        if cur_indent < indent:
            break
        if cur_indent != indent or not content.startswith("-"):
            raise YamlError("bad sequence item at line %d" % number)
        rest = content[1:].strip()
        if not rest:
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
                items.append(value)
            else:
                items.append(None)
        elif ":" in rest and not _is_flow(rest):
            # inline first key of a mapping item: "- key: value"
            synthetic = [(indent + 2, rest, number)]
            j = index + 1
            while j < len(lines) and lines[j][0] > indent:
                synthetic.append(lines[j])
                j += 1
            value, consumed = _parse_mapping(synthetic, 0, indent + 2)
            items.append(value)
            index = j
        else:
            items.append(_scalar(rest))
            index += 1
    return items, index


def _parse_mapping(lines, index, indent):
    mapping = {}
    while index < len(lines):
        cur_indent, content, number = lines[index]
        if cur_indent < indent:
            break
        if cur_indent != indent:
            raise YamlError("unexpected indent at line %d" % number)
        if content.startswith("- "):
            raise YamlError("sequence item where mapping expected, line %d"
                            % number)
        key, sep, rest = content.partition(":")
        if not sep:
            raise YamlError("expected 'key: value' at line %d" % number)
        key = _scalar(key.strip())
        rest = rest.strip()
        if rest:
            mapping[key] = _scalar(rest)
            index += 1
        else:
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
                mapping[key] = value
            elif (index < len(lines) and lines[index][0] == indent
                  and lines[index][1].startswith("- ")):
                value, index = _parse_sequence(lines, index, indent)
                mapping[key] = value
            else:
                mapping[key] = None
    return mapping, index


def _is_flow(text):
    return text.startswith("[") or text.startswith("{")


def _scalar(token):
    token = token.strip()
    if not token:
        return None
    if token[0] == '"' and token[-1] == '"' and len(token) >= 2:
        return token[1:-1].encode().decode("unicode_escape")
    if token[0] == "'" and token[-1] == "'" and len(token) >= 2:
        return token[1:-1]
    if token.startswith("["):
        return _flow_sequence(token)
    if token.startswith("{"):
        return _flow_mapping(token)
    low = token.lower()
    if low in ("null", "~", "none"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


def _split_flow(body):
    parts = []
    depth = 0
    quote = None
    current = []
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _flow_sequence(token):
    if not token.endswith("]"):
        raise YamlError("unterminated flow sequence: %s" % token)
    body = token[1:-1].strip()
    if not body:
        return []
    return [_scalar(part) for part in _split_flow(body)]


def _flow_mapping(token):
    if not token.endswith("}"):
        raise YamlError("unterminated flow mapping: %s" % token)
    body = token[1:-1].strip()
    if not body:
        return {}
    out = {}
    for part in _split_flow(body):
        key, sep, value = part.partition(":")
        if not sep:
            raise YamlError("bad flow mapping entry: %s" % part)
        out[_scalar(key.strip())] = _scalar(value.strip())
    return out
