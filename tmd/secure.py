"""Local secret storage: Windows DPAPI on Windows, macOS Keychain on macOS.
No plaintext session, API hash, phone, OTP or 2FA persistence."""
from __future__ import annotations
import base64, ctypes, json, os, secrets, subprocess, sys
from ctypes import wintypes
from pathlib import Path
from .model import Fault

KEYCHAIN_SERVICE = "TelegramBatchDownloader"
KEYCHAIN_PREFIX = "kc-"

class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

def _windows_protect(data: bytes, decrypt=False):
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte))); outgoing = Blob()
    crypt = ctypes.windll.crypt32; kernel = ctypes.windll.kernel32
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    ok = crypt.CryptUnprotectData(ctypes.byref(incoming),None,None,None,None,1,ctypes.byref(outgoing)) if decrypt else crypt.CryptProtectData(ctypes.byref(incoming),"TMD local",None,None,None,1,ctypes.byref(outgoing))
    if not ok: raise Fault("DPAPI","本机加密保存或解密失败；请在当前 Windows 账户重新登录")
    try: return ctypes.string_at(outgoing.pbData,outgoing.cbData)
    finally:
        kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
        kernel.LocalFree(ctypes.cast(outgoing.pbData,ctypes.c_void_p))

def _keychain(arguments):
    try: return subprocess.run(["/usr/bin/security", *arguments], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise Fault("KEYCHAIN","无法访问 macOS 钥匙串；请检查系统钥匙串是否可用后重试") from None

def _mac_protect(data: bytes, decrypt=False):
    # Secrets live in the login Keychain; profile.json only keeps the random item name.
    if not decrypt:
        token = KEYCHAIN_PREFIX + secrets.token_hex(24)
        encoded = base64.b64encode(data).decode()
        result = _keychain(["add-generic-password","-U","-s",KEYCHAIN_SERVICE,"-a",token,"-w",encoded])
        if result.returncode: raise Fault("KEYCHAIN","无法写入 macOS 钥匙串；请在系统提示中允许本程序保存登录信息")
        return token.encode()
    try: token = bytes(data).decode("utf8")
    except UnicodeDecodeError: raise Fault("KEYCHAIN","本机保存的登录信息格式无效；请在连接账号页重新登录") from None
    if not token.startswith(KEYCHAIN_PREFIX): raise Fault("KEYCHAIN","本机保存的登录信息不属于 macOS 钥匙串；请在连接账号页重新登录")
    result = _keychain(["find-generic-password","-s",KEYCHAIN_SERVICE,"-a",token,"-w"])
    if result.returncode: raise Fault("KEYCHAIN","钥匙串中没有保存的登录信息（可能已被删除）；请在连接账号页重新登录")
    try: return base64.b64decode(result.stdout.strip(), validate=True)
    except ValueError: raise Fault("KEYCHAIN","钥匙串中的登录信息无法解码；请在连接账号页重新登录") from None

def protect(data: bytes, decrypt=False):
    if os.name == "nt": return _windows_protect(data, decrypt)
    if sys.platform == "darwin": return _mac_protect(data, decrypt)
    raise Fault("PLATFORM","当前系统不支持本机的加密保存；仅支持 Windows 与 macOS")

class Profile:
    def __init__(self, directory):
        self.directory=Path(directory); self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/"profile.json"
        try: self.data=json.loads(self.path.read_text("utf8")) if self.path.exists() else {}
        except (OSError,ValueError): raise Fault("PROFILE","配置文件损坏；请保留备份后使用新的程序数据目录")
        self.data.setdefault("salt",secrets.token_hex(32))
    def secret(self,key):
        value=self.data.get(key,"")
        return protect(base64.b64decode(value),True).decode("utf8") if value else ""
    def save(self,**values):
        self.data.update(values)
        temp=self.path.with_suffix(".tmp")
        with temp.open("w",encoding="utf8") as file:
            json.dump(self.data,file,ensure_ascii=False,indent=2);file.flush();os.fsync(file.fileno())
        os.replace(temp,self.path)
    def save_login(self,api_id,api_hash,session):
        # Only encrypted secrets and a salted account fingerprint are persisted.
        self.save(api_id=api_id,api_hash_cipher=base64.b64encode(protect(api_hash.encode())).decode(),
                  session_cipher=base64.b64encode(protect(session.encode())).decode())
    def fingerprint(self,user_id):
        import hmac,hashlib
        return hmac.new(bytes.fromhex(self.data["salt"]),str(user_id).encode(),hashlib.sha256).hexdigest()[:24]

def _windows_proxy():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            if not winreg.QueryValueEx(key,"ProxyEnable")[0]: return ""
            server=winreg.QueryValueEx(key,"ProxyServer")[0]
        if "=" in server:
            options=dict(item.split("=",1) for item in server.split(";") if "=" in item)
            value=options.get("socks") or options.get("http") or options.get("https")
            scheme="socks5" if options.get("socks") else "http"
            return f"{scheme}://{value}" if value and "@" not in value else ""
        return ("http://"+server) if server and "@" not in server else ""
    except OSError: return ""

def _mac_proxy():
    # scutil only reports manually configured proxies; TUN/PAC setups stay undetected.
    try: result = subprocess.run(["/usr/bin/scutil","--proxy"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError): return ""
    values={}
    for line in result.stdout.splitlines():
        key,separator,value=line.partition(":")
        if separator: values[key.strip()]=value.strip()
    if values.get("SOCKSEnable")=="1" and values.get("SOCKSProxy") and values.get("SOCKSPort"):
        return f"socks5://{values['SOCKSProxy']}:{values['SOCKSPort']}"
    for prefix in ("HTTP","HTTPS"):
        if values.get(f"{prefix}Enable")=="1" and values.get(f"{prefix}Proxy") and values.get(f"{prefix}Port"):
            return f"http://{values[f'{prefix}Proxy']}:{values[f'{prefix}Port']}"
    return ""

def system_proxy():
    if os.name == "nt": return _windows_proxy()
    if sys.platform == "darwin": return _mac_proxy()
    return ""

def parse_proxy(text):
    if not text.strip(): return None
    from urllib.parse import urlparse
    parsed=urlparse(text.strip() if "://" in text else "http://"+text.strip())
    if parsed.scheme not in ("http","socks5","socks4") or not parsed.hostname:
        raise Fault("PROXY","代理格式为 http://127.0.0.1:端口 或 socks5://127.0.0.1:端口")
    if parsed.username or parsed.password: raise Fault("PROXY","此版本仅支持无需用户名/密码的代理")
    try: port=parsed.port
    except ValueError: raise Fault("PROXY","代理端口无效")
    if not port or not 1<=port<=65535: raise Fault("PROXY","请填写有效代理端口")
    import socks
    return ({"http":socks.HTTP,"socks4":socks.SOCKS4,"socks5":socks.SOCKS5}[parsed.scheme],parsed.hostname,port,True)
