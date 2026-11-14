"""Package only explicit public artifacts. Never include account state or downloads."""
import hashlib, json, shutil, zipfile, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
assert ROOT.name=='Telegram-Batch-Downloader-Local'
sys.path.insert(0,str(ROOT))
from tmd import __version__
PORTABLE=ROOT/'dist'/('v'+__version__)/'TelegramBatchDownloader'
exe=PORTABLE/'TelegramBatchDownloader.exe'
if not exe.is_file():raise SystemExit('Build the Windows application first')
report=ROOT/'docs'/'build-verification.json'
if report.is_file() and json.loads(report.read_text('utf8'))['exe_sha256']!=hashlib.sha256(exe.read_bytes()).hexdigest():
    raise SystemExit('Build verification belongs to another executable; regenerate it first')
public_docs=('README.md','CHANGELOG.md','LICENSE','THIRD-PARTY-NOTICES.md')
for name in public_docs:shutil.copy2(ROOT/name,PORTABLE/name)
shutil.copytree(ROOT/'licenses',PORTABLE/'licenses',dirs_exist_ok=True)
(PORTABLE/'docs').mkdir(exist_ok=True)
for name in ('TESTING.md','MOVED-FILES.md','test-results.json','build-verification.json','pipeline-benchmark.json'):
    source=ROOT/'docs'/name
    if source.is_file():shutil.copy2(source,PORTABLE/'docs'/name)

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda:file.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def pack(target,items,prefix):
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for source,relative in sorted(items,key=lambda pair:str(pair[1])):
            pieces={part.lower() for part in relative.parts}
            if pieces & {'state','downloads','qa','.venv','__pycache__','.git'} or relative.name=='instance.lock' or relative.suffix in ('.session','.part'):
                raise RuntimeError('Refusing to publish private/runtime content: '+str(relative))
            archive.write(source,str(Path(prefix)/relative))
    with zipfile.ZipFile(target) as archive:
        bad=archive.testzip()
        if bad:raise RuntimeError('Bad ZIP entry: '+bad)

portable_zip=ROOT.parent/f'Telegram-Batch-Downloader-Local-v{__version__}-Windows-x64.zip'
source_zip=ROOT.parent/f'Telegram-Batch-Downloader-Local-v{__version__}-Source.zip'
pack(portable_zip,[(p,p.relative_to(PORTABLE)) for p in PORTABLE.rglob('*') if p.is_file()],'TelegramBatchDownloader')
source_files=[ROOT/name for name in public_docs+('requirements.txt','requirements-build.txt','requirements-runtime.lock.txt','.gitignore','setup-source.cmd','launch-source.cmd','test.cmd')]
for folder in ('tmd','tests','scripts'):
    source_files.extend(p for p in (ROOT/folder).glob('*') if p.is_file() and p.suffix in ('.py','.ps1','.txt'))
source_files.append(ROOT/'app.py')
source_files.extend(p for p in (ROOT/'licenses').rglob('*') if p.is_file())
source_files.extend(p for p in (ROOT/'docs').glob('*') if p.is_file() and p.name in ('TESTING.md','MOVED-FILES.md','test-results.json','build-verification.json','pipeline-benchmark.json'))
pack(source_zip,[(p,p.relative_to(ROOT)) for p in source_files],ROOT.name)
manifest={'at':datetime.now(timezone.utc).isoformat(),'version':__version__,'files':[{'name':p.name,'bytes':p.stat().st_size,'sha256':sha(p)} for p in (portable_zip,source_zip,exe)]}
target=ROOT.parent/f'Telegram-Batch-Downloader-Local-v{__version__}-checksums.json'
target.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(manifest,ensure_ascii=False,indent=2))
