import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.relink_downloads import relink


class RelinkTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1]
        temporary_root = (workspace / "qa" / "relink-tests").resolve()
        self.assertTrue(temporary_root.is_relative_to(workspace))
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(temporary_root))
        self.addCleanup(self.temp.cleanup)
        self.database = self.root / "tasks.sqlite3"
        self.old = self.root / "old" / "video.mp4"
        self.new = self.root / "moved" / "video.mp4"
        self.new.parent.mkdir()
        self.data = b"offline video fixture"
        self.new.write_bytes(self.data)
        self.task = {"key": "fixture", "message_id": 1, "status": "completed", "size": len(self.data),
                     "sha256": hashlib.sha256(self.data).hexdigest(), "final_path": str(self.old),
                     "completed_at": 100, "output_root": str(self.root / "old")}
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("CREATE TABLE tasks(key TEXT PRIMARY KEY,status TEXT,payload TEXT)")
            connection.execute("INSERT INTO tasks VALUES(?,?,?)", ("fixture", "completed", json.dumps(self.task)))
            connection.commit()
        finally:
            connection.close()
        self.report = self.root / "audit.json"
        self.report.write_text(json.dumps({"database": str(self.database), "roots": [str(self.new.parent)],
            "completed_videos": [{"key": "fixture", "status": "verified_moved", "verified_path": str(self.new),
                "original_path": str(self.old), "expected_size": len(self.data), "expected_sha256": self.task["sha256"]}]}), encoding="utf-8")

    def apply(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return relink(self.database, self.report, self.root / "backups")

    def read_task(self, database=None):
        connection = sqlite3.connect(database or self.database)
        try:
            return json.loads(connection.execute("SELECT payload FROM tasks").fetchone()[0])
        finally:
            connection.close()

    def update(self, sql):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(sql)
            connection.commit()
        finally:
            connection.close()

    def test_success_changes_only_path_and_makes_restorable_backup(self):
        result = self.apply()
        expected = dict(self.task, final_path=str(self.new))
        self.assertEqual(self.read_task(), expected)
        self.assertEqual(self.read_task(result["backup"]), self.task)
        self.assertEqual(result["relinked"], 1)
        self.assertEqual(self.new.read_bytes(), self.data)

    def test_corrupt_same_length_file_is_rejected(self):
        self.new.write_bytes(b"x" * len(self.data))
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.read_task(), self.task)

    def test_unrelated_active_download_is_untouched(self):
        self.update("INSERT INTO tasks VALUES('active','downloading','{}')")
        result = self.apply()
        self.assertEqual(result['relinked'], 1)
        self.assertEqual(self.read_task()['final_path'], str(self.new))
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("SELECT status,payload FROM tasks WHERE key='active'").fetchone(), ('downloading', '{}'))
        finally:
            connection.close()

    def test_target_active_download_blocks_record_update(self):
        self.update("UPDATE tasks SET status='downloading' WHERE key='fixture'")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.read_task(), self.task)

    def test_changed_task_rolls_back_and_preserves_original_record(self):
        self.update("UPDATE tasks SET status='paused'")
        before = self.read_task()
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.read_task(), before)

    def test_wrong_database_report_is_rejected(self):
        report = json.loads(self.report.read_text("utf-8"))
        report["database"] = str(self.root / "other.sqlite3")
        self.report.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.read_task(), self.task)

    def test_later_record_failure_rolls_back_earlier_path_update(self):
        second = dict(self.task, key="second", message_id=2)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("INSERT INTO tasks VALUES(?,?,?)", ("second", "paused", json.dumps(second)))
            connection.commit()
        finally:
            connection.close()
        report = json.loads(self.report.read_text("utf-8"))
        report["completed_videos"].append(dict(report["completed_videos"][0], key="second"))
        self.report.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.read_task(), self.task)


if __name__ == "__main__":
    unittest.main()
