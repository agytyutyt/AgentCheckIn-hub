# CheckinHub

WorkBuddy + Qoder 的多账号每日签到、积分领取与宠物旅行自动化平台，带 Web 面板、执行日志、失败重试与补偿队列。

> 后端 Python 3.12（Docker 镜像固定 `python:3.12-slim`；本机 3.10+ 均可，实测 3.13/3.14 也能跑）/ FastAPI / APScheduler / SQLite；前端服务端渲染 + 原生 JS，零构建链。

> 🔁 **换机 / 重置系统后要接着干？先看 [HANDOFF.md](HANDOFF.md)** —— 哪些数据不进 git、怎么备份、
> 新机器上按什么顺序恢复、当前进度和待办都在那里。

---

## 1. 功能

| 能力 | 说明 |
|---|---|
| WorkBuddy 每日签到 | 先查状态再签，已签自动跳过；记录连续天数与净增积分 |
| WorkBuddy 宠物旅行 | 每 30 分钟探测状态机：`idle` 派出 → `traveling` 等待 → `arrived` 领积分 |
| 实时积分余额 | WorkBuddy 资源包账单逐包汇总 / Qoder `quota/usage` 两段求和；60s TTL 缓存 + 每 10 分钟后台刷新 + 手动刷新，余额上涨自动记一条流水，附带快过期提醒 |
| 扫码纳管 | 网页上出二维码，手机扫码在官方页确认即可自动建号（state → token → account 三步），不用打开客户端导出粘贴 |
| Qoder 登录添加 | 官方授权页手机号 + 短信验证码登录（设备码 PKCE 流程，验证码在官方页输入），面板轮询自动建号 |
| 令牌续期与保活 | 每小时巡检：WorkBuddy 剩余不足 3 天用 refreshToken 续期；Qoder deviceToken 剩余不足 7 天自动刷新。账号卡上也可手动触发，新令牌加密回写 |
| 双区域账号池 | 国际版叫 **Qoder**、国内版叫 **Qoder CN**，各自独立成池：添加账号时选区域，卡片带徽标，同一任务自动按账号各自的区域出站 |
| Qoder 每日签到（领 100） | **唯一的 Qoder 每日任务**：拉活动列表后逐个 `claim`，未开窗返回 `pending` 下轮再试；重复领取归一为「已签」；国内版活动平台故障时回退 sash `daily-check-in`，国际版无该接口则如实失败 |
| 多账号 | 同一平台可挂多个账号，逐账号容错，单账号失败不影响其他账号 |
| 失败重试 | 三层：HTTP 退避 → 任务内重试 → 补偿队列（10min / 30min / 2h，受执行窗口约束） |
| 可视化 | 账号卡片式看板（每账号一卡，任务状态、**积分余额**与今日新增直出）、14 天趋势、运行记录、单次详情、SSE 实时日志 |
| 通知 | 企业微信群机器人（免费）+ 通用 Webhook；失败、需重新登录、连续失败都会推 |
| 命令行 | `python -m app.cli list / run / probe / backup`，不依赖 Web 服务，可挂系统 cron 兜底 |
| 回归测试 | `tests/e2e.py` 用本地 Mock 上游跑通全链路（签到/旅行/领取/身份/401 自愈/迁移），不碰真实账号 |
| 安全 | 凭证 AES-256-GCM 落库、令牌脱敏、登录会话、面板默认只监听 127.0.0.1 |

> 「积分余额」口径：
> · **WorkBuddy** 走 billing 资源包账单接口 `POST https://www.codebuddy.cn/v2/billing/meter/get-user-resource`，
>   把十几个资源包的 `CycleCapacityRemain` 逐个钳位后求和，就是**实时可用余额**（点「测试」、开页面、
>   后台每 10 分钟都会推进；TTL 60 秒内不重复打上游）。**别用签到活动接口的 `total_credits`** ——
>   那是「本次活动累计、只增不减」，实测同一个号活动累计 800、真实余额 1254，两个数完全不同；
>   账单信封里的 `TotalDosage` 也不可用，它是各包额度上限之和。
> · **Qoder** 走 `GET /api/v2/quota/usage`，余额 = `userQuota.remaining + addOnQuota.remaining`
>   （口径参照 [qoder2api-hub](https://github.com/shuishuipingan/qoder2api-hub) 的
>   `qoder_accounts.Account.fetch_credits`）。注意该接口返回的是**扁平结构**（没有 code/data 信封），
>   且 `total` 字段是额度上限不是余额。

### 界面

6 个页面：总览（账号卡片 + 14 天趋势 + 最近失败）、账号、任务与调度、运行记录、日志、设置，
外加运行详情、扫码纳管、Qoder 登录三个弹层/独立页。

> 截图**不随仓库分发**（面板截图必然带真实账号标识，包括手机号、UID、令牌首尾片段）。
> 需要看界面：本机 `start.bat` 起来直接看，或对着 Mock 上游跑（见第 4 节）。

## 2. 快速开始（本机）

**Windows：双击 `start.bat`，就这一步。**

它会依次完成：

```
[1/4] Python environment ready.          ← 首次会自动建环境（约 1 分钟）
[2/4] Configuration ready.
[3/4] Checking accounts from desktop clients...
  [OK] 已添加账号「WorkBuddy 主账号」，连接正常（连续签到 6 天）
  [OK] 已添加账号「Qoder CN 账号」，连接正常（2 个活动，1 个可领）
[4/4] Starting panel...
       Login : admin / admin123
```

然后浏览器会自动打开 http://127.0.0.1:8000，用 `admin` / `admin123` 登录即可。

几个贴心处理：

- **重复双击不会启动第二个实例**，只会把浏览器再打开一次；
- 每次启动都会顺手刷新一次令牌（`已更新账号` 而不是重复添加）；
- 桌面端没装或没登录时，只提示「跳过」，不影响面板启动，可在面板里手动加账号；
- **日志默认只看结果行**：空跑（今日已签 / 旅行名额用完 / 宠物还在路上）和「开始执行」收进 `DEBUG`，日志页勾「显示空跑/调试」才显示——签到记录不会被 30 分钟一次的轮询顶掉；
- 详细导入日志在 `data/logs/import.log`。

**Linux / macOS：** `./start.sh`（不含自动导入，需先跑一次导入器，见第 3 节）。

> 本机调试只需 1 核 1G；常驻内存约 120MB。

### 常驻运行（长期自动签到请务必做）

`start.bat` 是前台窗口，关掉窗口调度器就停了——08:30 那次签到会直接丢（misfire 宽限 6 小时，过了当天不再补）。注册成计划任务即可常驻：

```powershell
powershell -ExecutionPolicy Bypass -File tools\install-autostart.ps1
```

它注册「登录时启动」的计划任务 `CheckinHubPanel`，用 `pythonw.exe -m app.serve` 无窗口运行，崩溃后 1 分钟自动重启（最多 3 次）；取消加 `-Remove`。

> 无窗口运行时解释器的 stdout/stderr 会重定向到 `data/logs/panel.log`，端口占用、启动失败这类问题在那里查。

## 3. 添加账号

### 方式一：扫码添加（推荐，云端可用）

面板「账号 → 扫码添加」会直接出二维码，用手机扫码、在官方页面确认登录即可，**不需要打开客户端导出凭证**。
适合云服务器：以前要在本机装客户端、导 JSON、再粘贴，现在扫一下就完事。

接口链路（三步，state 5 分钟有效、一次性）：

```
POST /v2/plugin/auth/state?platform=CLI   → {state, authUrl}   # authUrl 画成二维码
GET  /v2/plugin/auth/token?state=...      → 未扫时 code=11217；成功给 accessToken + refreshToken
GET  /v2/plugin/login/account?state=...   → uid / nickname
```

### 方式二：一键导入（默认已包含在 start.bat 里）

`start.bat` 每次启动都会自动导入。如果你**已经在用面板、只是想刷新令牌**（比如收到“需重新登录”告警），
单独双击 **`import-account.bat`** 即可——它只导入不启动服务，刷新页面就能看到新状态。

它做的事：读本机登录态 → 让客户端自己交出密钥解密（WorkBuddy 5.6+ 的 `$wbEncrypted`）→ DPAPI 解密（Qoder）→ 直接写入面板数据库 → 调一次接口校验。**重复执行等于更新令牌**，不会重复添加账号。

```
[1/2] WorkBuddy ...
正在向 WorkBuddy 客户端索取解密密钥…
✅ 已添加账号「WorkBuddy 主账号」，连接正常（连续签到 6 天）
[2/2] Qoder ...
✅ 已添加账号「Qoder CN 账号」，连接正常（2 个活动，1 个可领）
```

> 前提：面板和客户端在**同一台机器**上。面板跑在云服务器时用方式二。

### 方式三：手动粘贴（云服务器场景）

在登录了客户端的机器上执行：

```bash
python tools/import_workbuddy.py --out wb.json
python tools/import_qoder.py --out qoder.json
```

把输出的 JSON 整段粘贴到面板「账号 → 添加账号」（WorkBuddy 也可以只粘 `accessToken`）。

### 解密是怎么做到的

WorkBuddy 5.6+ 把 `accessToken` 存成 `{"$wbEncrypted":1,"envelope":"..."}`（AES-256-GCM），密钥由构建期注入、只在客户端运行时内存里，磁盘上没有独立密钥文件。

导入器不逆向算法，而是用 `ELECTRON_RUN_AS_NODE` 让**官方客户端自己**把 `atRestSecretKey` 吐到标准输出（仅进程间传递，不落盘），再按客户端的派生规则（`key=sha256(secret)`、`keyId=sha256(key)[:16]`、固定 AAD 结构）解密。Qoder 侧是标准的 DPAPI + AES-256-GCM。

三条兜底路径：

| 场景 | 做法 |
|---|---|
| 客户端装在非标准路径 | 设环境变量 `WORKBUDDY_CLIENT_EXE=D://path//WorkBuddy.exe` |
| 客户端版本改动导致取密钥失败 | `python tools/import_workbuddy.py --scan capture.har`，从抓包文件里挑出 Bearer |
| 什么都没有 | `python tools/import_workbuddy.py --token eyJ...` 手动粘贴 |

抓包取令牌的做法（5 分钟）：装 mitmproxy → 系统代理指向 `127.0.0.1:8888` → 客户端点一次签到 → 在 `copilot.tencent.com` 请求头复制 `Authorization: Bearer eyJ...`，或在 mitmweb 里导出 HAR 交给 `--scan`（只挑带 `sub` 的 JWT，回显打码后的结果）。

### 凭证生命周期：登录一次，之后不用管

签到走的是**纯 HTTP 接口调用**，不是 UI 自动化。服务器上不装客户端、不开浏览器、不模拟点击，所以**每次签到都不需要唤起任何程序**。

| 阶段 | WorkBuddy | Qoder |
|---|---|---|
| 首次（必须在本机） | 客户端处于登录态，双击 `import-account.bat` | 同左 |
| 日常（每天自动） | 服务器进程直接调接口，客户端可以关掉、卸载、换机器 | 同上 |
| 续期 | 剩余不足 3 天自动用 refreshToken 续期（每小时巡检，新令牌加密回写）；也可点账号卡「续期」手动刷。refreshToken 也失效时才需重新扫码 | deviceToken 剩余不足 7 天自动保活刷新（每小时巡检）；任务执行前 12h 内也会先刷一次 |
| 需要再动手的时机 | JWT 过期（面板「有效期」倒计时，社区反馈约 90 天） | refreshToken 也失效时 |
| 失效表现 | 账号状态变「需重新登录」+ 推送告警，不会静默断签 | 同左 |

两个会让令牌提前失效的动作，注意避开：

- **在桌面端重新登录 / 切换账号**，旧会话可能被服务端作废，需要重新导出；
- **同一账号在客户端与云端同时频繁刷新 Qoder 令牌**，会互相顶掉。本项目按「云端单点、不装客户端」设计，只要不在两台机器上同时跑同一份凭证就没问题。

面板会在令牌剩余 7 天内把有效期标红，`need_login` 时立即推送，所以正常情况下你是被提醒去重导，而不是发现断签。

### Qoder 登录添加（手机号 + 验证码，无需导出凭证）

面板「账号 → **Qoder 登录**」：选区域 → 获取授权链接 → 手机扫码（或电脑浏览器打开）→
在**官方授权页**用手机号 + 短信验证码登录并确认 → 面板自动建号。
协议与官方 qodercli 同流程（PKCE + nonce，`/api/v1/deviceToken/poll` 轮询，会话 5 分钟有效），
短信验证码那一步发生在官方页面，面板不经手、也拿不到。

### 国际版（Qoder）与国内版（Qoder CN）

命名约定：**国际版叫 Qoder，国内版叫 Qoder CN**（账号卡带徽标，`/api/credits` 返回 `realm`/`realm_name`）。

| 区域 | openapi 基址 | 客户端数据目录 |
|---|---|---|
| `Qoder CN`（cn） | `https://openapi.qoder.com.cn` | `%APPDATA%\com.qodercn.app.stable` |
| `Qoder`（intl） | `https://openapi.qoder.sh`（备用 `center.qoder.sh`、`openapi.qoder.com`） | `%APPDATA%\com.qoder.app.stable` |

**签到语义（两个区域统一）**：「每日签到」任务由**「每天领 100」活动**承接——
Qoder CN 领 100 **积分**、Qoder 领 100 **Credits**，同一任务按账号区域自动适配文案与基址。
官方业务日 **10:00（UTC+8）刷新**领取资格，任务窗口设在 10:05–12:55、每 10 分钟一跑；
活动平台整体不可达时，国内版回退老的 sash `daily-check-in`（国际版没有该接口，如实失败）。
原先与之重复的「每日 Credits」任务已自动停用（历史运行记录保留）。

**国际版活动接口（重要，已跑通）**：国际版服务端只给**完整桌面端身份头**的请求下发
「每天领 100 Credits」。缺任何一块都会**静默少活动**（不报错、列表里就是没有）。

> ★ **最隐蔽的坑（2026-10-30 用客户端日志定位）**：活动列表必须带
> `?clientType=10&forceRefresh=true`。不带 `forceRefresh` 时服务端返回**缓存视图**：
> 客户端里明明显示「可领取」，接口却只回几条已领的营销活动 —— 这正是国际版
> 「永远待下发」的真正原因。客户端自己也缓存 300 秒、靠 `forceRefresh` 强制刷新
> （日志 `活动状态使用缓存 {"expiresInMs":298087}`）。

请求头按客户端日志逐字节对齐（比 [qoder-trae-workbuddy-switch](https://github.com/NextAgentX/qoder-trae-workbuddy-switch)
的 `desktop_headers` 更贴近官方 —— 客户端**不发** `X-Request-ID`/`X-Machine-ID`/
`X-Session-ID`/`Origin`/`Referer`）：

```
Accept: application/json              User-Agent: Qoder
Authorization: Bearer <token>
Cosy-ClientType: 10                   Cosy-Version: 0.4.3
Cosy-MachineOS: x86_64_win32          Cosy-MachineHostname: DESKTOP-QODER
Cosy-MachineId: MD5("machine:<uid>")  ← 派生，不是客户端 auth.machine-id
Cosy-MachineToken / MachineCode / MachineType ← 官方 UMID 组件 runtime-info prod
```

`runtime-info` 组件来源（自动，无需手工）：本机客户端 `resources/umid` →
qodercli 解压组件 → 云服务器上从 npm 拉官方 `@qoder-ai/qodercli`（sha512 校验）
抽 Linux ELF。身份缓存 30 分钟；组件不可用时回退「派生分支」（不冒充机器身份，
如实少活动），**不阻断任务**。

**实测结果**（2026-10-30/31，真实国际版账号，**未用任何代理**）：
活动列表 3 条，其中 `act-...-489 CLAIM_BEFEFIT / CREDITS / 100 / CLAIMABLE` →
任务结论 `签到成功 +100 Credits（每日领取活动）` → **余额 500 → 600**；
重跑返回 `今日已签到（活动已领取，合计 100 Credits，10:00 后刷新）`，幂等无重复。

### 官方活动规则与结果分类（对照 qoder2api-hub 的 `_diag_campaign.py`）

**官方规则**（原样摘录，决定了一切判据）：

- 每日 100 Credits：**每账号每轮限领一次**，每日 **10:00（UTC+8）刷新，错过不补**；
  30 天有效；**仅桌面端可领**；
- 服务端按「人」去重：同一身份下的其它账号本轮领过，这个号会拿到
  `SAME_PERSON_ALREADY_CLAIMED`，**连活动都不显示**；
- 常见「没有活动」的原因：账号不在定向内（服务端按账号/风控决定）、**虚拟机环境**
  （`runtime-info` 会回报 `isVm`，虚拟化计入风险画像）、只注册了网页号没登录过
  桌面客户端、成就未完成。

**领取结果分类**（照抄 [qoder-trae-workbuddy-switch](https://github.com/NextAgentX/qoder-trae-workbuddy-switch)
的 `interpret_claim`，已纳入回归）：

| 上游形态 | 我们的处理 |
|---|---|
| HTTP **409** / `ALREADY_CLAIMED` / `REPLAYED` | **已领取**（幂等确认），不算失败 |
| `SAME_PERSON_ALREADY_CLAIMED` / `status=BLOCKED` | 「**同人已领取**」，不算失败（注意它含 “ALREADY”，判定顺序必须先于幂等） |
| `REDEMPTION_CODE_OUT_OF_STOCK` | 「今日名额已发完（10:00 刷新）」 |
| `ACHIEVEMENT_NOT_COMPLETED` / `CAMPAIGN_NOT_ACTIVE` / `RISK_BLOCKED` / `RISK_DEPENDENCY_UNAVAILABLE` | 各自中文原因，非致命 |
| `status=CLAIMED` 且无 `redemptionCode` | 发放「确认中」——**delta 用官方余额差，不用面额** |

### 自愈机制（六层，全部有回归）

任务失败大多是「某一层临时坏了」，能自愈的绝不留给人工：

| 层 | 触发 | 动作 |
|---|---|---|
| **基址** | 连接失败 / 404·405·410 | 换下一个候选基址；同一基址 404 **原样重发一次**再下结论（issue #66），成功的基址记忆优先 |
| **令牌** | 任意请求 401 | 用 `refresh_token` 就地换新令牌重试；**领取时 401 也走这条**（重建 Authorization，保留自愈换来的 `cosy-*`）；刷新失败才判 `need_login` |
| **身份来源** | 活动列表里**没有可领的每日活动**，且 `showCampaign≠false` | 固定身份 ⇄ 云端组件身份**换源重试一次**；成功则记住偏好（下次先用能用的那个），同账号 10 分钟内只换一次源 |
| **身份组件** | 内置 `runtime-info` 跑不起来 | 删掉组件 + manifest，从 npm 重装一次再试；仍失败则退回派生分支（不冒充身份） |
| **领取结果** | 409 / 同人已领 / 名额发完 / 风控 | 按上游语义分类（见上表），不误报失败、不谎报成功 |
| **记账** | 领取返回成功 | 前后各查一次官方余额，`delta` 记**实际到账**；面额与到账不符时消息写明「发放确认中」 |

自愈动作都会在「日志」页留一条 INFO（如 `Qoder 身份自愈：intl 账号 xxx 换用云端组件身份后可用`），
失败路径写 WARNING/DEBUG，方便回溯是哪一层出的问题。

**领取前后各查一次 `/api/v2/quota/usage`**：`delta_credit` 记**实际到账**，
面额与到账不一致时会在消息里写明「官方发放确认中（面额 X，当前到账 Y）」——
国际版实测出现过「返回 CLAIMED 但余额没动」，面额绝不能当到账。

另外：活动列表遇到 404/405/410 会**原样重发一次**再下结论（参考项目 issue #66：
单次 404 多是网关抖动，而「本区没有活动平台」会触发兜底，是强结论）。

### 设备身份：云端自造 vs 本机真实身份（证据与现状）

先说结论：**国际版每日活动是否可见，主要取决于「活动有没有下发到你这台机器」，
而不是「身份是不是本机导出的」**。我们实测到两个互相矛盾的时刻，如实记录：

| 时间 | 身份来源 | 结果 |
|---|---|---|
| 10-31 10:12 | 云端 ELF 组件（`isVm=true`） | 1 条活动，**没有**每日活动 |
| 10-31 10:12 | PC 客户端导出 | 2 条活动，`CREDITS/100/**CLAIMABLE**` → 领取成功 |
| 11-01 13:30 | 云端 ELF 组件（`isVm=true`） | 2 条活动，每日活动 **CLAIMED**（当天已领） |

并且实测 `runtime-info` 的 env 参数（`3` / `4` / `prod` / `0`）**产出的 machineToken
完全一致**——它只是环境码，不改变设备身份（qoder2api-hub 的 `_install_umid.py`
也确认「机器身份是机器级的，无法按账号拆分」）。

⇒ 目前更可信的解释是**活动分阶段下发**（不同设备/账号在不同时间收到），
而不是「云端身份被永久拒绝」。这一点仍需一次「只用云端身份领取」的干净实验
才能定论（见下）。

**仍然建议固定 PC 身份**（已验证能让云端在 11:05 领到，且省去分阶段下发的运气）：

```bat
tools\sync_qoder_identity.bat          # 改好里面的面板地址/口令后双击
```

它会调用 `tools/export_identity.py --push`：本机跑 `runtime-info prod` 取身份 →
写到 `data/qoder-umid/identity-<realm>-<uid>.json` → POST `/api/qoder/identity` 到云端。
云端 `qoderumid.prepare()` 会**优先**用这份固定身份（不再跑本地组件）。

> ⚠️ **一台设备每天只能有 1 个国际版账号领到**（qoder2api-hub 实测：服务端按设备
> 去重）。多开国际版账号要么用不同机器，要么接受「只有一个号能领」。

> 想全自动：任务计划程序 → 每天 09:50 跑那个 .bat（Qoder 业务日 10:00 刷新，
> 面板的签到任务 10:05 起每 10 分钟重试到 13:00）。

> ✅ **已闭环（2026-11-01 实测）**：固定 PC 身份后，云端在 **11:05** 自动领到
> `签到成功 +100 Credits（每日领取活动）`，余额 700 → **800**，上游 `quota/usage`
> 交叉验证一致（addOn 800/800）。注意时间线：10:05–10:55 六次都是「暂无可领取
> 活动（共 1 项）」——**每日活动到 11:00 才下发**（比 10:00 的业务日刷新晚约一小时），
> 所以重试窗口必须留够；面板默认窗口已放宽到 **10:00–13:00**（领取是幂等的，
> 多试几次没有副作用）。

> 早期曾把「看不到活动」误判为出口 IP 问题（并加了 `QODER_INTL_PROXY`）。
> 真实原因是**请求头 + forceRefresh + 设备身份**三件事，与出口无关。

**令牌续期**：Qoder 用 `refresh_token`（`drt-`）换 `device_token`（`dt-`，30 天）+
新的 `drt-`。请求体必须是 `{"refresh_token": …}`（snake_case），响应字段是
`device_token` / `refresh_token` / `expires_at`（ISO 字符串）—— 字段名对不上会
400 或「缺少 token 字段」，这一点已按官方实测响应修好并纳入回归。

## 4. 用 Mock 跑通全流程（不需要真实令牌）

```bash
# 终端 A：启动假上游
python -m uvicorn tools.mock_api:app --port 8899

# 终端 B：把面板指到假上游
set WB_BASE_URL=http://127.0.0.1:8899
set QODER_BASE_URL=http://127.0.0.1:8899
python -m app.serve
```

浏览器打开 `http://127.0.0.1:8899/demo/token` 拿到演示凭证，粘贴进面板即可体验完整流程：
签到 +100 → 再次执行显示「已跳过」→ 派出宠物 → 25 秒后「领取 +6」→ Qoder 领取 +100 → 再执行显示「今日已领取」。

这条链路已用于本项目的自动化验证。

### 自动化回归（改完代码先跑这个）

两个脚本都**不碰真实账号**，自己起 Mock、自己建临时数据目录：

```bash
.venv\Scripts\python.exe tests\e2e.py            # 全链路：签到/旅行/领取/扫码/401 自愈/身份换源/渲染
python tests\test_migration.py                   # 老库升级：造历史 schema，验证 _migrate() 补列+回填
```

- `tests/e2e.py` 固定随机端口起 `tools/mock_api.py`，用 `TestClient` 打真实路由，最后打印
  `N/N 通过`（进程退出码 0=全绿）；任何一条断言的描述就是失败原因。
- `tests/test_migration.py` 拿当前 schema 反推历史结构（DROP 掉后来新增的列）再跑升级，
  专门防「补列/回填顺序」这类只有老库才会炸的 bug（2026-10-30 真实踩过）。
- `tests/ui_seed.py` 只用来生成 README 里的截图数据，不参与回归。

## 5. 任务与调度

| 任务 | 默认 cron | 窗口 | 说明 |
|---|---|---|---|
| WorkBuddy 每日签到 | `30 8 * * *` | 00:05–23:30 | 每天一次，失败按退避补 |
| WorkBuddy 宠物旅行 | `*/30 * * * *` | 全天 | 状态机驱动，`traveling` 时不做动作 |
| Qoder 每日签到（领 100 积分 / Credits） | `5,15,25,35,45,55 10-13 * * *` | 10:00–13:00 | 唯一的 Qoder 每日任务，按账号区域自动适配：Qoder CN 领 100 **积分**、Qoder（国际版）领 100 **Credits**；活动约 10:00 开窗（国际版实测可能 11:00 才下发），未开则下轮再试；活动平台故障时国内版回退 sash 老接口 |

- cron 在面板「任务与调度」里改，保存后**立即热生效**，不用重启。
- 执行窗口外的任务不会被触发，也不会进补偿队列。
- 补偿退避：第 1 次失败后 10 分钟、第 2 次 30 分钟、第 3 次 2 小时（不超过窗口）。
- 错过触发时间有 **6 小时** misfire 宽限：面板晚开/重启也不会把当天的 08:30 签到丢掉（签到接口幂等，补跑无副作用）。

除上面三个业务任务，调度器还常驻四个内务作业（不可在面板里改）：

| 作业 | 间隔 | 作用 |
|---|---|---|
| 补偿队列轮询 | 1 分钟 | 取 `retry_queue` 里到期未跑的重试 |
| 积分余额刷新 | 10 分钟（`WB_CREDITS_REFRESH_MIN`） | 没人开页面时余额也往前走，并给上涨记流水 |
| 令牌续期 / 保活 | 60 分钟（`WB_RENEW_CHECK_MIN`） | WorkBuddy 不足 3 天、Qoder 不足 7 天自动换新 |
| 数据清理 | 每天 00:10 | 删 90 天前的 `runs` / `run_logs` / 已结束的重试 |

### 「今日新增」是怎么算的

官方**没有**可读的余额流水接口（`credits-summary` / `credits-heatmap` 是 AI 用量，
本例全为 0；网页版逐包明细 `me/usages/big_model_credits` 只认浏览器 Cookie，令牌直连 404）。
所以面板用**官方余额本身的当日差额**：

```
今日新增 = 当前余额 − 当日开盘余额
```

- 当日开盘余额 = 业务日第一次读到余额时记下的「上一次读到的余额」（即昨收）；
  跨天自动滚动，不需要人工维护。
- 这样能覆盖面板之外的增减（在客户端手动领的、上游补发的），不会漏。
- 升级当天的开盘价会用「当前余额 − 当天任务累计」和「当天最早的
  `积分余额 +N（prev → now）` 日志」两条线索取**更早**的那个来还原。
- 面板自己发起的领取累计另存一列，鼠标悬停「今日新增」即可看到对照
  （`官方余额差 · 任务累计 +N`）。

命令行兜底（可挂系统 cron）：

```bash
python -m app.cli list
python -m app.cli run all
python -m app.cli probe
python -m app.cli backup
```

## 6. 部署到云服务器

```bash
git clone <your-repo> checkin-hub && cd checkin-hub
cp .env.example .env
# 改 ADMIN_PASSWORD，并设置 MASTER_KEY=$(openssl rand -base64 32)
docker compose up -d --build
```

带域名时把 `.env` 的 `DOMAIN` 改成你的域名，Caddy 会自动申请证书；无域名时 `DOMAIN=localhost` 走内部自签。

**必须注意的三件事：**

1. `TZ=Asia/Shanghai` —— 容器默认 UTC，不设会让签到跨日判定错 8 小时。
2. **单 worker** —— 调度器在进程内，多 worker 会重复执行（镜像已固定 `--workers 1`）。
3. `data/` 必须挂载持久化 —— 里面有 SQLite、日志和 `master.key`。

### 两种发版方式

**A. 打包含数据（首次迁移用，最省事）**：双击 `package.bat` 生成 `deploy\checkin-hub.tar.gz`，
默认会把本地 `data/`（加密凭证 + `master.key`）一起打进去 —— 服务器起来就能直接签到，不用重新扫码。
这个包**等同于你的账号**，别传给别人、别上传到任何公开位置。干净包用 `package.bat --no-data`。

```bash
scp deploy/checkin-hub.tar.gz ubuntu@1.2.3.4:/tmp/
ssh ubuntu@1.2.3.4 'mkdir -p ~/checkin-hub && tar -xzf /tmp/checkin-hub.tar.gz -C ~/checkin-hub && bash ~/checkin-hub/deploy/deploy.sh'
```

`deploy.sh` 会自动生成 `.env`、随机管理员口令、构建镜像、等健康检查通过再打印访问地址。

**B. 从本机一条命令推上去**：`deploy/remote_deploy.py` 用 SSH 完成「检查 → 上传 → 部署」，
口令只从环境变量读，不落盘、不进命令行历史：

```bash
pip install paramiko          # 只有这个脚本需要，面板运行时不依赖
$env:SSH_HOST="1.2.3.4"; $env:SSH_USER="ubuntu"; $env:SSH_PASSWORD="..."
python deploy/remote_deploy.py all
```

> 升级（服务器已经在跑）时**绝不能**用带 `data/` 的包覆盖服务器：会把线上 `checkin.db` / `master.key`
> 换成你本地那份，令牌回退、运行记录丢失。升级要 `package.bat --no-data`，或解压时加
> `--exclude='data/*'`。完整操作手册见 [docs/deploy-tencent-cloud.md](docs/deploy-tencent-cloud.md)。

### 服务器怎么选

| 方案 | 成本 | 结论 |
|---|---|---|
| 腾讯云/阿里云轻量 2核2G 新客 | ¥68–118/年 | **推荐**：国内节点直连两家接口，延迟低、IP 干净 |
| Oracle Always Free ARM（4C24G） | 永久免费 | 技术上最强，但热门区抢不到容量、闲置可能被回收、需外币卡 |
| 群晖 / 软路由 / 树莓派 | 已有设备 | 家庭 IP 风控最友好，配 Tailscale 即可远程 |
| Render / Koyeb 免费档 | 免费 | **不推荐**：无流量会休眠，定时器随之停摆 |
| GitHub Actions | 免费 | 只能跑 CLI，没有面板；cron 延迟 5–20 分钟，60 天无提交会被禁用 |

## 7. 数据与备份

- 全部状态在 `data/checkin.db`（SQLite WAL）+ `data/master.key`。
- 面板「设置 → 下载数据库备份」可直接导出；迁移时**必须同时带走 `master.key`**，否则凭证解不开。
- 日志与运行记录保留 90 天，由每日 00:10 的清理任务自动裁剪。
- **`data/` 和 `.env` 都在 `.gitignore` 里**，不会被 push 到 GitHub。换机 / 重置系统前必须**单独**
  备份这三样：`data/checkin.db`、`data/master.key`、`data/qoder-umid/identity-*.json`
  （最后一个是国际版活动定向所依赖的机器身份）。只备份 db 不备份 key，凭证全部解不开。
- 备份/还原与「换机继续干」的完整清单见 **[HANDOFF.md](HANDOFF.md)**。

## 8. 安全说明

- 凭证字段用 AES-256-GCM 加密后入库，`MASTER_KEY` 只从环境变量或 `data/master.key` 读取，不进镜像。
- 页面只显示令牌首尾各 6 位；日志写入前统一脱敏（JWT 与长串都会被打码）。
- 面板默认绑定 `127.0.0.1`；公网部署请务必走 Caddy 反代 + 强口令，或加一层 IP 白名单。
- 默认口令是 `admin123`，**只在可信网络里可接受**。上公网前必须改掉，否则等于把凭证数据库送人。
- **不要把口令/服务器地址写进会被提交的文件**。含口令的本机脚本放 `*.local.bat`（已在 `.gitignore`），
  `tools/sync_qoder_identity.bat` 就是按这个约定读 `sync_qoder_identity.local.bat` 的。
- 口令一旦写进过 git 历史就等同于公开：换服务器、重置系统后建议连面板口令一起换掉。
- 本项目只应服务**你自己的账号**，不要做成多租户代签服务。

## 9. 已知限制

- 两个平台用的都是**未公开接口**，客户端升级可能改路径/字段，脚本必然会周期性失效。设计上已做「失败可见」：`need_login` 与连续失败都会推送告警，不会静默断签。
- WorkBuddy 5.6+ 的加密凭证无法离线解密（密钥只在客户端运行时内存中）。导入器不逆向客户端，只支持：明文令牌、抓包提取（`--scan`）、或你自行提供的密钥。
- 成长中心、开学季、抽奖等扩展玩法未纳入（接口未验证），`Provider` 层已预留扩展位：新增玩法只需在 `app/providers/workbuddy.py` 里加一个方法并注册任务。

## 10. 目录结构

```
checkin-hub/
├── app/
│   ├── main.py            # 路由 + API + SSE + 模板渲染
│   ├── runner.py          # 执行器：执行/落库/重试/补偿/通知
│   ├── scheduler.py       # APScheduler 封装：cron 热更新、补偿轮询、余额刷新、续期、清理
│   ├── db.py              # SQLite schema + 迁移（老库补列/回填）+ 任务种子
│   ├── config.py security.py notify.py cli.py serve.py
│   ├── credits.py         # 实时积分余额：TTL 缓存 + 当日开盘余额口径
│   ├── renew.py           # 令牌续期 / 保活巡检
│   ├── authflow.py        # 扫码纳管（state → token → account）
│   ├── qoderlogin.py      # Qoder 手机号+验证码登录（设备码 PKCE）
│   ├── qoderumid.py       # Qoder 设备身份（runtime-info 组件 / 本机固定身份 / npm 兜底）
│   ├── providers/         # 平台适配层：workbuddy.py / qoder.py / base.py / registry.py
│   ├── templates/         # 8 个页面 + base 布局
│   └── static/            # app.css app.js
├── tools/
│   ├── import_workbuddy.py  # 读客户端登录态 → 解密 → 落库（或 --out 导出 JSON）
│   ├── import_qoder.py      # DPAPI 解 Qoder 登录态
│   ├── import_all.py        # start.bat 调的串行导入器
│   ├── export_identity.py   # 导出/推送 Qoder 机器身份
│   ├── mock_api.py          # 本地假上游，联调与回归用
│   ├── _boot.py             # 缺依赖时自动切到 .venv 重跑
│   ├── install-autostart.ps1        # 注册计划任务常驻
│   └── sync_qoder_identity.bat      # 身份同步入口（口令读 *.local.bat）
├── tests/
│   ├── e2e.py             # Mock 上游全链路回归
│   ├── test_migration.py  # 老库升级回归
│   └── ui_seed.py         # 生成截图用的演示数据
├── deploy/                # Dockerfile / Caddyfile / deploy.sh / docker-entrypoint.sh / remote_deploy.py
├── docs/                  # design.md（方案设计）、deploy-tencent-cloud.md（部署手册）
│                          # screenshots/ 是本地截图目录，不进仓库（含真实账号标识）
├── data/                  # 运行时数据（gitignore：db、master.key、身份缓存、日志）
├── start.bat / start.sh / import-account.bat / package.bat
└── docker-compose.yml
```

## 11. 常见问题

**直接敲 `python tools\import_qoder.py --save` 报 `No module named 'httpx'`？**
那是用了系统 Python，而面板依赖只装在 `.venv` 里。现在脚本会**自愈**：检测到缺依赖就自动用 `.venv` 的解释器原地重跑，你会看到一行 `[提示] 当前解释器缺少 httpx，自动切换到项目虚拟环境重试…`，然后正常执行。
（想避免这行提示，直接用 `.venv\Scripts\python.exe tools\import_qoder.py --save`，或双击 `import-account.bat`——它本来就指定了 venv。）

**签到成功但积分没变？**
面板统计的是本次 `daily-checkin` 返回的 `credit` 增量。若上游返回 0，说明当天奖励已领或活动未发。

**Qoder 一直 `pending`？**
活动未开窗（约 10:00）或设备标识过期。前者等下一轮即可；后者需要重新导出凭证（`pending` 不会中断连续签到）。
国际版账号若本身没有发放类活动，也会一直是 `pending`（`暂无可领取活动（共 N 项）`）——这是账号资格问题，不是故障。

**WorkBuddy 报 401，但客户端还登录着？**
99% 是读到密文了。重新用导入器导出明文，或更新令牌。

**Qoder 令牌会不会和客户端打架？**
本项目按「云端单点运行、不装客户端」设计：到期前 12 小时刷新一次并立即落盘，不与客户端并发。如果同一账号还在桌面端活跃使用，建议把 `QODER_REFRESH_MARGIN_H` 调小或关闭自动刷新，改为过期后手动重导。

**改了 `.env` 不生效？**
`.env` 只在进程启动时读取；`docker compose restart` 或重启进程即可。

## 12. 环境变量

`.env` 只在进程启动时读一次（`app/config.py` 自带极简 dotenv 解析，已存在的系统环境变量优先）。

**常用（`.env.example` 里都有）**

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | 面板登录名 |
| `ADMIN_PASSWORD` | 空 → `admin123` | 填了就**每次启动强制重置**为该值；留空则以面板里改的为准 |
| `MASTER_KEY` | 空 → 自动生成 `data/master.key` | 凭证加密密钥。换机/重置必须带走，否则凭证全解不开 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口（容器内由 compose 覆盖为 `0.0.0.0`） |
| `TZ` | `Asia/Shanghai` | 业务日与跨日判定，错了签到会差 8 小时 |
| `CHECKIN_PROXY` | 空 | 出站代理；**程序默认忽略系统 `HTTP_PROXY`**，要代理只能在这里显式配 |
| `QODER_REFRESH_MARGIN_H` | `12` | Qoder 令牌剩余不足几小时就提前刷新 |
| `DOMAIN` | 空 | Caddy 站点地址：留空走 `:80`，填域名自动申证书 |

**调优（默认值一般够用，需要时写进 `.env`）**

| 变量 | 默认 | 说明 |
|---|---|---|
| `WB_CREDITS_TTL` | `60` | 一次真实余额查询的缓存秒数 |
| `WB_CREDITS_REFRESH_MIN` | `10` | 余额后台刷新间隔（分钟） |
| `WB_RENEW_BEFORE_DAYS` | `3` | WorkBuddy 剩余不足几天自动续期 |
| `QODER_KEEPALIVE_DAYS` | `7` | Qoder deviceToken 剩余不足几天自动保活 |
| `WB_RENEW_CHECK_MIN` | `60` | 续期巡检间隔（分钟） |
| `QODER_INTL_PROXY` | 空 | 只给国际版请求用的代理，其它平台仍直连 |
| `QODER_DESKTOP_VERSION` | `0.4.3` | 发 `cosy-version` 头 |
| `QODER_MACHINE_HOSTNAME` | `DESKTOP-QODER` | 发 `cosy-machinehostname` 头 |

**调试 / 运维（一般不出现在 `.env.example`）**

| 变量 | 默认 | 说明 |
|---|---|---|
| `CHECKIN_DATA_DIR` | `<repo>/data` | 数据目录（测试与容器都靠它隔离） |
| `WB_BASE_URL` | `https://copilot.tencent.com` | WorkBuddy 基址，联调时指向 Mock |
| `WB_BILLING_URL` | `https://www.codebuddy.cn` | 余额/资源包账单基址（与 chat 域不同） |
| `WB_AUTH_UA` | `WorkBuddy/2.137.1 …` | 授权请求的 User-Agent |
| `QODER_BASE_URL` | 空 | 一次覆盖所有区域基址（Mock 用） |
| `QODER_CN_URL` / `QODER_INTL_URL` | 见区域表 | 只覆盖某个区域的基址 |
| `WORKBUDDY_CLIENT_EXE` | 空 | 客户端装在非标准路径时指定 |
| `SSH_HOST` / `SSH_USER` / `SSH_PORT` / `SSH_PASSWORD` / `REMOTE_DIR` | — | 只给 `deploy/remote_deploy.py` 用 |
| `PIP_INDEX` / `PIP_TRUSTED_HOST` | 腾讯云镜像 | 只给 `docker build` 用 |

---

免责声明：本项目为个人自动化工具，依赖第三方未公开接口，可能不符合其服务条款；接口变更、账号风控等风险由使用者自行承担，请仅用于自己的账号。