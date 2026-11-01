# 部署到腾讯云（Docker CE）

面向：已装好 Docker CE 的 Linux 云服务器（Ubuntu / Debian / CentOS / TencentOS 均可）。

---

## 0. 先做两件事

**① 安全组放行端口**（腾讯云控制台 → 服务器 → 安全组 → 入站规则）

| 端口 | 用途 |
|---|---|
| 22 | SSH（一般已开） |
| 80 | 面板访问（HTTP / 证书校验） |
| 443 | 面板访问（HTTPS） |

**② 确认 Docker 可用**

```bash
docker version && docker compose version
```

没有 compose 插件的话：`sudo apt install docker-compose-plugin`（或 `dnf install docker-compose-plugin`）。

---

## 1. 部署（3 条命令）

### 本机：打包

双击项目根目录的 **`package.bat`**，生成 `deploy\checkin-hub.tar.gz`。

> 如果本地已经双击过 `start.bat` 导入过账号，打包会**自动带上 `data\`**（含加密凭证和 `master.key`），
> 服务器起来就能直接签到，不用再导一次。这个包等同于你的账号，别外传。
> 想打一个不含数据的干净包：`package.bat --no-data`。

### 上传 + 解压 + 部署

```bash
# 1) 上传（把 IP、用户名换成你的；腾讯云 Ubuntu 默认用户是 ubuntu）
scp deploy/checkin-hub.tar.gz ubuntu@1.2.3.4:/tmp/

# 2) 登录服务器
ssh ubuntu@1.2.3.4

# 3) 解压并部署
mkdir -p ~/checkin-hub
tar -xzf /tmp/checkin-hub.tar.gz -C ~/checkin-hub
bash ~/checkin-hub/deploy/deploy.sh
```

`deploy.sh` 会自动：生成 `.env` 和随机管理员口令 → 构建镜像 → 启动容器 → 等健康检查 → 打印访问地址。

首次构建约 2–4 分钟（已在 Dockerfile 里配好腾讯云 PyPI 镜像加速）。

输出示例：

```
✅ 部署完成
   访问地址：http://1.2.3.4
   用户名  ：admin
   初始口令：Xk3pQ9tLm2Wv7Rs1   （已写入 .env，请登录后修改）
```

---

## 2. 登录后立刻做的事

1. 打开访问地址，用上面的口令登录；
2. 右上「设置 → 登录口令」改成你自己的强口令；
3. 检查「总览」：如果账号已经在列表里（打包时带了 `data/`），点每个账号的「测试」确认能通。

如果账号列表是空的，说明打的是干净包，走下面任一条：

| 方式 | 操作 |
|---|---|
| 推荐 | 见文末「把本地账号迁移到服务器」：本地导入后按 stop → 替换 → 清 WAL → start 上传，**不能直接 `scp -r data/` 覆盖**（会把账号冲掉） |
| 手动 | 本地跑 `python tools/import_workbuddy.py --out wb.json`，把 JSON 粘到面板「添加账号」 |

---

## 3. HTTPS（不用买域名）

面板里是账号凭证，公网裸 HTTP 不合适。三种选择：

**A. 有域名**：把域名 A 记录指向服务器 IP，然后

```bash
sed -i 's/^DOMAIN=.*/DOMAIN=checkin.example.com/' ~/checkin-hub/.env
cd ~/checkin-hub && sudo docker compose up -d caddy
```

Caddy 会自动申请 Let's Encrypt 证书。

**B. 没域名，但想 HTTPS**：用 `sslip.io` 的免费泛解析——它把 `<任意IP>.sslip.io` 解析到该 IP：

```bash
sed -i 's/^DOMAIN=.*/DOMAIN=1.2.3.4.sslip.io/' ~/checkin-hub/.env   # 换成你的 IP
cd ~/checkin-hub && sudo docker compose up -d caddy
```

然后访问 `https://1.2.3.4.sslip.io`，证书有效。

**C. 只在公司内网/自己看**：保持默认 HTTP，但务必把安全组来源 IP 限制为你自己的出口 IP。

---

## 4. 日常运维

```bash
cd ~/checkin-hub

sudo docker compose logs -f app      # 实时日志
sudo docker compose restart app      # 重启
sudo docker compose down             # 停止
sudo docker compose up -d            # 启动

# 升级（重新上传包后）—— 不要覆盖服务器上的 data/，线上账号和运行记录都在那里
tar -xzf /tmp/checkin-hub.tar.gz -C ~/checkin-hub --exclude='data/*' --exclude='./data/*'
cd ~/checkin-hub && sudo docker compose up -d --build
```

> 升级包如果是用 `package.bat`（默认带 `data/`）打的，直接 `tar -xzf ... -C ~/checkin-hub` 会把服务器上的
> `checkin.db` / `master.key` 换成你本地那份：**账号令牌回退、运行记录丢失**，而且 WAL 还可能被旧进程回放。
> 升级一律带上面的 `--exclude`；或者打包时用 `package.bat --no-data`。

**备份**（两样都要，缺一不可）：

```bash
cd ~/checkin-hub
tar -czf ~/checkin-backup-$(date +%F).tar.gz data/
```

`data/` 里有 `checkin.db`（账号/运行记录）和 `master.key`（解密凭证的密钥）。
**只备份 db 不备份 key，换机器后凭证全部解不开。**

面板「设置 → 下载数据库备份」也能导出 db，但同样不含 key。

---

## 5. 常见问题

**构建卡在 `pip install`**
`.env` 里确认：

```
PIP_INDEX=https://mirrors.cloud.tencent.com/pypi/simple
PIP_TRUSTED_HOST=mirrors.cloud.tencent.com
```

海外机器改成 `https://pypi.org/simple`。

**拉 `python:3.12-slim` 很慢**
给 Docker 配镜像加速（腾讯云内网可直接用 `https://mirror.ccs.tencentyun.com`），
或先 `docker pull mirror.ccs.tencentyun.com/library/python:3.12-slim` 再改 Dockerfile 的 FROM。

**80 端口被占用（已有 nginx/宝塔）**
`sudo docker compose ps` 确认后，改 `docker-compose.yml` 里 caddy 的端口映射为 `"8080:80"`，
访问 `http://IP:8080`；或停掉宿主机的 nginx。

**面板打不开**
1. 安全组是否放行 80；
2. 服务器本机防火墙：`ufw status` / `firewall-cmd --list-ports`；
3. `sudo docker compose ps` 看容器是否 Up；
4. `docker compose logs caddy` 看反代是否报错。

**日志里 `Password authentication` / 登录失败**
`.env` 里的 `ADMIN_PASSWORD` 优先级最高，每次启动都会强制重置为该值。
想用面板里改的口令，就把 `.env` 的 `ADMIN_PASSWORD=` 留空。

**定时任务时区不对**
`docker-compose.yml` 已固定 `TZ=Asia/Shanghai`，不要删。删了会差 8 小时，跨日判定直接错。

---

## 6. 资源占用

| 项 | 实测 |
|---|---|
| 镜像 | ~260 MB |
| 常驻内存 | ~120 MB（app）+ ~25 MB（caddy） |
| 磁盘 | 代码 + 镜像约 500 MB，数据每天几十 KB |

1 核 1G 的轻量服务器完全够用。


---

## 附录：把本地账号迁移到服务器（实测流程）

云端没有客户端，`import-account.bat` 跑不了，所以凭证要「本地导入 → 连库带密钥一起上传」。

```bash
# 0) 本地：双击 start.bat（或 python tools/import_all.py）已完成导入，data/ 里有 checkin.db 和 master.key
# 1) 本地打包时带上 data（package.bat 检测到 data/ 会自动包含）

# 2) 上传到服务器临时目录
scp data/checkin.db  ubuntu@IP:/tmp/ch_checkin.db
scp data/master.key  ubuntu@IP:/tmp/ch_master.key

# 3) 服务器上：务必先停容器，再替换，再清 WAL，最后启动
cd ~/checkin-hub
sudo docker compose stop app
sudo install -o 10001 -g 10001 -m 600 /tmp/ch_checkin.db data/checkin.db
sudo install -o 10001 -g 10001 -m 600 /tmp/ch_master.key  data/master.key
sudo rm -f data/checkin.db-wal data/checkin.db-shm
sudo docker compose start app

# 4) 校验
sudo docker compose exec -T app python -m app.cli probe
sudo shred -u /tmp/ch_checkin.db /tmp/ch_master.key
```

**顺序不能变**：容器还在跑时替换 db，旧进程退出时会把旧 WAL 回放到新文件上，
表现为「任务还在、账号全没了」。必须 stop → 替换 → 删 wal/shm → start。

`-o 10001` 是因为容器内以 uid 10001 运行，文件属主不对会读不到。
