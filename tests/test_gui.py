"""In-process Tk tests of our own app. No external app automation or account login."""
import os, tempfile, time, unittest
from pathlib import Path

@unittest.skipUnless(os.name=='nt','Windows GUI release')
class GuiTests(unittest.TestCase):
    def test_demo_buttons_queue_pause_resume_and_layout(self):
        import tkinter as tk
        from app import App
        with tempfile.TemporaryDirectory() as temp:
            root=tk.Tk();app=App(root,Path(temp),demo=True)
            def until(test,timeout=8):
                end=time.monotonic()+timeout
                while time.monotonic()<end:
                    root.update()
                    if test():return
                    time.sleep(.02)
                self.fail('GUI did not reach expected state')
            try:
                until(lambda:app.worker.ready.is_set());root.update_idletasks()
                # The real widgets must fit at both supported sizes; the table scrolls.
                for geometry in ('1080x800','900x680'):
                    root.geometry(geometry);root.update()
                    for key,button in app.buttons.items():
                        if not button.winfo_ismapped():continue
                        left=button.winfo_rootx()-root.winfo_rootx();top=button.winfo_rooty()-root.winfo_rooty()
                        self.assertGreaterEqual(left,0,key);self.assertGreaterEqual(top,0,key)
                        self.assertLessEqual(left+button.winfo_width(),root.winfo_width()+2,key)
                        self.assertLessEqual(top+button.winfo_height(),root.winfo_height()+2,key)
                    self.assertGreater(app.tree.winfo_height(),45)
                self.assertEqual(str(app.buttons['start']['state']),'disabled')
                app.buttons['scan'].invoke()
                until(lambda:app.snapshot.get('summary',{}).get('video')==4 and not app.busy)
                app.buttons['start'].invoke();until(lambda:app.snapshot.get('running'))
                app.buttons['pause'].invoke();until(lambda:not app.snapshot.get('running') and not app.busy)
                self.assertGreater(app.snapshot['summary']['paused'],0)
                app.buttons['start'].invoke()
                until(lambda:app.snapshot.get('summary',{}).get('completed')==4 and not app.busy,12)
                self.assertEqual(app.snapshot['summary']['failed'],0)
                self.assertEqual(len(list((Path(temp)/'downloads').rglob('*.mp4'))),4)
            finally:
                if app.worker.thread.is_alive():
                    import asyncio
                    asyncio.run_coroutine_threadsafe(app.worker.shutdown(),app.worker.loop).result(5)
                    app.worker.loop.call_soon_threadsafe(app.worker.loop.stop);app.worker.thread.join(3)
                root.destroy()

if __name__=='__main__':unittest.main()
