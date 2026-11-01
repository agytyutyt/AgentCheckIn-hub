# CheckinHub · 多平台签到与积分领取自动化平台

> 方案设计 v1.0 · 2026-10-27
> 覆盖：WorkBuddy（签到 + 积分 + 旅行任务）、Qoder（签到 + 每日 Credits 领取）

> ⚠️ **这是 2026-10-27 的方案设计与上游调研记录，保留原貌不做维护**：
> 文中「Qoder 每日 Credits 独立任务」「10:05–11:00 补拉」「HTMX」等描述都已被实现推翻
> （现在 Qoder 只有一个签到任务、窗口到 13:00、前端是原生 JS）。
> **当前实际行为以 [README.md](../README.md) 和代码为准**，本文只用于看「当时为什么这么选」。

---

## 0. 结论摘要

| 维度 | 结论 |
|---|---|
| 后端语言 | **Python 3.12**（FastAPI + APScheduler + SQLite） |
| 前端 | **Jinja2 + HTMX + 少量原生 JS**，零构建链，CDN 不外依赖（vendor 本地化） |
| 调度 | 进程内 APScheduler（cron 存 DB，Web 可改）+ 每日 CLI 兜底 | 
| 部署 | 单容器 Docker Compose + Caddy 自动 HTTPS，`TZ=Asia/Shanghai` |
| 免费服务器 | 首选 **国内轻量新客机**（¥68–118/年，1 核 1G 足够）；纯零成本选 **Oracle Always Free ARM**（有回收与注册风险） |
| 最大技术风险 | **凭证加密**：WorkBuddy 5.6+ 的 accessToken 是 `$wbEncrypted` 信封，云端无法解密 → 必须做本地导入器 |

**一句话架构**：Windows 本地一次性解密导出凭证 → Web 面板录入 → FastAPI 单进程调度 → Provider 适配层直连两家官方接口 → SQLite 落库 → 面板展示状态矩阵与实时日志。

---

## 1. GitHub 同类项目调研

### 1.1 已找到的项目（按相关度排序）

| # | 项目 | 形态 / 技术栈 | 核心思路 | 可复用部分 | 局限 |
|---|---|---|---|---|---|
| 1 | [xmgzxmgz/workbuddy-checkin-qinglong](https://github.com/xmgzxmgz/workbuddy-checkin-qinglong) | Python 脚本 + 青龙面板，仅依赖 requests | 直连 `copilot.tencent.com` 官方接口，Token 走环境变量；签到先查状态再签；宠物旅行用状态机 `idle/traveling/arrived` | **接口清单与状态机**（最有价值）；幂等判定（code=10001 视为已签、claim 返回 400 `no unclaimed` 视为已领）；JWT 解 `sub` 拿 `X-User-Id` | 无 UI；Token 手动复制（新版客户端复制到的是密文会 401）；无重试与补偿 |
| 2 | [xz0609/workbuddy-checkin-ql](https://github.com/xz0609/workbuddy-checkin-ql) | Python，青龙 + auths/ 目录多账号 | **Token 永续**：refresh 轮换后回写 `auths/{uid}.json`；把签到/成长中心/玩法/兑换拆成子命令（`login/growth/query/refresh/status/claim/all`） | **Token 续期落盘机制**；子命令拆分方式；5.6.x 加密凭证的本地解密思路（`ELECTRON_RUN_AS_NODE` 向客户端要密钥） | 功能面很宽（33 项）导致脚本重；无 UI |
| 3 | [Cat-zaizai/ZaiZaiCat-Checkin](https://github.com/Cat-zaizai/ZaiZaiCat-Checkin) | 多平台签到合集 + `import_accounts.py` | 自动探测本机客户端登录态并导入，兼容 5.5（`state.vscdb` + DPAPI）与 5.6+（`workbuddy-desktop.info` + `$wbEncrypted`）两代格式 | **账号导入器**的分版本探测逻辑；多账号目录约定 | 面向 CLI 用户，无服务端 |
| 4 | [wallechfox/qoder-checkin](https://github.com/wallechfox/qoder-checkin) | 纯标准库 Python，零第三方依赖 | Qoder 无公开登录接口 → 本地 `01_extract.py` 一次性提取 `Cosy-*` 设备标识 + token；服务端 `02_checkin.py` 每日跑 | **8 个 Cosy-* 设备头的取值与优先级**；`campaigns → claim` 流程；**语义化状态码** `claimed/already/pending/login_required/error`；claim 幂等 | 设备标识过期后需回 Windows 重跑导入 |
| 5 | [L0NE-6/Qoder-Daily](https://github.com/L0NE-6/Qoder-Daily) | Python + GitHub Actions | 双地区设备身份（CN / 国际站）；活动 **10:00(UTC+8) 开窗**，未开不落已签、后续轮次补拉 | **"开窗补拉"补偿策略**（我们直接采纳为调度设计）；多端点容错 | 无 UI，状态落文件 |
| 6 | [xiangsir4088/signdock](https://github.com/xiangsir4088/signdock) | Rust + Tauri 2，Windows 托盘 | 同一需求域的桌面形态：**仅提醒 / 全自动双模式**；支持 WorkBuddy / Trae / Qoder / 秒哒 | 产品思路：**"提醒模式"作为风控保守选项**；托盘常驻心智 | 桌面端，不能云端定时 |
| 7 | [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin) 8.7k★ | Python 框架 | 通用签到框架范式：**Provider 插件 + 统一通知网关 + 多部署后端**（Docker/青龙/群晖） | **架构范式**（我们照此抽象 Provider）；通知渠道实现 | 目标站点列表与我们无关 |
| 8 | [whyour/qinglong](https://github.com/whyour/qinglong) 面板 | Node + Docker | 现成的「脚本 + cron + Web 面板 + 通知」全家桶 | 产品形态参照（任务/日志/环境变量三件套） | 面板是通用容器管理，不针对本需求，无法展示"签到状态矩阵"这种领域视图 |

### 1.2 关键判断：为什么自研而不是直接用青龙

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 直接上青龙 + 现成脚本 | 0 开发量，1 小时可用 | 面板是通用任务管理器，看不到"今天谁签了/谁没签/积分多少/宠物在路上"；多账号要拼环境变量字符串；无补偿队列；脚本各自为战 | 仅作为**应急兜底** |
| 自研 Web 平台 | 领域化看板、可读日志、可配置调度、失败告警 | 需要开发 | **主方案** |
| 混合（推荐） | 平台提供 UI + 调度；同时保留 CLI 子命令，可被青龙/GitHub Actions 二次调度 | 略增设计成本 | **采纳** |

### 1.3 我们比现成项目多做的三件事

1. **领域化状态矩阵**：账号 × 任务 × 今日状态的单屏视图，而不是翻日志。
2. **三层重试 + 补偿队列**：现成脚本基本只做"幂等跳过"，没有"失败了在窗口期内自动补"。
3. **凭证生命周期管理**：过期时间入库、到期前告警、一键重新导入，而不是 401 了才发现断签。

---

## 2. 关键接口事实（已核实，作为实现依据）

### 2.1 WorkBuddy（`https://copilot.tencent.com`）

| 用途 | 方法 | 路径 | 备注 |
|---|---|---|---|
| 签到状态 | **POST** | `/v2/billing/meter/checkin-activity-status` | ⚠️ **只接受 POST**，GET 返回 404 |
| 执行签到 | POST | `/v2/billing/meter/daily-checkin` | 返回 `credit`、`streak_days` |
| 旅行状态 | GET | `/activity/growth/buddy/travel/status` | `state` / `daily_limit_reached` / `arrive_at` / `server_now` |
| 旅行地点配置 | GET | `/activity/growth/buddy/travel/config` | 取 `locations[].id` / `code` / `duration_hours_*` |
| 派出宠物 | POST | `/activity/growth/buddy/travel/depart` | body `{"location_id": N}` |
| 领取旅行积分 | POST | `/activity/growth/buddy/travel/claim` | 空闲时返回 400 `no unclaimed`（视为已领） |

公共请求头：`Authorization: Bearer <accessToken>`、`X-User-Id: <JWT.sub>`、`Content-Type: application/json`。
业务码：`code=0` 成功；`code=10001` 今日已签（视为成功）；`401` 登录态失效。

旅行状态机：`idle --depart--> traveling --(arrive_at 到点)--> arrived --claim--> idle`

### 2.2 Qoder（`https://openapi.qoder.com.cn`，国际站 `openapi.qoder.sh`）

| 用途 | 方法 | 路径 |
|---|---|---|
| 活动列表 | GET | `/sash/api/v1/me/campaigns` |
| 领取奖励 | POST | `/sash/api/v1/me/campaigns/{cid}/claim` |
| 刷新令牌 | POST | `/api/v1/deviceToken/refresh` |

请求头：`Authorization: Bearer <token>`、`User-Agent: Qoder/claim`，外加 8 个设备头 `Cosy-ClientType / MachineOS / MachineHostname / MachineId / MachineToken / MachineCode / MachineType / Version`。
筛选逻辑：`actionType == "CLAIM_BENEFIT"`，`claimStatus == "CLAIMABLE"` 才领；成功判定为返回 `status == "CLAIMED"`。

### 2.3 凭证来源（决定架构的关键约束）

| 平台 | 本地位置 | 加密方式 | 服务端能否自行获取 |
|---|---|---|---|
| WorkBuddy 5.6+ | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` | `accessToken` 为 `{"$wbEncrypted":1,"envelope":"..."}`，AES-256-GCM，密钥内嵌在客户端 | **不能**，必须本地解密 |
| WorkBuddy ≤5.5 | `%APPDATA%\WorkBuddy\User\globalStorage\state.vscdb` | DPAPI + AES-GCM | 不能 |
| Qoder CN | `%APPDATA%\com.qodercn.app.stable\Local State` + `auth.v1.dat` | DPAPI + AES-256-GCM | 不能 |

> **架构结论**：云端只存"已解密后的 token / refreshToken / 设备头"，不做任何逆向后端。
> 本地导入器是一个**独立的一次性小工具**（Windows 上跑），产出标准 JSON 供面板粘贴或上传。

---

## 3. 技术选型

### 3.1 后端语言：Python

| 候选 | 优势 | 劣势 | 判定 |
|---|---|---|---|
| **Python 3.12 + FastAPI** | ① 全部现成脚本都是 Python，协议细节可直接复用；② `cryptography` / DPAPI 生态成熟，导入器好写；③ APScheduler 内嵌调度最省心；④ 迭代快 | 镜像 ~180MB，常驻 ~120MB 内存 | ✅ **选它** |
| Node.js + Express/Fastify | 与前端同语言；SSE 顺手 | 要重写全部协议与解密逻辑，收益为零 | ❌ |
| Go + Gin | 单二进制、内存 ~20MB，最适合 512MB 免费实例 | 需从零复刻 6 个接口 + 凭证解密 + 多状态机，开发成本 ×3；生态无可复用代码 | ❌（若将来要压到 128MB 容器再考虑） |

### 3.2 前端：服务端渲染 + HTMX

| 候选 | 判定 |
|---|---|
| **Jinja2 + HTMX（推荐）** | 单页局部刷新、无构建链、`htmx.min.js` 14KB 本地化即可离线；面板类应用 90% 是表格与表单，SSR 最省 |
| Vue 3 SPA | 需要 node 构建链 + 打包产物，对 6 个页面是过度设计 |
| React + shadcn | 同上，且引入 Node 运行时体积 |
| Streamlit / Gradio | 5 分钟出原型，但样式不可控、无法做状态矩阵、长期运行内存偏高 |

样式：手写 CSS 变量（深/浅色自适应）+ 极简 grid，不引入 Tailwind 构建。

### 3.3 存储与调度

- **SQLite（WAL 模式）**：单机、低写入（每天几十条）、备份 = 拷文件。Postgres 属于过度设计。
- **APScheduler 3.x AsyncIOScheduler**：cron 表达式存 DB，Web 改动后 `reschedule` 热生效，无需重启。
  - ⚠️ **必须 `uvicorn --workers 1`**：多 worker 会让每个进程各跑一份调度器，导致重复执行。
- **tenacity**：HTTP 层指数退避。

---

## 4. 系统架构与模块划分

```
checkin-hub/
├── app/
│   ├── main.py                  # FastAPI 入口、lifespan 启动/关闭调度器
│   ├── config.py                # 环境变量（MASTER_KEY / TZ / 代理 / 通知）
│   ├── cli.py                   # 命令行子集：run <task> / probe / backup（供 cron 兜底调用）
│   ├── core/
│   │   ├── crypto.py            # AES-256-GCM 字段加密（凭证落库）
│   │   ├── security.py          # 登录会话、密码哈希（argon2）
│   │   ├── httpc.py             # 统一 httpx.AsyncClient：超时/UA/代理/限速/重试
│   │   ├── retry.py             # 重试策略（区分可重试 / 不可重试）
│   │   └── logging.py           # 结构化日志 + 令牌脱敏过滤器（关键）
│   ├── db/
│   │   ├── models.py            # account / task_def / run / run_log / daily_stat
│   │   └── session.py
│   ├── providers/               # ★ 平台适配层，新增平台只动这里
│   │   ├── base.py              # Provider 协议 + TaskResult 统一模型
│   │   ├── workbuddy.py         # checkin / claim_credits / travel_tick
│   │   ├── qoder.py             # checkin(=领取 campaigns credit) / refresh
│   │   └── registry.py
│   ├── scheduler/
│   │   ├── manager.py           # APScheduler 封装 + 任务注册/热更新
│   │   ├── runner.py            # 执行一次任务：取账号→循环→落库→通知
│   │   └── compensate.py        # 补偿队列（当日窗口内重跑）
│   ├── notify/                  # 企微机器人 / PushPlus / 钉钉 / Bark / SMTP
│   ├── api/
│   │   ├── auth.py  accounts.py  tasks.py  runs.py  dashboard.py  stream.py
│   └── web/
│       ├── templates/           # base / dashboard / accounts / tasks / runs / logs / settings
│       └── static/              # app.css app.js vendor/htmx.min.js
├── tools/
│   ├── import_workbuddy.py      # Windows 本地：读登录态→解密→导出 token.json
│   └── import_qoder.py          # Windows 本地：提取 Cosy-* + token → 导出
├── deploy/
│   ├── Dockerfile  docker-compose.yml  Caddyfile  backup.sh
└── tests/                       # 接口桩 + 幂等/重试用例
```

### 4.1 Provider 统一契约

```python
class TaskResult(BaseModel):
    status: Literal["success", "skipped", "retryable", "failed", "need_login"]
    message: str                 # 面向人的一句话
    delta_credit: int = 0        # 本次净增积分，用于看板汇总
    detail: dict = {}            # 脱敏后的原始响应，供详情页展示

class Provider(Protocol):
    key: str                     # "workbuddy" | "qoder"
    tasks: list[str]             # ["checkin", "credit", "travel"]
    async def probe(self, cred) -> AccountInfo: ...       # 校验令牌、拿昵称/uid/过期时间
    async def run(self, cred, task: str) -> TaskResult: ...# 统一入口，内部分派
```

这样做的收益：**新增平台（如 Trae、秒哒）只写一个 `providers/xxx.py`**，调度、重试、落库、看板、通知全部自动复用——这正是 Sitoi/dailycheckin 被验证过的范式。

### 4.2 数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| `account` | id, platform, name, uid, cred_enc(BLOB), token_exp_at, status, last_ok_at | `cred_enc` = AES-GCM 加密 JSON，仅服务端可解 |
| `task_def` | id, key, platform, task, cron, enabled, max_attempts, window | `window` 定义补偿窗口（如 00:00–23:59） |
| `run` | id, task_def_id, account_id, biz_date, attempt, status, started_at, finished_at, delta_credit, idem_key | `idem_key = sha1(biz_date+account+task)` 唯一索引，防重复 |
| `run_log` | id, run_id, ts, level, msg | 面板日志详情来源 |
| `daily_stat` | biz_date, account_id, task, credit | 看板折线图，避免每次聚合大表 |

---

## 5. 任务与调度设计

### 5.1 任务矩阵

| 平台 | 任务 | 建议频率 | 幂等语义 |
|---|---|---|---|
| WorkBuddy | `checkin` 签到领 100 积分 | 每日 1 次（08:30）+ 补偿轮 | 已签 → `skipped` 且算成功 |
| WorkBuddy | `credit` 成长中心/礼包领取 | 每日 2 次（09:00 / 21:00） | 无可领 → `skipped` |
| WorkBuddy | `travel` 旅行状态机 | **每 30 分钟**（旅行 2–6 小时归来） | `traveling` 不前推；`idle` 且未达上限才派 |
| Qoder | `checkin` 领取每日 Credits | 10:05 起，每 10 分钟补拉到 11:00 | `pending` 不落已签，下轮再试 |

> 关键洞察：**"旅行"和"Qoder 开窗"都不是一天一次的模型**。用一个"每日一次"的 cron 去跑，会必然漏领。
> 因此调度器要支持**同一任务的多轮窗口**（`cron` + `window_end`），窗口内失败自动进补偿队列。

### 5.2 三层重试模型

| 层 | 负责 | 策略 | 不重试的情况 |
|---|---|---|---|
| L1 HTTP 层 | 网络抖动 | `tenacity` 指数退避 1s/2s/4s，最多 3 次 | 4xx（含 401）、业务码错误 |
| L2 任务层 | 单次 run 内 | 最多 `max_attempts`（默认 3），间隔 30s / 120s | `need_login`、`skipped` |
| L3 调度层 | 跨时间窗 | 失败写入补偿队列，在 `window_end` 前按退避重排（10min → 30min → 2h） | 窗口已过 → 标记 `failed` + 立即告警 |

告警触发条件：① 连续 2 天同一任务失败；② `need_login`（令牌失效）；③ 令牌 7 天内到期。

### 5.3 时区与调度陷阱（必须写进 Dockerfile）

- 容器默认 UTC，`TZ=Asia/Shanghai` 必须显式设置，否则签到会偏 8 小时（跨日判定直接错）。
- `misfire_grace_time=3600`、`coalesce=True`、`max_instances=1`：三者齐上，避免机器重启后堆积执行。
- `uvicorn --workers 1`：多 worker = 多调度器 = 重复签到。

---

## 6. 前端界面设计

| 页面 | 内容 |
|---|---|
| **总览 Dashboard** | ① 今日状态矩阵：行=账号、列=任务，格子用 ✔ / ⟳ / ✕ / ⏳ 表示；② 积分趋势折线（近 30 天）；③ 连续签到天数卡片；④ 待处理告警条 |
| **账号管理** | 平台标签页；令牌状态徽标（有效 / 7 天内到期 / 已失效）；「测试连接」按钮；「导入凭证」粘贴框（支持拖入导出的 JSON） |
| **任务与调度** | 任务表：启用开关、cron 表达式、补偿窗口、手动「立即执行」；改 cron 即时热更新 |
| **运行记录** | 分页表：时间 / 账号 / 任务 / 状态 / 耗时 / 重试次数 / 净增积分；点开抽屉看脱敏响应 |
| **日志** | SSE 实时 tail（`text/event-stream`），含等级过滤、关键字搜索、下载 |
| **设置** | 通知渠道、代理、时区、数据备份导出 |

状态语义统一为 4 种颜色：成功（绿）/ 跳过（灰）/ 重试中（黄）/ 失败（红），不用 emoji，避免跨平台渲染差异。

---

## 7. 安全设计

| 风险 | 措施 |
|---|---|
| 面板被公网扫到 → token 泄露 | ① 强制登录（argon2 哈希 + HttpOnly Session）；② 默认只监听 `127.0.0.1`，由 Caddy 反代；③ 可选 IP 白名单 / Basic Auth 双保险 |
| 数据库被拖走 | 凭证字段 AES-256-GCM 加密，`MASTER_KEY` 走环境变量（不入库、不入镜像） |
| 日志泄露令牌 | 全局日志过滤器：任何 `eyJ...` 形态字符串替换为 `eyJ…{last6}`；请求头只记 `Authorization: ***` |
| 面板被 CSRF | HTMX 请求统一带 `X-CSRF-Token` 头 |
| 高频请求触发风控 | 单账号任务间隔 ≥1s；每日请求数硬上限；单 IP 单账号 |
| 误操作 | 所有写操作记审计日志（谁、何时、改了什么） |

---

## 8. 部署与运维

### 8.1 推荐形态：单机 Docker Compose

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
      MASTER_KEY: ${MASTER_KEY}          # openssl rand -base64 32
      ADMIN_PASSWORD_HASH: ${ADMIN_PASSWORD_HASH}
      HTTP_PROXY: ""                      # 国内机器留空
    volumes:
      - ./data:/app/data                  # sqlite + 日志 + 令牌缓存
    expose: ["8000"]
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./deploy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
volumes:
  caddy_data:
```

要点：
- `restart: unless-stopped` + `HEALTHCHECK /healthz`
- 数据全部落在 `./data`，迁移服务器 = 拷目录
- **备份**：每日 03:00 `sqlite3 .backup` + tar，保留 7 份（可用同一个 APScheduler 跑，不必装 cron）
- **可选兜底**：宿主机 crontab 每天 12:00 执行 `docker compose exec app python -m app.cli run all`，防止进程假死

### 8.2 其他形态对比

| 形态 | 是否有 Web 面板 | 成本 | 适用 |
|---|---|---|---|
| VPS 常驻（推荐） | ✅ | ¥0–118/年 | 长期主力 |
| GitHub Actions（私有仓库 2000 分钟/月） | ❌ 仅日志 | 免费 | 没有服务器时的纯执行兜底；⚠️ 延迟 5–20 分钟、60 天无提交会被禁用 |
| 青龙面板 | ✅ 通用面板 | 需 VPS | 已有青龙时的应急 |
| 云函数（FC/SCF）+ 定时触发器 | ❌ | 免费额度 | 无常驻需求，但要改造为无状态 |
| Cloudflare Workers + Cron | 需另做静态面板 | 免费 | 需重写为 JS，且依赖 KV/D1 存状态，改造量大 |

---

## 9. 免费 / 低成本云服务器调研

### 9.1 各方案实测口径对比

| 方案 | 配置 | 费用 | 关键限制 | 对本项目适用性 |
|---|---|---|---|---|
| **Oracle Cloud Always Free** | ARM Ampere A1 最高 4 OCPU + 24GB（可分 4 台）+ 2×AMD 1C1G，200GB 块存储，10TB 出站/月 | **永久免费** | ① 热门区域长期 "out of capacity"；② 注册需外币卡，风控严格易拒；③ **闲置实例可能被回收**（CPU/网络/内存长期低于 20%）；④ 国内访问延迟一般；⑤ 海外 IP 可能触发上游风控 | ⭐⭐⭐⭐ 技术上最强，但需定期快照 + 备用方案 |
| **GCP Always Free** | e2-micro（1GB，共享 vCPU），30GB HDD | 永久免费（限美国特定区域） | 出站仅 1GB/月（超出计费），海外 IP | ⭐⭐⭐ 1GB 够跑，但流量太小 + 需绑定结算账号 |
| **AWS Free Tier** | t2/t3.micro 1C1G | **仅 12 个月** | 到期自动按量计费 | ⭐⭐ 不适合"长期" |
| **腾讯云轻量（新客）** | 2核2G 3M / 40GB / 200GB 月流量 | **¥68–118/年**（秒杀可到 ¥38/年 4核4G） | 首年价，续费恢复原价；需实名 | ⭐⭐⭐⭐⭐ **国内节点、延迟最低、无风控疑虑，实际最省心** |
| **阿里云轻量/ECS（新客）** | 2核2G 3M | ¥99/年 | 同上 | ⭐⭐⭐⭐⭐ 同上 |
| **京东云 / 百度云** | 2核2G | ¥50–59/年 | 首年促销 | ⭐⭐⭐⭐ 便宜，生态弱 |
| **腾讯云/阿里云 学生机** | 1核2G/2核2G | ¥9–10/月 | 需学生认证 | ⭐⭐⭐⭐ 有学生身份最优 |
| **三丰云** | 1核1G | 永久免费 | 需实名；性能与稳定性口碑一般，无 SLA | ⭐⭐ 仅测试 |
| **Render Free** | 512MB / 0.1 CPU / 750 小时每月 | 免费 | **15 分钟无流量休眠**，冷启动 30–60s；cron 不可靠 | ⭐⭐ 面板会"睡死"，定时器随之停摆 |
| **Koyeb Free** | 512MB / 0.1 vCPU / 2GB | 免费（1 个实例） | 1 小时无流量 scale-to-zero；**不支持 worker 服务** | ⭐ 不适合常驻调度 |
| **Fly.io** | — | 新账号已无长期免费层 | — | ✗ |
| **Railway** | — | 免费额度降为 ~$1/月 | 不够跑常驻进程 | ✗ |
| **ClawCloud Run** | 0.5C/1G ≈ $4.2/月 | 每月 $5 额度（需 GitHub 登录且账号 >6 个月） | 平台稳定性存疑（有报道其部分 VPS 业务已终止）；流量 10GB 后计费 | ⭐⭐ 可跑 Docker，但不宜作为唯一承载 |
| **Northflank Free** | 0.2 vCPU / 512MB / 4GB 持久卷 | 免费 | 需信用卡；地区受限 | ⭐⭐⭐ 持久卷是加分项 |
| **apply.build** | 0.5 vCPU / 512MB | 免费，无需信用卡 | 固定欧洲节点 | ⭐⭐ 延迟高 |
| **Zeabur** | 有 $0 档 | 免费 | 额度未经官方确认，亚洲节点 | ⭐⭐⭐ 需自行核实 |
| **Cloudflare Workers** | Cron Triggers | 免费（10 万次/天） | 无持久磁盘、无常驻、需改造为 D1/KV | ⭐⭐ 改造量大于收益 |

> 注：以上额度与价格随活动变动，签约前请以厂商官网实时页面为准。

### 9.2 建议组合

| 预算 | 方案 | 说明 |
|---|---|---|
| **¥0** | Oracle ARM（主）+ 本地/备用兜底 | 注册通过就有 24GB 内存；务必开自动快照，并接受"可能被回收"的现实 |
| **¥60–120/年（推荐）** | 腾讯云或阿里云轻量 2核2G | 国内节点直连两家接口，延迟 <30ms，IP 干净，几乎零风控；一年成本低于一顿饭 |
| **已有 NAS/软路由** | 群晖 Docker / 树莓派 | 家庭 IP 更"像真人"，风控最友好，但需公网访问方案（Tailscale / frp） |

**我的建议**：既然是长期无人值守，**别把稳定性押在免费额度的善意上**。用 ¥68/年的国内轻量机做主，Oracle 免费机做异地备份副本（跑同一份 docker compose，数据库每日单向同步）——成本最低、可用性最高。

---

## 10. 风险与合规

1. **均为未公开接口**。客户端升级可能改路径/字段/鉴权，脚本必然周期性失效。→ 设计上必须做"**失败可见**"：`need_login` 与连续失败立即推送，绝不静默。
2. **凭证加密是持续对抗**。5.6.0 已把 token 改为 `$wbEncrypted`，作者说明"下个版本可能再变"。→ 导入器要能优雅失败并提示"请升级导入器"，而不是产出乱码。
3. **refresh token 轮换存在争议**（需你拍板，见 §12）。两派观点：
   - 一派（qoder-checkin）：到期前 72h 主动刷新并落盘，可实现长期无人值守；
   - 另一派（社区文章）：**不要自行刷新**，refresh 轮换会与客户端竞争，导致客户端被登出。
   → 我的默认设计：**只在 accessToken 已过期时才刷新一次，立即持久化，加互斥锁禁止并发刷新**；同时提供开关，关闭后改为"过期即告警 + 重新导入"。
4. **风控边界**：单账号、每日 1–3 次、请求间隔 ≥1s、固定出口 IP。多账号批量代签不在设计目标内。
5. **使用边界**：仅限本人账号的个人自动化；不做代签服务、不对外提供、不商业化。上游条款可能禁止自动化，风险自担。

---

## 11. 实施计划

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0 骨架**（核心闭环） | FastAPI + SQLite + 登录；账号 CRUD；WorkBuddy `checkin`；手动执行 + 每日 cron；运行记录页 + 日志 | 手动点一下能签到成功，记录与日志可在面板查到；重启后数据不丢 |
| **P1 双平台 + 看板** | Qoder provider；WorkBuddy `credit`；总览状态矩阵；凭证过期时间与告警 | 两个平台各 1 账号，连续 3 天自动执行无人工干预；矩阵正确反映状态 |
| **P2 状态机 + 可靠性** | `travel` 30 分钟 tick；三层重试 + 补偿队列；通知渠道；令牌续期 | 断网 30 分钟模拟后能自动补执行；宠物旅行全自动闭环（派出→归来→领积分） |
| **P3 交付** | Dockerfile + compose + Caddy；备份脚本；README 与部署文档；快照/迁移说明 | 新机器 `git clone` + `docker compose up -d` 15 分钟内跑通 |

预估：P0 约 1 天，P1 约 1 天，P2 约 1 天，P3 半天。

---

## 12. 需要你确认的 4 个问题

1. **账号数量**：两个平台各 1 个账号，还是需要多账号？（影响是否要做账号切换导入流程）
2. **refresh token 策略**：按我默认的"过期才刷新 + 可开关"，还是完全交给客户端（过期即告警重导）？
3. **部署落点**：直接上 ¥68/年的国内轻量机，还是先本机跑通再决定？
4. **通知渠道**：企业微信机器人 / PushPlus / 钉钉 / 邮件，选哪个（或先不做）？

---

## 13. 实现记录（v1.0 已交付）

### 与设计稿的偏差

| 项 | 设计稿 | 实现 | 原因 |
|---|---|---|---|
| 前端 | Jinja2 + HTMX | Jinja2 + 原生 JS（约 120 行） | 只有 6 个页面、交互集中在几个按钮，引入 HTMX 反而多一个 vendor 文件；去掉后完全离线可跑 |
| 任务范围 | 含「成长中心/礼包」 | 仅 `checkin`、`travel`、`credit` | 成长中心与开学季接口未经验证，按"只接已核实接口"的原则暂不纳入；Provider 已预留扩展位 |
| 通知 | 企微/PushPlus/钉钉/邮件 | 企微机器人 + 通用 Webhook | 按你的选择收敛，减少依赖面 |
| refresh 策略 | 到期前 72h | 到期前 12h（`QODER_REFRESH_MARGIN_H` 可调） | 云端单点运行、不与客户端竞争，缩短提前量可减少无效轮换 |

### 已验证的行为（`tests/e2e.py`，24 项全通过）

- 登录/未登录跳转/6 个页面渲染
- WorkBuddy 签到 +100、重复签到幂等跳过
- 宠物旅行：`idle → depart`、`arrived → claim +6`
- Qoder：`campaigns → claim +100`、重复领取幂等
- 令牌临期 → 自动 refresh → 新 refreshToken 加密回写 → 有效期更新
- 失效令牌 → `need_login`（不进重试，避免无效请求）
- 上游 5xx → `retryable` → 进入补偿队列
- 积分汇总口径正确（100 + 6 + 100 = 206）

### 落地时的三个坑（已在代码中规避）

1. **Starlette 1.x 改了 `TemplateResponse` 签名**，第一个参数必须是 `request`，老写法会把 context 当成模板名（报 `unhashable type: dict`）。
2. **`uvicorn --workers 1` 不是建议而是硬约束**：调度器在进程内，多 worker 会各自跑一份 cron。
3. **耗时统计要用浮点时钟**：`int(time.time())` 只有秒级精度，会让大部分请求显示 0ms。

### 补充：WorkBuddy 5.6+ 令牌导出路径实探

- 客户端（`D://WorkBuddy`）内 `@genie/at-rest-crypto`：信封 = base64(JSON{suite,keyId,nonce,authTag,ciphertext})，AES-256-GCM；
  密钥派生 `key = sha256(atRestSecretKey 字符串)`、`keyId = sha256(key)[:16]`、AAD = `WB-AAD\\0` + scope 字节（field=2）+ …
- 密钥载荷 `{version:1, atRestSecretKey}` 由构建期注入、运行时由主进程持有，**磁盘上无独立密钥文件**，userData 下只有 `.info` 与 observation.json。
- 客户端无「导出令牌」UI（检索 i18n 无相关入口），因此可行路径收敛为：抓包取 Bearer / 旧版明文客户端 / 自行提供密钥。
- 已实现 `import_workbuddy.py --scan <HAR|文本>`：自动挑出带 `sub` 的 JWT 并按 exp 排序，输出 uid 与过期时间。

---

## 14. 一键导入（v1.1）

用户反馈「无代码基础，操作要尽可能简单」，因此把凭证获取从「命令行 + 复制粘贴」收敛为**双击一个 bat**。

### 做法（参考 GitHub 同类项目）

| 平台 | 参考项目 | 关键技术 |
|---|---|---|
| WorkBuddy | `Cat-zaizai/ZaiZaiCat-Checkin`、`xz0609/workbuddy-checkin-ql` | `ELECTRON_RUN_AS_NODE=1 WorkBuddy.exe -e "<js>"` → `process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()` 取 `atRestSecretKey` |
| Qoder | 自行实现 | `Local State` 的 `os_crypt.encrypted_key` → DPAPI → 解 `auth.v1.dat` |

### WorkBuddy 解密链（已在本机验证）

```
密钥获取：sha256(atRestSecretKey 字符串) → 32B key
keyId   ：sha256(key).hex[:16]  （与信封 keyId 校验）
AAD     ："WB-AAD\0\x01" + len("WBEV1")+WBEV1 + len("sym-v1")+sym-v1
          + u32(1) + len(keyId)+keyId + b"\x02\x00\x00"
解密    ：AES-256-GCM(nonce, ciphertext + authTag, aad)
```

密钥只在本进程 stdout 中传递、不落盘；`--key` 手动指定仍保留作为逃生舱。

### Qoder 的一个重要发现

社区文档普遍称「缺 `Cosy-*` 设备头上游必拒」，但实测（2026-10，当前版本客户端）
`/sash/api/v1/me/campaigns` **只校验 Bearer**，返回 2 个活动、1 个可领。
因此 Qoder 也能做到全自动导入，不必再要求用户提供设备标识。

### 入口

- `import-account.bat`：依次跑两个导入器，`--save` 直接写入本机面板数据库（AES-GCM 加密后落库），
  随后调一次接口校验，输出「✅ 已添加/已更新账号，连接正常」；重复执行只更新令牌，不会重复建号。
- 云端场景仍保留 `--out` 导出 JSON + 面板粘贴。
