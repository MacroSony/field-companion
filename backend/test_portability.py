"""Portable defaults and missing-fixture preflight (no credentials/network)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PortabilityTests(unittest.TestCase):
    def run_script(self, script, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / 'backend' / script), *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)

    def test_custom_cache_creates_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'nested' / 'cache'
            env = dict(os.environ, FIELD_COMPANION_CACHE=str(cache))
            result = subprocess.run([sys.executable, '-c',
                                     'import server; print(server.ALLOWLIST_DIR)'],
                                    cwd=ROOT / 'backend', env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(cache))
            self.assertTrue(cache.is_dir())

    def test_default_cache_is_repo_relative(self):
        env = dict(os.environ)
        env.pop('FIELD_COMPANION_CACHE', None)
        result = subprocess.run([sys.executable, '-c',
                                 'import server; print(server.ALLOWLIST_DIR)'],
                                cwd=ROOT / 'backend', env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(ROOT / 'out/cache'))

    def test_missing_selftest_fixtures_skip_before_api_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, FIELD_COMPANION_TEST_DATA=directory,
                       FIELD_COMPANION_TEST_IMAGE=str(Path(directory) / 'missing.jpg'))
            for args in ((), ('--chorus', '--chorus-file', str(Path(directory) / 'missing.wav'))):
                result = self.run_script('selftest.py', *args, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('selftest_skipped', result.stdout)
                self.assertIn('Missing licensed fixture', result.stdout)

    def test_missing_vision_fixtures_skip_before_model_load(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_script('test_vision.py', '--integration', '--data-dir', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('SKIP vision integration', result.stdout)
