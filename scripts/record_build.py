import argparse, hashlib, json, platform, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tmd import __version__
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
exe=ROOT/'dist'/('v'+__version__)/'TelegramBatchDownloader'/'TelegramBatchDownloader.exe'
code=[ROOT/'app.py',*(ROOT/'tmd').glob('*.py')]
parser=argparse.ArgumentParser(description='Record build hashes and explicitly observed startup evidence')
parser.add_argument('--observed-window-title',default='')
args=parser.parse_args()
tests=json.loads((ROOT/'docs'/'test-results.json').read_text('utf8'))
report={'version':__version__,'at':datetime.now(timezone.utc).isoformat(),'platform':platform.platform(),
        'build_tool':'PyInstaller 6.22.3, onedir, windowed, no UPX','exe_sha256':digest(exe),'exe_bytes':exe.stat().st_size,
        'startup':{'observed_window_title':args.observed_window_title or None,'checked':bool(args.observed_window_title),
                   'data_directory':'isolated empty QA directory','telegram_connection_attempted':False,
                   'method':'Window title supplied after separate Windows inventory/startup inspection' if args.observed_window_title else 'Not checked by this report-generation command'},
        'source_sha256':{str(path.relative_to(ROOT)):digest(path) for path in code},
        'tests':{key:tests[key] for key in ('tests','passed','errors','failures','skipped','at')},
        'live_account_tested':False,'restricted_channel_tested':False,
        'external_screenshot_qa':'not completed: app access approval timed out'}
(ROOT/'docs'/'build-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
print('Build verification recorded; no live account/channel claim.')
