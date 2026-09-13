"""CI 里打印抓取摘要，并写一份 Markdown 到 GitHub Actions 的 Job Summary。

用法（在 GitHub Actions 里）：
    python scripts/ci_summary.py
    python scripts/ci_summary.py --deploy-dir deploy --summary-out "$GITHUB_STEP_SUMMARY"
"""
import argparse
import json
import os
from pathlib import Path


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! 读取失败 {path}: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy-dir", default="deploy")
    ap.add_argument("--summary-out", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    args = ap.parse_args()

    d = Path(args.deploy_dir)
    meta = load_json(d / "meta.json") or {}
    latest = load_json(d / "latest.json") or {}
    target = latest.get("target") or None
    cheapest = latest.get("cheapest") or None

    print("=== 抓取摘要 ===")
    for k in ("flight", "date", "current_price", "min_price", "max_price",
              "sample_count", "flight_count", "last_fetch"):
        print(f"  {k}: {meta.get(k)}")
    if target:
        print(f"  目标航班: {target.get('flight_no')} {target.get('depart_time')}→"
              f"{target.get('arrive_time')} ¥{target.get('price')} "
              f"{target.get('dep_airport')}→{target.get('arr_airport')}")
    else:
        print("  目标航班: 未命中（本轮不告警）")
    if cheapest:
        print(f"  当日最低: {cheapest.get('flight_no') or '(全航线)'} ¥{cheapest.get('price')}")

    lines = [
        "### CZ3417 机票监控（云端抓取）",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| 目标航班 | {meta.get('flight') or '—'} |",
        f"| 日期 | {meta.get('date') or '—'} |",
        f"| 当前最低价 | ¥{meta.get('current_price')} |",
        f"| 历史区间 | ¥{meta.get('min_price')} – ¥{meta.get('max_price')} |",
        f"| 均价 | ¥{meta.get('avg_price')} |",
        f"| 样本数 | {meta.get('sample_count')} |",
        f"| 当日航班数 | {meta.get('flight_count')} |",
        f"| 最近抓取 | {meta.get('last_fetch') or '—'} |",
    ]
    if target:
        lines += [
            "",
            f"**目标航班明细**：{target.get('flight_no')} "
            f"{target.get('depart_time')}→{target.get('arrive_time')} · "
            f"{target.get('dep_airport')} → {target.get('arr_airport')} · "
            f"¥{target.get('price')}（{target.get('platform')}）",
        ]
    text = "\n".join(lines) + "\n"

    if args.summary_out:
        with open(args.summary_out, "a", encoding="utf-8") as f:
            f.write(text)
        print(f"\n已写入 Job Summary: {args.summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
