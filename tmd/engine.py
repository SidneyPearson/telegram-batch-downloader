from __future__ import annotations
import asyncio, hashlib, os, time, threading
from collections import deque
from pathlib import Path
from .model import BLOCK, Fault, Paused, safe_name
from .transport import metadata, fault
from . import __version__

def sample_transfer(info,received,now):
    samples=info.setdefault("samples",deque(maxlen=128))
    bucket=info.get("sample_bucket",samples[-1][0] if samples else now)
    if len(samples)>1 and now-bucket<.2:samples[-1]=(now,received)
    else:samples.append((now,received));info["sample_bucket"]=now

def transfer_rate(info,now,window=15):
    samples=info.get("samples")
    if not samples or len(samples)<2:return 0
    cutoff=now-window
    while len(samples)>1 and samples[1][0]<=cutoff:samples.popleft()
    if len(samples)<2 or now-samples[-1][0]>=window:return 0
    start,received=samples[0]
    if start<cutoff:
        after,amount=samples[1]
        received+=(amount-received)*(cutoff-start)/(after-start);start=cutoff
    duration=now-start
    return max(0,(samples[-1][1]-received)/duration) if duration>0 else 0

def checked_path(base,path):
    base=Path(base).resolve();path=Path(path).resolve()
    if not path.is_relative_to(base):raise Fault("PATH","保存路径超出用户选择目录")
    return path

def publish(part,desired):
    # Windows rename refuses an existing destination: no silent overwrite.
    for index in range(10000):
        candidate=desired if not index else desired.with_name(f"{desired.stem} ({index}){desired.suffix}")
        try:
            if os.name=="nt":os.rename(part,candidate)
            else:os.link(part,candidate);os.unlink(part)
            return candidate
        except FileExistsError:continue
    raise Fault("COLLISION","同名文件过多，请选择新的保存目录")

class Engine:
    def __init__(self,storage,transport=None,notify=lambda *_:None):
        self.store=storage;self.transport=transport;self.notify=notify
        self.account=getattr(transport,"account","");self.peer=None;self.channel=""
        self.running=False;self.scanning=False;self.active={};self.stop=threading.Event()
        self.scan_count=0;self.scan_scope="尚未扫描";self.cooldown=0;self._last_emit=0
        self.idle=asyncio.Event();self.idle.set();self.network_halt=False;self.scan_job=None
    def emit(self,force=False):
        now=time.monotonic()
        if not force and now-self._last_emit<.2:return
        self._last_emit=now
        summary=self.store.summary(self.account,self.peer) if self.account else {}
        tasks=[t.dump() for t in self.store.rows(self.account,self.peer,limit=200)] if self.account else []
        speed=sum(transfer_rate(v,now) for v in self.active.values())
        self.notify("snapshot",{"summary":summary,"tasks":tasks,"running":self.running,"scanning":self.scanning,
            "speed":speed,"scope":self.scan_scope,"channel":self.channel,"cooldown":self.cooldown})
    def log(self,text):self.notify("log",text)
    def check(self):
        if self.stop.is_set():raise Paused()
    def request_pause(self):
        self.stop.set()
        for job in tuple(self.active):job.cancel()
        if self.scan_job:self.scan_job.cancel()
    async def wait(self,seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            self.check();await asyncio.sleep(min(.15,max(.001,end-time.monotonic())))
    async def gate(self):
        while self.cooldown>time.time():
            self.check();self.emit();await self.wait(min(.5,self.cooldown-time.time()))
    async def scan(self,channel,kinds,limit,start,end,output_root):
        if self.running or self.scanning:raise Fault("BUSY","请先暂停当前任务")
        if not self.transport or not self.account:raise Fault("AUTH_REQUIRED","请先连接并登录")
        if not kinds:raise Fault("TYPE","至少选择图片或视频")
        if start and end and start>=end:raise Fault("DATE","开始日期不能晚于结束日期")
        self.scanning=True;self.stop.clear();self.scan_count=0;added=0;existing=0
        self.scan_job=asyncio.current_task();self.idle.clear()
        try:
            self.cooldown=self.store.setting("cooldown:"+self.account,0)
            self.scan_scope="正在等待服务器限速结束" if self.cooldown>time.time() else "正在读取频道"
            self.emit(True);await self.gate()
            self.peer,self.channel=await self.transport.resolve(channel)
            root=Path(output_root).expanduser().resolve();root.mkdir(parents=True,exist_ok=True)
            self.scan_scope="正在检索可访问的媒体消息…";self.emit(True)
            async for message in self.transport.history(self.peer,kinds,limit,end):
                self.check();self.scan_count+=1
                date=getattr(message,"date",None)
                if date and start and date<start:break
                if date and end and date>=end:continue
                task=metadata(message,self.account,self.peer,self.channel,str(root))
                if not task or task.kind not in kinds:continue
                old=self.store.get(task.key)
                if old:
                    existing+=1
                    missing=old.status=="completed" and (not old.final_path or not Path(old.final_path).is_file() or Path(old.final_path).stat().st_size!=old.size)
                    moved=old.status!="completed" and str(root)!=old.output_root
                    if missing or moved:
                        old.status="queued";old.received=0;old.checkpoint_bytes=0;old.checkpoint_sha256=""
                        old.final_path="";old.part_path="";old.output_root=str(root)
                        old.error_code="LOCAL_MISSING" if missing else "NEW_FOLDER"
                        old.error="已完成文件丢失或大小变化，已重新排队" if missing else "保存目录已改变，将在新目录重新下载；旧片段保留"
                        self.store.put(old);added+=1
                else:self.store.put(task);added+=1
                if self.scan_count%20==0:self.emit(True);await asyncio.sleep(0)
            self.scan_scope=f"检索 {self.scan_count} 条媒体消息；新入队 {added}，已记录 {existing}。上限 {'全部可访问' if not limit else limit}；不是频道媒体总数。"
            self.log(self.scan_scope)
        except (Paused,asyncio.CancelledError):self.scan_scope=f"扫描已停止；已检索 {self.scan_count} 条消息，发现的任务已保留"
        except Exception as error:
            issue=fault(error)
            if issue.wait:
                self.cooldown=max(self.cooldown,time.time()+issue.wait)
                self.store.set_setting("cooldown:"+self.account,self.cooldown)
            self.scan_scope=f"扫描未完成；已检索 {self.scan_count} 条消息，发现的任务已保留。[{issue.code}] {issue.message}"
            raise issue from None
        finally:self.scanning=False;self.scan_job=None;self.idle.set();self.emit(True)
    async def run(self,concurrency=2):
        if self.running or self.scanning:raise Fault("BUSY","任务正在运行")
        if not self.transport or not self.account or self.peer is None:raise Fault("CHANNEL","请先扫描需要下载的频道")
        self.stop.clear();self.running=True;self.network_halt=False;self.idle.clear()
        self.cooldown=self.store.setting("cooldown:"+self.account,0)
        for task in self.store.rows(self.account,self.peer,{"paused"}):
            task.status="queued";task.error_code=""
            task.error="等待校验已有片段后续传" if task.checkpoint_bytes else "等待下载"
            self.store.put(task)
        concurrency=max(1,min(3,int(concurrency)));self.emit(True)
        try:
            while not self.stop.is_set() or self.active:
                if not self.stop.is_set() and time.time()>=self.cooldown:
                    while len(self.active)<concurrency:
                        video_active=any(info["task"].kind=="video" for info in self.active.values())
                        task=self.store.next_task(self.account,self.peer,not video_active)
                        if not task:break
                        task.status="downloading";self.store.put(task)
                        info={"task":task,"speed":0,"time":time.monotonic()}
                        job=asyncio.create_task(self.execute(task,info));self.active[job]=info
                if not self.active:
                    if self.stop.is_set() or not self.store.next_task(self.account,self.peer):break
                    await self.gate();continue
                done,_=await asyncio.wait(tuple(self.active),timeout=.15,return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    self.active.pop(job,None)
                    try:job.result()
                    except asyncio.CancelledError:pass
                    except BaseException:
                        self.stop.set();self.log("任务记录或磁盘操作异常，已停止派发；请检查可用空间")
                self.emit()
        finally:
            if self.active:
                for job in self.active:job.cancel()
                await asyncio.gather(*self.active,return_exceptions=True);self.active.clear()
            for task in self.store.rows(self.account,self.peer,{"queued","downloading"}):
                if self.stop.is_set():task.status="paused";self.store.put(task)
            self.running=False;self.idle.set();self.emit(True)
    async def execute(self,task,info):
        for attempt in range(3):
            try:
                self.check();await self.gate()
                task.attempts+=1;task.status="downloading";task.error="正在读取附件元数据";task.error_code=""
                self.store.put(task);self.emit(True)
                try:message=await asyncio.wait_for(self.transport.current(task),60)
                except Exception as error:raise fault(error) from None
                root=Path(task.output_root).resolve()
                folder=checked_path(root,root/safe_name(task.channel)/("videos" if task.kind=="video" else "photos"))
                folder.mkdir(parents=True,exist_ok=True)
                part=folder/("."+task.basename()+"."+task.key[:12]+".part")
                if part.is_symlink() or (part.exists() and part.stat().st_nlink>1):
                    raise Fault("PATH","临时文件不能是符号链接或多重硬链接")
                part=checked_path(root,part)
                task.part_path=str(part)
                actual=part.stat().st_size if part.exists() else 0
                if actual>task.size:raise Fault("PART_TOO_LARGE","片段长度超过目标文件，保留片段并停止；请选择新目录重新扫描")
                offset=task.checkpoint_bytes
                if offset<0 or offset>actual or offset>task.size or (offset% BLOCK and offset!=task.size) or not task.checkpoint_sha256:
                    offset=0
                digest=hashlib.sha256()
                with part.open("r+b" if part.exists() else "xb",buffering=0) as file:
                    file.seek(0)
                    task.error="校验已下载片段"
                    remaining=offset
                    while remaining:
                        self.check();chunk=file.read(min(BLOCK,remaining))
                        if not chunk:raise Fault("PART_READ","无法读取已有片段")
                        digest.update(chunk);remaining-=len(chunk);self.emit();await asyncio.sleep(0)
                    if offset and digest.hexdigest()!=task.checkpoint_sha256:
                        self.store.error(task,"CHECKPOINT_MISMATCH","本地片段校验失败，将重新下载；不会把损坏片段拼入结果")
                        offset=0;digest=hashlib.sha256()
                    file.truncate(offset);file.seek(offset);task.received=offset
                    task.checkpoint_bytes=offset;task.checkpoint_sha256=digest.hexdigest() if offset else ""
                    task.error="下载中";self.store.put(task)
                    info["samples"]=deque([(time.monotonic(),offset)],maxlen=128)
                    info["sample_bucket"]=info["samples"][0][0]
                    last_bytes=offset;last_time=time.monotonic();last_store=last_time
                    if offset<task.size:
                        stream=self.transport.chunks(message,offset,task.size)
                        try:
                            while True:
                                self.check();await self.gate()
                                try:chunk=await asyncio.wait_for(anext(stream),60)
                                except StopAsyncIteration:break
                                except Exception as error:raise fault(error) from None
                                self.check()
                                if not chunk:continue
                                if task.received+len(chunk)>task.size:raise Fault("LENGTH","实际文件超过元数据声明长度，已拒绝保存")
                                view=memoryview(chunk)
                                while view:
                                    self.check();written=file.write(view)
                                    if not written:raise Fault("WRITE","文件写入未完成")
                                    view=view[written:]
                                digest.update(chunk);task.received+=len(chunk)
                                now=time.monotonic()
                                sample_transfer(info,task.received,now)
                                if now-last_time>=.2:
                                    info["speed"]=(task.received-last_bytes)/(now-last_time);info["time"]=now
                                    last_time=now;last_bytes=task.received
                                if now-last_store>=1 and (task.received%BLOCK==0 or task.received==task.size):
                                    self.checkpoint(file,task,digest);last_store=now
                                self.emit()
                        finally:
                            try:await stream.aclose()
                            except Exception as error:raise fault(error) from None
                    self.check()
                    if task.received!=task.size:raise Fault("TRUNCATED","下载体长度不足，将校验片段后有限重试",True)
                    self.checkpoint(file,task,digest)
                self.check()
                target=checked_path(root,folder/task.basename())
                final=publish(part,target)
                if final.stat().st_size!=task.size:raise Fault("DISK_LENGTH","文件落盘后长度校验失败")
                task.final_path=str(final);task.sha256=digest.hexdigest();task.status="completed";task.completed_at=time.time()
                task.received=task.size;task.error="";task.error_code="";self.store.put(task);self.emit(True);return
            except (Paused,asyncio.CancelledError):
                task.status="paused";task.error="已暂停；下次校验本地片段后继续";task.error_code=""
                task.received=task.checkpoint_bytes
                self.store.put(task);return
            except OSError as error:
                code="DISK_FULL" if getattr(error,"errno",None) in (28,112) else "DISK_IO"
                task.status="failed";task.error_code=code;task.error="磁盘写入或文件操作失败；已有片段保留"
                self.store.error(task,code,task.error);self.store.put(task)
                # A disk failure usually affects every pending file. Pause the queue.
                self.request_pause();return
            except Exception as error:
                problem=fault(error);task.error_code=problem.code;task.error=problem.message
                self.store.error(task,problem.code,problem.message)
                if problem.wait:
                    self.cooldown=max(self.cooldown,time.time()+problem.wait)
                    self.store.set_setting("cooldown:"+self.account,self.cooldown)
                    task.error=f"服务器要求等待 {problem.wait} 秒；可暂停，恢复后仍遵守等待时间"
                if problem.code=="AUTH_REQUIRED":
                    self.network_halt=True;task.status="paused";task.received=task.checkpoint_bytes
                    self.store.put(task);self.request_pause();return
                if problem.retryable and attempt<2 and not self.stop.is_set():
                    self.store.put(task);self.emit(True)
                    try:await self.wait(max(1,2**attempt));await self.gate()
                    except (Paused,asyncio.CancelledError):
                        task.status="paused";self.store.put(task);return
                    continue
                task.status="failed";self.store.put(task);self.emit(True);return
            finally:info["speed"]=0
    def checkpoint(self,file,task,digest):
        # Commit to SQLite only after the exact prefix has reached the OS disk flush.
        file.flush();os.fsync(file.fileno())
        task.checkpoint_bytes=task.received;task.checkpoint_sha256=digest.hexdigest()
        self.store.put(task)
    def retry_failed(self):
        for task in self.store.rows(self.account,self.peer,{"failed"}):
            if task.error_code=="ACCESS_DENIED":continue
            task.status="queued";task.error="等待重新核验附件并继续";self.store.put(task)
        self.emit(True)
    def export(self,path):
        import json
        rows=[]
        for task in self.store.rows(self.account,self.peer):
            value=task.dump();value.pop("account",None);value["errors"]=self.store.errors(task.key);rows.append(value)
        Path(path).write_text(json.dumps({"version":__version__,"scope":self.scan_scope,"tasks":rows},ensure_ascii=False,indent=2),encoding="utf8")
