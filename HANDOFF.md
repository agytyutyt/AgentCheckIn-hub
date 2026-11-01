# HANDOFF · 换机 / 重置系统后从这里接着干

> 写于 **2026-11-01 18:29（Asia/Shanghai）**，本机即将重置。
> 目的：让「新机器 + 刚 clone 下来的仓库」在 30 分钟内回到当前状态，并知道下一步该干什么。
> 配套：[README.md](README.md)（怎么用）、[docs/design.md](docs/design.md)（当初为什么这么设计）、
> [docs/deploy-tencent-cloud.md](docs/deploy-tencent-cloud.md)（服务器部署手册）。

---

## 0. 三十秒现状

| 项 | 状态 |
|---|---|
| 代码 | 本地 `D:\TestWorkSpace\checkin-hub`，本次首次入 git 并推到 GitHub |
| 线上 | 腾讯云服务器（`<SERVER_IP>`，见下「机密清单」）Docker Compose 跑 `app` + `caddy` |
| 线上数据 | **唯一权威副本**在服务器 `~/checkin-hub/data/`：`checkin.db` + `master.key` + `qoder-umid/` |
| 本地数据 | `data/` 是 **2026-10-30 的旧副本**（0 条运行记录、旧 schema），只够做本机演示，别拿它当账本 |
| 最后改动 | 2026-11-01 13:29–13:38：Qoder 国际版身份/领取自愈（`app/qoderumid.py`、`app/providers/qoder.py`）、Mock 覆盖（`tools/mock_api.py`）、回归（`tests/e2e.py`）、README、重新打包 |
| 业务状态 | 三个账号在跑：WorkBuddy 主账号、Qoder CN 一个号、Qoder 国际版一个号（真实昵称/UID/手机号只存在于服务器 `checkin.db`，**不写进仓库**）。国际版每日 100 Credits 已于 **11-01 11:05 实测领到**（余额 700 → 800） |

---

## 1. ⚠️ 重置前必须做的事（不做就会真的接不上）

代码可以重 clone，**下面这些不会进 git**（`.gitignore` 明确排除），只能自己备份：

| 必须带走 | 在哪 | 为什么 | 丢了会怎样 |
|---|---|---|---|
| **服务器 `data/`** | 服务器 `~/checkin-hub/data/` | 账号 + 加密凭证 + 运行记录的唯一权威副本 | 所有账号要重新扫码/登录一次 |
| `data/master.key` | 同上（或本地同名） | 解 `cred_enc` 的 AES 密钥 | 光有 db 也没用，凭证全解不开 |
| `data/qoder-umid/identity-*.json` | 同上；本地也有一份 | 国际版每日活动是**设备定向**的，靠这份机器身份 | 需在有 Qoder 客户端的机器上重跑身份同步 |
| 面板口令 | 服务器 `.env` 的 `ADMIN_PASSWORD` | 登录面板 + 身份同步脚本 | 改 `.env` 重启即换新口令 |
| 服务器 IP / SSH 口令 | 你自己记录 | 备份、升级、排查都要 | 去云控制台找 |
| 本地 `.env` | 仓库根（gitignore） | 本机调试配置 | 可从 `.env.example` 重建，影响很小 |
| `tools/sync_qoder_identity.local.bat` | 本地（gitignore） | 里面有面板地址 + 口令 | 照模板重填（口令见服务器 `.env`） |

**一条命令备份线上数据**（在服务器上执行）：

```bash
cd ~/checkin-hub && tar -czf ~/checkin-backup-$(date +%F).tar.gz data/
# 拉回本地/U盘（在能上网的机器上）：
scp ubuntu@<SERVER_IP>:~/checkin-backup-*.tar.gz ./
```

**备份后请确认这三样都在包里**（缺一个都会让恢复变得很麻烦）：

```bash
tar -tzf checkin-backup-*.tar.gz | grep -E 'checkin\.db$|master\.key$|qoder-umid/'
```

> 🔐 **安全提醒**：面板口令此前**明文写死在** `tools/sync_qoder_identity.bat` 里（同 IP 一起）。
> 本次已挪到 `tools/sync_qoder_identity.local.bat`（`*.local.bat` 已加进 `.gitignore`），
> 提交历史里不会出现。**换机后建议顺手把面板口令改掉一次**（设置 → 登录口令），
> 因为它在磁盘上以明文躺了几天。改完记得同步更新 `.local.bat`。

---

## 2. 新机器：从零回到可运行状态

### 2.1 环境准备

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | **3.12 推荐**，3.10+ 均可 | 镜像固定 `python:3.12-slim`；本机实测 3.13 / 3.14 也能跑 |
| Git | 任意 | 拉代码 |
| Docker | 只在要发版/本地起容器时需要 | 云服务器上必须有 |

> 旧机器的坑，别重演：上一个 `.venv` 是用 **WorkBuddy 客户端自带的 Python**
> （`C:\Users\yfjz\.workbuddy\binaries\python\versions\3.13.12\python.exe`）建的，
> 客户端一升级那个解释器就没了，`.venv` 当场变成「找不到 executable」的僵尸环境。
> **新建 venv 请用系统 Python**（`python -m venv .venv`），不要用客户端自带的。

### 2.2 拉代码 + 装依赖 + 跑回归

```powershell
git clone <your-repo> checkin-hub
cd checkin-hub

# 1) 建环境（不要复用任何旧的 .venv，直接新建）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 先跑回归，确认代码本身是好的（不碰真实账号）
.\.venv\Scripts\python.exe tests\e2e.py              # 期望最后一行 "N/N 通过"，退出码 0
python tests\test_migration.py                        # 老库升级回归

# 3) 恢复配置与数据（见 2.3）
# 4) 启动
.\start.bat                                           # 或 .\.venv\Scripts\python.exe -m app.serve
```

> 发版脚本 `deploy/remote_deploy.py` 额外需要 `paramiko`（面板运行时不依赖它，所以没进
> `requirements.txt`，避免打进镜像）：`.\.venv\Scripts\python.exe -m pip install paramiko`

### 2.3 恢复配置与数据

```powershell
# 配置文件
copy .env.example .env        # 然后按需改 ADMIN_PASSWORD / MASTER_KEY

# 线上数据（从 1. 的备份包解出来）：db + key + 机器身份，三样都要
#   data\checkin.db
#   data\master.key
#   data\qoder-umid\identity-intl-<uid>.json

# 本机身份同步脚本（口令只存在这个文件里）
#   tools\sync_qoder_identity.local.bat   内容见文件头模板
```

**只恢复 db 不恢复 `master.key`，账号列表还在但所有凭证解不开**，表现为全部账号 `need_login`。

> 如果这台新机器上**装了 Qoder 桌面客户端并登录过**，也可以不恢复身份文件，
> 直接双击 `tools\sync_qoder_identity.local.bat`（先填面板地址/口令）现场重新生成并推到云端。

### 2.4 常驻（可选，但长期自动签到建议做）

```powershell
powershell -ExecutionPolicy Bypass -File tools\install-autostart.ps1
# 取消：加 -Remove
```

---

## 3. 线上服务

重置**不影响**服务器，正常情况什么都不用做。真需要重装/迁移时：

| 场景 | 做法 |
|---|---|
| 只更新代码 | `package.bat --no-data` 打包 → 上传 → **解压时排除 `data/`** → `docker compose up -d --build` |
| 首次迁移（要带账号） | `package.bat`（默认带 `data/`）→ 上传 → 解压 → `bash deploy/deploy.sh` |
| 一条命令推上去 | `$env:SSH_HOST/SSH_USER/SSH_PASSWORD` 设好后 `python deploy/remote_deploy.py all` |

**铁律：服务器已经在跑时，绝不能用带 `data/` 的包覆盖。** 顺序也必须是
`stop 容器 → 替换 db → 删 wal/shm → start`，否则旧进程退出时回放旧 WAL，
表现为「任务还在、账号全没了」。详见 [docs/deploy-tencent-cloud.md](docs/deploy-tencent-cloud.md) 附录。

日常运维：

```bash
cd ~/checkin-hub
sudo docker compose logs -f app
sudo docker compose restart app
sudo docker compose exec -T app python -m app.cli probe     # 校验全部账号凭证
```

---

## 4. 代码地图（要改东西时看这里）

| 想改什么 | 去哪 |
|---|---|
| 页面 / 路由 / API | `app/main.py`（35 个路由，含 SSE `/api/logs/stream`、`/backup`、`/api/summary`） |
| 执行一次任务的流程、重试、补偿、通知 | `app/runner.py`（退避 `BACKOFF_SEC = [600, 1800, 7200]`） |
| 定时任务、内务作业、清理 | `app/scheduler.py` |
| 表结构、老库迁移、默认任务种子 | `app/db.py`（`SCHEMA` / `_migrate()` / `_seed_task_defs()`）← **改任务默认值要同时改 seed 和 migrate 里的归一语句** |
| WorkBuddy 接口（签到 / 旅行 / 余额 / 续期） | `app/providers/workbuddy.py` |
| Qoder 接口（活动领取 / 签到 / 余额 / 令牌 / 设备身份头） | `app/providers/qoder.py`（最大的一个文件，42KB） |
| Qoder 机器身份（runtime-info、云端组件、本机固定身份） | `app/qoderumid.py` |
| 余额口径与「今日新增」 | `app/credits.py` |
| 令牌续期/保活 | `app/renew.py` |
| 扫码纳管 / 手机号验证码登录 | `app/authflow.py` / `app/qoderlogin.py` |
| 加密、脱敏、登录会话 | `app/security.py` |
| 企业微信 / Webhook 推送 | `app/notify.py` |
| CLI（`list` / `run` / `probe` / `backup`） | `app/cli.py` |
| 本地假上游（联调 + 回归） | `tools/mock_api.py` |
| 导入器（本机读客户端登录态） | `tools/import_workbuddy.py` / `tools/import_qoder.py` / `tools/import_all.py` |

加一个新平台的成本很低：实现 `app/providers/base.py` 的 `Provider` 接口 →
在 `app/providers/registry.py` 登记 → 在 `db._seed_task_defs()` 加任务行。

---

## 5. 当前进度 / 未完成的事

### 已闭环（有实测）

- **WorkBuddy**：每日签到、宠物旅行状态机、资源包余额汇总、refreshToken 续期（剩余 <3 天）。
- **Qoder CN**：活动领取领 100 积分；活动平台故障时回退 sash `daily-check-in`。
- **Qoder 国际版**：补齐「桌面端同款身份头 + `?clientType=10&forceRefresh=true` + 固定 PC 身份」后，
  云端 **11-01 11:05 自动领到 100 Credits**（700 → 800），`quota/usage` 交叉验证一致，重跑幂等。
- **自愈六层**（基址 / 令牌 / 身份来源 / 身份组件 / 领取结果分类 / 记账）都有回归覆盖。
- 扫码纳管、手机号+验证码登录、双区域账号池、令牌保活、SSE 日志、通知、CLI 均可用。

### 还没做 / 待验证（按优先级）

1. **先跑一遍回归**：本次会话没能跑 `tests/e2e.py`（旧 `.venv` 的解释器已随客户端消失，
   而 `data/` 目录在本机又被 Windows 权限挡住写入）。新机器上**第一件事**就是跑它，
   确认 2026-11-01 那批改动（`qoderumid.py` / `qoder.py` / `mock_api.py` / `e2e.py`）是全绿的。
2. **「只用云端身份能不能领取」的干净实验还没做**。README §3「设备身份」里两条互相矛盾的实测记录
   （云端组件身份有时 1 条无每日活动、有时 2 条且已领）仍未定论；目前结论只是
   「活动大概率是分阶段下发，而不是云端身份被永久拒绝」，**属推测**。
   要做的话：找一个没固定过 PC 身份的账号，只用云端 `runtime-info` 组件身份跑一整天，记录活动条数。
3. **一台设备每天只能有 1 个国际版账号领到**（服务端按设备去重）。多开国际版账号目前无解，
   要么换机器，要么接受只有一个号能领。
4. **国际版活动下发时间不稳定**：11-01 实测 10:05–10:55 六次都是「暂无可领取活动」，
   11:00 才下发。窗口已放宽到 13:00 兜住，但抖动的根因没查。
5. **`docs/design.md` 是历史设计稿**（HTMX、独立的每日 Credits 任务等已被推翻），
   已在文首标注「以 README 为准」，没有逐条重写。
6. `requirements.txt` 与代码的真实依赖一致（含 `segno`），但 `paramiko` 只给发版脚本用，
   故意留在外面 —— 如要「一条命令发版」，记得先装。

### 已知环境问题（重置后自然消失，别浪费时间排查）

- 本机 `data/` 目录**拒绝写入**（Windows 权限/ACL 问题，`sqlite3` 报
  `unable to open database file`，`Set-Content` 报「访问被拒绝」）。新机器上目录是新建的，不会有这问题；
  若又遇到，用 DSH 的 `diagnose-windows-sandbox-acl` 技能修，或干脆删掉重建 `data/`。
- `.venv` 已损坏（见 2.1），**不要试图修**，直接删掉重建。

---

## 6. 关键约定（改代码前先读，都是踩过的坑）

1. **单 worker**：调度器在进程内，多 worker 会重复执行。镜像里写死 `--workers 1`。
2. **`TZ=Asia/Shanghai`**：容器默认 UTC，不设会让跨日判定差 8 小时。
3. **一切幂等**：签到/领取重复调用必须收敛成「已签」。补偿、misfire 补跑都依赖这一点。
4. **余额口径**：用官方「实时余额」，别用活动累计（`total_credits`）和账单 `TotalDosage`；
   到账 delta 必须查前查后两次数，不能拿面额当到账。
5. **改任务默认值**要同时动 `_seed_task_defs()`（新库）和 `_migrate()` 里的归一 UPDATE（老库）。
6. **`_migrate()` 里「补列」和「回填」严格两段**：先补齐所有列，再做 UPDATE。
   混在一起会让老库死在 `no such column`（2026-10-30 真实事故，`tests/test_migration.py` 就是防它的）。
7. **别提交** `data/`、`.env`、`*.local.bat`、`wb.json`、`qoder.json`、`deploy/*.tar.gz`、`docs/screenshots/`。
8. **两平台都是未公开接口**，客户端升级就可能失效。设计原则是「失败可见」：
   `need_login` 和连续失败都会推送，不静默断签。

---

## 7. 仓库与首次推送

本次会话之前**这个目录根本没有 git 仓库**（也就没有历史泄漏口令的问题）。首次推送：

```powershell
git init -b main
git add -A
git status                      # 确认没有 data/ .env *.local.bat 等
git commit -m "chore: initial commit — CheckinHub 现状（2026-11-01）"
git remote add origin https://github.com/<you>/checkin-hub.git
git push -u origin main
```

推送前自检（应无输出，或只匹配到示例/占位符）：

```powershell
git grep -n -E "43\.136\.|baxOws9|PANEL_PASS=.+" -- . ':!*.local.bat'
```

> 提交身份：本机 `git config --global` 已有 `user.name` / `user.email`。
> 若不想把真实邮箱写进公开提交历史，先设成 GitHub 的 noreply 地址：
> `git config --global user.email "<id>+<user>@users.noreply.github.com"`
>
> **仓库只放代码**：真实账号昵称/UID/手机号/令牌只存在于服务器的 `checkin.db`，
> 截图目录 `docs/screenshots/` 已从仓库移除（本地保留但被 gitignore）。
> 仓库建议设为 **private**：README 里虽然没有口令，但描述了风控绕过与设备身份细节。

---

## 8. 重置后第一小时的顺序（浓缩版）

1. 装 Python 3.12+ 与 Git → `git clone` → `python -m venv .venv` → `pip install -r requirements.txt`
2. 跑 `tests\e2e.py` 与 `tests\test_migration.py`，**全绿再往下**。
3. 恢复 `data\`（db + master.key + qoder-umid）、`.env`、`tools\sync_qoder_identity.local.bat`。
4. `python -m app.cli probe` 校验凭证；不通就重导（`import-account.bat`，需本机装客户端并登录）。
5. 需要本机常驻：`tools\install-autostart.ps1`；需要发版：`package.bat --no-data` + `remote_deploy.py`。
6. 确认线上还在跑：`docker compose ps` + 面板「运行记录」有没有今天的签到。
