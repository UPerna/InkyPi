"""Exercise the service's single Config instance with disposable device files."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from config import Config


class ConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "device.json"
        settings = {"future_plugin_option": {"colour": "ochre"}}
        plugin = {"name": "Morning", "plugin_id": "clock", "plugin_settings": settings, "refresh": {}}
        playlist = {"name": "Home", "start_time": "00:00", "end_time": "24:00", "plugins": [plugin]}
        self.original = {"name": "original", "playlist_config": {"playlists": [playlist], "active_playlist": "Home"},
                         "future_device_option": ["keep", 17]}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.backup = self.path.with_suffix(".json.bak")
        self.enterContext(patch.object(Config, "config_file", str(self.path)))
        self.config = Config()

    def test_legacy_settings_and_last_valid_backup(self):
        self.config.update_config({"name": "first"})
        self.config.update_value("name", "second", write=True)
        saved = Config().get_config()
        self.assertEqual(saved["name"], "second")
        self.assertEqual(saved["future_device_option"], ["keep", 17])
        self.assertEqual(saved["playlist_config"]["active_playlist"], "Home")
        plugin = saved["playlist_config"]["playlists"][0]["plugins"][0]
        self.assertEqual(plugin["plugin_settings"], {"future_plugin_option": {"colour": "ochre"}})
        self.assertEqual(json.loads(self.backup.read_bytes())["name"], "first")

    def test_failed_serialization_does_not_truncate_or_leak_into_next_save(self):
        before = self.path.read_bytes()
        with self.assertRaises(TypeError):
            self.config.update_value("invalid", object(), write=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("invalid", self.config.get_config())
        self.config.update_value("later", True, write=True)
        self.assertTrue(Config().get_config("later"))

    def test_failed_replace_preserves_primary_and_allows_retry(self):
        replace = os.replace

        def fail_primary(src, dst):
            if Path(dst) == self.path:
                raise OSError("simulated storage failure")
            return replace(src, dst)

        before = self.path.read_bytes()
        with patch("os.replace", side_effect=fail_primary), self.assertRaises(OSError):
            self.config.update_config({"name": "retry"})
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.config.get_config("name"), "original")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))
        self.config.update_config({"name": "retry"})
        self.assertEqual(Config().get_config("name"), "retry")

    def test_process_exit_during_save_preserves_previous_primary(self):
        # The old writer truncates before json.dump. The new writer commits
        # via os.replace. Stop a real subprocess at the corresponding boundary.
        program = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from config import Config
Config.config_file = sys.argv[2]
cfg = Config()
rename = os.replace
def stop_before_publish(src, dst):
    if Path(dst) == Path(Config.config_file):
        os._exit(71)
    return rename(src, dst)
def stop_in_legacy_dump(data, stream, **kwargs):
    stream.write('{')
    stream.flush()
    os._exit(71)
os.replace = stop_before_publish
json.dump = stop_in_legacy_dump
cfg.update_value('name', 'uncommitted', write=True)
"""
        before = self.path.read_bytes()
        child = subprocess.run([sys.executable, "-c", program, str(SRC), str(self.path)],
                               capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 71, child.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(Config().get_config("name"), "original")

    def test_recover_invalid_or_missing_primary_and_keep_backup(self):
        self.config.update_value("name", "new", write=True)
        backup_before = self.backup.read_bytes()
        for damaged in (b"{", b"[]", b'{"playlist_config":{"playlists":[{}]}}', None):
            with self.subTest(damaged=damaged):
                if damaged is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(damaged)
                with self.assertLogs("config", level="WARNING"):
                    recovered = Config()
                self.assertEqual(recovered.get_config("name"), "original")
                self.assertEqual(json.loads(self.path.read_bytes()), self.original)
                self.assertEqual(self.backup.read_bytes(), backup_before)

    def test_two_damaged_copies_are_left_for_manual_recovery(self):
        self.path.write_bytes(b"{")
        self.backup.write_bytes(b"[]")
        with self.assertRaises((ValueError, RuntimeError)):
            Config()
        self.assertEqual(self.path.read_bytes(), b"{")
        self.assertEqual(self.backup.read_bytes(), b"[]")

    def test_concurrent_edits_both_survive(self):
        start = Barrier(6)
        def edit(index):
            start.wait(timeout=10)
            self.config.update_config({f"setting_{index}": index})
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(edit, range(6)))
        saved = Config().get_config()
        self.assertEqual([saved[f"setting_{i}"] for i in range(6)], list(range(6)))

    @unittest.skipUnless(os.name == "posix", "Pi file permissions")
    def test_private_permissions_survive_backup_and_replace(self):
        self.path.chmod(0o640)
        old = self.path.stat()
        self.config.update_config({"name": "new"})
        for path in (self.path, self.backup):
            st = path.stat()
            self.assertEqual((st.st_uid, st.st_gid), (old.st_uid, old.st_gid))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)
