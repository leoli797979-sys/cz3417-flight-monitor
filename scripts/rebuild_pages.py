"""重新生成三个页面并打印各自的班次数/平台状态（避免用 python -c 踩引号坑）。

用法: python scripts/rebuild_pages.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = [("config.yaml", "deploy"), ("config.w5.yaml", "deploy-w5"),
         ("config.ctucan.yaml", "deploy-ctucan")]

for cfg, out in PAGES:
    r = subprocess.run([sys.executable, "report.py", "-c", cfg, "--deploy-dir", out],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    m = json.loads((ROOT / out / "meta.json").read_text(encoding="utf-8"))
    ps = ", ".join(f"{k}={v}" for k, v in (m.get("platform_status") or {}).items())
    print(f"  {cfg:<20} 班次={m.get('watch_flights')}  当前=¥{m.get('current_price')}  "
          f"当日航班数={m.get('flight_count')}  平台[{ps}]")
    if r.returncode != 0:
        print("    !! 生成失败:", (r.stderr or r.stdout)[-300:])
