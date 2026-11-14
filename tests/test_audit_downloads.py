import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from scripts.audit_downloads import audit, fingerprint, mp4_structure


def box(kind, payload=b""):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


VIDEO = box(b"ftyp", b"isom") + box(b"moov") + box(b"mdat", b"offline fixture")


class AuditTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1]
        temporary_root = (workspace / "qa" / "audit-tests").resolve()
        self.assertTrue(temporary_root.is_relative_to(workspace))
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(temporary_root))
        self.addCleanup(self.temp.cleanup)

    def task(self, original, key="fixture"):
        return {"key": key, "status": "completed", "kind": "video", "message_id": 1,
                "output_root": str(self.root / "downloads"), "final_path": str(original),
                "size": len(VIDEO), "sha256": hashlib.sha256(VIDEO).hexdigest(), "error_code": ""}

    def run_audit(self, tasks):
        database = self.root / "tasks.sqlite3"
        connection = sqlite3.connect(database)
        try:
            with connection:
                connection.execute("CREATE TABLE tasks(payload TEXT)")
                connection.executemany("INSERT INTO tasks VALUES(?)", [(json.dumps(task),) for task in tasks])
        finally:
            connection.close()
        before = database.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            result = audit(database, [self.root / "moved"], self.root / "report.json")
        self.assertEqual(database.read_bytes(), before)
        return result

    def test_prefix_hash_and_bounds(self):
        path = self.root / "part"
        path.write_bytes(b"abcxyz")
        self.assertEqual(fingerprint(path, 3), hashlib.sha256(b"abc").hexdigest())
        for length in (-1, 7):
            with self.assertRaises(OSError):
                fingerprint(path, length)

    def test_standard_extended_and_zero_length_boxes(self):
        path = self.root / "video.mp4"
        path.write_bytes(box(b"ftyp", b"isom") + struct.pack(">I4sQ", 1, b"moov", 16)
                         + struct.pack(">I4s", 0, b"mdat") + b"media")
        self.assertEqual(mp4_structure(path)["status"], "boundaries_ok")

    def test_truncated_box_warns(self):
        path = self.root / "video.mp4"
        path.write_bytes(box(b"moov") + struct.pack(">I4s", 100, b"mdat"))
        self.assertEqual(mp4_structure(path)["status"], "warning")

    def test_moved_file_verified_and_database_unchanged(self):
        original = self.root / "downloads" / "video.mp4"
        moved = self.root / "moved" / "sub" / original.name
        moved.parent.mkdir(parents=True)
        moved.write_bytes(VIDEO)
        result = self.run_audit([self.task(original)])
        self.assertEqual(result["completed_summary"], {"verified_moved": 1})
        self.assertEqual(result["completed_videos"][0]["verified_path"], str(moved.resolve()))

    def test_same_name_and_length_are_not_enough(self):
        original = self.root / "downloads" / "video.mp4"
        moved = self.root / "moved" / original.name
        moved.parent.mkdir(parents=True)
        moved.write_bytes(b"x" * len(VIDEO))
        result = self.run_audit([self.task(original)])
        self.assertEqual(result["completed_summary"], {"needs_review": 1})
        self.assertFalse(result["completed_videos"][0]["matches"])

    def test_copies_recognized_without_deletion(self):
        original = self.root / "downloads" / "video.mp4"
        moved = self.root / "moved" / original.name
        for path in (original, moved):
            path.parent.mkdir(parents=True)
            path.write_bytes(VIDEO)
        result = self.run_audit([self.task(original)])
        self.assertEqual(result["completed_summary"], {"verified_original": 1})
        self.assertEqual(len(result["extra_copies"]), 1)
        self.assertTrue(original.is_file() and moved.is_file())

    def test_partial_checkpoint_verified_with_uncommitted_tail(self):
        path = self.root / "downloads" / ".video.part"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"prefix" + b"uncommitted")
        task = self.task(self.root / "downloads" / "video.mp4")
        task.update(status="paused", part_path=str(path), checkpoint_bytes=6,
                    checkpoint_sha256=hashlib.sha256(b"prefix").hexdigest())
        result = self.run_audit([task])
        self.assertTrue(result["partial_files"][0]["checkpoint_hash_ok"])
        self.assertEqual(path.read_bytes(), b"prefixuncommitted")


if __name__ == "__main__":
    unittest.main()
