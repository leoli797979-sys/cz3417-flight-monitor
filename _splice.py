"""一次性脚本：把多班次版实现拼接进 report.py（按函数边界区间替换，避免手改大函数）。

替换三处：
  1. def svg_price_chart(...)  -> 多序列图表 + compute_stats 等公共函数
  2. def build_html(...)       -> 多班次页面
  3. def build_deploy_bundle(...) -> 多班次 JSON 接口
另外在导入区补上 carrier_name（缺 core 时安全降级）。
"""
import pathlib
import re

root = pathlib.Path(__file__).resolve().parent
src = (root / "report.py").read_text(encoding="utf-8")

chart = (root / "_new_chart.py").read_text(encoding="utf-8")
stats = (root / "_new_stats.py").read_text(encoding="utf-8")
new_html = (root / "_new_build_html.py").read_text(encoding="utf-8")
new_bundle = (root / "_new_bundle.py").read_text(encoding="utf-8")


def replace_block(text, start_pat, end_pat, new):
    m1 = re.search(start_pat, text, re.M)
    m2 = re.search(end_pat, text, re.M)
    if not (m1 and m2 and m1.start() < m2.start()):
        raise SystemExit(f"定位失败: {start_pat} / {end_pat}")
    return text[:m1.start()] + new + text[m2.start():]


before = len(src)
src = replace_block(src, r"^def svg_price_chart\(", r"^def _esc\(v\)",
                    chart.rstrip() + "\n\n" + stats.strip() + "\n\n")
src = replace_block(src, r"^def build_html\(", r"^def build_report\(",
                    new_html.rstrip() + "\n\n")
src = replace_block(src, r"^def build_deploy_bundle\(", r"^def main\(",
                    new_bundle.rstrip() + "\n\n")

# 承运人代码表（缺 core 时降级为空，保证只装了 PyYAML 的 CI 也能跑）
if "_carrier_name" not in src.split("def build_html")[0]:
    anchor = "import yaml\n"
    ins = anchor + """
try:
    from core.flights import carrier_name as _carrier_name
except Exception:  # 允许 report.py 在只装了 PyYAML 的环境里单独运行
    def _carrier_name(flight_no):
        return ""
"""
    src = src.replace(anchor, ins, 1)

(root / "report.py").write_text(src, encoding="utf-8")
print(f"report.py 已更新: {before} -> {len(src)} 字节")
for name in ("_new_chart.py", "_new_stats.py", "_new_build_html.py", "_new_bundle.py"):
    (root / name).unlink(missing_ok=True)
print("临时文件已清理:", ", ".join(
    n for n in ("_new_chart.py", "_new_stats.py", "_new_build_html.py", "_new_bundle.py")))
