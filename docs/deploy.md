# 部署到公网（腾讯云轻量 2C2G / 任意 Linux）

目标：一台 2 核 2G 的 Ubuntu，用 Docker 跑 PACE，朋友用浏览器访问。

## 0. 你需要准备的

- 一台服务器（腾讯云轻量 2C2G，Ubuntu 22.04 / 24.04）
- 记住 **公网 IP** 和 root 密码（或 SSH 私钥）
- 可选：一个域名（想要 HTTPS 就必须有；国内机用域名走 80/443 需要备案）

## 1. 装 Docker（在服务器上）

```bash
apt-get update && apt-get -y install git curl
curl -fsSL https://get.docker.com | sh
```

> 国内机器拉 Docker Hub 慢的话，配一个镜像加速器（腾讯云控制台「容器镜像服务」里有加速地址）。

## 2. 拉代码并启动

```bash
git clone https://github.com/lluxingzhe7/pace /opt/pace
cd /opt/pace
mkdir -p data
chown -R 10001:10001 data      # 容器里用非 root 用户跑，必须能写这个目录
docker compose up -d --build
docker compose logs -f pace    # 看启动日志，Ctrl+C 退出
```

启动后访问：`http://服务器IP:8765`

第一次打开会让你**创建站长账号**（用户名、邮箱、密码），建完就能用。

## 3. 云控制台放行端口

轻量应用服务器的「防火墙」里加一条：TCP **8765**（或者用域名方案时放行 80/443）。

## 4. 想要 HTTPS（推荐）

1. 域名解析：A 记录指向服务器 IP
2. 启动带 Caddy 的组合：

```bash
cd /opt/pace
PACE_DOMAIN=pace.example.com \
  docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build
```

Caddy 会自动申请 Let's Encrypt 证书，然后访问 `https://pace.example.com` 即可。
此时 8765 端口不再对外暴露（compose 覆盖里已去掉），只走 443。

> 国内服务器 + 域名 + 80/443 ⇒ 域名必须备案，否则会被拦。急着用就先走 `IP:8765`（不备案也能用，但流量是明文，密码会裸奔，只建议临时试用）。

## 5. 邮件（找回密码）

登录站长账号 → 设置 → **邮件发信**，填 SMTP：

| 字段 | QQ 邮箱示例 |
|---|---|
| SMTP 服务器 | `smtp.qq.com` |
| 端口 | `465` |
| 加密 | SSL |
| 发件人 | `你的QQ号@qq.com` |
| 账号 | 同上 |
| 授权码 | QQ 邮箱 → 设置 → 账户 → 开启 SMTP 后生成的**授权码**（不是登录密码） |

填完点「发测试邮件」验证。没配 SMTP 时，忘记密码的验证码只会写进容器日志（`docker compose logs pace`）。

## 6. 每日备份

```bash
cd /opt/pace
mkdir -p data/backups
crontab -e
# 加一行：每天凌晨 4 点备份，保留 14 天
0 4 * * * cd /opt/pace && docker compose exec -T pace python tools/backup_db.py >> data/backups/cron.log 2>&1
```

备份产物在 `data/backups/*.db`（每个用户一个文件，SQLite 在线快照，不用停服）。
建议再把 `data/` 定期同步到对象存储或另一台机器。

## 7. 更新版本

```bash
cd /opt/pace
git pull
docker compose up -d --build
```

数据库自动迁移，不会丢数据（每个用户一个库，结构变更走 `_ensure_columns`）。

## 8. 安全清单

- **不要**用 `--no-auth` 上公网（那等于把所有人数据摊开）
- 公网暴露 8765 时，最好在云防火墙里限制来源 IP；正式用请上 HTTPS
- 站长账号密码别和别处重复
- 定期看 `docker compose logs pace` 有没有异常登录尝试（失败次数会被限流，但日志里能看出来）

## 9. 常见问题

| 现象 | 原因 |
|---|---|
| 打不开页面、连接超时 | 云控制台安全组没放行端口 |
| `permission denied` 写 /data | 宿主机目录属主不对，`chown -R 10001:10001 data` |
| 登录后立刻掉线 | 反代没传 `X-Forwarded-Proto`，Cookie 的 Secure 判断错；用附带的 compose 覆盖文件即可 |
| 忘记密码收不到邮件 | 没配 SMTP，或授权码填成了登录密码 |
| 内存不够（2G） | 只跑 PACE + Caddy 足够；别在同一台再跑 MySQL/Redis 大件 |
