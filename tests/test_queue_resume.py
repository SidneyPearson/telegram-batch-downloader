import asyncio
import hashlib
from pathlib import Path
import tempfile
import unittest

from tmd.engine import Engine
from tmd.model import BLOCK, safe_name
from tmd.storage import Storage
from tests.helpers import FakeTransport, data_for, message


class ResumeQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.database = self.base / "tasks.sqlite3"
        self.store = Storage(self.database)
        self.transport = FakeTransport([message(i, size=4*BLOCK+19) for i in (1, 2, 3)])
        self.engine = Engine(self.store, self.transport)
        await self.engine.scan("@channel", ("video",), 10, None, None, str(self.base / "downloads"))
        self.tasks = {task.message_id: task for task in self.store.rows(self.transport.account)}

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def partial(self, number, blocks=1, status="queued"):
        task = self.tasks[number]
        folder = Path(task.output_root) / safe_name(task.channel) / "videos"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("." + task.basename() + "." + task.key[:12] + ".part")
        data = data_for(number, blocks*BLOCK)
        path.write_bytes(data)
        task.part_path = str(path)
        task.checkpoint_bytes = task.received = len(data)
        task.checkpoint_sha256 = hashlib.sha256(data).hexdigest()
        task.status = status
        task.error = "已暂停；下次校验本地片段后继续"
        self.store.put(task)
        return path

    async def test_existing_parts_before_older_fresh_jobs(self):
        self.partial(2)
        self.partial(3, 3)
        await self.engine.run(2)
        self.assertEqual(self.transport.calls, [(3, 3*BLOCK), (2, BLOCK), (1, 0)])
        for number, task in self.tasks.items():
            done = self.store.get(task.key)
            self.assertEqual(done.status, "completed")
            self.assertEqual(Path(done.final_path).read_bytes(), data_for(number, done.size))
            self.assertFalse(Path(done.part_path).exists())
        self.assertEqual(self.transport.max_videos, 1)

    async def test_pause_resume_does_not_send_parts_to_end(self):
        self.partial(2, status="paused")
        self.partial(3, 3, status="paused")
        task = self.tasks[1]
        task.status = "paused"
        self.store.put(task)
        await self.engine.run()
        self.assertEqual([number for number, _ in self.transport.calls], [3, 2, 1])

    async def test_database_reopen_retains_resume_priority(self):
        self.partial(2, 2, status="downloading")
        self.store.close()
        self.store = Storage(self.database)
        self.engine = Engine(self.store, self.transport)
        self.engine.peer, self.engine.channel = await self.transport.resolve("@channel")
        await self.engine.run()
        self.assertEqual(self.transport.calls[0], (2, 2*BLOCK))

    async def test_failed_part_visible_and_manual_retry_prioritized(self):
        self.partial(2, 3, status="failed")
        self.tasks[2].error_code = "NETWORK"
        self.store.put(self.tasks[2])
        self.engine.retry_failed()
        self.assertEqual(self.store.next_task(self.transport.account, self.engine.peer).message_id, 2)
        await self.engine.run()
        self.assertEqual(self.transport.calls[0], (2, 3*BLOCK))

    async def test_queued_parts_visible_before_fresh_within_ui_limit(self):
        self.partial(2)
        self.partial(3, 3)
        self.store.put(self.tasks[1])
        shown = self.store.rows(self.transport.account, self.engine.peer, limit=2)
        self.assertEqual([task.message_id for task in shown], [3, 2])

    async def test_resumed_waiting_status_does_not_say_paused(self):
        self.partial(2, status="paused")
        snapshots = []
        self.engine.notify = lambda event, data: snapshots.append(data) if event == "snapshot" else None
        await self.engine.run()
        waiting = [task for snapshot in snapshots for task in snapshot["tasks"]
                   if task["message_id"] == 2 and task["status"] == "queued"]
        self.assertTrue(waiting)
        self.assertEqual(waiting[0]["error"], "等待校验已有片段后续传")
        self.assertEqual(waiting[0]["error_code"], "")


if __name__ == "__main__":
    unittest.main()
