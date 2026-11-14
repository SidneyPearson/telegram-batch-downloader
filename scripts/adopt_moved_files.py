"""Adopt already-downloaded media into existing task records after reorganisation.

Reads the downloader's SQLite task database, locates a physical file for each task
whose recorded path no longer works, verifies the full SHA-256 against the hash
recorded at download time, and only then updates the record. A database backup is
taken before any write and every update happens in one transaction.

The media files themselves are never modified.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmd.model import Task  # noqa: E402

NAME_WITH_DATE = re.compile(r"^(?P<channel>.*?)_(?P<mid>\d+)_(?P<date>\d{8})\.(?P<ext>[A-Za-z0-9]+)$")
NAME_NO_DATE = re.compile(r"^(?P<channel>.*?)_(?P<mid>\d+)(?:\((?P<dup>\d+)\))?\.(?P<ext>[A-Za-z0-9]+)$")


def parse_name(name):
    for pattern in (NAME_WITH_DATE, NAME_NO_DATE):
        match = pattern.match(name)
        if match:
            return int(match.group("mid")), match.group("ext").lower()
    return None


def sha256_of(path, block=4 * 1024 * 1024):
    digest = hashlib.sha256()
    before = path.stat()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(block)
            if not chunk:
                break
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError(f"文件在读取期间发生变化：{path}")
    return digest.hexdigest()


def index_files(roots):
    """(message_id, ext, size) -> [relative paths]"""
    index = defaultdict(list)
    skipped = 0
    for root in roots:
        root = Path(root).resolve()
        if not root.is_dir():
            print(f"跳过不存在的目录：{root}", flush=True)
            continue
        for current, _dirs, files in os.walk(root):
            for name in files:
                path = Path(current) / name
                parsed = parse_name(name)
                if not parsed or path.is_symlink():
                    skipped += 1
                    continue
                mid, ext = parsed
                try:
                    size = path.stat().st_size
                except OSError:
                    skipped += 1
                    continue
                index[(mid, ext, size)].append(path.resolve())
    return index, skipped


def expected_ext(task):
    return Task(**task).basename().rsplit(".", 1)[-1].lower()


def adopt(database, roots, backup_directory, report_path, apply_changes):
    database = Path(database).resolve()
    connection = sqlite3.connect(str(database), timeout=10)
    try:
        rows = list(connection.execute("SELECT key,status,payload FROM tasks"))
    finally:
        connection.close()

    tasks = [(key, status, json.loads(payload)) for key, status, payload in rows]
    index, skipped = index_files(roots)
    print(f"索引完成：{sum(len(v) for v in index.values())} 个候选文件（跳过 {skipped}）", flush=True)

    candidates = []
    already_ok = 0
    no_file = []
    ambiguous = []
    for key, status, task in tasks:
        path_ok = bool(task.get("final_path")) and Path(task["final_path"]).is_file() \
            and Path(task["final_path"]).stat().st_size == task["size"]
        if status == "completed" and path_ok:
            already_ok += 1
            continue
        if status not in ("completed", "queued"):
            continue
        ext = expected_ext(task)
        hits = index.get((task["message_id"], ext, task["size"]), [])
        if not hits:
            no_file.append({"key": key, "status": status, "message_id": task["message_id"],
                            "kind": task["kind"], "size": task["size"],
                            "error_code": task.get("error_code", "")})
            continue
        if len(hits) > 1:
            ambiguous.append({"key": key, "message_id": task["message_id"], "paths": [str(p) for p in hits]})
            continue
        candidates.append((key, status, task, hits[0]))

    print(f"待校验：{len(candidates)} 条；已正常：{already_ok}；无文件：{len(no_file)}；多候选：{len(ambiguous)}", flush=True)

    verified = []
    hash_mismatch = []
    failed = []
    total_bytes = 0
    for index_no, (key, status, task, path) in enumerate(candidates, 1):
        try:
            digest = sha256_of(path)
        except OSError as error:
            failed.append({"key": key, "message_id": task["message_id"], "path": str(path), "detail": str(error)})
            continue
        expected = task.get("sha256", "")
        if not expected or digest != expected:
            hash_mismatch.append({"key": key, "message_id": task["message_id"], "path": str(path),
                                  "expected": expected, "actual": digest})
            continue
        stat = path.stat()
        verified.append((key, status, task, path, digest, (stat.st_size, stat.st_mtime_ns, stat.st_ino)))
        total_bytes += stat.st_size
        if index_no % 25 == 0 or index_no == len(candidates):
            print(f"校验进度 {index_no}/{len(candidates)}，通过 {len(verified)}，累计 {total_bytes/1024**3:.2f} GB", flush=True)

    result = {
        "created_at": datetime.now().astimezone().isoformat(),
        "database": str(database),
        "roots": [str(Path(root).resolve()) for root in roots],
        "read_only": not apply_changes,
        "verified_but_not_applied": len(verified) if not apply_changes else 0,
        "verified_bytes": total_bytes,
        "adopted": 0,
        "no_file": len(no_file),
        "no_file_items": no_file,
        "ambiguous": ambiguous,
        "hash_mismatch": hash_mismatch,
        "read_error": failed,
        "backup": None,
        "media_files_modified": False,
    }

    if not apply_changes:
        result["adopted"] = 0
        result["preview"] = [
            {"key": k, "message_id": t["message_id"], "status": s, "new_path": str(p)}
            for k, s, t, p, _d, _st in verified
        ]
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"mode": "preview", "verified": len(verified), "no_file": len(no_file),
                          "report": str(report_path)}, ensure_ascii=False), flush=True)
        return result

    if not verified:
        result["adopted"] = 0
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / (
        "tasks-before-adopt-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
    source = sqlite3.connect(str(database), timeout=10)
    try:
        target = sqlite3.connect(str(backup_path))
        try:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("数据库备份校验失败，未修改记录")
        finally:
            target.close()

        changes = []
        source.execute("BEGIN IMMEDIATE")
        try:
            for key, status, task, path, digest, stamp in verified:
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != stamp:
                    raise RuntimeError(f"文件在关联期间变化，已回滚：{path}")
                row = source.execute("SELECT status,payload FROM tasks WHERE key=?", (key,)).fetchone()
                if not row:
                    raise RuntimeError(f"任务记录已删除，已回滚：{key}")
                current = json.loads(row[1])
                if row[0] != status or current.get("status") != status \
                        or current.get("sha256") != task.get("sha256") or current.get("size") != task.get("size"):
                    raise RuntimeError(f"任务记录已变化，已回滚：{key}")
                current["final_path"] = str(path)
                if status == "queued":
                    current["status"] = "completed"
                    current["received"] = current["size"]
                    current["checkpoint_bytes"] = 0
                    current["checkpoint_sha256"] = ""
                    current["part_path"] = ""
                    current["error"] = ""
                    current["error_code"] = ""
                    if not current.get("completed_at"):
                        current["completed_at"] = datetime.now().timestamp()
                    new_status = "completed"
                else:
                    new_status = "completed"
                updated = source.execute(
                    "UPDATE tasks SET status=?, payload=? WHERE key=? AND status=? AND payload=?",
                    (new_status, json.dumps(current, ensure_ascii=False), key, row[0], row[1])).rowcount
                if updated != 1:
                    raise RuntimeError(f"任务记录同时发生变化，已回滚：{key}")
                changes.append({"message_id": task["message_id"], "previous_status": status,
                                "path": str(path), "bytes": task["size"]})
            source.commit()
        except BaseException:
            source.rollback()
            raise
        result["adopted"] = len(changes)
        result["backup"] = str(backup_path)
        result["changes"] = changes
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"mode": "apply", "adopted": len(changes), "backup": str(backup_path),
                          "report": str(report_path)}, ensure_ascii=False), flush=True)
        return result
    finally:
        source.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument("--backup-directory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="真正写入数据库；不加该参数只做预览")
    arguments = parser.parse_args()
    adopt(arguments.database, arguments.root, arguments.backup_directory, arguments.report, arguments.apply)
