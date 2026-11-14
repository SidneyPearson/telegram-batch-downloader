import asyncio, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from telethon import errors, types
from tmd.model import BLOCK, Fault
from tmd.secure import Profile
from tmd.transport import TelegramTransport, fault, metadata
from .helpers import message

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.profile=Profile(self.temp.name)
        self.client=MagicMock();self.client.session.save.return_value='DUMMY_LOCAL_SESSION_NOT_REAL'
        self.client.get_me=AsyncMock(return_value=NS(id=123))
        self.client.disconnect=AsyncMock();self.client.connect=AsyncMock();self.client.is_user_authorized=AsyncMock(return_value=False)
        with patch('tmd.transport.TelegramClient',return_value=self.client):self.transport=TelegramTransport(self.profile,12345,'b'*32,'')
    async def asyncTearDown(self):self.temp.cleanup()
    async def test_normal_local_login_and_encrypted_session_only(self):
        self.assertFalse(await self.transport.connect())
        self.client.send_code_request=AsyncMock(return_value=NS(phone_code_hash='DUMMY_CODE_HASH'))
        await self.transport.send_code('+12345678901')
        self.client.sign_in=AsyncMock();await self.transport.sign_in('12345')
        self.client.sign_in.assert_awaited_once_with(phone='+12345678901',code='12345',phone_code_hash='DUMMY_CODE_HASH')
        text=self.profile.path.read_text('utf8')
        for secret in ('+12345678901','DUMMY_CODE_HASH','DUMMY_LOCAL_SESSION_NOT_REAL','b'*32):self.assertNotIn(secret,text)
        self.assertIsNone(self.transport.phone);self.assertIsNone(self.transport.code_hash)
        self.assertEqual(self.profile.secret('session_cipher'),'DUMMY_LOCAL_SESSION_NOT_REAL')
    async def test_connect_deadline_also_covers_authorization_probe(self):
        async def pending():await asyncio.sleep(60)
        self.client.is_user_authorized=AsyncMock(side_effect=pending)
        with patch('tmd.transport.CONNECT_TIMEOUT',.02):
            with self.assertRaises(TimeoutError):await self.transport.connect()
    async def test_code_request_deadline_is_finite(self):
        async def pending(*args):await asyncio.sleep(60)
        self.client.send_code_request=AsyncMock(side_effect=pending)
        with patch('tmd.transport.AUTH_TIMEOUT',.02):
            with self.assertRaises(TimeoutError):await self.transport.send_code('+12345678901')
        self.assertIsNone(self.transport.code_hash)
    async def test_missing_country_prefix_is_rejected_before_request(self):
        with self.assertRaises(Fault) as raised:await self.transport.send_code('12345678901')
        self.assertEqual(raised.exception.code,'PHONE');self.client.send_code_request.assert_not_called()
    async def test_two_factor_flow_does_not_persist_password(self):
        self.transport.phone='+12345678901';self.transport.code_hash='dummy'
        self.client.sign_in=AsyncMock(side_effect=[errors.SessionPasswordNeededError(None),None])
        with self.assertRaises(Fault) as raised:await self.transport.sign_in('12345')
        self.assertEqual(raised.exception.code,'PASSWORD_REQUIRED')
        await self.transport.sign_in('','DUMMY_2FA_PASSWORD')
        self.assertNotIn('DUMMY_2FA_PASSWORD',self.profile.path.read_text('utf8'))
        self.assertFalse(self.transport.needs_password)
    async def test_source_revision_and_size_checked_before_resume(self):
        task=metadata(message(),'a',-100,'channel');self.transport.peers[-100]=NS()
        self.client.get_messages=AsyncMock(return_value=message(media_id=5000))
        with self.assertRaises(Fault) as raised:await self.transport.current(task)
        self.assertEqual(raised.exception.code,'SOURCE_CHANGED')
        self.client.get_messages.return_value=message(size=task.size+1)
        with self.assertRaises(Fault):await self.transport.current(task)
        self.client.get_messages.return_value=None
        with self.assertRaises(Fault) as raised:await self.transport.current(task)
        self.assertEqual(raised.exception.code,'MESSAGE_GONE')
    async def test_download_uses_message_aligned_offset_and_closes_iterator(self):
        class Iterator:
            def __init__(self):self.closed=False;self.step=0
            def __aiter__(self):return self
            async def __anext__(self):
                self.step+=1
                if self.step>2:raise StopAsyncIteration
                return memoryview(b'content')
            async def close(self):self.closed=True
        iterator=Iterator();self.client.iter_download.return_value=iterator
        item=message(kind='image');stream=self.transport.chunks(item,BLOCK,3*BLOCK)
        self.assertEqual(await anext(stream),b'content');await stream.aclose()
        self.assertTrue(iterator.closed)
        self.client.iter_download.assert_called_once_with(item,offset=BLOCK,request_size=BLOCK,chunk_size=BLOCK,file_size=3*BLOCK)
    async def test_history_filter_limit_date_and_peer(self):
        async def source():yield message()
        self.transport.peers[-100]=NS();self.client.iter_messages.return_value=source()
        items=[m async for m in self.transport.history(-100,('video',),0,None)]
        args=self.client.iter_messages.call_args
        self.assertEqual(len(items),1);self.assertIsNone(args.kwargs['limit'])
        self.assertIsInstance(args.kwargs['filter'],types.InputMessagesFilterVideo)
    def test_permission_and_temporary_error_classification(self):
        self.assertEqual(fault(errors.ChannelPrivateError(None)).code,'ACCESS_DENIED')
        self.assertFalse(fault(errors.ChatForwardsRestrictedError(None)).retryable)
        wait=fault(errors.FloodWaitError(None,capture=20));self.assertEqual(wait.wait,20)
        self.assertTrue(wait.retryable);self.assertEqual(fault(ConnectionResetError()).code,'NETWORK')
        self.assertEqual(fault(errors.AuthKeyUnregisteredError(None)).code,'AUTH_REQUIRED')
    def test_real_telethon_video_message_compatibility(self):
        # Real v1.45 types, not browser URL stubs: the iterator accepts this Message.
        from datetime import datetime, timezone
        from telethon import utils
        doc=types.Document(id=42,access_hash=1,file_reference=b'dummy',date=datetime.now(timezone.utc),mime_type='video/mp4',size=BLOCK,
                           dc_id=2,attributes=[types.DocumentAttributeVideo(duration=1,w=16,h=16),types.DocumentAttributeFilename('sample.mp4')])
        item=types.Message(id=7,peer_id=types.PeerChannel(123),date=datetime.now(timezone.utc),message='',media=types.MessageMediaDocument(document=doc))
        task=metadata(item,'account',-100123,'channel')
        self.assertEqual(task.size,BLOCK);self.assertEqual(task.kind,'video');self.assertEqual(task.media_id,'42')
        self.assertIsNotNone(utils._get_file_info(item).location)

class WorkerLifecycleTests(unittest.TestCase):
    def test_shutdown_cancels_scan_and_closes_database_thread(self):
        import queue
        from app import Worker
        with tempfile.TemporaryDirectory() as temp:
            worker=Worker(temp,queue.Queue(),True);self.assertTrue(worker.ready.wait(3))
            async def history(*args):yield message();await asyncio.sleep(60)
            worker.transport.history=history
            future=asyncio.run_coroutine_threadsafe(worker._command(worker.scan,'@demo',('video',),100,'','',str(Path(temp)/'downloads')),worker.loop)
            for _ in range(100):
                if worker.engine.scanning:break
                time.sleep(.01)
            asyncio.run_coroutine_threadsafe(worker.shutdown(),worker.loop).result(5)
            future.result(2);worker.loop.call_soon_threadsafe(worker.loop.stop);worker.thread.join(3)
            self.assertFalse(worker.thread.is_alive());self.assertFalse(worker.engine.scanning)

if __name__=='__main__':unittest.main()
