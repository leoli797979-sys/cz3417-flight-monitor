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

### 抓取节奏与限流（实测数据，决定了"能不能缩短间隔"）

**当前节奏**：计划任务每 **90 分钟**触发一次（实测间隔 89–90 分钟，很规律；睡眠中会被唤醒）。
一轮里先抓 `CAN→CTU 09-22`，等 **360 秒**，再抓 `CTU→CAN 09-27`，然后发布。一轮约 8–9 分钟。

**为什么要等 360 秒**：去哪儿 `touchInnerList` 是**全局限流**，同一轮里隔几秒连发两次请求，
第二次基本必被 `1999` 拦。配置项 `schedule.route_delay_seconds`（默认 360）控制这个等待，
且是在"上一个航向抓完之后"再计时，**实际间隔恒 ≥ 该值**，不会因为前一航向跑久了被压缩。

实测（2026-09-17 21:28）：

```
21:28:08 [qunar] 开始抓取 CAN->CTU   -> httpx 命中风控(1999)，本轮 0 条
21:29:53 [间隔] 等待 360 秒再抓下一个航向
21:35:53 [qunar] 开始抓取 CTU->CAN   -> 解析到 144 架航班，最低 ¥429   ✅
```

**能不能靠缩短间隔加密取数？不能。** 从 61 轮历史日志统计：

| httpx 结果 | 本轮拿到数据 | 次数 |
|---|---|---|
| 直连成功 | ✅ | 27（100%） |
| 被 1999 拦 | ⚠️ 浏览器兜底救回 | 5（22%） |
| 被 1999 拦 | ❌ 失败 | 18（78%） |
| 无 token | ❌ | 2 |

* **90 分钟才请求一次，已有 45%（23/51）被 1999 拦掉** —— 说明限制不是"每 5 分钟一次"这种短窗口
  规则，而更像**按 IP/令牌的累计配额或风险评分**。缩短轮次间隔只会让拦截比例更高。
* 被拦之后补救很弱：浏览器兜底只有 **22%** 能救回来（因为兜底在限流窗口内立刻发起）。
* 想把有效频率提上来，正确方向是**减少浪费**而不是加密请求：
  ① 错开同轮内的多次请求（已做，见上）；
  ② 撞 1999 后等待再兜底（未做，会拉长失败轮次约 5 分钟）；
  ③ 用不受去哪儿风控的源（途牛/同程）加密"航线最低价"采样，
  但按航班号的精确价仍只能靠去哪儿保持 90 分钟。
* **没有主动试探限流临界点**：当前通道已有 45% 被拦，贸然探测可能触发风控升级
  （携程就是被墙住后需要真人过拼图的先例），所以以上结论全部来自历史日志。

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

## 九、携程数据源现状（暂缓，附开启方法）

**结论：携程的航班列表接口目前在风控拦截下拿不到数据，页面会如实标注 `ctrip 本轮无数据`。**

实测证据（`debug/ctrip_xhr_*.txt` 只有 186 字节）：

```
https://m.ctrip.com/restapi/soa2/14488/flightListSearchForH5?...
whaleguard block
```

页面快照里是拼图验证墙（"请完成以下验证：依次点击图标验证 / 滑动将展现拼图"）。
这类验证靠改指纹过不去，必须真人过一次。

已经做过的尝试（**均未通过**，留着以后继续）：

* 指纹伪装提到基类：清掉 iPhone UA 下不该存在的 `navigator.userAgentData` /
  `connection` / `deviceMemory`，WebGL 伪装成 Apple GPU，plugins/mimeTypes 置空
* 列表页之前先访问机票首页"预热"，建立访客 cookie
* 有头 / 无头两种模式都试过；等待窗放宽到 12 秒 + 6 次滚动
* 风控识别：命中时明确打出 `被风控拦截：接口返回风控: whaleguard block`

顺带一个交叉印证：携程**跨日期低价日历没有被拦**，它显示广州→成都 09-22 最低 ¥360，
与去哪儿抓到的航线最低价一致 —— 说明去哪儿的数据可信。

**以后要启用携程**（两条路，任选其一）：

```powershell
# 路线 A：过一次拼图（不需要携程账号）
.\.venv\Scripts\python.exe solve_ctrip.py --from CTU --to CAN --date 2026-09-27 --wait 300
#   弹出可见浏览器 -> 手动滑动拼图 -> 脚本每 5 秒检测，拿到数据即保存会话到 user_data/ctrip
#   之后 python probe_ctrip.py --from CTU --to CAN --date 2026-09-27 可验证无头模式能否复用

# 路线 B：用携程账号登录（登录态通常能绕过拼图）
.\.venv\Scripts\python.exe main.py --login ctrip
```

成功之后不需要改任何代码：`config.yaml` / `config.ctucan.yaml` 的 `platforms` 里
本来就写着 `ctrip`，会话一旦有效，每轮抓取就会自动多出携程的价格。

## 十、外链部署与"关机可访问"的真实边界

### 公开地址（均已验证可用）

本项目现在有**两个独立监控页面**（各自独立的班次清单、版面与配置）：

| 页面 | Cloudflare Pages（主） | GitHub Pages（备） | 监控班次 |
|---|---|---|---|
| CZ3417 | https://cz3417-monitor.pages.dev/ | https://leoli797979-sys.github.io/cz3417-flight-monitor/ | CZ3417（15:15→17:35，09-22） |
| **晚间 5 班** | **https://can-ctu-evening.pages.dev/** | .../cz3417-flight-monitor/w5/ | 3U8736 · 3U1149 · MF1192 · CZ3413 · CZ9088 |
| **成都→广州** | **https://ctu-can-monitor.pages.dev/** | .../cz3417-flight-monitor/ctucan/ | CZ3444 · 3U8729（15:00/15:05→17:30，09-27） |

每个页面都提供同路径的机器可读接口：

| 用途 | 路径 |
|---|---|
| 摘要（各班次当前价/区间/样本数/更新时间） | `/meta.json` |
| 各班次价格历史序列 | `/history.json` |
| 最近一轮全部航班 + 各班次当前价 | `/latest.json` |

这些地址在**电脑关机后照样能打开**（页面托管在 Cloudflare / GitHub 的服务器上，与本机无关）。
页面内含查询框（搜索/排序/过滤），JSON 接口供外部程序查询。

* **Cloudflare Pages**：由本机每轮抓取后自动重新发布，
  `Cache-Control: public, max-age=60`，所以更新后约 1 分钟内可见。
  发布节流状态**按项目分开存**（`.last-publish.<项目名>.json`）——早期两页共用一个状态文件时，
  互相覆盖"上次发布价"会让节流误判为"价格变了"而失效。
* **GitHub Pages**：本机推送 `data/prices.db` 后由 `publish.yml` 自动渲染发布；
  该工作流会同时构建两个页面（晚间 5 班挂在 `/w5/` 子路径下）。

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
