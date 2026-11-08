# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud
"""Driver ranking: the same vendor PPD reachable under two ppd-name paths is one choice."""
import unittest
from unittest.mock import Mock, patch

from agent.engine import Engine


class Ranking(unittest.TestCase):
    def test_same_ppd_file_under_two_paths_is_listed_once(self):
        engine = Engine(Mock())
        dup = [{"ppd_name": "Brother/brother_mfc9340cdw_printer_en.ppd",
                "make_and_model": "Brother MFC-9340CDW CUPS"},
               {"ppd_name": "lsb/usr/Brother/brother_mfc9340cdw_printer_en.ppd",
                "make_and_model": "Brother MFC-9340CDW CUPS"},
               {"ppd_name": "drv:///brlaser.drv/br9340.ppd",
                "make_and_model": "Brother MFC-9340CDW, using brlaser"}]
        with patch.object(engine, "get_ppds", return_value=dup):
            ranked = engine.rank_drivers({"device_id": "MFG:Brother;MDL:MFC-9340CDW;CMD:PJL;",
                                          "pdl": "", "transport": "jetdirect"})
        names = [c["ppd_name"] for c in ranked]
        self.assertEqual(names.count("Brother/brother_mfc9340cdw_printer_en.ppd"), 1)
        self.assertNotIn("lsb/usr/Brother/brother_mfc9340cdw_printer_en.ppd", names)
        self.assertIn("drv:///brlaser.drv/br9340.ppd", names)
