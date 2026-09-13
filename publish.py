"""把报告静态包发布到 Cloudflare Pages —— 电脑关机后仍可访问。

为什么是 Pages 而不是 Cloudflare Tunnel
--------------------------------------
Tunnel（cloudflared）只是把**本机**服务暴露到公网，本机一关机链接就失效。
你的要求是"电脑关闭也能访问"，所以必须把页面**托管到 Cloudflare 边缘**：
发布完成后页面由 Cloudflare 提供，与这台电脑无关。

数据新鲜度的边界（必须说清楚）
------------------------------
静态托管只解决"随时能打开"；**新数据仍需本机在线时抓取**。
每轮抓取结束后会自动重新发布最新快照，因此：
本机在线 → 页面持续更新；本机关机 → 页面停留在最后一次发布的快照。

用法
----
    python publish.py --dry-run          # 只生成部署包，不上传（无需凭据，用来验证包内容）
    python publish.py                    # 生成并发布
    python publish.py --no-build         # 跳过生成，直接发布现有 deploy/

凭据（三选一）
--------------
1. 环境变量 ``CLOUDFLARE_API_TOKEN``（可选再加 ``CLOUDFLARE_ACCOUNT_ID``）
2. 项目根目录的 ``cloudflare.env``，内容形如::

       CLOUDFLARE_API_TOKEN=xxxxxxxx
       CLOUDFLARE_ACCOUNT_ID=yyyyyyyy

   （该文件含密钥，切勿提交到版本库）
3. 已经用 ``npx wrangler login`` 完成过 OAuth 登录

建议的 Token 权限：Account → Cloudflare Pages → Edit。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "cloudflare.env"
LAST_PUBLISH = ROOT / ".last-publish.json"
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.pages\.dev\S*")


def has_oauth_login() -> bool:
    """判断是否已有 wrangler OAuth 登录。

    不同版本的 wrangler 把凭据放在不同位置（本机上实测落在
    %APPDATA%\\xdg.config\\.wrangler\\config），所以先查常见路径，
    查不到再用 `wrangler whoami` 做权威判断。
    """
    candidates = [
        Path.home() / ".wrangler" / "config" / "default.toml",
        Path.home() / ".config" / ".wrangler" / "config" / "default.toml",
        Path(os.environ.get("APPDATA", "")) / ".wrangler" / "config" / "default.toml",
        Path(os.environ.get("APPDATA", "")) / "xdg.config" / ".wrangler" / "config" / "default.toml",
        Path(os.environ.get("XDG_CONFIG_HOME", "")) / ".wrangler" / "config" / "default.toml",
    ]
    for p in candidates:
        try:
            if p.is_file():
                return True
        except Exception:
            pass

    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        return False
    try:
        proc = subprocess.run([npx, "--yes", "wrangler", "whoami"], cwd=str(ROOT),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
        return proc.returncode == 0 and "You are not authenticated" not in (proc.stdout or "")
    except Exception:
        return False


def load_credentials() -> dict:
    """从 cloudflare.env 或环境变量装载凭据；只读取需要的两个键。"""
    creds = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        if os.environ.get(k):
            creds[k] = os.environ[k]
    return creds


def run(cmd: list, cwd: Path, env: dict, quiet: bool = False):
    if not quiet:
        print("  $ " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, shell=False,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout or ""


def main() -> int:
    ap = argparse.ArgumentParser(description="发布报告到 Cloudflare Pages")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--project", default="", help="Pages 项目名（默认取配置 publish.project）")
    ap.add_argument("--dir", default="", help="部署目录（默认取配置 publish.deploy_dir）")
    ap.add_argument("--no-build", action="store_true", help="跳过重新生成部署包")
    ap.add_argument("--dry-run", action="store_true", help="只生成部署包，不上传")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    cfg_path = ROOT / args.config
    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    pub = cfg.get("publish") or {}
    out_cfg = cfg.get("output") or {}
    project = args.project or pub.get("project") or "cz3417-monitor"
    deploy_dir = Path(args.dir or out_cfg.get("deploy_dir") or "deploy")
    if not deploy_dir.is_absolute():
        deploy_dir = ROOT / deploy_dir

    # ---- 1) 生成部署包 ----
    if not args.no_build:
        sys.path.insert(0, str(ROOT))
        from report import build_deploy_bundle, connect
        db_path = str(ROOT / (out_cfg.get("db_path") or "data/prices.db"))
        if not Path(db_path).exists():
            print(f"数据库不存在: {db_path}", file=sys.stderr)
            return 2
        conn = connect(db_path)
        try:
            info = build_deploy_bundle(cfg, conn, str(deploy_dir), db_path)
        finally:
            conn.close()
        m = info["meta"]
        print(f"部署包: {deploy_dir}")
        for f in info["files"]:
            print(f"  - {f}  ({(deploy_dir / f).stat().st_size / 1024:.1f} KB)")
        print(f"  摘要: 当前 ¥{m['current_price']}  区间 ¥{m['min_price']}–¥{m['max_price']}  "
              f"样本 {m['sample_count']}  航班 {m['flight_count']}  更新 {m['updated_at']}")

    if args.dry_run:
        print("\n--dry-run：已生成部署包，未上传。")
        print(f"预览方式：直接打开 {deploy_dir / 'index.html'}")
        return 0

    # ---- 2) 凭据与工具检查 ----
    creds = load_credentials()
    token = creds.get("CLOUDFLARE_API_TOKEN", "")
    oauth_ok = has_oauth_login()

    if not token and not oauth_ok:
        print("\n未找到 Cloudflare 凭据，无法发布。请任选一种方式：", file=sys.stderr)
        print("  A) 在项目根目录创建 cloudflare.env，写入：", file=sys.stderr)
        print("       CLOUDFLARE_API_TOKEN=你的Token", file=sys.stderr)
        print("       CLOUDFLARE_ACCOUNT_ID=你的AccountID", file=sys.stderr)
        print("     Token 建议权限：Account → Cloudflare Pages → Edit", file=sys.stderr)
        print("  B) 设置环境变量 CLOUDFLARE_API_TOKEN", file=sys.stderr)
        print("  C) 先执行 npx wrangler login 完成浏览器授权", file=sys.stderr)
        return 2

    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        print("未找到 npx（需要 Node.js）", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["CLOUDFLARE_API_TOKEN"] = token or env.get("CLOUDFLARE_API_TOKEN", "")
    if creds.get("CLOUDFLARE_ACCOUNT_ID"):
        env["CLOUDFLARE_ACCOUNT_ID"] = creds["CLOUDFLARE_ACCOUNT_ID"]
    env.setdefault("CI", "1")          # 让 wrangler 走非交互模式

    # ---- 3) 确保 Pages 项目存在（已存在时报错可忽略）----
    code, out = run([npx, "--yes", "wrangler", "pages", "project", "create", project,
                     "--production-branch", "main"], ROOT, env, args.quiet)
    if code != 0 and "already exists" not in out.lower():
        if not args.quiet:
            print("  (项目创建返回非零，继续尝试直接部署)")
            print("  " + out.strip()[:500])

    # ---- 4) 发布 ----
    code, out = run([npx, "--yes", "wrangler", "pages", "deploy", str(deploy_dir),
                     "--project-name", project, "--branch", "main",
                     "--commit-dirty=true"], ROOT, env, args.quiet)
    if out and not args.quiet:
        print("\n".join("  " + ln for ln in out.strip().splitlines()[-25:]))

    urls = URL_RE.findall(out)
    url = urls[0] if urls else f"https://{project}.pages.dev"

    if code != 0:
        print(f"\n发布失败（退出码 {code}）。常见原因：Token 权限不足 / 未开通 Pages / 网络问题。",
              file=sys.stderr)
        return 3

    record = {
        "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "project": project,
        "url": url,
        "deploy_dir": str(deploy_dir),
    }
    try:
        meta = json.loads((deploy_dir / "meta.json").read_text(encoding="utf-8"))
        record["meta"] = meta
    except Exception:
        pass
    LAST_PUBLISH.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n发布成功：{url}")
    print(f"  - 页面      : {url}/")
    print(f"  - 最新一轮  : {url}/latest.json")
    print(f"  - 价格历史  : {url}/history.json")
    print(f"  - 摘要      : {url}/meta.json")
    print(f"  发布记录: {LAST_PUBLISH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
