"""Copy installed third-party license texts without private runtime data."""
import importlib.metadata as md, json, shutil, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PACKAGES=('Telethon','PySocks','cryptg','pyaes','rsa','pyasn1','PyInstaller','pyinstaller-hooks-contrib','altgraph','packaging','pefile','pywin32-ctypes')
records=[]
for package in PACKAGES:
    dist=md.distribution(package);target=ROOT/'licenses'/package;target.mkdir(parents=True,exist_ok=True)
    copied=[]
    for file in dist.files or []:
        if any(token in file.name.lower() for token in ('license','copying','notice')) and '.dist-info' in str(file):
            source=Path(dist.locate_file(file))
            if source.is_file():shutil.copy2(source,target/source.name);copied.append(source.name)
    records.append({'package':package,'version':dist.version,'license':dist.metadata.get('License-Expression') or dist.metadata.get('License') or '', 'files':copied})
base=Path(sys.base_prefix)
for name,source in [('Python',base/'LICENSE.txt'),('Python',base/'LICENSE'),('Tcl',base/'tcl'/'tcl8.6'/'license.terms'),('Tk',base/'tcl'/'tk8.6'/'license.terms')]:
    if source.is_file():
        target=ROOT/'licenses'/name;target.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target/source.name)
(ROOT/'licenses'/'manifest.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(records,ensure_ascii=False,indent=2))
