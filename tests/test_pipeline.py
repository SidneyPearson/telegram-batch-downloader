import asyncio, hashlib, tempfile, time, tracemalloc, unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch
from tmd.engine import Engine, sample_transfer, transfer_rate
from tmd.model import BLOCK, Fault
from tmd.pipeline import ordered_chunks
from tmd.secure import Profile
from tmd.storage import Storage
from tmd.transport import TelegramTransport
from .helpers import FakeTransport, message

def block_data(position,size):return bytes([(position//BLOCK)%251])*min(BLOCK,size-position)

class FakeSDK:
    def __init__(self):
        self.active=0;self.maximum=0;self.iterators=[];self.offsets=[];self.delay=.005;self.errors={};self.reverse_delay=False
    def iter_download(self,item,*,offset,request_size,chunk_size,file_size,limit=None):
        owner=self
        class Iterator:
            step=0;closed=False
            async def __anext__(self):
                if self.step>=(limit or 999) or offset+self.step*BLOCK>=file_size:raise StopAsyncIteration
                position=offset+self.step*BLOCK;self.step+=1;owner.offsets.append(position)
                owner.active+=1;owner.maximum=max(owner.maximum,owner.active)
                try:
                    await asyncio.sleep(owner.delay*(3 if owner.reverse_delay and position//BLOCK%2==0 else 1))
                    if position in owner.errors:raise owner.errors[position]
                    return block_data(position,file_size)
                finally:owner.active-=1
            async def close(self):self.closed=True
        iterator=Iterator();self.iterators.append(iterator);return iterator
    async def disconnect(self):pass

class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.sdk=FakeSDK()
        with patch('tmd.transport.TelegramClient',return_value=self.sdk):self.adapter=TelegramTransport(Profile(self.root/'profile'),12345,'d'*32)
    async def asyncTearDown(self):self.temp.cleanup()
    async def test_out_of_order_responses_are_yielded_in_exact_offset_order(self):
        self.sdk.reverse_delay=True;size=7*BLOCK+37;item=message(size=size)
        chunks=[chunk async for chunk in self.adapter.chunks(item,0,size)]
        self.assertEqual(b''.join(chunks),b''.join(block_data(i,size) for i in range(0,size,BLOCK)))
        self.assertEqual(self.sdk.maximum,2);self.assertTrue(all(it.closed for it in self.sdk.iterators))
    async def test_all_downloads_share_two_network_slots(self):
        size=5*BLOCK+19
        async def consume(index):
            count=0
            async for chunk in self.adapter.chunks(message(index,size=size),0,size):count+=len(chunk)
            return count
        results=await asyncio.gather(*(consume(i) for i in range(1,4)))
        self.assertEqual(results,[size]*3);self.assertEqual(self.sdk.maximum,2);self.assertEqual(self.sdk.active,0)
    async def test_closing_stream_cancels_prefetched_work_and_releases_readers(self):
        self.sdk.delay=.04;size=8*BLOCK;stream=self.adapter.chunks(message(size=size),0,size)
        self.assertEqual(len(await anext(stream)),BLOCK)
        await stream.aclose();await asyncio.sleep(0)
        self.assertEqual(self.sdk.active,0);self.assertTrue(all(it.closed for it in self.sdk.iterators))
        self.assertLess(len(self.sdk.offsets),size//BLOCK)
    async def test_later_flood_wait_stops_before_requesting_replacement(self):
        self.sdk.reverse_delay=True;self.sdk.errors[BLOCK]=Fault('FLOOD_WAIT','wait',True,2)
        size=6*BLOCK
        with self.assertRaises(Fault) as raised:
            async for _ in self.adapter.chunks(message(size=size),0,size):pass
        self.assertEqual(raised.exception.code,'FLOOD_WAIT');self.assertEqual(set(self.sdk.offsets),{0,BLOCK})
        self.assertEqual(self.sdk.active,0);self.assertTrue(all(it.closed for it in self.sdk.iterators))
    async def test_wrong_chunk_length_cannot_be_yielded(self):
        async def short(position):return b'bad'
        with self.assertRaises(Fault) as raised:
            async for _ in ordered_chunks(0,3*BLOCK,short):pass
        self.assertEqual(raised.exception.code,'TRUNCATED')
    async def test_resume_with_real_transport_pipeline_preserves_full_file_hash(self):
        size=9*BLOCK+53;fake=FakeTransport([message(size=size)]);self.adapter.account=fake.account
        self.adapter.resolve=fake.resolve;self.adapter.history=fake.history;self.adapter.current=fake.current
        store=Storage(self.root/'queue.sqlite3')
        try:
            engine=Engine(store,self.adapter);await engine.scan('@offline',('video',),10,None,None,str(self.root/'files'))
            task=store.rows(fake.account)[0];folder=Path(task.output_root)/task.channel/'videos';folder.mkdir(parents=True)
            part=folder/('.'+task.basename()+'.'+task.key[:12]+'.part')
            prefix=block_data(0,size)+block_data(BLOCK,size);part.write_bytes(prefix+b'uncommitted')
            task.checkpoint_bytes=len(prefix);task.checkpoint_sha256=hashlib.sha256(prefix).hexdigest();task.received=len(prefix);store.put(task)
            await engine.run();done=store.get(task.key)
            digest=hashlib.sha256()
            for position in range(0,size,BLOCK):digest.update(block_data(position,size))
            self.assertEqual(done.status,'completed');self.assertEqual(done.sha256,digest.hexdigest())
            self.assertEqual(Path(done.final_path).stat().st_size,size);self.assertEqual(min(self.sdk.offsets),2*BLOCK)
            self.assertFalse(part.exists())
        finally:store.close()
    async def test_large_prefetch_does_not_buffer_the_file(self):
        self.sdk.delay=0;size=64*BLOCK+73;digest=hashlib.sha256();tracemalloc.start()
        try:
            async for chunk in self.adapter.chunks(message(size=size),0,size):digest.update(chunk)
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        expected=hashlib.sha256()
        for position in range(0,size,BLOCK):expected.update(block_data(position,size))
        self.assertEqual(digest.hexdigest(),expected.hexdigest());self.assertLess(peak,8*1024*1024)
    async def test_uninitialized_telethon_iterator_can_be_closed_after_cancel(self):
        class Incomplete:
            async def close(self):raise AttributeError("object has no attribute '_sender'")
        await self.adapter.close_iterator(Incomplete())
    async def test_real_telethon_iterator_with_mocked_rpc_preserves_ranges(self):
        from datetime import datetime,timezone
        from telethon import types,functions
        from unittest.mock import AsyncMock
        size=5*BLOCK+11
        adapter=TelegramTransport(Profile(self.root/'real-sdk'),12345,'e'*32)
        document=types.Document(id=42,access_hash=1,file_reference=b'dummy',date=datetime.now(timezone.utc),mime_type='video/mp4',size=size,
                                dc_id=adapter.client.session.dc_id,attributes=[types.DocumentAttributeVideo(duration=1,w=16,h=16)])
        item=types.Message(id=7,peer_id=types.PeerChannel(123),date=datetime.now(timezone.utc),message='',media=types.MessageMediaDocument(document=document))
        offsets=[]
        async def rpc(sender,request,*args,**kwargs):
            self.assertIsInstance(request,functions.upload.GetFileRequest);offsets.append(request.offset)
            self.assertEqual(request.limit,BLOCK);await asyncio.sleep(.001)
            return types.upload.File(type=types.storage.FileMp4(),mtime=0,bytes=block_data(request.offset,size))
        adapter.client._call=AsyncMock(side_effect=rpc)
        try:
            result=b''.join([chunk async for chunk in adapter.chunks(item,0,size)])
            self.assertEqual(result,b''.join(block_data(i,size) for i in range(0,size,BLOCK)))
            self.assertEqual(sorted(offsets),list(range(0,size,BLOCK)))
        finally:await adapter.disconnect()

class RateTests(unittest.TestCase):
    def test_five_second_chunks_do_not_show_zero_after_two_seconds(self):
        info={'samples':deque([(0,0)],maxlen=128)};sample_transfer(info,BLOCK,5)
        self.assertEqual(transfer_rate(info,5),BLOCK/5)
        self.assertGreater(transfer_rate(info,8),0)
        sample_transfer(info,2*BLOCK,10);self.assertAlmostEqual(transfer_rate(info,10),2*BLOCK/10)
    def test_genuine_inactivity_goes_to_zero(self):
        info={'samples':deque([(0,0),(5,BLOCK)],maxlen=128)}
        self.assertEqual(transfer_rate(info,21),0)
    def test_rate_window_excludes_old_bytes_and_samples_stay_bounded(self):
        info={'samples':deque(maxlen=128)}
        for second in range(60):sample_transfer(info,second*1000,second)
        self.assertAlmostEqual(transfer_rate(info,59),1000);self.assertLessEqual(len(info['samples']),17)
        for n in range(10000):sample_transfer(info,60000+n,60+n*.001)
        self.assertLessEqual(len(info['samples']),128)
    def test_dense_arrivals_use_recent_rate_after_throughput_changes(self):
        info={'samples':deque([(0,0)],maxlen=128)}
        for millisecond in range(1,60001):
            stamp=millisecond/1000;amount=stamp*1000 if stamp<30 else 30000+(stamp-30)*2000
            sample_transfer(info,amount,stamp)
        self.assertAlmostEqual(transfer_rate(info,60),2000,places=4)

if __name__=='__main__':unittest.main()
