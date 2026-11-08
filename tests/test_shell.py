# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud
"""The engine shell: holds keep the engine up and never leak."""
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent import shell


class Holds(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch("agent.shell.SUPPORT", self.temp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = Mock()
        self.runtime.share_root.return_value = self.temp.name
        # a non-ssh argv: the port wait is only for real ssh targets
        self.runtime.shell_command.return_value = ["/usr/bin/podman", "exec", "-it", "engine", "bash"]
        self.lifecycle = Mock()

    def test_open_boots_holds_and_publishes_the_key(self):
        holds = shell.ShellHolds(self.runtime, self.lifecycle, confirm=lambda: True)
        info = holds.open()
        self.runtime.ensure_up.assert_called_once()
        self.lifecycle.job_started.assert_called_once_with(setup=True)
        self.assertEqual(info["argv"][:2], ["/usr/bin/podman", "exec"])
        self.assertTrue(info["transfer"].endswith("/transfer"))
        with open(self.temp.name + "/.ssh-authorized-keys") as handle:
            self.assertTrue(handle.read().startswith("ssh-ed25519 "))
        self.runtime.shell_start.assert_called_once()
        self.assertTrue(holds.close(info["token"]))
        self.lifecycle.job_finished.assert_called_once()
        self.runtime.shell_stop.assert_called_once()     # last shell: listener down
        self.assertFalse(holds.close(info["token"]))     # idempotent
        self.runtime.shell_stop.assert_called_once()

    def test_listener_stays_up_until_the_last_shell_closes(self):
        holds = shell.ShellHolds(self.runtime, self.lifecycle, confirm=lambda: True)
        first, second = holds.open(), holds.open()
        holds.close(first["token"])
        self.runtime.shell_stop.assert_not_called()
        holds.close(second["token"])
        self.runtime.shell_stop.assert_called_once()

    def test_key_permissions_are_tight(self):
        import os
        import stat
        private, _public = shell.ensure_keypair()
        self.assertEqual(stat.S_IMODE(os.stat(private).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(private)).st_mode), 0o700)

    def test_boot_failure_releases_the_hold(self):
        self.runtime.ensure_up.side_effect = RuntimeError("no VM")
        holds = shell.ShellHolds(self.runtime, self.lifecycle, confirm=lambda: True)
        with self.assertRaises(RuntimeError):
            holds.open()
        self.lifecycle.job_finished.assert_called_once()

    def test_stale_hold_is_reaped(self):
        with patch("agent.shell.HOLD_TTL", 0.3):
            holds = shell.ShellHolds(self.runtime, self.lifecycle, confirm=lambda: True)
            info = holds.open()
            self.assertTrue(holds.renew(info["token"]))
            time.sleep(0.8)
            self.lifecycle.job_finished.assert_called_once()   # reaped, not leaked
            self.assertFalse(holds.renew(info["token"]))


if __name__ == "__main__":
    unittest.main()


class Confirmation(Holds):
    def test_refusal_boots_nothing_and_holds_nothing(self):
        holds = shell.ShellHolds(self.runtime, self.lifecycle, confirm=lambda: False)
        with self.assertRaises(shell.ShellRefused):
            holds.open()
        self.lifecycle.job_started.assert_not_called()
        self.runtime.ensure_up.assert_not_called()
        self.runtime.shell_start.assert_not_called()

    def test_dialog_result_parsing(self):
        def result(code, out):
            return Mock(returncode=code, stdout=out)
        with patch("agent.shell.subprocess.run", return_value=result(0, "button returned:Allow, gave up:false")):
            self.assertTrue(shell.confirm_with_dialog(timeout=1))
        with patch("agent.shell.subprocess.run", return_value=result(0, "button returned:Allow, gave up:true")):
            self.assertFalse(shell.confirm_with_dialog(timeout=1))   # nobody answered
        with patch("agent.shell.subprocess.run", return_value=result(1, "")):
            self.assertFalse(shell.confirm_with_dialog(timeout=1))   # Don't Allow
        with patch("agent.shell.subprocess.run", side_effect=OSError("no osascript")):
            self.assertFalse(shell.confirm_with_dialog(timeout=1))

    def test_authentication_helper_results(self):
        def result(code, err=""):
            return Mock(returncode=code, stderr=err)
        with patch("agent.shell._authorize_helper", return_value="/x/lpbridge-authorize"):
            with patch("agent.shell.subprocess.run", return_value=result(0)) as run:
                self.assertTrue(shell.authenticate_user())
                self.assertEqual(run.call_count, 1)
            with patch("agent.shell.subprocess.run", return_value=result(1, "cancelled")) as run:
                self.assertFalse(shell.authenticate_user())
                self.assertEqual(run.call_count, 1)             # no fallback on a decline
            with patch("agent.shell.subprocess.run", side_effect=[result(2), result(0)]) as run:
                self.assertTrue(shell.authenticate_user())      # unavailable -> admin prompt
                self.assertEqual(run.call_count, 2)
        with patch("agent.shell._authorize_helper", return_value=None):
            with patch("agent.shell.subprocess.run", return_value=result(1, "User canceled.")):
                self.assertFalse(shell.authenticate_user())

    def test_gate_stops_at_the_dialog(self):
        with patch("agent.shell.confirm_with_dialog", return_value=False), \
             patch("agent.shell.authenticate_user") as auth:
            self.assertFalse(shell.confirm_and_authenticate())
            auth.assert_not_called()
