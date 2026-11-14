"""Read-only speed probe: same session, one message, measure throughput.

Never touches the task database. Compares a direct connection with a proxy so the
download can be pointed at whichever is actually faster on this network.
"""
from __future__ import annotations

import argparse, asyncio, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmd.secure import Profile                 # noqa: E402
from tmd.transport import TelegramTransport    # noqa: E402

BLOCK = 512 * 1024


async def probe(data_dir, proxy, peer_id, message_id, seconds):
    profile = Profile(Path(data_dir))
    transport = TelegramTransport(profile, profile.data["api_id"], profile.secret("api_hash_cipher"), proxy)
    await transport.connect()
    peer, name = await transport.resolve(str(peer_id))
    message = await transport.client.get_messages(transport.peers[peer], ids=message_id)
    total = getattr(message.file, "size", 0)
    received = 0
    started = time.monotonic()
    iterator = transport.client.iter_download(message, offset=0, request_size=BLOCK, chunk_size=BLOCK, file_size=total)
    try:
        while time.monotonic() - started < seconds:
            chunk = await asyncio.wait_for(anext(iterator), 60)
            received += len(chunk)
    except (StopAsyncIteration, asyncio.TimeoutError):
        pass
    finally:
        await transport.close_iterator(iterator)
    elapsed = time.monotonic() - started
    print(f"proxy={proxy or 'direct'} peer={name} size={total/1e6:.1f}MB "
          f"read={received/1e6:.1f}MB in {elapsed:.0f}s => {received/elapsed/1024/1024:.3f} MB/s", flush=True)
    await transport.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--peer", default="-1001540882802")
    parser.add_argument("--message", type=int, default=15145)
    parser.add_argument("--seconds", type=int, default=40)
    a = parser.parse_args()
    asyncio.run(probe(a.data_dir, a.proxy, int(a.peer), a.message, a.seconds))
