"""Read-only audit of completed videos and resumable parts. No login required."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import time


def fingerprint(path, limit=None):
    before = path.stat()
    digest = hashlib.sha256()
    remaining = before.st_size if limit is None else limit
    if remaining < 0 or remaining > before.st_size:
        raise OSError("校验长度超出文件边界")
    with path.open("rb") as source:
        while remaining:
            block = source.read(min(4 * 1024 * 1024, remaining))
            if not block:
                raise OSError("文件长度在读取期间变化")
            digest.update(block)
            remaining -= len(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("文件在校验期间变化，请暂停下载后再检查")
    return digest.hexdigest()


def mp4_structure(path):
    """Inspect top-level box boundaries; this is NOT a decode/playback test."""
    total = path.stat().st_size
    boxes = []
    position = 0
    with path.open("rb") as source:
        while position < total:
            source.seek(position)
            header = source.read(8)
            if len(header) != 8:
                return {"status": "warning", "detail": "末尾不足一个 MP4 box 头"}
            length, kind = struct.unpack(">I4s", header)
            minimum = 8
            if length == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    return {"status": "warning", "detail": "扩展 box 长度缺失"}
                length = struct.unpack(">Q", extended)[0]
                minimum = 16
            elif length == 0:
                length = total - position
            if length < minimum or position + length > total:
                return {"status": "warning", "detail": "box 声明长度超过文件边界"}
            boxes.append(kind.decode("ascii", errors="replace"))
            position += length
            if len(boxes) > 100000:
                return {"status": "warning", "detail": "box 数量异常"}
    if "moov" not in boxes or "mdat" not in boxes:
        return {"status": "warning", "detail": "缺少 moov 或 mdat，需进一步检查", "boxes": boxes}
    return {"status": "boundaries_ok", "boxes": boxes}


def audit(database, extra_roots, report):
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        tasks = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM tasks")]
    finally:
        connection.close()
    completed = [task for task in tasks if task["kind"] == "video" and task["status"] == "completed"]
    roots = {Path(task["output_root"]).resolve() for task in tasks if task.get("output_root")}
    roots.update(root.resolve() for root in extra_roots)
    candidates = defaultdict(list)
    parts = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() == ".part":
                parts.add(path.resolve())
            elif path.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"}:
                candidates[path.name.casefold()].append(path.resolve())

    result = {"created_at": datetime.now().astimezone().isoformat(),
              "database": str(database.resolve()), "read_only": True,
              "task_statuses": dict(Counter(task["status"] for task in tasks)),
              "task_error_codes": dict(Counter(task["error_code"] for task in tasks if task.get("error_code"))),
              "roots": sorted(map(str, roots)), "completed_videos": [], "partial_files": [],
              "extra_copies": [], "limits": "SHA-256 与下载完成时的本地记录比较；MP4 只检查顶层结构，没有逐帧解码，也不是源站签名。"}
    hashes = {}
    last_log = time.monotonic()
    for index, task in enumerate(completed, 1):
        original = Path(task.get("final_path") or "")
        paths = list(dict.fromkeys(([original] if original.is_file() else []) + candidates.get(original.name.casefold(), [])))
        entry = {"key": task["key"], "message_id": task["message_id"], "original_path": str(original),
                 "expected_size": task["size"], "expected_sha256": task.get("sha256", ""),
                 "status": "missing", "matches": [], "problems": []}
        for path in paths:
            try:
                length = path.stat().st_size
                if length != task["size"]:
                    entry["problems"].append({"path": str(path), "status": "size_mismatch", "actual_size": length})
                    continue
                if path not in hashes:
                    hashes[path] = fingerprint(path)
                if not task.get("sha256") or hashes[path] != task["sha256"]:
                    entry["problems"].append({"path": str(path), "status": "hash_mismatch_or_no_record"})
                    continue
                entry["matches"].append({"path": str(path), "size": length, "sha256": hashes[path],
                                         "structure": mp4_structure(path) if path.suffix.lower() == ".mp4" else {"status": "not_checked"}})
            except OSError as error:
                entry["problems"].append({"path": str(path), "status": "read_error", "detail": str(error)})
        if entry["matches"]:
            entry["status"] = "verified_original" if any(match["path"].casefold() == str(original).casefold() for match in entry["matches"]) else "verified_moved"
            entry["verified_path"] = next((match["path"] for match in entry["matches"] if match["path"].casefold() == str(original).casefold()), entry["matches"][0]["path"])
        elif entry["problems"]:
            entry["status"] = "needs_review"
        if len(entry["matches"]) > 1:
            result["extra_copies"].append({"message_id": task["message_id"], "paths": [match["path"] for match in entry["matches"]]})
        result["completed_videos"].append(entry)
        if time.monotonic() - last_log > 8 or index == len(completed):
            print(f"已校验完成记录 {index}/{len(completed)}，实际读取视频 {len(hashes)} 个", flush=True)
            last_log = time.monotonic()

    by_part = {str(Path(task["part_path"]).resolve()).casefold(): task for task in tasks if task.get("part_path")}
    for path in sorted(parts):
        task = by_part.get(str(path).casefold())
        entry = {"path": str(path), "size": path.stat().st_size, "task_status": task["status"] if task else "untracked"}
        if task:
            checkpoint = task.get("checkpoint_bytes", 0)
            entry.update(expected_size=task["size"], checkpoint_bytes=checkpoint,
                         error_code=task.get("error_code", ""), message_id=task["message_id"])
            try:
                entry["checkpoint_hash_ok"] = (checkpoint <= entry["size"] and bool(task.get("checkpoint_sha256"))
                                                and fingerprint(path, checkpoint) == task["checkpoint_sha256"])
            except OSError as error:
                entry["checkpoint_error"] = str(error)
        result["partial_files"].append(entry)
    result["completed_summary"] = dict(Counter(entry["status"] for entry in result["completed_videos"]))
    result["structure_warnings"] = [{"path": match["path"], "structure": match["structure"]}
                                    for entry in result["completed_videos"] for match in entry["matches"]
                                    if match["structure"]["status"] == "warning"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": result["completed_summary"], "partial_files": len(parts),
                      "extra_copy_groups": len(result["extra_copies"]), "structure_warnings": len(result["structure_warnings"]),
                      "report": str(report.resolve())}, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--search-root", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    audit(arguments.database, arguments.search_root, arguments.report)
