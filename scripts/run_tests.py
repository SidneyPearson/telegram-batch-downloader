"""Offline tests: no real Telegram account, no network, no real credentials."""
import json, platform, sys, time, unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tmd import __version__
if __name__=='__main__':
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),top_level_dir=str(ROOT))
    start=time.monotonic();result=unittest.TextTestRunner(verbosity=2).run(suite)
    report={'version':__version__,'at':datetime.now(timezone.utc).isoformat(),'python':platform.python_version(),
            'platform':platform.platform(),'tests':result.testsRun,'passed':result.testsRun-len(result.errors)-len(result.failures)-len(result.skipped),
            'errors':len(result.errors),'failures':len(result.failures),'skipped':len(result.skipped),
            'seconds':round(time.monotonic()-start,3),'scope':'Offline simulated Telegram transport; no live login/channel verification',
            'details':[{'test':str(test),'traceback':detail} for test,detail in result.errors+result.failures]}
    target=ROOT/'docs'/'test-results.json';target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    sys.exit(0 if result.wasSuccessful() else 1)
