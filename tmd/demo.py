"""Explicit offline demo only: no Telegram login, no network, fake media bytes."""
import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone
from .model import BLOCK
class DemoTransport:
    account="offline-demo"
    async def resolve(self,text):return -100123456,"offline_demo"
    async def dialogs(self):return [{"id":-100123456,"name":"离线演示频道","username":"offline_demo"}]
    def message(self,index):
        video=index%3!=0;length=(2+index)*BLOCK
        file=SimpleNamespace(size=length,mime_type="video/mp4" if video else "image/png",name="offline_test")
        doc=SimpleNamespace(id=1000+index) if video else None
        photo=SimpleNamespace(id=1000+index) if not video else None
        return SimpleNamespace(id=index,date=datetime(2026,10,25,tzinfo=timezone.utc),file=file,media=SimpleNamespace(ttl_seconds=None),video=doc,video_note=None,document=doc,photo=photo)
    async def history(self,peer,kinds,limit,end=None):
        count=0
        for index in range(6,0,-1):
            await asyncio.sleep(.1);message=self.message(index)
            if end and message.date>=end:continue
            if ("video" if message.video else "image") in kinds:
                if limit and count>=limit:break
                count+=1;yield message
    async def current(self,task):return self.message(task.message_id)
    async def chunks(self,message,offset,size):
        for position in range(offset,size,BLOCK):
            await asyncio.sleep(.15);yield bytes([message.id%251])*min(BLOCK,size-position)
    async def disconnect(self):pass
