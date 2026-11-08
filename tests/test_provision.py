# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""provision: the engine's ranking is cached, never second-guessed, and a
printer is never handed back to macOS."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from agent import provision
from agent.__main__ import device_to_profile


class Provision(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch('agent.provision.SUPPORT', self.temp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = device_to_profile(dict(make='Samsung', model='ML-2580N', uri='usb://test',
                                              transport='usb-raw'))

    def _engine(self, candidates):
        engine = Mock()
        engine.rank_drivers.return_value = candidates
        engine.runtime = Mock()
        return engine

    def test_negative_lookup_is_retried(self):
        path = provision._cache(self.profile['id'] + '.resolve.json')
        Path(path).write_text(json.dumps({'available': False, 'ppd_name': None}))
        engine = self._engine([{'ppd_name': 'drv:///splix-samsung.drv/ml2580n.ppd', 'driver': 'splix',
                                'make_and_model': 'Samsung ML-2580N', 'source': 'device-id'}])
        rec = provision.resolve_driver(engine, self.profile)
        self.assertTrue(rec['available'])
        self.assertEqual(rec['ppd_name'], 'drv:///splix-samsung.drv/ml2580n.ppd')
        engine.rank_drivers.assert_called_once()
        # cached now
        provision.resolve_driver(engine, self.profile)
        engine.rank_drivers.assert_called_once()

    def test_candidates_are_kept_for_the_user_to_choose(self):
        engine = self._engine([
            {'ppd_name': 'foomatic:Samsung-ML-2580-lj5gray.ppd', 'driver': 'foomatic lj5gray',
             'make_and_model': 'Samsung ML-2580', 'source': 'device-id'},
            {'ppd_name': 'drv:///splix-samsung.drv/ml2580n.ppd', 'driver': 'splix',
             'make_and_model': 'Samsung ML-2580N', 'source': 'make-and-model'}])
        rec = provision.resolve_driver(engine, self.profile)
        self.assertEqual([c['ppd_name'] for c in rec['candidates']],
                         ['foomatic:Samsung-ML-2580-lj5gray.ppd', 'drv:///splix-samsung.drv/ml2580n.ppd'])

    def test_a_pinned_ppd_skips_lookup(self):
        self.profile['driver']['ppd'] = 'everywhere'
        engine = self._engine([])
        rec = provision.resolve_driver(engine, self.profile)
        self.assertEqual(rec['ppd_name'], 'everywhere')
        engine.rank_drivers.assert_not_called()

    def test_no_driver_is_reported_not_deferred_to_macos(self):
        engine = self._engine([])
        with patch('agent.provision.vendor.match', return_value=None):
            rec = provision.resolve_driver(engine, self.profile)
        self.assertFalse(rec['available'])
        self.assertNotIn('already_works', rec)
        with self.assertRaisesRegex(provision.EngineError, 'no driver found'):
            provision.ensure_queue(engine, self.profile, rec, 'lpbridge:///x')

    def test_driverless_is_a_candidate_through_lpbridge(self):
        # a printer that "should work" on macOS is still bridged
        engine = self._engine([{'ppd_name': 'everywhere', 'driver': 'IPP Everywhere (driverless)',
                                'make_and_model': 'IPP Everywhere', 'source': 'driverless'}])
        rec = provision.resolve_driver(engine, self.profile)
        self.assertTrue(rec['available'])
        self.assertEqual(rec['ppd_name'], 'everywhere')

    def test_ensure_queue_installs_and_mirrors_the_ppd(self):
        engine = Mock()
        engine.queue_ppd.return_value = b'*PPD-Adobe: "4.3"\n*NickName: "x"\n'
        rec = {'ppd_name': 'drv:///x.drv/y.ppd', 'available': True}
        path = provision.ensure_queue(engine, self.profile, rec, 'lpbridge:///%s' % self.profile['id'])
        engine.add_queue.assert_called_once_with(self.profile['id'], 'lpbridge:///%s' % self.profile['id'],
                                                 'drv:///x.drv/y.ppd', info=self.profile['name'], ipp_uri=None,
                                                 location='lpbridge')
        self.assertTrue(Path(path).read_bytes().startswith(b'*PPD-Adobe'))


if __name__ == '__main__':
    unittest.main()


class UserDrivers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target in ('agent.provision.SUPPORT', 'agent.provision.USER_DRIVERS'):
            patcher = patch(target, self.temp.name if target.endswith('SUPPORT') else self.temp.name + '/user-drivers')
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ppd_is_installed_kept_and_restored(self):
        engine = Mock()
        engine.install_ppd.return_value = 'lsb/usr/lpbridge-user/x.ppd'
        kind, detail = provision.add_user_driver(engine, 'x.ppd', b'*PPD-Adobe: "4.3"\n')
        self.assertEqual((kind, detail), ('ppd', 'lsb/usr/lpbridge-user/x.ppd'))
        engine.install_ppd.assert_called_once_with('x.ppd', b'*PPD-Adobe: "4.3"\n')
        engine.install_ppd.reset_mock()
        self.assertEqual(provision.restore_user_drivers(engine), 1)
        engine.install_ppd.assert_called_once()

    def test_gzipped_ppd_is_unpacked(self):
        import gzip
        engine = Mock()
        engine.install_ppd.return_value = 'lsb/usr/lpbridge-user/y.ppd'
        provision.add_user_driver(engine, 'y.ppd.gz', gzip.compress(b'*PPD-Adobe: "4.3"\n'))
        engine.install_ppd.assert_called_once_with('y.ppd', b'*PPD-Adobe: "4.3"\n')

    def test_deb_is_installed_once_per_engine(self):
        engine = Mock()
        engine.exec.return_value = {'exit_code': 1, 'stdout': '', 'stderr': ''}   # no marker yet
        kind, _ = provision.add_user_driver(engine, 'drv.deb', b'!<arch>\n')
        self.assertEqual(kind, 'deb')
        engine.install_deb.assert_called_once()
        engine.exec.return_value = {'exit_code': 0, 'stdout': '', 'stderr': ''}   # marker present
        provision.restore_user_drivers(engine)
        engine.install_deb.assert_called_once()

    def test_other_files_are_refused(self):
        with self.assertRaisesRegex(provision.EngineError, 'only .ppd'):
            provision.add_user_driver(Mock(), 'driver.exe', b'MZ')
