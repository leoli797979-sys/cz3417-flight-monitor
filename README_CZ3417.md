# CZ3417 机票监控（在 JiPiao 基础上的"按航班号"改造）

> 目标：**2026-09-22 南航 CZ3417，广州白云(CAN T2) → 成都双流(CTU T1)，15:15 起飞 / 17:35 到达**
> 上游项目：[yangka1212/JiPiao](https://github.com/yangka1212/JiPiao)（MIT）
> 工作目录：`D:\Harness\cz3417-monitor`

## 一、结论先看

2026-09-13 实测抓取（去哪儿）：

| 项目 | 结果 |
|---|---|
| 航班 | **CZ3417 南方航空** |
| 时刻 | **15:15 → 17:35**（与官方时刻一致） |
| 机场 | **广州白云 T2 → 成都双流 T1**（不是天府 TFU） |
| 去哪儿最低价 | **¥510**（经济舱展示价） |
| 机型 | 波音 737(中) |

同日同航线的市场参考：落到双流 CTU 的最低约 ¥460，落到天府 TFU 的最低约 ¥360。
注意 15:15 这一班在去哪儿被按共享航班号重复列了多行（CZ3417 / 长龙 GJ3029 / 厦航 MF1196 / 吉祥 HO7413），价格同为 ¥510——所以**必须按航班号监控**，否则会拿到别的班次的价。

## 二、为什么要改造：上游做不到"按航班号"

上游 `crawlers/qunar.py`、`crawlers/ctrip.py` 构造记录时只填了
`platform/from_city/to_city/depart_date/price` 五个字段，`flight_no` 一直是空字符串，
价格取的是 `min(当天所有航班)`。`core/alerter.py` 的阈值告警也只看当天最低价。

**后果**：若直接用它监控"CAN→CTU 09-22"，告警给你的是当天最便宜那班（清早或红眼），
永远不会理 15:15 的 CZ3417。

## 三、改了什么

| 文件 | 改动 |
|---|---|
| `core/flights.py` | **新增**。逐航班解析工具：按"值的形态"识别航班号/时刻（不绑死字段名），含承运人代码表 `carrier_name()`、时间窗 `in_time_window()` |
| `core/models.py` | `FlightPrice` 增 `route_level`（标记"只是全航线最低价，无法归属航班"）；`Route` 增 `watch_flights` / `depart_time_from` / `depart_time_to` |
| `core/alerter.py` | **重写告警逻辑**：支持按航班号筛选；**防误报保护**（指定航班号却没命中时只打 WARNING，绝不拿全航线最低价顶替）；去抖状态带上航班号；推送含航班号/时刻/机场/机型 + 当日对比 |
| `crawlers/qunar.py` | 新增 `_parse_flight_objects()`：针对去哪儿"截断+转义+拼接"响应按行抽航班；`fetch()` 改为每航班一条记录；反爬逻辑（Bella token / httpx 续航 / 浏览器握手）原样保留 |
| `crawlers/ctrip.py` | 新增 `_extract_ctrip_flights()`：利用其 `fltitem` 逐航班结构（本身就逐航班，原代码把航班身份丢了），保留"无票诱饵价"剔除 |
| `main.py` | `build_routes()` 解析 `watch_flights` / 时间窗字段；新增 `--headless` / `--no-report`；每轮结束自动刷新 HTML 报告 |
| `config.yaml` | **新增**。CZ3417 专用配置 |
| `report.py` | **新增**。生成自包含 HTML 报告（内联 SVG 走势图，无 CDN） |
| `run_monitor.ps1` | **新增**。定时抓取包装脚本（无头 + 防重入 + 日志轮转） |
| `install_task.ps1` | **新增**。注册/查看/触发/卸载 Windows 计划任务 |
| `verify_report.py` / `shot.py` | **新增**。报告 DOM 断言校验 / 渲染成 PNG |
| `show.py` | **新增**。查历史价格与统计（读 SQLite） |
| `probe_qunar.py` / `verify_real.py` | **新增**。诊断/离线复验脚本（对已存原始响应跑解析器） |
| `tests/test_flights.py` | **新增**。45 项自测，含真实结构回归 |

## 四、去哪儿响应的三个坑（都是实测踩出来的）

1. **`data` 不是合法 JSON**：头部被挪进 `t1000` 字段的混淆 JS 里重建
   （`JSON.parse(join('')+...)`），整串 `json.loads` 必然失败。
2. **共享航班号串行**：同一物理航班按多个销售航班号重复列多行，
   行内 `"code"` 字段经常指向**邻座**航班 → 不能当航班号用。
   因此改用 **`binfo.airCode`** 作行锚点。
3. **字段被拼接污染**：`name` / `extparams` 等字段中间会被别的片段截断
   （实测 `extparams` 337 个只有 7 个能完整解析），所以：
   - 航司名改用**航班号前缀权威推导**（`carrier_name()`），不信任页面字段；
   - 价格取"本行自己的 `code` 之后"的 `minPrice`，并剔除两位数字碎片。

## 五、怎么用

```powershell
cd D:\Harness\cz3417-monitor

# 跑一轮（已含浏览器握手，首次建议 headless: false）
.\.venv\Scripts\python.exe main.py --once -c config.yaml

# 定时监控（按 config.yaml 的 90±30 分钟）
.\.venv\Scripts\python.exe main.py -c config.yaml

# 查历史 / 统计
.\.venv\Scripts\python.exe show.py --flight CZ3417 --history

# 自测（45 项，不联网）
.\.venv\Scripts\python.exe tests\test_flights.py

# 对已保存的原始响应离线复验解析器
.\.venv\Scripts\python.exe verify_real.py
```

要收**微信降价推送**：在 `config.yaml` 里把 `notifier.serverchan.enabled` 改成 `true`，
填入 [Server酱](https://sct.ftqq.com/) 的 SendKey。不配推送时只在控制台/日志里提示。

调阈值：`routes[0].alert_threshold`（当前 1200，CZ3417 现价 510，所以现在一跑就会提示；
想只在"真便宜"时收到通知就调低到 500 左右）。

## 六、已知局限（不藏着）

- **携程那条线没打通**：`m.ctrip.com` H5 在本机环境下未解析到价格
  （`debug/ctrip_nopx_2026-09-22.png/html` 是它的调试快照），
  所以当前**只有去哪儿一个数据源**。去哪儿单源已足够完成本目标，但少一层交叉验证。
- **少数行的起飞时刻可能被拼接内容串到**（表现为"到达早于起飞"）。
  日志里这类行会标 `[起飞时刻可疑]`；目标航班 CZ3417 的时刻经两次核实无误。
- **去哪儿有限流**：其 `touchInnerList` 接口有 Bella token + 指纹风控，
  且全局约 5 分钟/次限制，故配置间隔为 90 分钟。别指望分钟级盯价。
- **价格口径**：取的是去哪儿展示的经济舱最低价，不含会员价/券后价，
  实际下单以航司页面为准。项目原声明"仅供个人学习参考，勿高频请求"。

## 七、HTML 报告页面

生成物：`report.html`（**单文件、零依赖、离线可看**，无 CDN、无 JS 库，图表是脚本自己算坐标画的 SVG）。

页面内容：

* **顶部卡片**：目标航班当前最低价（含较首次样本的涨跌）、航班/时刻/机型、机场、历史区间与均价、告警阈值及是否已触发
* **价格走势**：每次抓取的目标航班最低价折线图，带 y 轴金额刻度、数据点数值标注、红色虚线阈值线（低于阈值的点标绿）
* **当日全部航班价目**：同轮抓到的全部航班按价格升序，**目标航班高亮**，含跨平台价格明细；
  被拼接内容串到起飞时刻的行会标 `起飞时刻可疑`
* **目标航班抓取明细**：每条原始记录（时间/平台/时刻/价格/航司）
* **运行状态**：航线、日期、时刻窗、阈值、抓取间隔、本轮记录数、历史样本数
* **陈旧检测**：若距上次抓取超过 3 倍间隔，页面顶部会弹黄色告警条，提示定时任务可能已停

页面每 **300 秒自动刷新**（`output.report_refresh_seconds`，设 0 关闭），
所以把它开在浏览器或副屏上就是一个实时监控面板。

```powershell
# 手动生成
.\.venv\Scripts\python.exe report.py
# 指定输出文件 / 关闭自动刷新
.\.venv\Scripts\python.exe report.py -o 我的报告.html --no-refresh

# 打开报告（Windows）
start report.html
```

每轮抓取结束后会自动覆盖刷新 `report.html`（由 `config.yaml` 的
`output.report_html` 控制，留空则关闭）。

**渲染校验**（无需人眼）：

```powershell
.\.venv\Scripts\python.exe verify_report.py   # 24 项 DOM 断言：走势图数据点/坐标越界/表格/横向溢出/自动刷新
.\.venv\Scripts\python.exe shot.py            # 另外渲染一张 report_preview.png
```

## 八、定时抓取（Windows 计划任务）

```powershell
# 注册（默认每 90 分钟，与去哪儿的限流匹配）
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -IntervalMinutes 60   # 改间隔
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Status              # 看状态/上次结果/下次运行
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -RunNow              # 立即触发一次
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Uninstall           # 卸载
```

任务名 `CZ3417-Flight-Monitor`，每轮做的事：

1. `run_monitor.ps1` 以**无头浏览器**跑一轮抓取（不弹窗口，实测无头也能过去哪儿风控）
2. 用全局互斥量防重入——上一轮没跑完就跳过本次触发
3. Python 把结果写入 SQLite，告警器判断目标航班是否低于阈值
4. 自动重新生成 `report.html`
5. 全程日志追加到 `logs/task.log`（超过 2 MB 自动轮转），应用自身日志在 `logs/monitor.log`

设计取舍：

* 任务注册为**"仅在用户登录时运行"**——抓取依赖真实桌面会话里的浏览器，
  比"不管用户是否登录"（会话 0）成功率高得多。
* **编码坑（踩过两次，务必留意）**：
  1. Windows PowerShell 5.1 按系统 ANSI(GBK) 解码子进程**管道**输出，中文日志会变乱码；
     而计划任务里没有控制台，改 `[Console]::OutputEncoding` 无效。
     所以包装脚本不让 Python 输出进 PowerShell 管道，而是 `Start-Process` 重定向到
     临时文件后按 UTF-8 读回。
  2. `.ps1` 文件若**没有 UTF-8 BOM**，PS 5.1 会按 ANSI 读取该文件，
     中文注释/字符串会被错误解码，**严重时直接造成语法解析失败**——
     实测踩过一次：任务以退出码 1 立刻失败，连一行日志都没写。
     因此 `run_monitor.ps1` / `install_task.ps1` 一律保持**纯 ASCII**，
     中文说明只放在这份 README 和 HTML 报告里（它们由 Python 生成，UTF-8 安全）。
* 想改成"开机自启 + 常驻"的另一种形态：直接 `.\.venv\Scripts\python.exe main.py -c config.yaml`
  跑内置 APScheduler（`interval_minutes` / `jitter_minutes`），进程活着就按间隔抓。

## 九、验证记录（2026-09-13 实测）

| 验证项 | 结果 |
|---|---|
| 单元自测（解析/告警/回归） | **45 项全通过** `.\.venv\Scripts\python.exe tests\test_flights.py` |
| 报告 DOM 校验 | **24 项全通过** `.\.venv\Scripts\python.exe verify_report.py` |
| 真实报文离线复验 | CZ3417 = 15:15→17:35，白云T2→双流T1，¥510（与人工核对一致） |
| 无头模式抓取 | 132 架航班，CZ3417 正常命中（定时任务不弹窗可行） |
| 计划任务注册 | `CZ3417-Flight-Monitor`，每 90 分钟，`LastTaskResult = 0` |
| 定时任务端到端 | 任务触发 → 抓取 → 落库 → 告警 → `report.html` 自动刷新，中文日志正常 |
| 实测价格变动 | 6 轮样本：`¥510 → ¥510 → ¥510 → ¥510 → ¥510 → ¥470`（当日盘中降 ¥40） |

> 注：`LastTaskResult` 含义 —— `0` 成功，`1` 脚本报错，`267011` 表示尚未运行过。
