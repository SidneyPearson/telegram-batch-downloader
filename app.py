from __future__ import annotations
import argparse, asyncio, json, os, queue, re, sys, threading, time, tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from tmd.model import Fault, parse_date
from tmd.secure import Profile, system_proxy, parse_proxy
from tmd.storage import Storage
from tmd.transport import TelegramTransport, fault, validate_api, validate_phone
from tmd.engine import Engine, available_space, DISK_RESERVE
from tmd import __version__

VERSION=__version__
FONT_FAMILY="PingFang SC" if sys.platform=="darwin" else "Microsoft YaHei UI"
SECRET_STORE="Windows DPAPI" if os.name=="nt" else "macOS 钥匙串" if sys.platform=="darwin" else "本机安全存储"
AUTH_ACTIONS={"connect","send_code","login"}
STATUS={"queued":"等待中","downloading":"下载中","completed":"已完成","failed":"失败","paused":"已暂停"}
def size(value):
    value=float(value or 0)
    for unit in ("B","KiB","MiB","GiB","TiB"):
        if value<1024 or unit=="TiB":return f"{value:.1f} {unit}"
        value/=1024

class Worker:
    def __init__(self,base,events,demo=False):
        self.base=Path(base);self.events=events;self.demo=demo;self.profile=Profile(self.base/"state")
        self.loop=asyncio.new_event_loop();self.store=None;self.engine=None;self.transport=None
        self.ready=threading.Event();self.busy=False;self.connected=False;self.command_job=None
        self.network_ready=False;self.code_requested=False;self.auth_action=None
        self.cancel_requested=threading.Event()
        self.thread=threading.Thread(target=self._thread,daemon=True);self.thread.start()
    def send(self,event,data):self.events.put((event,data))
    def _thread(self):
        asyncio.set_event_loop(self.loop)
        try:
            from telethon.crypto.aes import cryptg
            self.send("log","原生加密加速已启用" if cryptg is not None else "当前使用兼容加密，下载可能较慢：原生加速库未加载")
            self.store=Storage(self.base/"state"/"tasks.sqlite3")
            self.engine=Engine(self.store,notify=self.send)
            if self.demo:
                from tmd.demo import DemoTransport
                self.transport=DemoTransport();self.engine.transport=self.transport;self.engine.account=self.transport.account;self.connected=True;self.network_ready=True
            self.ready.set();self.loop.run_forever()
        except Exception as error:self.send("error",fault(error).message);self.ready.set()
        finally:
            pending=asyncio.all_tasks(self.loop)
            for task in pending:task.cancel()
            if pending:self.loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
            if self.store:self.store.close()
            self.loop.close()
    def submit(self,func,*args):
        if not self.ready.wait(2) or not self.engine or not self.loop.is_running():self.send("error","后台未能初始化，请重新打开程序");return False
        if func.__name__ in AUTH_ACTIONS:self.cancel_requested.clear()
        future=asyncio.run_coroutine_threadsafe(self._command(func,*args),self.loop)
        future.add_done_callback(lambda result: self._done(result))
        return True
    def _done(self,result):
        try:result.result()
        except BaseException:self.send("error","后台任务未完成，请重新打开程序；已记录的下载片段保留")
    async def _command(self,func,*args):
        if self.busy:self.send("error","正在处理上一项操作，请稍候");return
        self.busy=True;self.command_job=asyncio.current_task();self.auth_action=func.__name__ if func.__name__ in AUTH_ACTIONS else None;self.send("busy",True)
        try:
            if self.auth_action and self.cancel_requested.is_set():raise asyncio.CancelledError()
            await func(*args)
        except asyncio.CancelledError:
            if self.auth_action:
                self.network_ready=False;self.code_requested=False;self.connected=False
                self.auth("idle","已取消连接/登录操作，可检查代理后重新连接")
                await self.drop_transport()
            else:self.send("log","操作已停止；已保存的任务和片段保留")
        except Exception as error:
            issue=fault(error);self.send("error",f"[{issue.code}] {issue.message}")
            if self.auth_action:
                if issue.code=="PASSWORD_REQUIRED":phase="password_required"
                elif self.auth_action=="connect" or issue.code in {"NETWORK","AUTH_REQUIRED"}:
                    phase="failed";self.network_ready=False;self.code_requested=False;self.connected=False
                else:phase="password_required" if getattr(self.transport,"needs_password",False) else "code_sent" if self.code_requested else "ready" if self.network_ready else "failed"
                message=f"[{issue.code}] {issue.message}"
                if issue.code=="NETWORK":
                    message="[NETWORK] 连接失败或超时。请填写代理软件实际提供的本地 HTTP/SOCKS 端口；浏览器能打开 Telegram 不代表本程序能直连。"
                    if self.auth_action=="send_code":message+=" 验证码请求结果未确定，不会自动重发。"
                self.auth(phase,message,issue.code!="PASSWORD_REQUIRED")
                if phase=="failed":await self.drop_transport()
            if issue.code=="PASSWORD_REQUIRED":self.send("password_required",True)
        finally:self.busy=False;self.command_job=None;self.auth_action=None;self.cancel_requested.clear();self.send("busy",False);self.engine.emit(True)
    def auth(self,phase,message,error=False):
        self.send("auth",{"phase":phase,"message":message,"error":error,"network_ready":self.network_ready,"code_requested":self.code_requested})
    async def drop_transport(self):
        transport=self.transport;self.transport=None;self.engine.transport=None
        if transport:
            try:await asyncio.wait_for(transport.disconnect(),5)
            except Exception:pass
    def cancel_auth(self):
        self.cancel_requested.set()
        def cancel():
            if self.auth_action and self.command_job:self.command_job.cancel()
        self.loop.call_soon_threadsafe(cancel)
    async def connect(self,api_id,api_hash,proxy):
        if self.engine.running:raise Fault("BUSY","请先暂停下载")
        validate_api(api_id,api_hash);parse_proxy(proxy)
        self.network_ready=False;self.code_requested=False
        self.connected=False;self.engine.account="";self.engine.peer=None;self.engine.channel="";self.send("connected",False)
        self.auth("connecting","正在通过代理连接 Telegram，最多等待约 30 秒；可点击取消。" if proxy.strip() else "正在直连 Telegram，最多等待约 30 秒；使用代理软件时请填写实际代理端口。")
        if self.transport:await self.drop_transport()
        self.transport=TelegramTransport(self.profile,api_id,api_hash,proxy);self.engine.transport=self.transport
        try:self.profile.save(proxy=proxy)
        except OSError:raise Fault("PROFILE_WRITE","程序目录不可写；请将完整便携包解压到可写目录") from None
        authorized=await self.transport.connect()
        self.network_ready=True
        if authorized:await self._authorized()
        else:self.auth("ready","已连接 Telegram，尚未登录。下一步：输入 +国家区号 的手机号，再点击“发送验证码”。")
    async def send_code(self,phone):
        if not self.transport or not self.network_ready:raise Fault("CONNECTION","请先连接成功，再发送验证码")
        phone=validate_phone(phone)
        self.auth("sending_code","正在请求验证码，最多等待约 45 秒；不会自动重复发送。")
        if await self.transport.send_code(phone):await self._authorized()
        else:
            self.code_requested=True
            self.auth("code_sent","验证码已请求。请在自己的 Telegram 已登录设备或平台提供的接收方式查看，然后在本机验证登录。")
    async def login(self,code,password):
        if not self.transport or not self.network_ready or not self.code_requested:raise Fault("CONNECTION","请先连接成功并发送验证码")
        self.auth("verifying","正在验证登录，最多等待约 45 秒；验证码和密码不会写入日志。")
        await self.transport.sign_in(code,password);await self._authorized()
    async def _authorized(self):
        self.connected=True;self.network_ready=True;self.code_requested=False;self.engine.account=self.transport.account
        self.engine.peer=None;self.engine.channel="";self.engine.scan_scope="尚未扫描"
        self.send("connected",True);self.send("auth_success",True);self.send("log","登录成功；无需在 Chrome 逐个打开视频")
        self.auth("authorized","登录成功；现在可以选择频道并扫描媒体。登录会话仅在本机加密保存。")
        last=self.store.setting("last-peer:"+self.engine.account)
        if last:self.engine.peer=last["peer"];self.engine.channel=last["channel"];self.engine.scan_scope=last.get("scope","已恢复本地记录；可继续或重新扫描")
    async def dialogs(self):
        if not self.connected:raise Fault("AUTH_REQUIRED","请先登录")
        values=await self.transport.dialogs();self.send("dialogs",values);self.send("log",f"已读取 {len(values)} 个频道/群组；不会自动加入新频道")
    async def scan(self,channel,kinds,limit,start,end,folder):
        if not self.connected:raise Fault("AUTH_REQUIRED","请先登录")
        await self.engine.scan(channel,kinds,limit,parse_date(start),parse_date(end,True),folder)
        self.profile.save(channel=channel,folder=folder)
        if self.engine.peer is not None:self.store.set_setting("last-peer:"+self.engine.account,{"peer":self.engine.peer,"channel":self.engine.channel,"scope":self.engine.scan_scope})
    async def start(self,concurrency):
        if not self.connected:raise Fault("AUTH_REQUIRED","请先登录")
        await self.engine.run(concurrency)
        if self.engine.network_halt:
            self.connected=False;self.network_ready=False;self.send("connected",False);self.auth("failed","登录已失效；请返回连接账号页面重新连接。",True)
    def pause(self):
        if self.engine:self.engine.stop.set();self.loop.call_soon_threadsafe(self.engine.request_pause)
    async def retry(self):self.engine.retry_failed()
    async def export(self,path):self.engine.export(path);self.send("log","任务记录已导出；不含登录秘密")
    async def shutdown(self):
        self.engine.request_pause()
        if self.command_job:
            self.command_job.cancel()
            await asyncio.gather(self.command_job,return_exceptions=True)
        try:await asyncio.wait_for(self.engine.idle.wait(),10)
        except asyncio.TimeoutError:pass
        if self.transport:
            try:await asyncio.wait_for(self.transport.disconnect(),10)
            except Exception:pass

class App:
    def __init__(self,root,base,demo=False):
        self.root=root;self.base=Path(base);self.demo=demo;self.events=queue.Queue()
        self.window_file=self.base/"window.json";self.ui_signature=None;self._last_space_check=0
        self.worker=Worker(base,self.events,demo);self.snapshot={};self.row_cache={};self.row_index={};self.busy=False;self.connected=demo;self.closing=False
        self.network_ready=demo;self.code_requested=False;self.auth_phase="authorized" if demo else "idle";self.cancelling=False
        self.root.title(f"Telegram 本地批量下载器 v{VERSION}"+(" · 离线演示" if demo else ""))
        self.root.geometry("1080x800");self.root.minsize(900,680);self.root.configure(bg="#eef3f8")
        self.restore_geometry()
        style=ttk.Style();style.theme_use("clam");style.configure(".",font=(FONT_FAMILY,10))
        style.configure("TFrame",background="#eef3f8");style.configure("TLabelframe",background="#eef3f8")
        style.configure("TLabelframe.Label",background="#eef3f8");style.configure("TLabel",background="#eef3f8")
        style.configure("Header.TLabel",font=(FONT_FAMILY,19,"bold"),foreground="#173451")
        style.configure("Accent.TButton",foreground="white",background="#1686c8",padding=(12,7))
        style.map("Accent.TButton",background=[("disabled","#a9bfd0"),("active","#126caa")])
        style.configure("Treeview",rowheight=29,background="white",fieldbackground="white")
        main=ttk.Frame(root,padding=18);main.pack(fill="both",expand=True)
        ttk.Label(main,text="Telegram 本地批量下载器",style="Header.TLabel").pack(anchor="w")
        ttk.Label(main,text="正常 API 登录 · 直接读取消息附件 · 无需打开网页视频",foreground="#536b80").pack(anchor="w",pady=(4,12))
        self.connection=tk.StringVar(value="离线演示：不连接账号，模拟文件不是可播放视频" if demo else "尚未登录：先在“连接账号”填写自己的 API 配置")
        ttk.Label(main,textvariable=self.connection,wraplength=800).pack(anchor="w",pady=(0,8))
        self.book=ttk.Notebook(main);self.book.pack(fill="both",expand=True)
        auth=ttk.Frame(self.book,padding=16);download=ttk.Frame(self.book,padding=14)
        self.book.add(auth,text="连接账号");self.book.add(download,text="批量下载")
        self.download_page=download;self.auth_page=auth;self.buttons={};self.inputs=[]
        profile=self.worker.profile
        self.api_id=tk.StringVar(value=str(profile.data.get("api_id","")))
        try:hash_value=profile.secret("api_hash_cipher")
        except Exception:hash_value="";self.connection.set("本机配置不能解密，请使用自己的 API 配置重新连接")
        self.api_hash=tk.StringVar(value=hash_value);self.proxy=tk.StringVar(value=profile.data.get("proxy",system_proxy()))
        self.phone=tk.StringVar();self.code=tk.StringVar();self.password=tk.StringVar()
        ttk.Label(auth,text="首次准备：在 my.telegram.org → API development tools 获取 API ID / API Hash。",wraplength=900).grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,12))
        labels=[("API ID",self.api_id,False),("API Hash",self.api_hash,True),("现有 HTTP/SOCKS5 代理（可选）",self.proxy,False),
                ("手机号（以 +国家区号 开头）",self.phone,False),("Telegram 验证码",self.code,True),("两步验证密码（启用时才需要）",self.password,True)]
        for row,(text,var,secret) in enumerate(labels,1):
            ttk.Label(auth,text=text).grid(row=row,column=0,sticky="w",padx=(0,12),pady=7)
            entry=ttk.Entry(auth,textvariable=var,width=54,show="●" if secret else "");entry.grid(row=row,column=1,sticky="ew",pady=7)
            self.inputs.append(entry)
        auth.columnconfigure(1,weight=1)
        self.add_button(auth,"connect","连接 / 恢复登录",self.connect,row=1,column=2)
        self.add_button(auth,"cancel_auth","取消连接 / 登录",self.cancel_auth,row=2,column=2)
        self.add_button(auth,"system_proxy","读取系统代理",self.read_proxy,row=3,column=2)
        self.add_button(auth,"send_code","发送验证码",self.request_code,row=4,column=2)
        self.add_button(auth,"login","验证并登录",self.login,row=5,column=2)
        self.auth_progress=ttk.Progressbar(auth,mode="indeterminate",length=130);self.auth_progress.grid(row=6,column=2,padx=10,sticky="w");self.auth_progress.grid_remove()
        self.auth_message=tk.StringVar(value="先点击连接；连接成功后，发送验证码按钮才会启用。")
        self.auth_notice=ttk.Label(auth,textvariable=self.auth_message,wraplength=760,foreground="#36546e")
        self.auth_notice.grid(row=7,column=0,columnspan=3,sticky="w",pady=(12,4))
        ttk.Label(auth,text=f"验证码和两步验证密码只在本机输入，不保存。API Hash 与登录会话通过 {SECRET_STORE} 加密。\n独立程序不会读取浏览器 Cookie，也不会自动继承 Chrome 扩展代理。",wraplength=760,foreground="#536b80").grid(row=8,column=0,columnspan=3,sticky="w",pady=10)
        links=ttk.Frame(auth);links.grid(row=9,column=0,columnspan=3,sticky="w")
        ttk.Button(links,text="打开 Telegram 官方 API 配置页",command=self.open_api).pack(side="left")
        ttk.Button(links,text="打开安装与使用说明",command=self.open_readme).pack(side="left",padx=8)
        settings=ttk.LabelFrame(download,text="发现范围与保存位置",padding=10);settings.pack(fill="x")
        self.channel=tk.StringVar(value=profile.data.get("channel","@jiepairewu"))
        self.channel_box=ttk.Combobox(settings,textvariable=self.channel,width=48);self.channel_box.grid(row=0,column=1,columnspan=3,sticky="ew")
        self.dialog_map={}
        self.channel_box.bind("<<ComboboxSelected>>",self.select_dialog)
        ttk.Label(settings,text="频道 / 群组").grid(row=0,column=0,sticky="w",padx=(0,10))
        self.add_button(settings,"dialogs","读取已加入频道",lambda:self.worker.submit(self.worker.dialogs),row=0,column=4)
        self.folder=tk.StringVar(value=profile.data.get("folder",str(self.base/"downloads")))
        ttk.Label(settings,text="保存目录").grid(row=1,column=0,sticky="w")
        folder_entry=ttk.Entry(settings,textvariable=self.folder);folder_entry.grid(row=1,column=1,columnspan=3,sticky="ew",pady=7);self.inputs.append(folder_entry)
        self.add_button(settings,"folder","选择目录",self.choose_folder,row=1,column=4)
        self.start_date=tk.StringVar();self.end_date=tk.StringVar();self.limit=tk.StringVar(value="500");self.concurrency=tk.StringVar(value="2")
        ttk.Label(settings,text="北京时间日期").grid(row=2,column=0,sticky="w")
        dates=ttk.Frame(settings);dates.grid(row=2,column=1,columnspan=4,sticky="w")
        for text,var,width in [("从",self.start_date,12),("至",self.end_date,12),("媒体消息上限",self.limit,7),("文件并发",self.concurrency,3)]:
            ttk.Label(dates,text=text).pack(side="left",padx=(0,4))
            entry=ttk.Entry(dates,textvariable=var,width=width);entry.pack(side="left",padx=(0,10));self.inputs.append(entry)
        types=ttk.Frame(settings);types.grid(row=3,column=1,columnspan=4,sticky="w",pady=(7,0))
        self.videos=tk.BooleanVar(value=True);self.photos=tk.BooleanVar(value=False)
        ttk.Checkbutton(types,text="视频（始终串行）",variable=self.videos).pack(side="left")
        ttk.Checkbutton(types,text="图片",variable=self.photos).pack(side="left",padx=12)
        ttk.Label(types,text="日期留空不限；上限 0 表示全部可访问媒体",foreground="#536b80").pack(side="left")
        settings.columnconfigure(1,weight=1)
        controls=ttk.Frame(download);controls.pack(fill="x",pady=8)
        for key,label,cmd in [
            ("scan","扫描并加入队列",self.scan),
            ("start","开始 / 继续",self.start),
            ("pause","暂停",self.worker.pause),("stop","停止",self.worker.pause),
            ("retry","重试失败",lambda:self.worker.submit(self.worker.retry)),("export","导出记录",self.export)]:
            self.add_button(controls,key,label,cmd,pack=True,accent=key=="start")
        self.stats=tk.StringVar(value="图片 0    视频 0    等待 0    成功 0    失败 0")
        ttk.Label(download,textvariable=self.stats,font=(FONT_FAMILY,11,"bold")).pack(anchor="w")
        self.speed=tk.StringVar(value="0 B/s");self.scope=tk.StringVar(value="先扫描指定范围；不会把检索数量当作频道媒体总数")
        ttk.Label(download,textvariable=self.speed).pack(anchor="w",pady=(5,0))
        self.disk=tk.StringVar(value="");ttk.Label(download,textvariable=self.disk,foreground="#536b80").pack(anchor="w")
        self.progress=ttk.Progressbar(download,maximum=100);self.progress.pack(fill="x",pady=7)
        ttk.Label(download,textvariable=self.scope,wraplength=970,foreground="#536b80").pack(anchor="w",pady=(0,8))
        columns=("name","type","status","progress","error")
        frame=ttk.Frame(download);frame.pack(fill="both",expand=True)
        self.tree=ttk.Treeview(frame,columns=columns,show="headings",height=9)
        for col,title,width in zip(columns,["文件 / 消息","类型","状态","进度","提示"],[340,65,85,150,290]):
            self.tree.heading(col,text=title);self.tree.column(col,width=width,minwidth=50,stretch=col in ("name","error"))
        scrollbar=ttk.Scrollbar(frame,orient="vertical",command=self.tree.yview);self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left",fill="both",expand=True);scrollbar.pack(side="right",fill="y")
        ttk.Label(download,text="显示最近/优先 200 项；已有片段优先续传。暂停/失败会保留 .part，数量可超过并发数；失败项需点“重试失败”，服务器拒绝访问的任务不重试。",wraplength=980,foreground="#536b80").pack(anchor="w",pady=(7,0))
        self.log=tk.Text(main,height=3,wrap="word",font=(FONT_FAMILY,9),bg="#f7f9fc",fg="#36546e",relief="flat",state="disabled")
        self.log.pack(side="bottom",fill="x",pady=(6,0),before=self.book);self.log_lines=0
        if demo:self.book.select(download)
        self.build_menu()
        self.root.protocol("WM_DELETE_WINDOW",self.close);self.root.after(150,self.poll)
        if sys.platform=="darwin":
            # Tk windows can start behind other apps; an inactive first click is not delivered.
            self.root.lift();self.root.focus_force();self.root.after(300,self.root.focus_force)
    def add_button(self,parent,key,label,cmd,row=None,column=None,pack=False,accent=False):
        button=ttk.Button(parent,text=label,command=cmd,style="Accent.TButton" if accent else "TButton")
        if pack:button.pack(side="left",padx=(0,7))
        else:button.grid(row=row,column=column,padx=(10,0),pady=3,sticky="w")
        self.buttons[key]=button;return button
    def restore_geometry(self):
        try:value=json.loads(self.window_file.read_text("utf8")).get("geometry","")
        except (OSError,ValueError):return
        match=re.fullmatch(r"(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?",str(value).strip())
        if not match:return
        width,height=int(match.group(1)),int(match.group(2))
        sw,sh=self.root.winfo_screenwidth(),self.root.winfo_screenheight()
        width=min(max(width,900),sw);height=min(max(height,680),sh)
        if match.group(3) is None:self.root.geometry(f"{width}x{height}")
        else:
            x=max(0,min(int(match.group(3)),sw-200));y=max(0,min(int(match.group(4)),sh-200))
            self.root.geometry(f"{width}x{height}+{x}+{y}")
    def save_geometry(self):
        try:self.window_file.write_text(json.dumps({"geometry":self.root.geometry()}),encoding="utf8")
        except OSError:pass
    def build_menu(self):
        # Native Aqua menu bar; accelerators are display hints, bindings do the work.
        if sys.platform!="darwin":return
        menubar=tk.Menu(self.root)
        app_menu=tk.Menu(menubar,name="apple",tearoff=False)
        app_menu.add_command(label="关于 Telegram 本地批量下载器",command=self.about)
        app_menu.add_separator()
        app_menu.add_command(label="退出",accelerator="Command+Q",command=self.close)
        menubar.add_cascade(menu=app_menu)
        edit=tk.Menu(menubar,tearoff=False)
        for label,event,key in [("撤销","<<Undo>>","Command+Z"),("重做","<<Redo>>","Command+Shift+Z"),("剪切","<<Cut>>","Command+X"),
                                ("复制","<<Copy>>","Command+C"),("粘贴","<<Paste>>","Command+V"),("全选","<<SelectAll>>","Command+A")]:
            edit.add_command(label=label,accelerator=key,command=lambda value=event:self.menu_event(value))
        menubar.add_cascade(label="编辑",menu=edit)
        self.root.configure(menu=menubar)
        self.root.createcommand("tk::mac::Quit",self.close)
        self.root.createcommand("tk::mac::ReopenApplication",self.reopen)
        self.root.bind_all("<Command-q>",lambda event:self.close())
    def menu_event(self,event):
        widget=self.root.focus_get()
        if widget:
            try:widget.event_generate(event)
            except tk.TclError:pass
    def about(self):
        messagebox.showinfo("关于",f"Telegram 本地批量下载器 v{VERSION}\n正常 API 登录、直接读取消息附件；登录会话通过 {SECRET_STORE} 加密保存。")
    def reopen(self):
        self.root.deiconify();self.root.lift();self.root.focus_force()
    def refresh_space(self):
        folder=self.folder.get().strip()
        if not folder:return
        free=available_space(folder)
        self.disk.set(f"保存目录可用空间 {size(free)}" if free>=0 else "无法读取保存目录的可用空间")
    def open_api(self):
        import webbrowser
        webbrowser.open("https://my.telegram.org")
    def open_readme(self):
        candidates=[Path(getattr(sys,"_MEIPASS",Path(sys.executable).parent))] if getattr(sys,"frozen",False) else [Path(__file__).resolve().parent]
        candidates.append(Path(__file__).resolve().parent)
        for origin in candidates:
            try:text=(origin/"README.md").read_text(encoding="utf8");break
            except OSError:continue
        else:self.append("未找到 README.md；请保留程序包内的说明文件");return
        window=tk.Toplevel(self.root);window.title("安装与使用说明");window.geometry("900x650")
        frame=ttk.Frame(window,padding=12);frame.pack(fill="both",expand=True)
        content=tk.Text(frame,wrap="word",font=(FONT_FAMILY,10),padx=10,pady=10)
        scroll=ttk.Scrollbar(frame,orient="vertical",command=content.yview);content.configure(yscrollcommand=scroll.set)
        content.pack(side="left",fill="both",expand=True);scroll.pack(side="right",fill="y")
        content.insert("1.0",text);content.configure(state="disabled")
    def choose_folder(self):
        folder=filedialog.askdirectory(title="选择下载保存目录",initialdir=self.folder.get() if Path(self.folder.get()).exists() else str(self.base))
        if folder:self.folder.set(folder)
    def select_dialog(self,event=None):
        chosen=self.channel.get()
        if chosen in self.dialog_map:self.channel.set(self.dialog_map[chosen])
    def apply_auth(self,data):
        self.auth_phase=data.get("phase",self.auth_phase)
        self.network_ready=data.get("network_ready",self.network_ready);self.code_requested=data.get("code_requested",self.code_requested)
        self.cancelling=False;message=data["message"]
        self.connection.set(message);self.auth_message.set(message)
        self.auth_notice.configure(foreground="#aa3030" if data.get("error") else "#36546e")
        self.auth_progress.stop()
        if self.auth_phase in {"connecting","sending_code","verifying"}:
            self.auth_progress.grid();self.auth_progress.start(60)
        else:self.auth_progress.grid_remove()
    def auth_error(self,message):
        self.connection.set(message);self.auth_message.set(message);self.auth_notice.configure(foreground="#aa3030");self.append(message)
    def begin_auth(self,func,phase,message,*args):
        if self.busy:return
        self.busy=True;self.apply_auth({"phase":phase,"message":message});self.update_buttons()
        if not self.worker.submit(func,*args):
            self.busy=False;self.apply_auth({"phase":"failed","message":"后台未能启动，请重新打开程序","error":True,"network_ready":False});self.update_buttons()
    def connect(self):
        if self.busy:return
        try:
            api_id,api_hash=validate_api(self.api_id.get(),self.api_hash.get());proxy=self.proxy.get().strip();parse_proxy(proxy)
        except Fault as error:self.auth_error(f"[{error.code}] {error.message}");return
        self.begin_auth(self.worker.connect,"connecting","正在连接 Telegram（最多约 30 秒）；可点击取消。",api_id,api_hash,proxy)
    def cancel_auth(self):
        if not self.busy or self.auth_phase not in {"connecting","sending_code","verifying"}:return
        self.cancelling=True;self.connection.set("正在取消当前连接/登录操作…");self.auth_message.set(self.connection.get())
        self.buttons["cancel_auth"].configure(state="disabled");self.worker.cancel_auth()
    def read_proxy(self):
        value=system_proxy()
        if value:
            self.proxy.set(value);self.auth_message.set("已读取系统手动代理。请确认代理软件正在运行，再点击连接。")
        else:self.auth_error("未发现启用的系统手动代理。请从代理软件查看本地 HTTP/SOCKS 端口并填写；此按钮不会自动猜测端口。")
    def request_code(self):
        if self.busy:return
        if not self.network_ready:self.auth_error("请先点击连接，并等待显示“已连接 Telegram”");return
        try:phone=validate_phone(self.phone.get())
        except Fault as error:self.auth_error(f"[{error.code}] {error.message}");return
        self.begin_auth(self.worker.send_code,"sending_code","正在请求验证码，最多约 45 秒；不会自动重发。",phone)
    def login(self):
        if self.busy:return
        if not self.network_ready or not self.code_requested:self.auth_error("请先连接成功并发送验证码");return
        code,password=self.code.get(),self.password.get()
        if self.auth_phase=="password_required" and not password:self.auth_error("请在本机填写两步验证密码");return
        if self.auth_phase!="password_required" and not code.strip():self.auth_error("请在本机填写收到的验证码");return
        self.code.set("");self.password.set("")
        self.begin_auth(self.worker.login,"verifying","正在验证登录，最多约 45 秒。",code,password)
    def scan(self):
        try:
            limit=int(self.limit.get())
            if not 0<=limit<=1000000:raise ValueError()
            kinds=tuple(k for k,on in [("video",self.videos.get()),("image",self.photos.get())] if on)
            if not kinds:raise Fault("TYPE","至少选择一种媒体")
            parse_date(self.start_date.get());parse_date(self.end_date.get(),True)
        except (ValueError,Fault) as error:
            self.append(error.message if isinstance(error,Fault) else "扫描上限必须是 0–1000000 的整数");return
        self.worker.submit(self.worker.scan,self.channel.get(),kinds,limit,self.start_date.get(),self.end_date.get(),self.folder.get())
    def start(self):
        try:
            number=int(self.concurrency.get())
            if not 1<=number<=3:raise ValueError()
        except ValueError:self.append("并发数应为 1–3");return
        summary=self.snapshot.get("summary",{})
        remaining=max(0,summary.get("size",0)-summary.get("received",0))
        free=available_space(self.folder.get().strip() or str(self.base))
        if remaining and free>=0 and remaining+DISK_RESERVE>free:
            message=(f"保存目录空间不足：还需约 {size(remaining)}，当前可用 {size(free)}，"
                     f"程序会预留 {size(DISK_RESERVE)} 余量。请清理磁盘或更换保存目录后再开始。")
            self.append(message);messagebox.showwarning("空间不足",message);return
        self.worker.submit(self.worker.start,number)
    def export(self):
        path=filedialog.asksaveasfilename(title="导出本地任务记录",defaultextension=".json",initialfile="telegram-download-records.json")
        if path:self.worker.submit(self.worker.export,path)
    def append(self,text):
        self.log.configure(state="normal");self.log.insert("end",str(text)+"\n");self.log_lines+=1
        if self.log_lines>100:self.log.delete("1.0","2.0");self.log_lines-=1
        self.log.see("end");self.log.configure(state="disabled")
    def render(self,snapshot):
        self.snapshot=snapshot;summary=snapshot.get("summary",{})
        self.stats.set((f"{snapshot['channel']}  ·  " if snapshot.get('channel') else "")+"    ".join(f"{label} {summary.get(key,0)}" for key,label in [("video","视频"),("image","图片"),("queued","等待"),("paused","暂停"),("completed","成功"),("failed","失败")]))
        self.speed.set(f"近期平均速度 {size(snapshot.get('speed',0))}/s    已写入 {size(summary.get('received',0))} / {size(summary.get('size',0))}")
        self.scope.set(snapshot.get("scope",""))
        total=summary.get("size",0);self.progress["value"]=min(100,100*summary.get("received",0)/total) if total else 0
        existing=set(self.tree.get_children())
        for index,task in enumerate(snapshot.get("tasks",[])):
            key=task["key"];percent=f"{100*task['received']/task['size']:.1f}%" if task["size"] else "未知大小"
            values=(Path(task["final_path"]).name if task["final_path"] else f"{task['channel']}_{task['message_id']}",
                    "视频" if task["kind"]=="video" else "图片",STATUS.get(task["status"],task["status"]),
                    percent+" · "+size(task["size"]),f"[{task['error_code']}] {task['error']}" if task["error_code"] else task["error"])
            if key in existing:
                # Only touch changed rows; rewriting 200 rows every tick makes clicks sluggish.
                if self.row_cache.get(key)!=values:self.tree.item(key,values=values);self.row_cache[key]=values
                existing.remove(key)
            else:
                self.tree.insert("","end",iid=key,values=values);self.row_cache[key]=values
            if self.row_index.get(key)!=index:self.tree.move(key,"",index);self.row_index[key]=index
        for key in existing:
            self.tree.delete(key);self.row_cache.pop(key,None);self.row_index.pop(key,None)
    def update_buttons(self):
        running=bool(self.snapshot.get("running"));scanning=bool(self.snapshot.get("scanning"))
        summary=self.snapshot.get("summary",{})
        signature=(self.busy,running,scanning,self.connected,self.network_ready,self.code_requested,self.cancelling,self.auth_phase,
                   summary.get("queued",0),summary.get("paused",0),bool(self.snapshot.get("tasks")),self.demo)
        if signature==self.ui_signature:return
        self.ui_signature=signature
        for key,button in self.buttons.items():
            enable=not self.busy and not running and not scanning
            if key in ("scan","dialogs"):enable=enable and self.connected
            if key=="start":
                summary=self.snapshot.get("summary",{})
                enable=enable and self.connected and bool(summary.get("queued",0)+summary.get("paused",0))
            if key in ("pause","stop"):enable=running or scanning
            if key in ("retry","export"):enable=enable and self.connected and bool(self.snapshot.get("tasks"))
            if key=="send_code":enable=enable and self.network_ready and not self.connected
            if key=="login":enable=enable and self.network_ready and self.code_requested and not self.connected
            if key=="cancel_auth":enable=self.busy and not self.cancelling and self.auth_phase in {"connecting","sending_code","verifying"}
            if key in ("connect","send_code","login","cancel_auth") and self.demo:enable=False
            button.configure(state="normal" if enable else "disabled")
        self.buttons["connect"].configure(text="正在连接…" if self.auth_phase=="connecting" and self.busy else "连接 / 恢复登录")
        for entry in self.inputs:entry.configure(state="disabled" if self.busy or running or scanning else "normal")
        self.channel_box.configure(state="disabled" if self.busy or running or scanning else "normal")
    def poll(self):
        while True:
            try:event,data=self.events.get_nowait()
            except queue.Empty:break
            if event=="snapshot":self.render(data)
            elif event=="log":self.append(data)
            elif event=="error":
                self.append(data)
                if not self.busy:self.connection.set(data)
            elif event=="busy":self.busy=bool(data)
            elif event=="connected":self.connected=bool(data)
            elif event=="auth":self.apply_auth(data)
            elif event=="auth_success":self.code.set("");self.password.set("");self.phone.set("");self.book.select(self.download_page)
            elif event=="password_required":self.append("在连接账号页面输入两步验证密码，再点验证并登录")
            elif event=="dialogs":
                self.dialog_map={}
                for item in data:
                    label=f"{item['name']} ({'@'+item['username'] if item['username'] else item['id']})"
                    self.dialog_map[label]="@"+item["username"] if item["username"] else str(item["id"])
                self.channel_box["values"]=list(self.dialog_map)
        now=time.monotonic()
        if now-self._last_space_check>=5:self._last_space_check=now;self.refresh_space()
        self.update_buttons()
        if not self.closing:self.root.after(150,self.poll)
    def close(self):
        if self.closing:return
        if self.snapshot.get("running") or self.snapshot.get("scanning"):
            if not messagebox.askyesno("保留片段并退出","下载或扫描仍在运行，是否暂停、保留本地片段后退出？"):return
        self.closing=True;self.save_geometry();self.connection.set("正在关闭连接并保存任务记录…")
        self.worker.pause()
        future=asyncio.run_coroutine_threadsafe(self.worker.shutdown(),self.worker.loop)
        def finish():
            if future.done():
                self.worker.loop.call_soon_threadsafe(self.worker.loop.stop);self.worker.thread.join(2);self.root.destroy()
            else:self.root.after(100,finish)
        self.root.after(100,finish)

def acquire_lock(base):
    # A lock prevents two processes from concurrently writing this app's DB/session.
    lock=(base/"instance.lock").open("a+b")
    try:
        if os.name=="nt":
            import msvcrt
            lock.seek(0)
            if lock.read(1)==b"":lock.write(b"0");lock.flush()
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        return lock
    except OSError:
        lock.close();return None

def default_base():
    if getattr(sys,"frozen",False):
        # A packaged .app may be read-only; keep writable data in Application Support.
        if sys.platform=="darwin":return Path.home()/"Library"/"Application Support"/"TelegramBatchDownloader"
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--demo",action="store_true");parser.add_argument("--data-dir")
    args=parser.parse_args()
    base=Path(args.data_dir).resolve() if args.data_dir else default_base()
    base.mkdir(parents=True,exist_ok=True)
    root=tk.Tk()
    lock=acquire_lock(base)
    if lock is None:
        root.withdraw();messagebox.showerror("无法打开数据目录","另一实例可能正在运行，或程序所在目录不可写。请使用可写目录并只打开一个实例。");root.destroy();return
    try:App(root,base,args.demo)
    except Exception as error:
        root.withdraw();messagebox.showerror("无法初始化",fault(error).message);root.destroy();lock.close();return
    root.mainloop();lock.close()
if __name__=="__main__":main()
