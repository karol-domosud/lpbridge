# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

import tempfile
import unittest
from pathlib import Path
from agent.build_id import BUILD_ID, source_build_id


class BuildIdentity(unittest.TestCase):
    def test_replacement_changes_fingerprint_but_not_running_identity(self):
        running = BUILD_ID
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory)/'agent.py'
            module.write_text('old = True\n')
            old = source_build_id(directory)
            module.write_text('old = False\n')
            self.assertNotEqual(old, source_build_id(directory))
            (Path(directory)/'ignored.pyc').write_bytes(b'compiled')
            current = source_build_id(directory)
            (Path(directory)/'ignored.pyc').unlink()
            self.assertEqual(current, source_build_id(directory))
        self.assertEqual(BUILD_ID, running)
