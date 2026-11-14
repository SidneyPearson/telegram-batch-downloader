import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from tmd.model import BLOCK

def message(index=1, kind="video", size=2*BLOCK+91, media_id=None):
    identity=NS(id=media_id or 1000+index)
    return NS(id=index,date=datetime(2026,10,25,12,tzinfo=timezone.utc),
              media=NS(ttl_seconds=None),file=NS(size=size,mime_type="video/mp4" if kind=="video" else "image/jpeg",name="original"),
              photo=identity if kind=="image" else None,document=identity if kind=="video" else None,
              video=identity if kind=="video" else None,video_note=None)

def data_for(index, size):return bytes([index%251])*size

class FakeTransport:
    account="test-account"
    def __init__(self, messages=None, delay=0):
        self.messages=messages or [message()];self.delay=delay;self.calls=[];self.failures={};self.current_calls=[]
        self.open=0;self.videos=0;self.max_open=0;self.max_videos=0;self.closed=0;self.max_chunk=0
    async def resolve(self,channel):return -100123,"test_channel"
    async def dialogs(self):return []
    async def history(self,peer,kinds,limit,end=None):
        count=0
        for item in self.messages:
            if ("video" if item.video else "image") not in kinds:continue
            if end and item.date>=end:continue
            if limit and count>=limit:break
            count+=1;await asyncio.sleep(0);yield item
    async def current(self,task):
        self.current_calls.append(task.message_id)
        value=next(m for m in self.messages if m.id==task.message_id)
        return value
    async def chunks(self,item,offset,size):
        self.calls.append((item.id,offset));self.open+=1;self.videos+=bool(item.video)
        self.max_open=max(self.max_open,self.open);self.max_videos=max(self.max_videos,self.videos)
        try:
            for position in range(offset,size,BLOCK):
                await asyncio.sleep(self.delay)
                failures=self.failures.get(item.id,[])
                if failures:raise failures.pop(0)
                chunk=data_for(item.id,min(BLOCK,size-position));self.max_chunk=max(self.max_chunk,len(chunk));yield chunk
        finally:self.open-=1;self.videos-=bool(item.video);self.closed+=1
    async def disconnect(self):pass
