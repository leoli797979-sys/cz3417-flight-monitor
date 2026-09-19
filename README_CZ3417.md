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
| `config.w5.yaml` / `config.ctucan.yaml` | **新增**。晚间 5 班页 / 成都→广州页的独立报告配置 |
| `crawlers/base.py` | 反检测脚本提到基类 `MOBILE_STEALTH_JS`，所有移动端爬虫共用（携程 Whale Guard 会查这些破绽） |
| `solve_ctrip.py` | **新增**。交互式过一次携程拼图验证，过关后信任会话存入 `user_data/ctrip` |
| `probe_ctrip.py` | **新增**。单独测试携程爬虫（诊断，不写库） |
| `scripts/check_flights.py` | **新增**。命令行查指定班次的当前价与历史 |
| `scripts/probe_report.py` / `probe-sources.yml` | **新增**。各数据源"云端可用性"探测与汇总 |
| `scripts/check_source_badge.py` | **新增**。校验页面"数据源状态"是否渲染成徽标 |

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

### 抓取节奏与限流（实测数据）

**当前节奏**：计划任务每 **30 分钟**触发一次（睡眠中会被唤醒）。
一轮里先抓 `CAN→CTU 09-22`，等 **360 秒**，再抓 `CTU→CAN 09-27`，然后发布。一轮约 8–9 分钟。

#### 瓶颈是「令牌放太久」，不是「请求太密」

把 60 次直连请求按**距上次令牌刷新（浏览器握手）的年龄**分组：

| 令牌年龄 | 直连成功 | 被 1999 拦 | 被拦比例 |
|---|---|---|---|
| ≤30 分钟 | 4 | 4 | 50% |
| **30–90 分钟** | **19** | **1** | **5%** ← 最佳 |
| 1.5–5 小时 | 5 | 20 | 80% |
| >5 小时 | 0 | 7 | **100%** |

状态转移同样吻合：**上次成功 → 本次成功率仅 21%**；**上次被拦 → 本次成功率 71%**
（被拦会触发浏览器握手刷新令牌，下一次就好）。也就是说历史上那 53% 的拦截**大部分是令牌过期**，
而不是限流 —— 这也解释了为什么"距上次请求多久"与被拦比例几乎无关（40%/50%/53%/50%/100%）。

> 更正：本文档早期版本据此误判为"按 IP 的累计配额，缩短间隔会更糟"。真实原因见上表。

真正的**短窗口限流**只体现在一处：同一轮内隔几秒连发两次请求，第二次必被拦
（上表 ≤30 分钟档里的 4 次被拦就是它）。这已由 `schedule.route_delay_seconds`（360 秒）修掉，
且该等待从"上一个航向抓完之后"计时，**实际间隔恒 ≥ 360 秒**。

#### 两个对应措施

| 措施 | 配置 | 作用 |
|---|---|---|
| 错开同轮内的两次请求 | `schedule.route_delay_seconds: 360` | 避开短窗口限流 |
| **令牌超龄就主动换新** | `crawler.token_max_age_minutes: 60` | 不再拿快过期的令牌去撞 1999（撞了兜底只有 22% 能救回），而是直接用浏览器握手换新令牌 |
| 轮次间隔 | `interval_minutes: 30` + 计划任务 `-IntervalMinutes 30` | 让令牌始终处于 30–90 分钟的高成功区间 |

实测（2026-09-17 21:45，令牌年龄约 50 分钟）：

```
21:45:02 [qunar] 开始抓取 CAN->CTU  -> httpx 命中，135 架航班，最低 ¥360   ✅
21:45:54 [间隔] 等待 360 秒再抓下一个航向
21:51:54 [qunar] 开始抓取 CTU->CAN  -> httpx 命中，145 架航班，最低 ¥400   ✅
```

两个航向全部直连成功、零风控拦截。

#### 回退判据

改成 30 分钟后若 `scripts/analyze_gap.py` 显示**被拦比例明显高于 53%**（说明存在真实的
请求量配额），就把 `-IntervalMinutes` 调回 60/90 —— 一条命令即可，无需改代码：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -IntervalMinutes 90
```

分析脚本（都是只读日志、零风险）：`scripts/analyze_cadence.py`（分支与路径成率）、
`scripts/analyze_gap.py`（间隔 vs 拦截）、`scripts/analyze_token_age.py`（令牌年龄 vs 拦截）。

`report.py` 的"抓取陈旧"告警阈值取 `max(3×间隔, 240 分钟)`，避免 30 分钟节奏下
因为常见的单轮失败而误报"任务已停止"。

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

## 九、携程数据源现状（间歇可用，附实测与启用方法）

**结论：携程是"间歇可用"——多数轮次被 Whale Guard 拦，但偶尔整批成功。**
2026-09-17 21:54 那轮实测拿到 **155 架航班**（CTU→CAN 09-27），
两个目标航班双源价格一致：

| 航班 | 去哪儿 | 携程 |
|---|---|---|
| CZ3444 | ¥770 | **¥770** |
| 3U8729 | ¥860 | **¥860** |

> 早期本文档写的是"完全拿不到数据"，那是**统计口径的误导**：52 轮里 ctrip 0 条成功，
> 但后来发现成功是可能的，只是概率低；而且页面/日志的"被风控拦截"提示有过**假阳性**（见下）。

**被拦时的证据**（`debug/ctrip_xhr_*.txt` 只有 186 字节）：

```
https://m.ctrip.com/restapi/soa2/14488/flightListSearchForH5?...
whaleguard block
```

**修掉的一个假阳性**：早期用"页面 HTML 里出现 `captcha`"来判定被拦，但携程页面**本身就引用**
`captcha.min.js` / `jigsawCaptcha` 等脚本（正常页面也有）。实测出现过"打了被拦日志、
同一轮却解析到 155 架航班"的矛盾输出。现在只认**接口返回内容**（`whaleguard` 或长度异常的 block）。

已做过的改造（保留）：

* 指纹伪装提到基类：清掉 iPhone UA 下不该存在的 `navigator.userAgentData` /
  `connection` / `deviceMemory`，WebGL 伪装成 Apple GPU，plugins/mimeTypes 置空
* 列表页之前先访问机票首页"预热"，建立访客 cookie
* 风控识别只依据接口返回内容（见上）
* 同一轮内两次请求间隔 360 秒（`schedule.route_delay_seconds`）

顺带一个交叉印证：携程**跨日期低价日历没有被拦**，它显示广州→成都 09-22 最低 ¥360，
与去哪儿抓到的航线最低价一致。

**想提高携程的成功率**（两条路，任选其一）：

```powershell
# 路线 A：过一次拼图（不需要携程账号）
.\.venv\Scripts\python.exe solve_ctrip.py --from CTU --to CAN --date 2026-09-27 --wait 300
#   弹出可见浏览器 -> 手动滑动拼图 -> 脚本每 5 秒检测，拿到数据即保存会话到 user_data/ctrip

# 路线 B：用携程账号登录（登录态通常能绕过拼图）
.\.venv\Scripts\python.exe main.py --login ctrip
```

成功之后不需要改任何代码：`config.yaml` / `config.ctucan.yaml` 的 `platforms` 里
本来就写着 `ctrip`，会话一旦有效，每轮抓取就会自动多出携程的价格。

## 十、外链部署与"关机可访问"的真实边界

### 公开地址（均已验证可用）

> **当前只监控一个页面**：成都→广州 09-27（CZ3444 / 3U8729，携程 + 去哪儿）。
> CZ3417 与晚间 5 班的页面**已删除**（Cloudflare 项目已删，链接下线）；
> 它们的配置与数据仍在本地，需要时可随时重建。

| 页面 | 状态 | Cloudflare Pages | GitHub Pages |
|---|---|---|---|
| **成都→广州 09-27** | ✅ **监控中** | **https://ctu-can-monitor.pages.dev/** | https://leoli797979-sys.github.io/cz3417-flight-monitor/ （根路径）<br>…/cz3417-flight-monitor/ctucan/ （旧链接仍可访问） |
| CZ3417 | ❌ 已删除 | ~~cz3417-monitor.pages.dev~~（DNS 已移除） | 根路径已被上方页面取代 |
| 晚间 5 班 | ❌ 已删除 | ~~can-ctu-evening.pages.dev~~（返回 530） | …/w5/ 已随新部署下线（404） |

每个页面都提供同路径的机器可读接口：

| 用途 | 路径 |
|---|---|
| 摘要（各班次当前价/区间/样本数/**各数据源本轮状态**/更新时间） | `/meta.json` |
| 各班次价格历史序列 | `/history.json` |
| 最近一轮全部航班 + 各班次当前价 | `/latest.json` |

这些地址在**电脑关机后照样能打开**（页面托管在 Cloudflare / GitHub 的服务器上，与本机无关）。
页面内含查询框（搜索/排序/过滤），JSON 接口供外部程序查询。

* **本机计划任务**每 **30 分钟**跑一轮 `main.py -c config.ctucan.yaml`（只抓成都→广州一个航向），
  一轮约 **46 秒**；每轮结束自动发布到 Cloudflare Pages（价格未变时按 180 分钟节流）。
* **GitHub Pages**：本机推送 `data/prices.db` 后由 `publish.yml` 自动重建并发布。

> 授权踩坑记录：Cloudflare 的 `wrangler login` 走 localhost 回调且只给约 2 分钟窗口。
> 本机**默认浏览器无法正常打开该授权页**，导致连续三次超时；
> 改用 Firefox 显式打开授权链接后一次成功（`firefox.exe "<授权URL>"`）。
> 凭据存放在 `%APPDATA%\xdg.config\.wrangler\config\default.toml`。

### 新增一个监控页面要做什么

1. 复制一份配置，例如 `config.w5.yaml`：改 `watch_flights`、`output.report_html`、
   `output.deploy_dir`、`publish.project`，**时间窗留空**（按航班号匹配即可，
   留成别的班次的时刻窗会把目标班次过滤掉——这个坑踩过）。
2. `python report.py -c config.w5.yaml --deploy-dir deploy-w5` → 生成页面
3. `python publish.py -c config.w5.yaml` → 第一次会自动创建 Cloudflare Pages 项目并发布
4. 在 `run_monitor.ps1` 的 `Publish-SecondPage` 旁边照样加一个函数调用，
   或在 `publish.yml` 里加一个构建步骤（挂到新子路径）

**不需要额外抓取**：抓取是按航线取全部航班的，新页面只是"换个班次清单重新渲染"，
零额外请求、零额外配额。

### 架构：本机抓取 + 双通道发布

```
本机计划任务（每90分钟，开机时才跑）
   └─ 抓取去哪儿 → 写入 SQLite → 刷新本地 report.html
        ├─ 自动发布到 Cloudflare Pages（publish.py，约 10 秒）→ cz3417-monitor.pages.dev 更新
        └─ 若有新数据：git push data/prices.db
             └─ GitHub Actions: publish.yml（只渲染，不抓取）
                  └─ 部署到 GitHub Pages → 备站更新（约 50 秒）
```

**为什么云端不自己抓取**（实测结论，不是猜测）：

GitHub Actions 的机房 IP 访问去哪儿时，页面被**跳转到登录页**：

```
[qunar] 90 秒内未拦到 touchInnerList 响应
（当前页 https://user.qunar.com/mobile/login.jsp?ret=...）
```

即 Bella 指纹握手能过，但航班列表接口因登录墙不返回，抓取结果是 0 条。
因此 `monitor.yml` 的定时抓取已停用（保留 `workflow_dispatch` 供手动实验）。

### 必须说清的边界

* **电脑开机 / 睡眠**：每 90 分钟自动抓取并刷新外链（睡眠时由任务自己唤醒，见下节）。
* **电脑关机**：外链**仍然可以打开**（由 Cloudflare / GitHub 托管），但显示的是**最后一次的快照**，
  价格不会更新。这是数据源限制而非配置问题——原因见上面"云端抓取登录墙"。
* **CI 闸门**：`scripts/ci_gate.py` 保证"抓不到数据就报红且不覆盖线上好数据"。
  这是踩过坑后加的——最初的实现里，云端抓取失败照样变绿，
  然后把仓库里的旧快照重新发布一遍，页面看着正常其实早已停止更新。

### 睡眠 + 定时唤醒（让"没关机只是休眠"也能更新）

计划任务已启用 **WakeToRun**：电脑在睡眠/休眠状态下也会被定时叫醒抓取，
抓完若无人使用则自动睡回去。验证过的三项前提：

| 前提 | 状态 |
|---|---|
| 任务设置 `WakeToRun` | `True` |
| 电源计划允许唤醒定时器（`SUB_SLEEP` / `RTCWAKE`） | AC=1 DC=1（已启用） |
| 抓完睡回（仅当空闲 ≥10 分钟） | 已启用（`-SleepAfter -IdleMinutes 10`） |

```powershell
# 查看状态（含 WakeToRun 与唤醒定时器）
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Status
# 关掉"抓完睡回"，或改成空闲 30 分钟才睡
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -SleepAfterMinutes 0
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -SleepAfterMinutes 30
# 干跑验证睡眠判定（只记日志，不会真的睡）
powershell -ExecutionPolicy Bypass -File .\run_monitor.ps1 -SleepAfter -IdleMinutes 0 -DryRunSleep
```

三点注意：

* **只对睡眠/休眠有效**。完全关机（S5）叫不醒——那种场景需要一台常开设备
  （迷你主机 / 旧笔记本 / NAS / 树莓派），或在云端换数据源（见下）。
* 若你离开电脑超过 10 分钟且恰好赶上定时任务，它会把机器**睡掉**。
  如果这会打断你（例如正在后台下载），用 `-SleepAfterMinutes 0` 关掉这个行为。
* 若发现唤醒了却没更新，先看 `logs/task.log`，再确认唤醒定时器是否仍被允许：
  `powercfg /query SCHEME_CURRENT SUB_SLEEP RTCWAKE`（某些"节能"软件会把它改回禁用；
  重新启用需管理员权限：`powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1`）。

### 各数据源在云端的实测结论（决定"关机后能不能更新"）

| 数据源 | 本机（住宅 IP） | 云端（GitHub 机房 IP） | 能给出 CZ3417 吗 |
|---|---|---|---|
| 去哪儿 | ✓ 144 架逐航班 | ✗ 被跳转登录页，0 条 | ✓ 但只有本机能 |
| 途牛 | ✓ ¥399 | ✓ ¥360（**云端可用**） | ✗ 报文里没有 CZ3417（实测 0 次命中） |
| 同程 | ✓ 仅航线级 | ✓ 仅航线级 | ✗ 连航班号都不给 |
| 飞猪 / 携程 | ✗ 0 条 | ✗ 0 条 | ✗ |

复现方式：`gh workflow run probe-sources.yml`（配置见 `config.probe.yaml`，只写 `data/probe.db`，不碰正式数据）。

**结论**：关机期间云端能拿到的是"航线级最低价"（途牛/同程），
拿不到 CZ3417 这一班的精确价格。后者必须由住宅 IP + 真实浏览器会话抓取。

### Cloudflare 现状

**已部署**：https://cz3417-monitor.pages.dev （项目 `cz3417-monitor`，
账号 leoli797979@gmail.com），每轮抓取后由 `publish.py` 自动重新发布，
并带发布节流（价格未变时最短间隔 180 分钟、每天最多 12 次）以保护免费版
500 次/月的额度。

授权要点（踩过坑）：`wrangler login` 走 localhost 回调且只有约 2 分钟窗口，
**默认浏览器打不开该授权页**，连续三次超时；改用 Firefox 显式打开授权链接后一次成功：

```powershell
& "C:\Program Files\Mozilla Firefox\firefox.exe" (Get-Content .cf-oauth-url.txt -Raw)
```

凭据存放于 `%APPDATA%\xdg.config\.wrangler\config\default.toml`。
若要改用 API Token 方式（无需时间窗口），在 `cloudflare.env` 写入
`CLOUDFLARE_API_TOKEN`（权限 Account → Cloudflare Pages → Edit）与 `CLOUDFLARE_ACCOUNT_ID` 即可。

## 十一、2026-09-17 故障复盘：网页为什么"收不到最新推送"

**现象**：外部网页停在 22:29 的旧数据，而库里已经有 22:53 的记录。

**根因链**（一条条查出来的，不是猜的）：

1. 22:53:30 那轮，去哪儿走 httpx 正常（2 秒、141 条已落库），接着携程**页面崩溃**
   （`playwright ... Page crashed`），22:53:52 被 `except` 捕获。
2. 之后 `base.py` 的 `finally: ctx.close()` **卡死**——关闭一个已经崩掉的持久化上下文，
   Playwright 会一直等浏览器进程退出。整轮就此挂住（monitor.log 24 分钟零输出）。
3. 任务计划有 `ExecutionTimeLimit=30 分钟`，挂到点才被强杀（结果码 `0xC000013A`）。
   于是那一轮**报告没刷新、快照没推送** → 网页停在旧数据。

**排除掉的假设**（查证过才敢说）：不是被睡回去杀的 —— 电源方案 `STANDBYIDLE=0x0`
（从不自动睡眠），22:00–23:30 也没有任何 Kernel-Power 事件。

**修复**：

| 位置 | 改动 |
| --- | --- |
| `run_monitor.ps1` | 轮次看门狗 `-RoundTimeoutMinutes`（默认 12，任务里设 5）：超时 `taskkill /F /T` 杀整棵进程树，随后 `main.py --report-only` 用库里已有数据补报告+发布；快照推送改为每轮都试（无变更自动跳过）并重试 3 次；顺手修掉 `Start-Process -PassThru` 下 `ExitCode` 读不到（需 `EnableRaisingEvents`），日志里 `exit code` 不再为空 |
| `main.py` | 抽出 `refresh_report_and_publish()`，新增 `--report-only`（只刷新+发布，不抓取） |
| `crawlers/base.py` | Chromium 加 `--disable-gpu`：无头渲染进程崩溃基本都出在 GPU/ANGLE 初始化 |
| `report.py` | 新增 `backfill_times()`：报文被反爬掺假时按班次补齐起降时刻（实测某轮 116 行缺 27 行，且恰好含监控班次，页面会渲染成 `--:--`） |

## 十二、2026-09-18 ~ 09-25：把频率开到最大

用户只需要监控到 **2026-09-25**（航班 09-27），因此这段时间不再保留额度：

- **抓取**：`schedule.interval_minutes: 10`（每天 144 轮），计划任务已重装为 `PT10M`。
- **不再自动睡眠**（`-SleepAfterMinutes 0`）：10 分钟一轮没必要睡回醒回，
  144 次唤醒/天既折腾硬件又增加"唤醒后渲染崩溃"的机会。代价是这段时间电脑不睡。
- **GitHub Pages 每轮重建**（无额度限制，是刷新最快的一路）→ 10 分钟一刷。
  但它的触发方式是"推送 `data/prices.db`"，而 2 MB 的库按 10 分钟推一次，
  8 天能堆出 1 GB 以上 git 历史，所以加了 `core/storage.py: prune()`：
  **监控班次的历史全留**（页面曲线要用），非监控班次只留最近 3 批。
  实测库从 **2.01 MB / 6253 行 → 0.17 MB / 431 行**（小 92%）。
- **Cloudflare Pages 30 分钟一刷**（`min_interval_minutes: 30`、`max_per_day: 45`）：
  免费版 500 次/月（本项目 09-17 才建），剩余 8.5 天按 45 次/天约 383 次，留约 100 次余量。
  价格真变了这两个限制都不拦，会立刻发布。
- **携程改为 30 分钟一次**（`schedule.platform_skip_minutes: {ctrip: 30}`）：
  它每次都要开一次浏览器（~40 秒）、整晚都被 Whale Guard 挡、逐航班价格又与去哪儿
  完全一致，10 分钟一轮跑它既拖慢轮次又更容易触发上面那个崩溃。
  判据用 `crawl_attempts` 表记录"上次**尝试**时间"—— 不能用 `flight_prices` 的最新时间，
  因为平台被挡时一条都存不下来，会把"刚抓过"误判成"很久没抓"从而每轮重试。

**qunar 两处新防护**（当晚真踩到）：

1. **登录页令牌不许入库**：23:45 那次页面被跳到 `user.qunar.com/mobile/login.jsp`，
   请求拦截器照样抓到一个 Bella，代码却把它当"已刷新"存了下来 ——
   缓存被毒化后每轮 httpx 必然 1999，而浏览器回退又被登录墙挡住，从此拿不到真令牌。
   现在只有**真的拦到 touchInnerList 响应**才写缓存。
2. **登录墙冷却 30 分钟**：去哪儿偶发登录墙（当晚出现 3 次，每次 ~50 分钟内自行恢复），
   撞墙后暂停浏览器握手，避免用 10 分钟一轮的节奏反复撞墙把风控喂得更狠；
   冷却期间 httpx 仍照常尝试（有效令牌往往能绕过页面级风控）。

若登录墙长时间不恢复，需要人工登录一次：`python main.py --login qunar`（可见浏览器 + 手机验证码）。

## 十三、代码共享号的坑（2026-09-18，用户发现）

**现象**：页面上出现 `3U1172 15:00→17:30 ¥770`，看起来像"比 3U8729 便宜 ¥90 的另一个选择"，
但用户在去哪儿 App 里**搜不到这个航班**。

**真相**：报文里这一行是

```json
"binfo":{"airCode":["3U1172"], "arrTime":"17:30", "codeShare":1, ...,
         "mainCarrier":"CZ3444", "mainCarrierSimpleNameAndNo":"南航CZ3444"},
"code":"3U1172", "minPrice":"770"
```

`codeShare:1` + `mainCarrier:CZ3444` —— **3U1172 就是 CZ3444 挂川航号卖的别名**，
App 里只会显示实际承运的 CZ3444。同一趟飞机挂多家航司号出售是常态：13:00 那班里
`CZ3438 / 3U1168 / MF1207 / HO7418` 四个号其实是同一架飞机。

**影响面比想象的大**：整份报文 **304 行里有 224 行是共享号**（`codeShare:1` 232 次、
`:0` 168 次），所以页面此前显示的"航班 148 架"是**虚高一倍多**的，
而且这些行的 `binfo` 多是被拼接的碎片 —— **这正是"某轮 116 行里 27 行缺起降时刻"的根因**。

**修复**：`_parse_flight_objects` 读 `codeShare` 与 `mainCarrier`，把共享行的航班号
**改记成实际承运号**。为什么不是直接丢弃：报文掺假程度每轮不同，有时"实际承运行"那一行
根本解析不出来（实测 12:51 那批 61 行里 CZ3444 缺失、只剩它的别名 3U1172），
丢弃规则会失效 —— 页面既留着假航班号，监控班次还整批消失。改记后两个问题一起解决
（实测 CZ3444 从"缺失"恢复为 15:00→17:30 ¥770）。修复后同一份报文解析出 **47 个真实航班**
（06:40→23:20，符合成都→广州一天的班次量），页脚也注明"只列实际承运航班"。

**教训**：拿报文里的航班号做"还有更便宜的选择"这类判断前，先看它是不是 `codeShare`。

### 追加：同一航班号有两个候选行时怎么选（2026-09-19）

上一条修完后又出现新症状：页面把 **3U8729 显示成 `16:05→17:30 天府(TFU)`**，
而它三十多个样本一直是 `15:05→17:30 双流(CTU)`。打印候选行后真相很清楚：

| 候选 | 时刻 | 出发 | 机场名 | 判断 |
| --- | --- | --- | --- | --- |
| 拼接碎片行 | 16:05→17:30（航程仅 85 分钟，真实约 2h25m） | id=TFU | **空** | 假 |
| 真实行 | 15:05→17:30 | id=CTU | 双流 | 真 |

两行的"字段填充数"和价格完全一样，旧选优逻辑只看 id 类字段、同分取先出现者，
于是碎片行赢了。修复：选优字段加入**机场名**（`dep_airport` / `arr_airport`），
并新增"本行 `code` 是否指向自己"这一位（指向自己说明这块没被拼接串行），
打分顺序为 `字段填充数 -> code 自洽 -> 低价`。复验同一份报文，3U8729 恢复为 15:05 双流。

**通用教训**：这份报文是"拼接碎片 + 掺假"的，任何"按行取字段"的地方都要有
**多候选互相比对**的机制（填充度、自洽性、票数），不能只信第一个能解析出来的行。
