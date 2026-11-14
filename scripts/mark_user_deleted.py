"""Mark task records whose files the user deleted as skipped, so they never requeue.

Only records that have no matching file on disk are touched.  Completed records
with a missing file would otherwise be re-queued on the next scan; queued
LOCAL_MISSING records would be downloaded again.  Both become status "failed"
with error_code USER_DELETED, which the engine never downloads automatically.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import uuid


def main(database, backup_directory, report_path, apply_changes):
    database = Path(database).resolve()
    connection = sqlite3.connect(str(database), timeout=10)
    try:
        rows = list(connection.execute("SELECT key,status,payload FROM tasks"))
        targets = []
        for key, status, payload in rows:
            task = json.loads(payload)
            path = task.get("final_path") or ""
            present = bool(path) and Path(path).is_file() and Path(path).stat().st_size == task.get("size")
            if status == "completed" and not present:
                targets.append((key, status, payload, task))
            elif status == "queued" and task.get("error_code") == "LOCAL_MISSING" and not present:
                targets.append((key, status, payload, task))
        result = {"created_at": datetime.now().astimezone().isoformat(), "database": str(database),
                  "targets": len(targets), "by_previous_status": {},
                  "items": [{"message_id": t[3]["message_id"], "kind": t[3]["kind"], "size": t[3]["size"],
                             "date": t[3]["date"], "previous_status": t[1]} for t in targets],
                  "backup": None, "applied": 0}
        for t in targets:
            key = t[1]
            result["by_previous_status"][key] = result["by_previous_status"].get(key, 0) + 1

        if apply_changes and targets:
            backup_directory.mkdir(parents=True, exist_ok=True)
            backup = backup_directory / ("tasks-before-skip-" + datetime.now().strftime("%Y%m%d-%H%M%S")
                                         + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
            target = sqlite3.connect(str(backup))
            try:
                connection.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("备份校验失败，未修改记录")
            finally:
                target.close()
            connection.execute("BEGIN IMMEDIATE")
            try:
                for key, status, payload, task in targets:
                    current = json.loads(payload)
                    current["status"] = "failed"
                    current["error_code"] = "USER_DELETED"
                    current["error"] = "文件已被用户删除，不再重新下载；如需恢复请手动重试或重新扫描到其他目录"
                    changed = connection.execute(
                        "UPDATE tasks SET status='failed', payload=? WHERE key=? AND status=? AND payload=?",
                        (json.dumps(current, ensure_ascii=False), key, status, payload)).rowcount
                    if changed != 1:
                        raise RuntimeError(f"记录同时发生变化，已回滚：{key}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            result["backup"] = str(backup)
            result["applied"] = len(targets)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"targets": len(targets), "applied": result["applied"],
                          "backup": result["backup"], "report": str(report_path)}, ensure_ascii=False), flush=True)
        return result
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-directory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    main(arguments.database, arguments.backup_directory, arguments.report, arguments.apply)
