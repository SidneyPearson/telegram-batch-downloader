from __future__ import annotations
import hashlib, re, unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BLOCK = 512 * 1024
BEIJING = timezone(timedelta(hours=8))
class Fault(Exception):
    def __init__(self, code, message, retryable=False, wait=0):
        super().__init__(message)
        self.code, self.message, self.retryable, self.wait = code, message, retryable, wait
class Paused(Exception): pass

def safe_name(value, limit=65):
    value = unicodedata.normalize("NFKC", str(value or "unnamed"))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', "_", value).strip().rstrip(". ")
    out = ""
    for char in value:
        if len((out + char).encode("utf-16-le")) // 2 > limit: break
        out += char
    out = out.rstrip(". ") or "unnamed"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", out, re.I): out = "_" + out
    return out

def parse_date(text, end=False):
    if not text.strip(): return None
    try: value = datetime.strptime(text.strip(), "%Y-%m-%d").replace(tzinfo=BEIJING)
    except ValueError: raise Fault("DATE", "日期格式应为 YYYY-MM-DD，例如 2026-10-25")
    return value + timedelta(days=1) if end else value

def channel_input(text):
    text = text.strip()
    if not text: raise Fault("CHANNEL", "请填写频道用户名、链接或从已加入频道列表选择")
    if text.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        parsed = urlparse(text)
        if parsed.hostname not in ("t.me", "www.t.me", "telegram.me"): raise Fault("CHANNEL", "仅接受 t.me/telegram.me 频道链接")
        parts = [p for p in parsed.path.split("/") if p]
        if not parts or parts[0].startswith("+") or parts[0] == "joinchat":
            raise Fault("INVITE", "本工具不自动加入频道；请先在 Telegram 正常加入，再从已加入列表选择")
        if parts[0] == "c":
            if len(parts) < 2 or not parts[1].isdigit(): raise Fault("CHANNEL", "私有频道链接格式无效")
            return int("-100" + parts[1])
        text = parts[0]
    if text.lstrip("-").isdigit(): return int(text)
    if not re.fullmatch(r"@?[A-Za-z0-9_]{3,64}", text): raise Fault("CHANNEL", "频道用户名格式无效")
    return text.lstrip("@")

def extension(kind, mime, original=""):
    known = {"video/mp4":".mp4","video/webm":".webm","video/quicktime":".mov","video/x-matroska":".mkv",
             "image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif"}
    if mime in known: return known[mime]
    ext = Path(original or "").suffix.lower()
    return ext if ext in {".mp4",".webm",".mov",".mkv",".jpg",".jpeg",".png",".webp",".gif"} else (".mp4" if kind=="video" else ".jpg")

@dataclass
class Task:
    key: str
    account: str
    peer: int
    channel: str
    message_id: int
    media_id: str
    kind: str
    size: int
    date: str
    mime: str
    original: str = ""
    status: str = "queued"
    received: int = 0
    attempts: int = 0
    output_root: str = ""
    final_path: str = ""
    part_path: str = ""
    sha256: str = ""
    checkpoint_bytes: int = 0
    checkpoint_sha256: str = ""
    completed_at: float = 0
    error_code: str = ""
    error: str = ""
    updated: float = 0
    def dump(self): return asdict(self)
    @classmethod
    def make(cls, account, peer, channel, message_id, media_id, kind, size, date, mime, original="", output_root=""):
        raw = f"{account}|{peer}|{message_id}|{kind}|{media_id}"
        key = hashlib.sha256(raw.encode()).hexdigest()
        return cls(key, account, peer, channel, message_id, str(media_id), kind, int(size), date, mime, original, output_root=output_root)
    def basename(self):
        try: stamp = datetime.fromisoformat(self.date).astimezone(BEIJING).strftime("%Y%m%d")
        except (ValueError, TypeError): stamp = ""
        return f"{safe_name(self.channel)}_{self.message_id}" + (f"_{stamp}" if stamp else "") + extension(self.kind,self.mime,self.original)
