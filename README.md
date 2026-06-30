# 机票价格监控小工具

监控 **携程 / 飞猪 / 同程 / 去哪儿 / 途牛** 多家平台同一航线、同一日期的机票价格，支持：

- 多航线 + 多日期同时监控
- 多平台价格对比，自动找出最低价来源
- 多日期对比，自动挑出最便宜的一天
- 低价阈值提醒：控制台 + 日志 + **微信推送（Server酱）**
- 推送去抖：价格波动太小不重复推送，避免消息轰炸
- 价格历史入库（SQLite），可用于后续画走势
- 定时调度带随机扰动，规避机器人定时特征

> 数据获取：携程/同程/去哪儿走 **Playwright 浏览器自动化**，飞猪/途牛走 **纯 httpx 逆向**（无需浏览器）。仅用于个人学习/出行参考，请勿高频请求。

---

## 1. 安装

```powershell
git clone <your-repo-url> JiPiao
cd JiPiao
pip install -r requirements.txt
# 携程/同程/去哪儿 需要 Playwright 浏览器内核（飞猪/途牛无需）
python -m playwright install chromium
```

依赖见 `requirements.txt`：`playwright`、`PyYAML`、`APScheduler`、`httpx`。

## 2. 配置

复制示例配置并按需修改：

```powershell
Copy-Item config.example.yaml config.yaml
```

`config.yaml` 主要字段：

```yaml
routes:
  - from: SHA           # 出发地三字码
    from_name: 上海
    to: BJS             # 目的地三字码
    to_name: 北京
    dates:
      - "2026-07-15"
      - "2026-07-16"
      - "2026-07-20"
    alert_threshold: 800   # 低于此价触发提醒（元，0 表示不启用）

# 监控平台，可选: ctrip, fliggy, tongcheng, qunar, tuniu
# fliggy / tuniu 为纯 httpx 逆向实现，无需浏览器；其余走 Playwright
platforms: [ctrip, fliggy, tongcheng, qunar, tuniu]

schedule:
  interval_minutes: 90       # 基础间隔
  jitter_minutes: 30          # 随机扰动，实际间隔 = [90-30, 90+30]
  run_on_start: true          # 启动时立即跑一次

crawler:
  headless: true              # 无头模式；首次建议 false 观察反爬
  timeout_seconds: 45
  delay_min: 5                # 单次请求间随机等待（秒）
  delay_max: 15
  user_agent: "..."           # 桌面 UA
  mobile_user_agent: ""       # 移动 UA，留空用内置 iPhone Safari
  debug: true                 # 解析失败时存截图/HTML 到 debug_dir
  debug_dir: debug
  user_data_dir: user_data    # 浏览器会话目录（各平台独立子目录）

output:
  db_path: data/prices.db
  log_path: logs/monitor.log

notifier:
  push_drop_min: 30           # 较上次推送价降幅 ≥ 此值才再推
  push_rise_min: 50           # 涨幅 ≥ 此值才再推（提醒涨价赶紧买）
  serverchan:
    enabled: true
    send_key: "SCTxxxxxxxxxxxxx"   # 你的 Server酱 SendKey
    channel: ""                     # 可选推送通道，多个用 | 分隔
```

> 常用城市三字码：北京 `BJS`、上海 `SHA`、广州 `CAN`、深圳 `SZX`、成都 `CTU`、杭州 `HGH`、西安 `SIA`、重庆 `CKG`、昆明 `KMG`。

## 3. 运行

### 前台运行

```powershell
python main.py                # 按 config.yaml 启动定时监控，Ctrl+C 退出
python main.py --once         # 只跑一次（调试用）
python main.py -c my.yaml      # 指定配置文件
```

### 后台运行（Windows）

仓库附带三个 PowerShell 脚本，把监控跑成后台进程：

```powershell
.\start.ps1      # 后台启动，PID 写入 .monitor.pid，输出重定向到 logs\monitor.out.log
.\status.ps1     # 查看运行状态、PID、内存占用及最近 20 行日志
.\stop.ps1       # 停止监控
```

## 4. 输出说明

- **控制台 / `logs/monitor.log`**：每轮最低价、多平台对比、多日期对比、低价提醒
- **`data/prices.db`**：SQLite，表 `flight_prices` 存全部历史价；表 `alert_state` 存上次已推送价（用于去抖）
- **`debug/`**：开启 `crawler.debug` 后，解析失败时保存的截图与 HTML
- **微信**：触发低价阈值时由 Server酱 推送，消息含航线/日期/价格/来源/详情链接

查询历史最低价示例：

```bash
sqlite3 data/prices.db
> SELECT depart_date, platform, MIN(price)
  FROM flight_prices
  WHERE from_city='SHA' AND to_city='BJS'
  GROUP BY depart_date, platform;
```

## 5. 项目结构

```
JiPiao/
├── main.py                 # 入口：调度 / --once
├── config.yaml             # 真实配置（含密钥，不提交，已 gitignore）
├── config.example.yaml     # 示例配置（脱敏）
├── requirements.txt
├── start.ps1 / status.ps1 / stop.ps1   # Windows 后台运行脚本
├── core/
│   ├── models.py           # 数据模型 FlightPrice / Route
│   ├── storage.py          # SQLite 存储 + alert_state 去抖
│   ├── alerter.py          # 价格对比 + 低价提醒 + 推送
│   ├── notifier.py         # 消息推送（Server酱）
│   ├── scheduler.py        # APScheduler 调度
│   └── logger.py           # 日志
├── crawlers/
│   ├── base.py             # Playwright 持久化上下文 + XHR 拦截
│   ├── ctrip.py            # 携程      (Playwright)
│   ├── fliggy.py           # 飞猪      (httpx 逆向)
│   ├── tongcheng.py        # 同程      (Playwright)
│   ├── qunar.py            # 去哪儿    (Playwright)
│   └── tuniu.py            # 途牛      (httpx 逆向)
├── data/                   # 运行产物：prices.db（gitignore）
├── logs/                   # 运行产物：monitor.log（gitignore）
├── debug/                  # 调试快照（gitignore）
└── user_data/              # 浏览器会话（gitignore）
```

## 6. 常见问题

- **抓不到价格 / 一直 0 条记录**：把 `crawler.headless` 改为 `false` 肉眼观察，是否触发验证码/风控；打开 `crawler.debug` 看截图。
- **被风控 / 出现滑块**：加大 `delay_min/delay_max`，降低 `schedule.interval_minutes` 频率。
- **推不到微信**：确认 Server酱 SendKey 正确（`notifier.serverchan.send_key`），免费版每日 5 条额度；去抖阈值 `push_drop_min/push_rise_min` 过大也会抑制推送。
- **想推送到钉钉/企业微信**：在 `core/notifier.py` 新增一个 Notifier 类，并在 `build_notifier` 里按配置择一构造即可。
- **想看历史走势**：基于 `data/prices.db` 写个 matplotlib 脚本，或接 Grafana。

## 7. 免责声明

本工具仅供学习交流，所有数据均来自各平台公开网页。请遵守各平台服务条款，控制请求频率，勿用于商业用途。使用本工具产生的一切后果由使用者自行承担。

## 8. License

MIT
