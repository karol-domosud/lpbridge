#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""IPP RFC 8010 encode/decode.

Two complementary approaches:
* drive the encoder and read it back with the decoder (round-trip);
* feed the decoder hand-built byte sequences for things the encoder path
  does not exercise (repeated values, ranges, nested collections).
"""

import struct
import unittest

from agent.ipp import codec
from agent.ipp.codec import (
    TAG_BEG_COLLECTION, TAG_ENUM, TAG_INTEGER, TAG_KEYWORD, TAG_OPERATION,
    TAG_PRINTER, TAG_RANGE, TAG_RESOLUTION, TAG_URI,
)


def _hex(*parts):
    return b"".join(parts)


class RoundTrip(unittest.TestCase):
    def test_get_printer_attributes_request(self):
        body = codec.build_response(codec.OK, 42, [
            (TAG_OPERATION, [
                ("attributes-charset", codec.TAG_CHARSET, ["utf-8"]),
                ("attributes-natural-language", codec.TAG_LANGUAGE, ["en"]),
                ("printer-uri", TAG_URI, ["ipp://localhost/ipp/print"]),
            ]),
        ])
        version, op, rid, groups, offset = codec.parse_request(body)
        self.assertEqual(version, (1, 1))
        self.assertEqual(rid, 42)
        merged = codec.group_dict(groups, TAG_OPERATION)
        self.assertEqual(codec.first(merged, "printer-uri"),
                         "ipp://localhost/ipp/print")
        self.assertEqual(offset, len(body))  # nothing trails the message

    def test_repeated_values_and_ranges(self):
        body = codec.build_response(codec.OK, 1, [
            (TAG_PRINTER, [
                ("operations-supported", TAG_ENUM, [2, 4, 11]),
                ("copies-supported", TAG_RANGE, [(1, 999)]),
                ("printer-resolution-supported", TAG_RESOLUTION,
                 [(300, 300, 3), (600, 600, 3)]),
            ]),
        ])
        _, _, _, groups, _ = codec.parse_request(body)
        printer = codec.group_dict(groups, TAG_PRINTER)
        self.assertEqual(printer["operations-supported"][1], [2, 4, 11])
        self.assertEqual(printer["copies-supported"][1], [(1, 999)])
        self.assertEqual(printer["printer-resolution-supported"][1],
                         [(300, 300, 3), (600, 600, 3)])

    def test_media_col_nested_collection_round_trip(self):
        media_col = {
            "media-size": (TAG_BEG_COLLECTION, [{
                "x-dimension": (TAG_INTEGER, [21000]),
                "y-dimension": (TAG_INTEGER, [29700]),
            }]),
            "media-type": (TAG_KEYWORD, ["stationery"]),
        }
        body = codec.build_response(codec.OK, 7, [
            (codec.TAG_JOB, [("media-col", TAG_BEG_COLLECTION, [media_col])]),
        ])
        _, _, _, groups, _ = codec.parse_request(body)
        job = codec.group_dict(groups, codec.TAG_JOB)
        col = codec.first(job, "media-col")
        self.assertIsInstance(col, dict)
        size = codec.first(col, "media-size")
        self.assertEqual(codec.first(size, "x-dimension"), 21000)
        self.assertEqual(codec.first(size, "y-dimension"), 29700)
        self.assertEqual(codec.first(col, "media-type"), "stationery")


class HandBuiltDecode(unittest.TestCase):
    def _wrap(self, attr_bytes):
        head = struct.pack(">BBHI", 1, 1, codec.OP_PRINT_JOB, 9)
        return head + struct.pack(">B", TAG_OPERATION) + attr_bytes \
            + struct.pack(">B", codec.TAG_END)

    def test_keyword_then_two_extra_values(self):
        # name-length 0 continuation values must attach to the previous name
        def kw(name, value):
            return (struct.pack(">BH", TAG_KEYWORD, len(name)) + name
                    + struct.pack(">H", len(value)) + value)
        attrs = kw(b"sides", b"two-sided-long-edge")
        attrs += (struct.pack(">BH", TAG_KEYWORD, 0)
                  + struct.pack(">H", len(b"x")) + b"x")
        data = self._wrap(attrs)
        _, _, _, groups, _ = codec.parse_request(data)
        merged = codec.group_dict(groups, TAG_OPERATION)
        self.assertEqual(merged["sides"][1], ["two-sided-long-edge", "x"])

    def test_truncated_message_raises(self):
        with self.assertRaises(codec.IppParseError):
            codec.parse_request(b"\x01\x01\x00")

    def test_datetime_is_11_octets(self):
        self.assertEqual(len(codec.ipp_datetime()), 11)


if __name__ == "__main__":
    unittest.main()
