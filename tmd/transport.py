from __future__ import annotations
import asyncio, inspect
from datetime import datetime, timezone
from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession
from telethon.tl.types import InputMessagesFilterVideo, InputMessagesFilterPhotos, InputMessagesFilterPhotoVideo
from .model import BLOCK, Fault, Task, channel_input
from .secure import parse_proxy
from . import __version__
from .pipeline import ordered_chunks

CONNECT_TIMEOUT=30
AUTH_TIMEOUT=45

def validate_api(api_id,api_hash):
    import re
    api_id=str(api_id).strip();api_hash=api_hash.strip()
    if not api_id.isdigit() or int(api_id)<=0:raise Fault("API_ID","请填写有效 API ID")
    if not re.fullmatch("[a-fA-F0-9]{32}",api_hash):raise Fault("API_HASH","API Hash 应为 32 位十六进制字符")
    return int(api_id),api_hash

def validate_phone(phone):
    import re
    phone=re.sub(r"[\s()-]","",phone)
    if not re.fullmatch(r"\+\d{6,16}",phone):raise Fault("PHONE","手机号必须以 +国家区号 开头；中国大陆号码请填 +86 后接自己的手机号")
    return phone

def fault(error):
    if isinstance(error,Fault): return error
    name=type(error).__name__
    if name=="FloodWaitError":
        seconds=int(getattr(error,"seconds",60));return Fault("FLOOD_WAIT",f"Telegram 要求等待 {seconds} 秒后再试",True,seconds)
    if name in {"FileReferenceExpiredError","FilerefUpgradeNeededError"}: return Fault("FILE_REFERENCE","文件引用过期，将重新获取当前消息",True)
    if name in {"AuthKeyUnregisteredError","SessionRevokedError","AuthKeyDuplicatedError","UserDeactivatedError","UserDeactivatedBanError"}:
        return Fault("AUTH_REQUIRED","登录已失效，请重新连接并登录")
    if isinstance(error,errors.ForbiddenError) or name in {"ChannelPrivateError","ChatAdminRequiredError","ChatForwardsRestrictedError"}:
        return Fault("ACCESS_DENIED","Telegram 拒绝访问或下载此内容；该任务不会反复尝试")
    if name=="SessionPasswordNeededError":return Fault("PASSWORD_REQUIRED","此账号需要两步验证密码")
    if name in {"PhoneCodeInvalidError","PhoneCodeExpiredError"}:return Fault("LOGIN_CODE","验证码无效或已过期，请重新输入或重新发送")
    if name=="PhoneNumberInvalidError":return Fault("PHONE","手机号无效，请包含国家区号")
    if name in {"ApiIdInvalidError","ApiIdPublishedFloodError"}:return Fault("API_CREDENTIALS","API ID/API Hash 无效或不可用，请核对自己的开发者配置")
    if name=="PasswordHashInvalidError":return Fault("PASSWORD","两步验证密码不正确")
    if isinstance(error,(asyncio.TimeoutError,TimeoutError,ConnectionError,OSError)):
        return Fault("NETWORK","连接中断或超时，请检查网络与代理",True)
    if isinstance(error,errors.ServerError):return Fault("SERVER","Telegram 暂时无法处理请求",True)
    return Fault(name,"操作未完成（"+name+"）；可查看任务错误码，不会记录登录秘密")

def metadata(message,account,peer,channel,output_root=""):
    media=getattr(message,"media",None)
    if not media or getattr(media,"ttl_seconds",None) or getattr(message,"video_note",None):return None
    photo=getattr(message,"photo",None);doc=getattr(message,"document",None);file=getattr(message,"file",None)
    if not file:return None
    mime=getattr(file,"mime_type",None) or ("image/jpeg" if photo else "")
    if photo:kind="image";media_id=str(photo.id)
    elif doc and (getattr(message,"video",None) or mime.startswith("video/")):kind="video";media_id=str(doc.id)
    else:return None
    size=getattr(file,"size",None)
    if not isinstance(size,int) or size<=0:return None
    date=getattr(message,"date",None)
    if not date or not getattr(message,"id",0):return None
    if date.tzinfo is None:date=date.replace(tzinfo=timezone.utc)
    return Task.make(account,peer,channel,int(message.id),media_id,kind,size,date.isoformat(),mime,getattr(file,"name",None) or "",output_root)

class TelegramTransport:
    def __init__(self,profile,api_id,api_hash,proxy=""):
        self.profile=profile;self.api_id,self.api_hash=validate_api(api_id,api_hash)
        self.client=TelegramClient(StringSession(profile.secret("session_cipher")),self.api_id,self.api_hash,
            proxy=parse_proxy(proxy),request_retries=1,connection_retries=2,retry_delay=1,
            timeout=15,flood_sleep_threshold=0,receive_updates=False,device_model="Windows local downloader",app_version=__version__)
        self.account="";self.phone=None;self.code_hash=None;self.needs_password=False;self.peers={}
        self.download_slots=asyncio.Semaphore(2)
    async def connect(self):
        # One deadline covers transport, authorization check, and saved-session restore.
        async with asyncio.timeout(CONNECT_TIMEOUT):
            await self.client.connect()
            if await self.client.is_user_authorized():await self.finish_login();return True
            return False
    async def send_code(self,phone):
        phone=validate_phone(phone)
        async with asyncio.timeout(AUTH_TIMEOUT):
            sent=await self.client.send_code_request(phone);self.phone=phone
            self.code_hash=getattr(sent,"phone_code_hash",None)
            if await self.client.is_user_authorized():await self.finish_login();return True
            return False
    async def sign_in(self,code,password=""):
        async with asyncio.timeout(AUTH_TIMEOUT):await self._sign_in(code,password)
    async def _sign_in(self,code,password=""):
        try:
            if self.needs_password:
                if not password:raise Fault("PASSWORD_REQUIRED","请在本机输入两步验证密码")
                await self.client.sign_in(password=password)
            else:
                if not self.phone or not self.code_hash:raise Fault("LOGIN_CODE","请先发送验证码")
                if not code.strip():raise Fault("LOGIN_CODE","请在本机输入验证码")
                await self.client.sign_in(phone=self.phone,code=code.strip(),phone_code_hash=self.code_hash)
        except errors.SessionPasswordNeededError:
            self.needs_password=True;raise Fault("PASSWORD_REQUIRED","请输入账号的两步验证密码")
        await self.finish_login()
    async def finish_login(self):
        me=await self.client.get_me()
        self.account=self.profile.fingerprint(me.id)
        self.profile.save_login(self.api_id,self.api_hash,self.client.session.save())
        self.phone=None;self.code_hash=None;self.needs_password=False
    async def dialogs(self):
        values=[]
        async for dialog in self.client.iter_dialogs(limit=500):
            if not (dialog.is_channel or dialog.is_group):continue
            peer=utils.get_peer_id(dialog.entity);self.peers[peer]=dialog.entity
            values.append({"id":peer,"name":dialog.name or str(peer),"username":getattr(dialog.entity,"username",None) or ""})
        return values
    async def resolve(self,text):
        value=channel_input(text)
        if isinstance(value,int) and value not in self.peers:
            await self.dialogs()
        entity=self.peers.get(value) if isinstance(value,int) else None
        if entity is None:
            try:entity=await self.client.get_entity(value)
            except ValueError:raise Fault("CHANNEL","未找到此频道；请核对用户名，私有频道请先正常加入并从列表选择") from None
        # Ordinary channels/groups only. Never auto-join, unlock or resolve secret chats.
        if not (getattr(entity,"broadcast",False) or getattr(entity,"megagroup",False) or type(entity).__name__=="Chat"):
            raise Fault("CHANNEL","请选择频道或群组，此版本不扫描私聊")
        peer=utils.get_peer_id(entity);self.peers[peer]=entity
        name=getattr(entity,"username",None) or getattr(entity,"title",None) or str(peer)
        return peer,name
    async def history(self,peer,kinds,limit,end=None):
        filt=InputMessagesFilterPhotoVideo() if len(kinds)==2 else InputMessagesFilterVideo() if "video" in kinds else InputMessagesFilterPhotos()
        async for message in self.client.iter_messages(self.peers[peer],limit=limit or None,filter=filt,offset_date=end,wait_time=1):
            yield message
    async def current(self,task):
        entity=self.peers.get(task.peer)
        if entity is None:await self.resolve(str(task.peer));entity=self.peers[task.peer]
        message=await self.client.get_messages(entity,ids=task.message_id)
        if not message:raise Fault("MESSAGE_GONE","消息已删除或当前账号无法访问")
        meta=metadata(message,task.account,task.peer,task.channel,task.output_root)
        if not meta or meta.media_id!=task.media_id or meta.size!=task.size or meta.kind!=task.kind:
            raise Fault("SOURCE_CHANGED","消息媒体已改变；保留旧片段，请重新扫描发现新的媒体记录")
        return message
    async def chunks(self,message,offset,size):
        if getattr(message,"video",None) and size-offset>2*BLOCK:
            stream=ordered_chunks(offset,size,lambda position:self.read_part(message,position,size),2)
            try:
                async for chunk in stream:yield chunk
            finally:await stream.aclose()
            return
        iterator=self.client.iter_download(message,offset=offset,request_size=BLOCK,chunk_size=BLOCK,file_size=size)
        try:
            while True:
                try:
                    async with self.download_slots:chunk=await anext(iterator)
                except StopAsyncIteration:break
                yield bytes(chunk)
        finally:await self.close_iterator(iterator)
    async def read_part(self,message,offset,size):
        async with self.download_slots:
            iterator=self.client.iter_download(message,offset=offset,limit=1,request_size=BLOCK,chunk_size=BLOCK,file_size=size)
            try:
                try:chunk=bytes(await anext(iterator))
                except StopAsyncIteration:chunk=b""
                expected=min(BLOCK,size-offset)
                if len(chunk)!=expected:raise Fault("TRUNCATED" if len(chunk)<expected else "LENGTH","附件分段长度异常，停止本轮并保留检查点",len(chunk)<expected)
                return chunk
            finally:await self.close_iterator(iterator)
    async def close_iterator(self,iterator):
        close=getattr(iterator,"close",None)
        if close:
            try:
                result=close()
                if inspect.isawaitable(result):await result
            except AttributeError as error:
                # Telethon 1.45 may be cancelled during _init, before it creates _sender.
                if "_sender" not in str(error) or hasattr(iterator,"_sender"):raise
    async def disconnect(self):await self.client.disconnect()
