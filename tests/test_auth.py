"""Own-app authentication state tests with dummy data and NO Telegram connections."""
import asyncio, json, queue, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
from tmd.model import Fault

class FakeAuthTransport:
    def __init__(self,*args,mode='ready',two_factor=False,**kwargs):
        self.mode=mode;self.two_factor=two_factor;self.account='';self.needs_password=False
        self.started=threading.Event();self.codes=0;self.closed=0
    async def connect(self):
        self.started.set()
        if self.mode=='pending':await asyncio.sleep(60)
        if self.mode=='timeout':raise TimeoutError()
        if self.mode=='secret_error':raise RuntimeError('DUMMY_PRIVATE_VALUE')
        return False
    async def send_code(self,phone):self.codes+=1;return False
    async def sign_in(self,code,password):
        if self.two_factor and not self.needs_password:
            self.needs_password=True;raise Fault('PASSWORD_REQUIRED','需要两步验证密码')
        self.account='offline-auth-test';self.needs_password=False
    async def disconnect(self):self.closed+=1

class WorkerAuthTests(unittest.TestCase):
    def setUp(self):
        from app import Worker
        self.temp=tempfile.TemporaryDirectory();self.events=queue.Queue();self.worker=Worker(self.temp.name,self.events)
        self.assertTrue(self.worker.ready.wait(3));self.transport=FakeAuthTransport()
        self.patcher=patch('app.TelegramTransport',return_value=self.transport);self.patcher.start()
    def tearDown(self):
        self.patcher.stop()
        asyncio.run_coroutine_threadsafe(self.worker.shutdown(),self.worker.loop).result(5)
        self.worker.loop.call_soon_threadsafe(self.worker.loop.stop);self.worker.thread.join(3);self.temp.cleanup()
    def command(self,func,*args):
        return asyncio.run_coroutine_threadsafe(self.worker._command(func,*args),self.worker.loop).result(3)
    def drain(self):
        events=[]
        while not self.events.empty():events.append(self.events.get_nowait())
        return events
    def test_connection_ready_is_distinct_from_account_authorized(self):
        self.command(self.worker.connect,12345,'c'*32,'socks5://127.0.0.1:7890')
        events=self.drain();auth=[value for name,value in events if name=='auth']
        self.assertEqual([item['phase'] for item in auth],['connecting','ready'])
        self.assertTrue(self.worker.network_ready);self.assertFalse(self.worker.connected)
        self.command(self.worker.send_code,'+12345678901');self.assertTrue(self.worker.code_requested)
        self.command(self.worker.login,'00000','');self.assertTrue(self.worker.connected)
        self.assertEqual([value['phase'] for name,value in self.drain() if name=='auth'][-1],'authorized')
    def test_connection_timeout_is_visible_and_disables_code_requests(self):
        self.transport.mode='timeout';self.command(self.worker.connect,12345,'c'*32,'')
        auth=[value for name,value in self.drain() if name=='auth']
        self.assertEqual(auth[-1]['phase'],'failed');self.assertIn('NETWORK',auth[-1]['message'])
        self.assertIn('代理',auth[-1]['message']);self.assertFalse(self.worker.network_ready)
        self.assertEqual(self.transport.closed,1);self.assertIsNone(self.worker.transport)
        self.command(self.worker.send_code,'+12345678901');self.assertEqual(self.transport.codes,0)
    def test_user_can_cancel_pending_connection_and_retry(self):
        self.transport.mode='pending'
        future=asyncio.run_coroutine_threadsafe(self.worker._command(self.worker.connect,12345,'c'*32,''),self.worker.loop)
        self.assertTrue(self.transport.started.wait(2));self.worker.cancel_auth();future.result(2)
        auth=[value for name,value in self.drain() if name=='auth']
        self.assertEqual(auth[-1]['phase'],'idle');self.assertFalse(self.worker.busy);self.assertEqual(self.transport.closed,1)
        self.transport.mode='ready';self.command(self.worker.connect,12345,'c'*32,'')
        self.assertTrue(self.worker.network_ready)
    def test_error_feedback_never_echoes_exception_arguments(self):
        self.transport.mode='secret_error';self.command(self.worker.connect,12345,'c'*32,'')
        text=json.dumps(self.drain());self.assertNotIn('DUMMY_PRIVATE_VALUE',text);self.assertNotIn('c'*32,text)

class GuiAuthTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from app import App
        self.temp=tempfile.TemporaryDirectory();self.root=tk.Tk();self.transport=FakeAuthTransport()
        self.patcher=patch('app.TelegramTransport',return_value=self.transport);self.patcher.start()
        self.app=App(self.root,Path(self.temp.name));self.until(lambda:self.app.worker.ready.is_set());self.root.update()
    def tearDown(self):
        self.patcher.stop();worker=self.app.worker
        asyncio.run_coroutine_threadsafe(worker.shutdown(),worker.loop).result(5)
        worker.loop.call_soon_threadsafe(worker.loop.stop);worker.thread.join(3);self.root.destroy();self.temp.cleanup()
    def until(self,predicate,seconds=4):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            self.root.update()
            if predicate():return
            time.sleep(.01)
        self.fail('GUI auth state did not settle')
    def configure_api(self):self.app.api_id.set('12345');self.app.api_hash.set('c'*32)
    def test_click_has_immediate_busy_feedback_and_cancel_button(self):
        self.configure_api();self.transport.mode='pending';self.app.buttons['connect'].invoke()
        self.assertIn('正在连接',self.app.connection.get())
        self.assertEqual(str(self.app.buttons['connect']['state']),'disabled')
        self.assertEqual(str(self.app.buttons['cancel_auth']['state']),'normal')
        self.assertEqual(str(self.app.buttons['send_code']['state']),'disabled')
        self.until(lambda:self.transport.started.is_set());self.app.buttons['cancel_auth'].invoke()
        self.until(lambda:self.app.auth_phase=='idle' and not self.app.busy)
        self.assertIn('已取消',self.app.connection.get());self.assertEqual(str(self.app.buttons['connect']['state']),'normal')
    def test_ready_country_code_validation_two_factor_and_login(self):
        self.configure_api();self.transport.two_factor=True;self.app.buttons['connect'].invoke()
        self.until(lambda:self.app.auth_phase=='ready' and not self.app.busy)
        self.assertIn('已连接 Telegram',self.app.connection.get());self.assertFalse(self.app.connected)
        self.assertEqual(str(self.app.buttons['send_code']['state']),'normal');self.assertEqual(str(self.app.buttons['login']['state']),'disabled')
        self.app.phone.set('12345678901');self.app.buttons['send_code'].invoke()
        self.assertIn('+国家区号',self.app.auth_message.get());self.assertEqual(self.transport.codes,0)
        self.app.phone.set('+12345678901');self.app.buttons['send_code'].invoke()
        self.until(lambda:self.app.auth_phase=='code_sent' and not self.app.busy)
        self.app.code.set('00000');self.app.buttons['login'].invoke()
        self.until(lambda:self.app.auth_phase=='password_required' and not self.app.busy)
        self.app.password.set('DUMMY_LOCAL_PASSWORD');self.app.buttons['login'].invoke()
        self.until(lambda:self.app.auth_phase=='authorized' and not self.app.busy)
        self.assertTrue(self.app.connected);self.assertEqual(self.app.password.get(),'');self.assertEqual(self.app.phone.get(),'')
        self.assertEqual(self.app.book.select(),str(self.app.download_page))
    def test_connection_failure_is_shown_above_tabs_and_in_auth_page(self):
        self.configure_api();self.transport.mode='timeout';self.app.buttons['connect'].invoke()
        self.until(lambda:self.app.auth_phase=='failed' and not self.app.busy)
        self.assertIn('NETWORK',self.app.connection.get());self.assertIn('NETWORK',self.app.auth_message.get())
        self.assertEqual(str(self.app.buttons['send_code']['state']),'disabled')
        self.assertEqual(str(self.app.buttons['connect']['state']),'normal')
    def test_log_remains_visible_at_small_and_large_window_sizes(self):
        for geometry in ('900x680','1080x800','2490x1300'):
            self.root.geometry(geometry);self.root.update()
            log=self.app.log;bottom=log.winfo_rooty()-self.root.winfo_rooty()+log.winfo_height()
            self.assertTrue(log.winfo_ismapped());self.assertGreater(log.winfo_height(),40)
            self.assertLessEqual(bottom,self.root.winfo_height())
            for key in ('connect','cancel_auth','send_code','login'):
                button=self.app.buttons[key]
                self.assertLessEqual(button.winfo_rootx()-self.root.winfo_rootx()+button.winfo_width(),self.root.winfo_width(),key)
    def test_invalid_api_is_visible_before_any_connection_attempt(self):
        self.app.buttons['connect'].invoke()
        self.assertIn('API_ID',self.app.connection.get());self.assertFalse(self.transport.started.is_set())

if __name__=='__main__':unittest.main()
