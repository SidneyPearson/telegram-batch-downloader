"""Relink audited, moved completed files after rechecking their full SHA-256."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import uuid

try:
    from .audit_downloads import fingerprint
except ImportError:
    from audit_downloads import fingerprint


def relink(database, report_path, backup_directory):
    database = database.resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if Path(report["database"]).resolve() != database:
        raise ValueError("报告对应的数据库与目标不一致")
    roots = [Path(root).resolve() for root in report["roots"]]
    entries = [entry for entry in report["completed_videos"] if entry["status"] == "verified_moved"]
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5)
    backup_path = None
    try:
        verified = []
        for index, entry in enumerate(entries, 1):
            path = Path(entry["verified_path"])
            if path.is_symlink() or not any(path.resolve().is_relative_to(root) for root in roots):
                raise ValueError("已移动文件不在报告的检索目录内")
            before = path.stat()
            if before.st_size != entry["expected_size"] or fingerprint(path) != entry["expected_sha256"]:
                raise ValueError(f"文件校验与报告不同，未修改记录：{path}")
            stat = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                raise ValueError("文件在校验期间发生变化，未修改记录")
            verified.append((entry, path.resolve(), (stat.st_size, stat.st_mtime_ns, stat.st_ino)))
            if index % 10 == 0 or index == len(entries):
                print(f"Relink validation {index}/{len(entries)}", flush=True)
        if not verified:
            return {"relinked": 0, "backup": None}
        backup_directory = backup_directory.resolve()
        backup_directory.mkdir(parents=True, exist_ok=True)
        backup_path = backup_directory / ("tasks-before-relink-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
        backup = sqlite3.connect(backup_path)
        try:
            connection.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("数据库备份校验失败，未修改记录")
        finally:
            backup.close()
        changes = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            for entry, path, stamp in verified:
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != stamp:
                    raise ValueError("文件在关联期间发生变化，已回滚")
                row = connection.execute("SELECT status,payload FROM tasks WHERE key=?", (entry["key"],)).fetchone()
                if not row:
                    raise ValueError("任务记录已删除，已回滚")
                task = json.loads(row[1])
                if (row[0] != "completed" or task["status"] != "completed"
                        or task["size"] != entry["expected_size"] or task.get("sha256") != entry["expected_sha256"]
                        or task.get("final_path") != entry["original_path"]):
                    raise ValueError("任务记录与报告不同，已回滚；请重新校验")
                task["final_path"] = str(path)
                # Keep status, completion time, original save root and every other field.
                changed = connection.execute("UPDATE tasks SET payload=? WHERE key=? AND status='completed' AND payload=?",
                    (json.dumps(task, ensure_ascii=False), entry["key"], row[1])).rowcount
                if changed != 1:
                    raise ValueError("任务记录同时发生变化，已回滚")
                changes.append({"message_id": task["message_id"], "old_path": entry["original_path"], "new_path": str(path)})
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return {"relinked": len(changes), "backup": str(backup_path), "changes": changes,
                "at": datetime.now().astimezone().isoformat(), "media_files_modified": False}
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--backup-directory", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.result.parent.mkdir(parents=True, exist_ok=True)
    result = relink(arguments.database, arguments.report, arguments.backup_directory)
    arguments.result.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"relinked": result["relinked"], "backup": result["backup"]}, ensure_ascii=False), flush=True)
