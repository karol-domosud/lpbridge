# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Setup and lookup hold the same activity guard as printing."""
import unittest
from unittest.mock import Mock, patch
from agent.runtime.lifecycle import Lifecycle, STOPPED


class SetupActivity(unittest.TestCase):
    def test_shutdown_waits_for_all_lookup_and_setup_operations(self):
        with patch('agent.runtime.lifecycle.threading.Thread'), \
             patch('agent.runtime.lifecycle.os.path.exists', return_value=False):
            runtime = Mock()
            lifecycle = Lifecycle(runtime)
            lifecycle.job_started(setup=True)  # background driver lookup
            lifecycle.job_started()  # Add Printer
            lifecycle._teardown('running on battery')
            lifecycle.job_finished()
            lifecycle._teardown('running on battery')
            runtime.shutdown.assert_not_called()
            lifecycle.job_finished()
            lifecycle._teardown('running on battery')
            runtime.shutdown.assert_called_once()
            self.assertEqual(lifecycle.state, STOPPED)
            lifecycle.stop()

    def test_setup_receives_idle_grace_on_battery(self):
        with patch('agent.runtime.lifecycle.threading.Thread'), \
             patch('agent.runtime.lifecycle.os.path.exists', return_value=False), \
             patch('agent.runtime.lifecycle._on_battery', return_value=True):
            lifecycle = Lifecycle(Mock())
            lifecycle.job_started(setup=True)
            lifecycle.job_finished()
            def one_tick(_):
                lifecycle._stop = True
            with patch('agent.runtime.lifecycle.time.sleep', side_effect=one_tick):
                lifecycle._loop()
            lifecycle.runtime.shutdown.assert_not_called()
            lifecycle._stop = False
            lifecycle._deadline = 1
            with patch('agent.runtime.lifecycle.time.sleep', side_effect=one_tick):
                lifecycle._loop()
            lifecycle.runtime.shutdown.assert_called_once()
