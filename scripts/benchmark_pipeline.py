"""Latency simulation only: no real Telegram access or speed claim."""
import asyncio, hashlib, json, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tmd.model import BLOCK
from tmd.pipeline import ordered_chunks

async def run(width):
    size=12*BLOCK;digest=hashlib.sha256()
    async def read(position):
        await asyncio.sleep(.08)
        return bytes([(position//BLOCK)%251])*BLOCK
    start=time.perf_counter()
    async for chunk in ordered_chunks(0,size,read,width):digest.update(chunk)
    return {'width':width,'seconds':round(time.perf_counter()-start,4),'sha256':digest.hexdigest()}
async def main():
    serial=await run(1);prefetch=await run(2)
    assert serial['sha256']==prefetch['sha256']
    result={'scope':'Offline 80ms-per-block latency simulation; NOT a Telegram/real proxy benchmark','bytes':12*BLOCK,
            'serial':serial,'prefetch':prefetch,'ratio':round(serial['seconds']/prefetch['seconds'],3)}
    (ROOT/'docs'/'pipeline-benchmark.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2))
asyncio.run(main())
