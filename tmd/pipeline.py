"""Small, bounded out-of-order reads; only ordered, complete blocks may be yielded."""
import asyncio
from collections import deque
from .model import BLOCK, Fault

async def ordered_chunks(offset,size,read,width=2):
    positions=iter(range(offset,size,BLOCK));pending=deque();width=max(1,min(2,int(width)))
    def fill():
        while len(pending)<width:
            position=next(positions,None)
            if position is None:break
            pending.append((position,asyncio.create_task(read(position))))
    try:
        fill()
        while pending:
            position,job=pending[0];chunk=bytes(await job)
            # A later request may already have failed (e.g. FloodWait). Stop before issuing more.
            for _,other in pending:
                if other.done() and not other.cancelled():
                    error=other.exception()
                    if error is not None:raise error
            expected=min(BLOCK,size-position)
            if len(chunk)!=expected:
                raise Fault("TRUNCATED" if len(chunk)<expected else "LENGTH","分段长度与请求范围不符，保留检查点并停止本轮",len(chunk)<expected)
            pending.popleft();fill()
            yield chunk
    finally:
        tasks=[job for _,job in pending]
        for job in tasks:
            if not job.done():job.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
