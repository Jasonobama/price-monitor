# Price Monitor — Linux 云端 VPS 部署指南

> 适用于 Ubuntu 20.04+ / Debian 11+ / CentOS 8+ / Rocky Linux 等主流发行版。

---

## 目录

- [环境要求](#环境要求)
- [第一步：服务器基础配置](#第一步服务器基础配置)
- [第二步：上传程序文件](#第二步上传程序文件)
- [第三步：创建虚拟环境与安装依赖](#第三步创建虚拟环境与安装依赖)
- [第四步：编辑配置文件](#第四步编辑配置文件)
- [第五步：验证运行](#第五步验证运行)
- [第六步：配置 systemd 服务](#第六步配置-systemd-服务)
- [第七步：日志与监控](#第七步日志与监控)
- [第八步：磁盘空间管理](#第八步磁盘空间管理)
- [故障排查](#故障排查)

---

## 环境要求

| 项目 | 最低要求 |
|---|---|
| 操作系统 | Linux (Ubuntu / Debian / CentOS / Rocky) |
| Python | 3.9 及以上 |
| 内存 | 512MB 可用 |
| 磁盘 | 1GB 可用（CSV 数据约 2MB/天，归档约 3-5MB/周） |
| 网络 | 出站 HTTPS 访问（Yahoo Finance / 新浪财经 / CNBC / timeapi.io / api.telegram.org） |

### VPS 防火墙/端口要求

程序自身**不监听任何端口**，所有网络请求均为**出站**。VPS 需开放以下**出站**端口：

| 端口 | 协议 | 用途 | 是否必需 |
|------|------|------|----------|
| 443 | TCP (HTTPS) | 所有数据源：新浪财经、Yahoo Finance、CNBC、U.S. Treasury、TimeAPI、WorldTimeAPI、Telegram Bot API | ✅ 必需 |
| 80 | TCP (HTTP) | WorldTimeAPI 回退（当 HTTPS 失败时尝试 HTTP） | ⚠️ 建议开放 |
| 25 | TCP (SMTP) | 邮件通知（163 邮箱默认端口） | ❌ 仅 SMTP 模式 |
| 465 | TCP (SMTPS) | 邮件通知（SSL 加密，部分服务商推荐） | ❌ 仅 SMTP 模式 |
| 587 | TCP (SMTP) | 邮件通知（STARTTLS，QQ/Gmail 等推荐） | ❌ 仅 SMTP 模式 |

> **注意**：若使用 Telegram 通知（推荐），仅需开放端口 443（和 80），不需要任何 SMTP 端口。

**快速检查防火墙：**

```bash
# 检查出站 HTTPS 是否被阻止
iptables -L OUTPUT -n | grep -E 'REJECT.*443|DROP.*443'

# 若使用 UFW，检查默认出站策略
ufw status verbose

# 测试关键域名的连通性
curl -sI --max-time 5 https://hq.sinajs.cn       | head -1  # 新浪财经
curl -sI --max-time 5 https://api.telegram.org    | head -1  # Telegram
curl -sI --max-time 5 https://query1.finance.yahoo.com | head -1  # Yahoo
```

**常见云服务商防火墙说明：**

| 云服务商 | 默认出站策略 | 注意事项 |
|----------|-------------|----------|
| AWS EC2 / Lightsail | 全部允许 | 安全组默认放行所有出站 |
| 阿里云 / 腾讯云 | 全部允许 | 部分轻量应用服务器默认放行 |
| Azure VM | 全部允许 | NSG 默认放行出站 |
| Google Cloud | 全部允许 | 防火墙默认放行出站 |
| Vultr / DigitalOcean / Linode | 全部允许 | 无额外防火墙限制 |
| Oracle Cloud | 全部允许 | 默认安全列表放行出站 |

> 绝大多数 VPS 服务商默认放行所有出站端口，无需额外配置。仅当主动添加了出站防火墙规则时才需检查。

---

## 第一步：服务器基础配置

### 1.1 更新系统并安装 Python

```bash
# Ubuntu / Debian
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv

# CentOS / Rocky
dnf update -y
dnf install -y python3 python3-pip
```

### 1.2 验证 Python 版本

```bash
python3 --version
# 应输出 Python 3.9.x 或更高
```

---

## 第二步：上传程序文件

### 2.1 创建部署目录

```bash
mkdir -p /home/pi/price_monitor/data
mkdir -p /home/pi/price_monitor/archive
```

### 2.2 上传文件

将以下文件上传到 `/home/pi/price_monitor/`：

| 文件 | 说明 | 是否必需 |
|---|---|---|
| `price_monitor.py` | 主程序 | ✅ 必需 |
| `monitor_config.json` | 产品列表 + 全局参数 + 通知方式 | ✅ 必需 |
| `alerts_config.json` | 价格告警规则 | ✅ 必需 |
| `smtp_config.json` | SMTP 邮件配置 | ❌ Telegram/Webhook 模式不需要 |

**上传方式**：

```bash
# 方式 A：scp（从本地）
scp price_monitor.py monitor_config.json smtp_config.json alerts_config.json \
    root@your-vps-ip:/home/pi/price_monitor/

# 方式 B：如果文件在 GitHub
cd /home/pi/price_monitor
git clone https://github.com/your-repo/price_monitor.git .
```

---

## 第三步：创建虚拟环境与安装依赖

```bash
cd /home/pi/price_monitor
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install requests
```

验证安装：

```bash
.venv/bin/python -c "import requests; print(requests.__version__)"
```

---

## 第四步：编辑配置文件

### 4.1 主配置 `monitor_config.json`

```bash
nano /home/pi/price_monitor/monitor_config.json
```

关键检查项：
- `products` 列表确认要监控的产品
- `notification.recipient_email` 填入接收归档/清理通知的邮箱
- `poll_interval_seconds` 建议设为 10-30 秒
- `max_workers` VPS 低配建议 2-3，高配可 4-5

### 4.2 选择通知方式（推荐 Telegram）

程序支持三种通知方式，在 `monitor_config.json` 的 `notification.method` 中切换：

| method | 安全级别 | 需要 VPS 存密码？ | 被入侵风险 |
|---|---|---|---|
| `telegram` | ✅ 高 | 否（仅 bot_token） | 攻击者只能发消息，token 可随时吊销 |
| `webhook` | ✅ 高 | 否（仅 URL） | 同上，URL 可随时删除 |
| `smtp` | ⚠️ 低 | 是（邮箱密码） | 攻击者获取邮箱完全控制权 |
| `none` | ✅ 高 | 否 | 无通知 |

#### 方式一：Telegram（推荐）

**准备工作（在本地电脑或手机上操作，只需一次）**：

1. 打开 Telegram，搜索 `@BotFather`，发送 `/newbot`，按提示创建机器人
2. BotFather 会返回一个 `bot_token`（格式如 `123456:ABC-def...`），**保存好不要泄露**
3. 搜索 `@userinfobot`，发送任意消息，获取你的数字 `chat_id`
4. **关键步骤**：在 Telegram 中搜索你创建的 Bot 用户名（`@xxx_bot`），点击「开始」或发送 `/start`——Bot 必须先被用户主动联系才能发送消息

**配置 `monitor_config.json`**：

```bash
nano /home/pi/price_monitor/monitor_config.json
```

找到 `notification` 段，修改为：

```json
"notification": {
  "method": "telegram",
  "recipient_email": "",
  "telegram": {
    "bot_token": "1234567890:ABCdefGhIJklMnOPqrSTuvWXyz",
    "chat_id": "987654321"
  },
  "webhook": { "url": "", "headers": {} }
}
```

配置完成后可以删除 `smtp_config.json`（Telegram 模式不需要它）。

#### 方式二：SMTP 邮件（传统）

如果坚持使用邮件，编辑 `smtp_config.json`：

```json
{
  "host": "smtp.163.com",
  "port": 25,
  "username": "your_email@163.com",
  "password": "your_smtp_auth_code",
  "sender_name": "价格监控系统",
  "use_tls": false
}
```

并在 `monitor_config.json` 中保持 `"method": "smtp"`。

> ⚠️ 如果 VPS 被入侵，`smtp_config.json` 中的密码会被攻击者获取，可能导致邮箱被盗。

#### 方式三：Webhook（进阶）

支持 Discord、Slack、企业微信、钉钉等任意 Webhook 服务：

```json
"notification": {
  "method": "webhook",
  "webhook": {
    "url": "https://discord.com/api/webhooks/xxx/yyy",
    "headers": {}
  }
}
```

### 4.3 价格告警 `alerts_config.json`

```bash
nano /home/pi/price_monitor/alerts_config.json
```

按需启用告警规则，将 `enabled` 设为 `true`。

### 4.4 添加新的金融产品

在 `monitor_config.json` 的 `products` 数组中按以下格式添加条目：

```json
{
  "name": "产品显示名",
  "source": "数据源类型",
  "code": "新浪或 Yahoo 代码",
  "decimals": 2,
  "multiplier": 1.0,
  "category": "分类名"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 显示名称，需唯一，会出现在终端输出和 CSV 表头中 |
| `source` | ✅ | 数据源类型，见下方 7 种可选值 |
| `code` | ✅ | 新浪或 Yahoo Finance 的行情代码 |
| `decimals` | 否 | 价格小数位数，默认 2 |
| `multiplier` | 否 | 价格倍率，默认 1.0（如百日元设为 100） |
| `category` | 否 | 分类标签，用于归档时按分类打包 ZIP，默认"未分类" |

#### 7 种数据源及示例

**1. `sina_index` — 中国股指（新浪财经）**

| 名称 | code |
|------|------|
| 上证指数 | `sh000001` |
| 深证成指 | `sz399001` |
| 创业板指 | `sz399006` |
| 上证50 | `sh000016` |
| 沪深300 | `sh000300` |
| 中证500 | `sh000905` |

示例：
```json
{"name": "沪深300", 
  "source": "sina_index", 
  "code": "sh000300", 
  "decimals": 2, 
  "category": "股指"}
```

**2. `sina_stock` — A股个股（新浪财经）**

code 格式：`sh` + 6位代码（上海）或 `sz` + 6位代码（深圳）。

| 名称 | code |
|------|------|
| 招商银行 | `sh600036` |
| 贵州茅台 | `sh600519` |
| 宁德时代 | `sz300750` |
| 比亚迪 | `sz002594` |

示例：
```json
{"name": "招商银行", 
  "source": "sina_stock", 
  "code": "sh600036", 
  "decimals": 2, 
  "category": "股票"}
```

**3. `sina_fx` — 外汇（新浪财经）**

code 格式：`fx_s` + 币种缩写 + `cnh`（离岸人民币）。

| 名称 | code | multiplier |
|------|------|------------|
| 美元/人民币 | `fx_susdcnh` | 1.0 |
| 欧元/人民币 | `fx_seurcnh` | 1.0 |
| 英镑/人民币 | `fx_sgbpcnh` | 1.0 |
| 百日元/人民币 | `fx_sjpycnh` | 100.0 |

示例：
```json
{"name": "美元/人民币", 
  "source": "sina_fx", 
  "code": "fx_susdcnh", 
  "decimals": 4, 
  "multiplier": 1.0, 
  "category": "外汇"}
```

**4. `yahoo` — 期货/全球指数（Yahoo Finance）**

适用期货（`=F`）和大部分全球指数（`^` 前缀）。

| 名称 | code |
|------|------|
| COMEX 黄金 | `GC=F` |
| COMEX 白银 | `SI=F` |
| WTI 原油 | `CL=F` |
| 布伦特原油 | `BZ=F` |
| COMEX 铜 | `HG=F` |
| 天然气 | `NG=F` |
| 道琼斯 | `^DJI` |
| 纳斯达克 | `^IXIC` |
| 标普500 | `^GSPC` |
| 恒生指数 | `^HSI` |
| 英国 FTSE 100 | `^FTSE` |
| 德国 DAX | `^GDAXI` |
| 日经 225 | `^N225` |
| 比特币期货 | `BTC=F` |

示例：
```json
{"name": "日经225", 
  "source": "yahoo", 
  "code": "^N225", 
  "decimals": 2, 
  "category": "股指"}
```

**5. `yahoo_stock` — 美股/港股（Yahoo Finance）**

| 名称 | code |
|------|------|
| 苹果 | `AAPL` |
| 特斯拉 | `TSLA` |
| 英伟达 | `NVDA` |
| 腾讯控股 | `0700.HK` |
| 阿里巴巴 | `9988.HK` |

示例：
```json
{"name": "特斯拉", 
  "source": "yahoo_stock", 
  "code": "TSLA", 
  "decimals": 2, 
  "category": "股票"}
```

**6. `yahoo_fx` — 外汇（Yahoo Finance）**

| 名称 | code | multiplier |
|------|------|------------|
| 美元/人民币 | `USDCNY=X` | 1.0 |
| 欧元/美元 | `EURUSD=X` | 1.0 |
| 美元/日元 | `USDJPY=X` | 1.0 |
| 百日元/人民币 | `JPYCNY=X` | 100.0 |

示例：
```json
{"name": "美元/日元", 
  "source": "yahoo_fx", 
  "code": "USDJPY=X", 
  "decimals": 4, 
  "category": "外汇"}
```

**7. `treasury` — 美国国债收益率**

| 名称 | code |
|------|------|
| 美债 2 年 | `2y` |
| 美债 5 年 | `5y` |
| 美债 10 年 | `10y` |

示例：
```json
{"name": "美债2年", 
  "source": "treasury", 
  "code": "2y", 
  "decimals": 3, 
  "category": "国债"}
```

> **提示**：添加新条目后保存文件，重启程序即生效（`systemctl restart price_monitor`）。首次运行时程序会自动对新产品的数据源做连通性检测。
> 如需查找 Yahoo Finance 代码，在 [finance.yahoo.com](https://finance.yahoo.com) 搜索产品，URL 中 `/quote/` 之后的部分即为 code。

---

## 第五步：验证运行

```bash
cd /home/pi/price_monitor
.venv/bin/python price_monitor.py --once
```

预期输出（使用 Telegram 通知时）：

```
[启动] 监控 19 个金融产品
[启动] 轮询间隔: 10s  |  CSV 记录间隔: 60s
[启动] CSV 输出目录: /home/pi/price_monitor/data
[启动] 并发线程数: 4
[启动] 美国冬夏令时: 夏令时 | 来源: timeapi.io | 开始: 2026-03-08 15:00 北京时间 | 结束: 2026-11-01 14:00 北京时间
[启动] 通知方式: Telegram
[启动] 每周归档: 已启用（周5 1:00 执行）
[启动] 数据清理: 已启用（每 4 周，提前 3 天提醒）
...
  成功: 19/19  |  失败: 0/19
```

确认所有产品数据获取成功（`成功: 19/19`）。

如果出现网络错误，检查 VPS 防火墙是否放行出站 HTTPS（端口 443）。

---

## 第六步：配置 systemd 服务

### 6.1 安装服务文件

> ⚠️ 部署前请确认 `price_monitor.service` 中的 `User=` 和 `Group=` 与你实际的运行用户一致（root 环境建议设为 `root`）。

```bash
cp price_monitor.service /etc/systemd/system/
```

### 6.2 重新加载并启动

```bash
systemctl daemon-reload
systemctl enable --now price_monitor
```

### 6.3 查看状态

```bash
systemctl status price_monitor
```

预期输出：

```
● price_monitor.service - 金融产品实时价格监控
   Active: active (running)
```

### 6.4 常用管理命令

```bash
systemctl status price_monitor     # 查看状态
systemctl restart price_monitor    # 重启
systemctl stop price_monitor       # 停止
systemctl disable price_monitor    # 禁用开机自启
```

---

## 第七步：日志与监控

### 7.1 查看实时日志

```bash
journalctl -u price_monitor -f
```

### 7.2 查看近期日志

```bash
# 最近 1 小时
journalctl -u price_monitor --since "1 hour ago"

# 最近 100 条
journalctl -u price_monitor -n 100

# 仅查看错误
journalctl -u price_monitor -p err
```

### 7.3 日志持久化（保留重启前的日志）

```bash
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
```

### 7.4 磁盘使用检查

```bash
# CSV 数据目录
du -sh /home/pi/price_monitor/data/

# 归档目录
du -sh /home/pi/price_monitor/archive/

# 整体占用
du -sh /home/pi/price_monitor/
```

---

## 第八步：磁盘空间管理

程序每分钟写入一行 CSV，每天约 2MB（19 个产品）。归档 ZIP 每周约 500KB-1MB。

### 8.1 自动清理（已内置）

`monitor_config.json` 中的清理配置：

```json
"cleanup": {
  "enabled": true,
  "interval_weeks": 4,
  "warning_days_before": 3,
  "keep_archives": true
}
```

- 每 4 周自动删除超过 4 周的 CSV 日数据
- 清理前 3 天发送邮件提醒
- 归档 ZIP 文件保留不受影响

### 8.2 手动清理

```bash
# 删除 30 天前的 CSV
find /home/pi/price_monitor/data/ -name "prices_*.csv" -mtime +30 -delete

# 删除 90 天前的归档
find /home/pi/price_monitor/archive/ -name "*.zip" -mtime +90 -delete
```

### 8.3 磁盘报警（推荐）

```bash
# 安装监控工具
apt install -y smartmontools

# 或添加到 crontab 定期检查
# 当 /home/pi/price_monitor 超过 500MB 时发送邮件
0 6 * * * du -sm /home/pi/price_monitor | awk '$1>500{print "磁盘告警"}' | mail -s "VPS 磁盘告警" your@email.com
```

---

## 故障排查

### Q: 启动后所有产品显示"获取失败"

检查 VPS 网络连通性：

```bash
curl -sI https://finance.yahoo.com | head -1
curl -sI https://hq.sinajs.cn | head -1
```

如果均超时，检查防火墙（详见上方「VPS 防火墙/端口要求」）：

```bash
# 确认出站 HTTPS(443) 和 HTTP(80) 未被阻止
iptables -L OUTPUT -n | grep -E 'REJECT|DROP'

# UFW 用户
ufw status verbose
```

### Q: systemd 服务启动失败 (exit code)

查看详细错误：

```bash
journalctl -u price_monitor -n 50 --no-pager
```

常见原因：
- Python 路径错误 → 确认 `.venv/bin/python` 存在
- 配置文件 JSON 格式错误 → 用 `python -m json.tool monitor_config.json` 校验
- 权限不足 → 确认 `/home/pi/price_monitor/` 及其子目录可被运行用户读写
- service 文件中的 `User=/Group=` 与实际运行用户不匹配

### Q: 程序运行但 CPU 占用过高

编辑 `monitor_config.json`，降低 `max_workers` 和 `poll_interval_seconds`：

```json
"poll_interval_seconds": 30,
"max_workers": 2
```

### Q: Telegram 通知发送失败 (400 Bad Request: chat not found)

这是最常见的问题——Bot 必须先被用户主动联系才能发送消息。

**解决步骤**：

1. 在 Telegram 中搜索你的 Bot 用户名（创建 Bot 时 `@BotFather` 告诉你的那个）
2. 点击「开始」或发送 `/start`
3. 重新运行 `python price_monitor.py --once`

验证 Bot 是否可用：
```bash
curl -s "https://api.telegram.org/bot<你的bot_token>/getMe"
```

### Q: Telegram 通知发送失败 (401 Unauthorized)

Bot Token 无效。回到 `@BotFather`，发送 `/mybots` → 选择你的 Bot → 获取新的 Token。

### Q: 邮件发送失败

- 检查 SMTP 端口是否被 VPS 服务商封锁（部分厂商封锁 25 端口）
- 尝试改用 465 端口（SSL）或 587 端口（STARTTLS）
- 查看日志：`journalctl -u price_monitor | grep -i smtp`

### Q: 如何升级程序

```bash
cd /home/pi/price_monitor
systemctl stop price_monitor
# 替换 price_monitor.py
systemctl start price_monitor
systemctl status price_monitor
```

---

## 附录：一键部署脚本

将以下内容保存为 `deploy.sh`：

```bash
#!/bin/bash
set -e

APP_DIR="/home/pi/price_monitor"

# ── 检查是否为 root ──
if [ "$(id -u)" != "0" ]; then
  echo "错误: 此脚本需以 root 用户运行。"
  exit 1
fi

echo "=== Price Monitor VPS 部署脚本 (root 环境) ==="

# 1. 创建目录
mkdir -p $APP_DIR/data $APP_DIR/archive

# 2. 安装 Python（如未安装）
if ! command -v python3 &>/dev/null; then
  echo "[*] 安装 Python3 ..."
  apt update && apt install -y python3 python3-pip python3-venv
fi

# 3. 拷贝文件（假设当前目录有所有文件；smtp_config.json 在 Telegram 模式下可选）
cp price_monitor.py $APP_DIR/
cp monitor_config.json $APP_DIR/
cp alerts_config.json $APP_DIR/
cp smtp_config.json $APP_DIR/ 2>/dev/null || echo "  跳过 smtp_config.json（Telegram 模式不需要）"
cp price_monitor.service /etc/systemd/system/

# 4. 创建虚拟环境
cd $APP_DIR
python3 -m venv .venv
.venv/bin/pip install requests

# 5. 启用服务
systemctl daemon-reload
systemctl enable --now price_monitor

echo ""
echo "=== 部署完成 ==="
echo "查看状态: systemctl status price_monitor"
echo "查看日志: journalctl -u price_monitor -f"
```

运行：

```bash
bash deploy.sh
```

---

## 附录：程序更新日志

### 2026-06-09

本次更新涉及以下九个方面：

**1. 修复数据清理通知 Bug**

`DataCleaner.check_and_run()` 中 `_perform_cleanup()` 返回 0（无过期文件）时，仍发送"[清理完成] 已删除 0 个过期 CSV 文件"通知并保存状态，导致用户收到通知但文件未删除、且无法在当天重试。
- **修复方案**：仅在 `deleted_count > 0` 时发送清理完成通知，避免误导用户。

**2. 修复清理警告每天重复发送 Bug**

`cleanup_warning` 状态键以当天日期作为标识，由于日期每天变化，警告在清理提醒阶段每天都会被触发重复发送。
- **修复方案**：改用 `next_cleanup.isoformat()` 作为状态标识，同一清理周期只发送一次警告。

**3. 状态文件内存缓存**

`_load_state()` / `_save_state()` 每次调用都直接读写磁盘 JSON 文件，主循环每 60 秒触发一次，I/O 频繁。
- **优化方案**：新增 `_state_cache` 内存缓存，`_load_state()` 优先返回缓存，`_save_state()` 同时更新缓存和磁盘，减少不必要的磁盘 I/O。

**4. HTTP 请求自动重试**

`_request_json()` 与 `write_csv_row()` 原先失败直接报错，无重试机制。
- **优化方案**：
  - `_request_json()` 增加最多 2 次重试 + 指数退避
  - `write_csv_row()` 增加最多 3 次重试（间隔 1 秒），并新增 `_csv_write_failures` 累计失败计数器，每 5 分钟在统计报告中输出

**5. `_die()` 退出时释放调度锁**

`_die()` 直接调用 `sys.exit(1)`，若在 `_scheduler_lock` 持锁区间调用会导致死锁。
- **修复方案**：退出前检查 `_scheduler_lock.locked()` 并释放锁。

**6. `_perform_cleanup()` 目录诊断**

CSV 目录不存在时原函数静默返回 0，与真正无过期文件的情况无法区分。
- **优化方案**：目录不存在时输出 `[清理] CSV 目录不存在，跳过清理` 的 stderr 警告。

**7. Webhook 多平台支持**

`_send_webhook()` 原先固定使用 Discord 格式的 `{"content": text}`，无法适配 Slack、企微、钉钉、飞书等平台。
- **优化方案**：
  - 新增 `_WEBHOOK_PAYLOAD_BUILDERS` 支持 6 种平台：`discord` / `slack` / `wecom`(企微) / `dingtalk`(钉钉) / `feishu`(飞书) / `auto`(自动检测)
  - 新增 `_detect_webhook_type()` 根据 URL 关键字自动识别平台
  - `monitor_config.json` 的 `webhook` 段新增 `"type"` 字段（默认 `"auto"`）

**8. 国债收益率缓存逻辑解耦**

`_get_treasury_payload()` 原先混合了缓存检查与数据获取逻辑，异常路径复用缓存时 `ts` 不更新。
- **优化方案**：抽取出独立的 `_fetch_treasury_payload()` 函数，`_get_treasury_payload()` 仅负责缓存调度。

**9. 修复 A 股指数/个股盘前竞价阶段价格为 0 的 Bug**

`_get_index_price()` 和 `_get_sina_stock_price()` 在盘前竞价阶段（9:15 之前及集合竞价期间）从新浪 API 获取的当前价为 `"0.000"`。`float("0")` 不会触发 `ValueError`，导致 `(0.0, -100%)` 被当作有效数据写入 CSV。
- **修复方案**：在 `_get_index_price()`、`_get_sina_stock_price()`、`_get_fx_price()` 中，解析 `current` 后增加 `if current <= 0: return None, None` 校验，盘前时段 CSV 写入空字符串而非 0。

> 修改位置：`price_monitor.py` → `DataCleaner.check_and_run()` / `_load_state` / `_save_state` / `_request_json` / `write_csv_row` / `_die` / `_perform_cleanup` / `_send_webhook` / `_get_treasury_payload` / `_get_index_price` / `_get_sina_stock_price` / `_get_fx_price` 等函数

### 2026-06-06

本次更新主要涉及以下四个方面：

**1. 内存限制调整（256MB → 512MB）**

程序在 Linux 下通过 `resource.setrlimit()` 控制进程虚拟内存上限。原有硬限制 256MB 在监控产品数量较多或网络波动（请求积压）时容易触发 OOM 杀死，现已放宽至 **512MB**。

> 修改位置：`price_monitor.py` → `_linux_set_resource_limits()` 函数

**2. 修复美国国债收益率数据源解析 Bug**

`_get_treasury_official_payload()` 函数在解析 U.S. Treasury 官网 HTML 收益率表格时存在逻辑缺陷：

- **原问题**：内部闭包 `_pick_latest_two()` 被调用三次（2 年 / 5 年 / 10 年），但每次均遍历同一组数据并提取**第一个数值列**（实际为 1 个月期收益率），导致三个期限得到完全相同的值。
- **修复方案**：增加表头行解析，通过关键词匹配定位各期限在 HTML 表格中的列索引，确保 2 年 / 5 年 / 10 年收益率各自正确。

**3. 正则表达式预编译优化**

所有模块级和函数内使用的正则表达式均改为**预编译为 `re.compile()` 对象**，避免每次调用时重复编译，提升运行时性能。涉及 10 组正则模式（空白分割、HTML 清洗、CNBC 收益率提取、邮箱校验、文件名转换等）。

**4. 更新环境要求**

README 内存最低要求从 256MB 同步更新为 512MB。

### 2026-05-23

**归档机制重构 — CSV 停止录入后 20 分钟自动触发**

- 归档不再依赖 VPS 系统时钟（原来的 `day_of_week`/`hour` 配置项已废弃）
- 主循环实时监测全球股市休市状态，一旦检测到 CSV 记录停止，启动 20 分钟倒计时，到点自动执行归档并推送通知
- 支持程序重启恢复：启动时若发现已休市超过 20 分钟且本周尚未归档，立即补归档
- 保留 `--archive-now` 手动触发方式供测试使用

**新增手动归档命令**

```
python price_monitor.py --archive-now
```

- 立即对本周 CSV 数据执行分类打包 + 推送通知，绕过定时调度
- 适用于新部署 VPS 后快速验证归档 → 通知全链路

**归档 ZIP 文件自动推送到客户端**

- `_notify` 函数新增 `attachments` 参数，支持附带文件发送
- **Telegram 模式**：先发文字通知，再逐文件调用 `sendDocument` API（上限 50MB）
- **SMTP 模式**：EmailMessage 添加 `application/zip` MIME 附件
- **Webhook 模式**：仅文字通知（提示不支持附件）
- 每周归档完成后，分类 ZIP 文件随通知自动发送

**新增 Telegram 连通性测试工具**

```
python price_monitor.py --test-telegram           # 测试 Bot Token + 文字消息
python price_monitor.py --test-telegram-file ./test.zip  # 测试文件发送
```

- `--test-telegram`：依次执行 getMe 验证 Token → sendMessage 验证 chat_id
- `--test-telegram-file`：通过 sendDocument API 发送指定文件
- 对常见错误（chat not found / bot blocked / unauthorized / 网络不通）输出中文排查指引

### 2026-05-22

**命令行接口完善**

- 新增 `--test-telegram`、`--test-telegram-file`、`--archive-now` 三个命令
- `--help` 输出包含所有命令示例

**端口要求文档化**

- README 新增「VPS 防火墙/端口要求」章节，明确列出 443/80/25/465/587 各端口用途与必需性
- 附常见云服务商默认出站策略速查表
