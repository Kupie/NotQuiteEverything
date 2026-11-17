"""Cross-platform regression tests for persistence, search, and incremental updates."""

import gc
from contextlib import closing
import json
import os
import sqlite3
from unittest import mock
import time
import tempfile
import unittest
from pathlib import Path

from notquiteeverything.config import Settings
from notquiteeverything.indexer import Indexer


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        self.base = Path(self.temp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "notes.txt").write_text("alpha")
        (self.root / "backup2026.log").write_text("logs")
        (self.root / "foo%_literal.txt").write_text("literal")
        (self.root / "Projects").mkdir()
        (self.root / "Projects" / "report2026.py").write_text("print('ok')")
        self.settings = Settings(
            scan_roots=[str(self.root)], db_path=self.base / "index.db",
            config_path=self.base / "settings.json",
        )
        self.indexer = Indexer(self.settings)
        self.assertEqual(self.indexer.initialize(), (0, False))

    def test_scan_search_filters_and_cache(self):
        n, completed, error = self.indexer.scan()
        self.assertTrue(completed, error)
        self.assertEqual(n, 5)
        self.assertEqual([r.name for r in self.indexer.search("2026").records],
                         ["backup2026.log", "report2026.py"])
        self.assertEqual(self.indexer.search("backup 2026").total, 1)
        self.assertEqual(self.indexer.search("ext:log").total, 1)
        self.assertEqual(self.indexer.search("*.py").total, 1)
        self.assertEqual(self.indexer.search("folder:Projects").total, 2)
        self.assertEqual(self.indexer.search('"report2026"').total, 1)
        self.assertEqual(self.indexer.search("%_literal").total, 1)
        reopened = Indexer(self.settings)
        self.assertEqual(reopened.initialize(), (5, True))

    def test_delete_and_add_full_scan(self):
        self.indexer.scan()
        (self.root / "backup2026.log").unlink()
        (self.root / "brandnew.txt").write_text("new")
        self.assertTrue(self.indexer.scan()[1])
        self.assertEqual(self.indexer.search("backup").total, 0)
        self.assertEqual(self.indexer.search("brandnew").total, 1)

    def test_unavailable_root_keeps_old_records(self):
        self.indexer.scan()
        self.root.rename(self.base / "moved")
        count, complete, error = self.indexer.scan()
        self.assertFalse(complete)
        self.assertIn("unavailable", error)
        self.assertEqual(count, 5)
        self.assertEqual(self.indexer.search("notes").total, 1)

    def test_live_update_and_directory_rename(self):
        self.indexer.scan()
        (self.root / "live.txt").write_text("live")
        self.assertTrue(self.indexer.apply_changes([str(self.root / "live.txt")]))
        self.assertEqual(self.indexer.search("live").total, 1)
        (self.root / "live.txt").unlink()
        self.assertTrue(self.indexer.apply_changes([str(self.root / "live.txt")]))
        self.assertEqual(self.indexer.search("live").total, 0)
        old = self.root / "Projects"
        new = self.root / "Renamed"
        old.rename(new)
        self.assertTrue(self.indexer.apply_changes([str(old), str(new)]))
        self.assertEqual(self.indexer.search("path:Projects").total, 0)
        self.assertEqual(self.indexer.search("folder:Renamed").total, 2)

    def test_index_does_not_index_or_update_its_own_database(self):
        # The database may sit inside a monitored scan root in portable mode.
        self.indexer.stop()
        settings = Settings(
            scan_roots=[str(self.base)],
            config_path=self.base / "settings.json",
            db_path=self.base / "index.db",
        )
        indexer = Indexer(settings)
        indexer.initialize()
        count, complete, error = indexer.scan()
        self.assertTrue(complete, error)
        self.assertEqual(indexer.search("index.db").total, 0)
        self.assertTrue(indexer.apply_changes([str(self.base / "index.db")]))
        self.assertEqual(indexer.search("index.db").total, 0)
        indexer.stop()

    def test_page_and_sort(self):
        self.indexer.scan()
        first = self.indexer.search("2", "name", False, 1, 0)
        second = self.indexer.search("2", "name", False, 1, 1)
        self.assertEqual(first.total, 2)
        self.assertNotEqual(first.records[0].path, second.records[0].path)
        backwards = self.indexer.search("2", "name", True)
        self.assertEqual(backwards.records[0].name, second.records[0].name)

    def test_exclusions(self):
        self.indexer.scan()
        self.indexer.settings.exclude_patterns = ["*/Projects*"]
        replaced = Indexer(self.indexer.settings)
        replaced.initialize()
        self.assertTrue(replaced.scan()[1])
        self.assertEqual(replaced.search("report").total, 0)

    def _cleanup_temp(self):
        # Windows indexers/antivirus can temporarily hold SQLite files open.
        # Wait for the lock to be released rather than hiding cleanup errors.
        for attempt in range(20):
            gc.collect()
            try:
                self.temp.cleanup()
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.1)

    def tearDown(self):
        self.indexer.stop()


class ConfigTests(unittest.TestCase):
    def test_portable_database_and_migration(self):
        from notquiteeverything import config
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            repo = base / "repository"
            package = repo / "notquiteeverything"
            package.mkdir(parents=True)
            legacy = base / "LocalAppData" / "NotQuiteEverything"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text(json.dumps({
                "scan_roots": ["C:\\\\"], "rescan_interval_seconds": 600,
            }))
            with closing(sqlite3.connect(str(legacy / "index.db"))) as con:
                con.execute("CREATE TABLE example (value TEXT)")
                con.execute("INSERT INTO example VALUES ('migrated')")
                con.commit()
            with mock.patch.object(config, "__file__", str(package / "config.py")):
                with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(base / "LocalAppData"),
                                                  "APPDATA": str(base / "OldAppData")}):
                    settings = config.Settings.load()
            self.assertEqual(settings.config_path.resolve(), (repo / "settings.json").resolve())
            self.assertEqual(settings.db_path.resolve(), (repo / "index.db").resolve())
            with closing(sqlite3.connect(str(settings.db_path))) as con:
                self.assertEqual(con.execute("SELECT value FROM example").fetchone()[0], "migrated")

    def test_old_settings_and_bad_interval(self):
        with tempfile.TemporaryDirectory() as work:
            app = Path(work)
            (app / "settings.json").write_text(json.dumps({
                "scan_drive": str(app),
                "rescan_interval_seconds": "invalid",
            }))
            setting = Settings.load(app)
            self.assertEqual(setting.scan_roots, [str(app)])
            self.assertGreaterEqual(setting.rescan_interval_seconds, 30)
            setting.scan_roots = [str(app / "first"), str(app / "second")]
            setting.exclude_patterns = ["*/tmp/*"]
            setting.save()
            again = Settings.load(app)
            self.assertEqual(len(again.scan_roots), 2)
            self.assertEqual(again.exclude_patterns, ["*/tmp/*"])


if __name__ == "__main__":
    unittest.main()
