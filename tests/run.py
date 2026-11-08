#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Test runner -- stdlib unittest only, no pytest (invariant 4 spirit).

    python3 tests/run.py            # everything
    python3 tests/run.py golden     # just tests/test_golden.py
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def main(argv):
    loader = unittest.TestLoader()
    if argv:
        names = ["tests.test_%s" % a for a in argv]
        suite = loader.loadTestsFromNames(names)
    else:
        suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
