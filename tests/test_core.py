import asyncio, errno, hashlib, json, os, sqlite3, tempfile, time, tracemalloc, unittest
from pathlib import Path
from unittest.mock import patch
from tmd.model import BLOCK, Fault, Task, channel_input, parse_date, safe_name
from tmd.storage import Storage
from tmd.secure import Profile, parse_proxy, protect
from tmd.engine import Engine, checked_path, publish
from tmd.transport import metadata
from .helpers import FakeTransport, message, data_for

class ModelSecurityTests(unittest.TestCase):
    def test_windows_names_and_unicode_length(self):
        self.assertEqual(safe_name('CON.txt'),'_CON.txt')
        name=safe_name('频道<>:"/\\|?*\n'+'😀'*100)
        self.assertLessEqual(len(name.encode('utf-16-le'))//2,65)
        self.assertFalse(any(c in name for c in '<>:"/\\|?*\n'))
        self.assertEqual(safe_name('...'),'unnamed')
    def test_dates_in_beijing_and_inclusive_end(self):
        start=parse_date('2026-10-25');end=parse_date('2026-10-25',True)
        self.assertEqual((end-start).total_seconds(),86400)
        self.assertEqual(start.utcoffset().total_seconds(),28800)
        with self.assertRaises(Fault):parse_date('2026-02-30')
    def test_channel_links_no_autojoin(self):
        self.assertEqual(channel_input('https://t.me/jiepairewu/123'),'jiepairewu')
        self.assertEqual(channel_input('https://t.me/c/12345/9'),-10012345)
        self.assertEqual(channel_input('@jiepairewu'),'jiepairewu')
        for value in ('https://example.com/x','https://t.me/+invite','https://t.me/joinchat/secret',''):
            with self.assertRaises(Fault):channel_input(value)
    def test_proxy_validation(self):
        self.assertIsNone(parse_proxy(''))
        self.assertEqual(parse_proxy('socks5://127.0.0.1:1080')[1:3],('127.0.0.1',1080))
        for text in ('ftp://127.0.0.1:80','http://user:pass@127.0.0.1:80','http://127.0.0.1:90000','127.0.0.1'):
            with self.assertRaises(Fault):parse_proxy(text)
    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_dpapi_profile_roundtrip_no_plaintext(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=Profile(temp);before=profile.fingerprint(123456)
            profile.save_login(10000,'a'*32,'DUMMY_SESSION_SECRET_NO_REAL_ACCOUNT')
            text=profile.path.read_text('utf8')
            self.assertNotIn('DUMMY_SESSION',text);self.assertNotIn('a'*32,text)
            restored=Profile(temp)
            self.assertEqual(restored.secret('session_cipher'),'DUMMY_SESSION_SECRET_NO_REAL_ACCOUNT')
            self.assertEqual(restored.secret('api_hash_cipher'),'a'*32)
            self.assertEqual(restored.fingerprint(123456),before)
            self.assertNotEqual(restored.fingerprint(123457),before)
            self.assertEqual(protect(protect(b'roundtrip'),True),b'roundtrip')
    def test_metadata_scope(self):
        item=message();item.noforwards=True
        self.assertIsNotNone(metadata(item,'account',-1,'channel'))
        item.media.ttl_seconds=5
        self.assertIsNone(metadata(item,'account',-1,'channel'))
        item=message();item.video_note=True
        self.assertIsNone(metadata(item,'account',-1,'channel'))
    def test_identity_and_filename(self):
        task=metadata(message(),'a',-100,'频道')
        self.assertEqual(task.basename(),'频道_1_20261025.mp4')
        self.assertEqual(task.key,metadata(message(),'a',-100,'renamed').key)
        self.assertNotEqual(task.key,metadata(message(media_id=5000),'a',-100,'频道').key)
    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(Fault):checked_path(temp,Path(temp).parent/'outside')
    def test_publish_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'video.mp4';target.write_bytes(b'existing')
            part=Path(temp)/'.part';part.write_bytes(b'new')
            final=publish(part,target)
            self.assertEqual(target.read_bytes(),b'existing');self.assertEqual(final.read_bytes(),b'new')
            self.assertEqual(final.name,'video (1).mp4')

class StorageTests(unittest.TestCase):
    def test_persist_dedup_and_recover_interruption(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'tasks.sqlite3';store=Storage(path);task=metadata(message(),'a',-100,'channel')
            task.status='downloading';store.put(task);store.put(task);store.error(task,'NETWORK','safe error');store.close()
            store=Storage(path)
            self.assertEqual(len(store.rows('a')),1);self.assertEqual(store.get(task.key).status,'paused')
            self.assertEqual(store.summary('a')['paused'],1);self.assertEqual(store.errors(task.key)[0]['code'],'NETWORK')
            self.assertEqual(store.rows('other'),[]);store.close()

class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.store=Storage(self.base/'tasks.sqlite3');self.transport=FakeTransport();self.events=[]
        self.engine=Engine(self.store,self.transport,lambda event,data:self.events.append((event,data)))
    async def asyncTearDown(self):self.store.close();self.temp.cleanup()
    async def scan(self,kinds=('video','image'),limit=500):
        await self.engine.scan('@channel',kinds,limit,None,None,str(self.base/'downloads'))
        return self.store.rows(self.transport.account,self.engine.peer)
    def fast_retries(self):
        async def wait(seconds):self.engine.check();await asyncio.sleep(min(seconds,.005))
        self.engine.wait=wait
    async def test_complete_exact_length_digest_and_disk_before_success(self):
        task=(await self.scan())[0];real_put=self.store.put
        def put(value):
            if value.status=='completed':
                self.assertTrue(Path(value.final_path).is_file());self.assertEqual(Path(value.final_path).stat().st_size,value.size)
                self.assertEqual(value.checkpoint_bytes,value.size);self.assertEqual(value.checkpoint_sha256,value.sha256)
            real_put(value)
        self.store.put=put
        await self.engine.run();done=self.store.get(task.key);expected=data_for(1,task.size)
        self.assertEqual(done.status,'completed');self.assertEqual(Path(done.final_path).read_bytes(),expected)
        self.assertEqual(done.sha256,hashlib.sha256(expected).hexdigest());self.assertFalse(Path(done.part_path).exists())
        self.assertLessEqual(self.transport.max_chunk,BLOCK)
    async def test_scan_dedup_completed_record_and_missing_file_requeue(self):
        await self.scan();await self.scan();self.assertEqual(self.store.summary(self.transport.account)['total'],1)
        await self.engine.run();done=self.store.rows(self.transport.account)[0];await self.scan()
        self.assertEqual(self.store.get(done.key).status,'completed')
        Path(done.final_path).unlink();await self.scan()
        self.assertEqual(self.store.get(done.key).status,'queued');self.assertEqual(self.store.get(done.key).error_code,'LOCAL_MISSING')
    async def test_scan_date_limit_and_truthful_scope(self):
        self.transport.messages=[message(i) for i in range(10,0,-1)]
        await self.scan(limit=3)
        self.assertEqual(self.store.summary(self.transport.account)['total'],3)
        self.assertIn('不是频道媒体总数',self.engine.scan_scope)
    async def test_video_serial_images_bounded_parallel(self):
        self.transport.messages=[message(i,'image' if i%2==0 else 'video') for i in range(1,7)];self.transport.delay=.01
        await self.scan();await self.engine.run(3)
        self.assertEqual(self.transport.max_videos,1);self.assertLessEqual(self.transport.max_open,3)
        self.assertGreater(self.transport.max_open,1);self.assertEqual(self.store.summary(self.transport.account)['completed'],6)
    async def test_network_failure_is_retried_not_disk_failure(self):
        self.fast_retries();self.transport.failures[1]=[ConnectionResetError('offline')]
        task=(await self.scan())[0];await self.engine.run()
        done=self.store.get(task.key)
        self.assertEqual(done.status,'completed');self.assertEqual(done.attempts,2)
        self.assertEqual(self.store.errors(task.key)[0]['code'],'NETWORK')
    async def test_denied_once_other_tasks_continue(self):
        self.transport.messages=[message(1),message(2)];self.transport.failures[1]=[Fault('ACCESS_DENIED','denied')]
        await self.scan();await self.engine.run();rows={t.message_id:t for t in self.store.rows(self.transport.account)}
        self.assertEqual(rows[1].status,'failed');self.assertEqual(rows[1].attempts,1);self.assertEqual(rows[2].status,'completed')
        self.engine.retry_failed();self.assertEqual(self.store.get(rows[1].key).status,'failed')
    async def test_retry_limit_and_manual_retry(self):
        self.fast_retries();self.transport.failures[1]=[TimeoutError() for _ in range(3)]
        task=(await self.scan())[0];await self.engine.run();done=self.store.get(task.key)
        self.assertEqual(done.status,'failed');self.assertEqual(done.attempts,3)
        self.engine.retry_failed();await self.engine.run();self.assertEqual(self.store.get(task.key).status,'completed')
    async def test_expired_login_pauses_and_can_resume_after_reconnect(self):
        self.transport.failures[1]=[Fault('AUTH_REQUIRED','login expired')]
        task=(await self.scan())[0];await self.engine.run()
        self.assertTrue(self.engine.network_halt);self.assertEqual(self.store.get(task.key).status,'paused')
        await self.engine.run();self.assertEqual(self.store.get(task.key).status,'completed')
    async def test_server_wait_not_shortened_by_resume(self):
        self.fast_retries();self.transport.failures[1]=[Fault('FLOOD_WAIT','wait',True,.08)]
        task=(await self.scan())[0];start=time.time();await self.engine.run()
        self.assertGreaterEqual(time.time()-start,.075);self.assertEqual(self.store.get(task.key).status,'completed')
        until=time.time()+.06;self.store.set_setting('cooldown:'+self.transport.account,until)
        self.engine.retry_failed();await self.engine.run();self.assertEqual(self.engine.cooldown,until)
    async def test_pause_resume_committed_checkpoint(self):
        self.transport.messages=[message(size=7*BLOCK+91)];self.transport.delay=.28
        task=(await self.scan())[0];job=asyncio.create_task(self.engine.run())
        for _ in range(150):
            await asyncio.sleep(.02)
            if self.store.get(task.key).checkpoint_bytes>=BLOCK:break
        self.engine.request_pause();await job;paused=self.store.get(task.key)
        self.assertEqual(paused.status,'paused');self.assertGreater(paused.checkpoint_bytes,0)
        self.assertEqual(paused.checkpoint_bytes%BLOCK,0);self.assertEqual(self.transport.open,0)
        self.transport.delay=0;await self.engine.run()
        done=self.store.get(task.key);self.assertEqual(done.status,'completed')
        self.assertEqual(self.transport.calls[-1][1],paused.checkpoint_bytes)
        self.assertEqual(Path(done.final_path).read_bytes(),data_for(1,done.size))
    def make_part(self,task,prefix,checkpoint_bytes,checkpoint_hash,tail=b''):
        folder=Path(task.output_root)/safe_name(task.channel)/'videos';folder.mkdir(parents=True,exist_ok=True)
        part=folder/('.'+task.basename()+'.'+task.key[:12]+'.part');part.write_bytes(prefix+tail)
        task.part_path=str(part);task.checkpoint_bytes=checkpoint_bytes;task.checkpoint_sha256=checkpoint_hash
        task.received=checkpoint_bytes;self.store.put(task);return part
    async def test_corrupted_prefix_restarts_instead_of_appending(self):
        task=(await self.scan())[0];good=data_for(1,BLOCK)
        self.make_part(task,b'X'*BLOCK,BLOCK,hashlib.sha256(good).hexdigest())
        await self.engine.run();done=self.store.get(task.key)
        self.assertEqual(self.transport.calls[0][1],0);self.assertEqual(Path(done.final_path).read_bytes(),data_for(1,task.size))
        self.assertIn('CHECKPOINT_MISMATCH',[e['code'] for e in self.store.errors(task.key)])
    async def test_uncommitted_tail_is_truncated(self):
        task=(await self.scan())[0];prefix=data_for(1,BLOCK)
        self.make_part(task,prefix,BLOCK,hashlib.sha256(prefix).hexdigest(),b'corrupt-uncommitted')
        await self.engine.run();done=self.store.get(task.key)
        self.assertEqual(self.transport.calls[0][1],BLOCK);self.assertEqual(Path(done.final_path).read_bytes(),data_for(1,task.size))
    async def test_shorter_part_than_checkpoint_restarts(self):
        task=(await self.scan())[0];self.make_part(task,b'partial',BLOCK,hashlib.sha256(data_for(1,BLOCK)).hexdigest())
        await self.engine.run();self.assertEqual(self.transport.calls[0][1],0)
        self.assertEqual(self.store.get(task.key).status,'completed')
    async def test_oversized_part_preserved_no_publish(self):
        task=(await self.scan())[0];part=self.make_part(task,b'X'*(task.size+1),0,'')
        await self.engine.run();done=self.store.get(task.key)
        self.assertEqual(done.error_code,'PART_TOO_LARGE');self.assertTrue(part.exists());self.assertEqual(done.final_path,'')
    async def test_part_hardlink_cannot_truncate_unrelated_file(self):
        task=(await self.scan())[0];part=self.make_part(task,b'placeholder',0,'');part.unlink()
        existing=self.base/'keep.txt';existing.write_bytes(b'preserve-this-file')
        os.link(existing,part);await self.engine.run();done=self.store.get(task.key)
        self.assertEqual(done.error_code,'PATH');self.assertEqual(existing.read_bytes(),b'preserve-this-file')
    async def test_short_body_never_completed(self):
        self.fast_retries()
        async def chunks(item,offset,size):yield b'short'
        self.transport.chunks=chunks;task=(await self.scan())[0];await self.engine.run();done=self.store.get(task.key)
        self.assertEqual(done.status,'failed');self.assertEqual(done.error_code,'TRUNCATED');self.assertEqual(done.attempts,3)
        self.assertTrue(Path(done.part_path).exists());self.assertEqual(done.final_path,'')
    async def test_oversized_body_rejected_without_retry(self):
        async def chunks(item,offset,size):yield b'X'*(size+1)
        self.transport.chunks=chunks;task=(await self.scan())[0];await self.engine.run();done=self.store.get(task.key)
        self.assertEqual(done.error_code,'LENGTH');self.assertEqual(done.attempts,1);self.assertEqual(done.final_path,'')
    async def test_disk_full_pauses_rest_of_queue(self):
        self.transport.messages=[message(1),message(2)];await self.scan()
        with patch('tmd.engine.publish',side_effect=OSError(errno.ENOSPC,'disk full')):await self.engine.run(1)
        rows=self.store.rows(self.transport.account);self.assertEqual({t.status for t in rows},{'failed','paused'})
        self.assertEqual(next(t for t in rows if t.status=='failed').error_code,'DISK_FULL')
    async def test_failed_sqlite_completion_never_reports_success(self):
        task=(await self.scan())[0];real_put=self.store.put
        def put(task):
            if task.status=='completed':raise sqlite3.OperationalError('test disk db failure')
            real_put(task)
        self.store.put=put;await self.engine.run();done=self.store.get(task.key)
        self.assertEqual(done.status,'failed')
    async def test_short_disk_writes_are_fully_awaited(self):
        task=(await self.scan())[0];original=Path.open
        class ShortWriter:
            def __init__(self,wrapped):self.wrapped=wrapped
            def __enter__(self):self.wrapped.__enter__();return self
            def __exit__(self,*args):return self.wrapped.__exit__(*args)
            def __getattr__(self,name):return getattr(self.wrapped,name)
            def write(self,data):return self.wrapped.write(data[:max(1,len(data)//3)])
        def patched(path,*args,**kwargs):
            file=original(path,*args,**kwargs)
            return ShortWriter(file) if str(path).endswith('.part') else file
        with patch.object(Path,'open',patched):await self.engine.run()
        done=self.store.get(task.key);self.assertEqual(done.status,'completed')
        self.assertEqual(Path(done.final_path).read_bytes(),data_for(1,task.size))
    async def test_large_stream_has_bounded_python_memory(self):
        self.transport.messages=[message(size=64*BLOCK+53)];task=(await self.scan())[0]
        tracemalloc.start()
        try:
            await self.engine.run();_,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertEqual(self.store.get(task.key).status,'completed');self.assertLess(peak,8*1024*1024)
    async def test_cancel_scan_keeps_discovered_records(self):
        async def history(*args):
            yield message();await asyncio.sleep(60)
        self.transport.history=history;job=asyncio.create_task(self.scan())
        for _ in range(30):
            await asyncio.sleep(.01)
            if self.store.summary(self.transport.account)['total']:break
        self.engine.request_pause();await asyncio.wait_for(job,1)
        self.assertEqual(self.store.summary(self.transport.account)['total'],1)
        self.assertFalse(self.engine.scanning);self.assertTrue(self.engine.idle.is_set())
    async def test_output_directory_change_restarts_without_deleting_old_part(self):
        task=(await self.scan())[0];part=self.make_part(task,b'old',0,'')
        await self.engine.scan('@channel',('video',),10,None,None,str(self.base/'new'))
        await self.engine.run();done=self.store.get(task.key)
        self.assertTrue(part.exists());self.assertTrue(Path(done.final_path).is_relative_to((self.base/'new').resolve()))
    async def test_export_includes_errors_no_account_secrets(self):
        task=(await self.scan())[0];self.store.error(task,'NETWORK','safe')
        target=self.base/'report.json';self.engine.export(target);report=json.loads(target.read_text('utf8'))
        self.assertNotIn('account',report['tasks'][0]);self.assertEqual(report['tasks'][0]['errors'][0]['code'],'NETWORK')
        for secret in ('session_cipher','api_hash_cipher','phone_code_hash'):self.assertNotIn(secret,target.read_text('utf8'))

if __name__=='__main__':unittest.main()
