"""Headless driver for the same engine the GUI uses (no window, same database).

Reuses the program's own Profile / Storage / Engine / TelegramTransport so queue
semantics, checkpoints, dedup and error codes are identical to the GUI. The saved
DPAPI session is reused; if Telegram asks for a fresh code, the script stops and
tells you to log in through the GUI instead of handling secrets here.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmd.engine import Engine            # noqa: E402
from tmd.model import Fault, parse_date  # noqa: E402
from tmd.secure import Profile           # noqa: E402
from tmd.storage import Storage          # noqa: E402
from tmd.transport import TelegramTransport  # noqa: E402


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


async def main(arguments):
    data_dir = Path(arguments.data_dir).resolve()
    profile = Profile(data_dir)
    store = Storage(data_dir / "tasks.sqlite3")
    engine = Engine(store)

    state = {"last_print": 0.0}

    def notify(event, payload):
        if event == "log":
            log(payload)
            return
        if event != "snapshot":
            return
        now = time.monotonic()
        if now - state["last_print"] < 20:
            return
        state["last_print"] = now
        summary = payload.get("summary", {})
        speed = payload.get("speed", 0)
        log("进度：完成 {}/{}，队列 {}，下载中 {}，失败 {}，暂停 {}；速度 {:.2f} MB/s；{}".format(
            summary.get("completed", 0), summary.get("total", 0), summary.get("queued", 0),
            summary.get("downloading", 0), summary.get("failed", 0), summary.get("paused", 0),
            speed / 1024 / 1024, payload.get("scope", "")))

    engine.notify = notify
    api_id = profile.data.get("api_id")
    api_hash = profile.secret("api_hash_cipher")
    proxy = arguments.proxy
    if proxy == "profile":
        proxy = profile.data.get("proxy", "")
    elif proxy == "none":
        proxy = ""
    if not api_id or not api_hash:
        log("配置里没有 API ID/API Hash，请先在图形界面填写")
        return 4

    transport = TelegramTransport(profile, api_id, api_hash, proxy)
    engine.transport = transport
    log("正在连接 Telegram（复用本机已加密保存的会话）…")
    try:
        authorized = await asyncio.wait_for(transport.connect(), 40)
    except Exception as error:
        log(f"连接失败：{error}")
        await transport.disconnect()
        return 5
    if not authorized:
        log("NEED_LOGIN：本机会话已失效或未登录，请在图形界面里由你本人完成验证码登录")
        await transport.disconnect()
        return 3
    engine.account = transport.account
    log(f"已连接；账号指纹 {engine.account}")

    if arguments.no_scan:
        # run() needs a resolved peer; scan would normally set it.
        engine.peer, engine.channel = await transport.resolve(arguments.channel)
        log(f"已定位频道 {engine.channel}（peer {engine.peer}），跳过扫描直接下载现有队列")

    if not arguments.no_scan:
        kinds = ["video", "image"] if arguments.kinds == "both" else [arguments.kinds]
        total_before = store.summary(engine.account)
        log(f"扫描前：总数 {total_before['total']}，完成 {total_before['completed']}，队列 {total_before['queued']}")
        log(f"开始扫描 {arguments.channel}，类型 {kinds}，上限 {arguments.limit or '全部'}，"
            f"日期 {arguments.start or '不限'} → {arguments.end or '不限'}")
        try:
            await engine.scan(arguments.channel, kinds, arguments.limit,
                              parse_date(arguments.start or ""), parse_date(arguments.end or "", True),
                              arguments.folder)
        except Fault as error:
            log(f"扫描出错 [{error.code}] {error.message}")
            await transport.disconnect()
            return 6
        total_after = store.summary(engine.account)
        log(f"扫描后：总数 {total_after['total']}，完成 {total_after['completed']}，队列 {total_after['queued']}")

    if not arguments.scan_only:
        log(f"开始下载，文件并发 {arguments.concurrency}")
        await engine.run(arguments.concurrency)
        log("下载循环结束")

    final = store.summary(engine.account, engine.peer)
    log("最终：总数 {total}，完成 {completed}，队列 {queued}，失败 {failed}，暂停 {paused}，"
        "已传输 {received} 字节".format(**final))
    store.close()
    await transport.disconnect()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--channel", default="@jiepairewu")
    parser.add_argument("--kinds", choices=["video", "image", "both"], default="video")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--proxy", default="profile",
                        help="profile=用配置里的代理；none=直连；也可直接给 socks5://host:port")
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--no-scan", action="store_true")
    arguments = parser.parse_args()
    try:
        code = asyncio.run(main(arguments))
    except KeyboardInterrupt:
        log("已中断")
        code = 130
    sys.exit(code)
