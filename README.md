# 多 VPS 集群一键批量安装与巡检控制台 (VPS Cluster Manager)

> 🚀 **统一配置与个性化定制兼备的高效集群管理工具**。专为多台海外 VPS、NAT 受限主机、纯净 Alpine 机器设计，一键批量部署 Sing-box 核心王牌协议（VLESS-Reality / Hysteria 2 / TUIC v5 / Cloudflare Argo），并无缝与 **[cf-node-manager-plus](https://github.com/kzhx666/cf-node-manager-plus)** 节点管理平台联动。

---

## ✨ 核心亮点与痛点解决

### 1. 统一策略配置 vs 单机个性化区分
- **全局统一设置（免重复输入）**：
  - 节点管理平台 URL 与 Token (`PUSH_URL`, `PUSH_TOKEN`)，安装后自动同步；
  - Cloudflare API 令牌 (`CF_DNS_TOKEN`)，用于所有指定域名的主机通过 DNS-01 自动化申请证书；
  - 默认三合一协议开关、默认 SSH 用户/端口、安装脚本源、并发控制。
- **单机个性化设置（精准定制）**：
  - 每台 VPS 独立的 IP、SSH 端口、密码或私钥；
  - 主节点独立订阅名称 (`TAG_MAIN`) 与 Argo 独立订阅名称 (`TAG_ARGO`)；
  - 专属 TLS 域名 (`domain`) 与专属 Argo 隧道 Token/域名。

### 2. NAT VPS 受限端口池智能规划
- 针对 NAT 主机仅分配 20 个或更少端口的场景：
  - 只需输入端口范围（例如 `10020-10039`）；
  - 控制台自动严格按序分配：`Reality=10020`, `TUIC=10021`, `Hy2=10022`；
  - 剩余端口（`10023-10039`）自动作为 Hysteria 2 端口跳跃池；若剩余端口不足 3 个，自动关闭跳跃，杜绝端口冲突！

### 3. DNS-01 域名证书自动化（无视 80 端口关闭）
- 传统 Standalone 申请证书需监听 80 端口，在 NAT VPS 或 80 端口被封的主机上必定失败；
- 本系统在全局配置一次 Cloudflare Token，VPS 只需填入域名，即可全自动通过 Cloudflare API 完成 DNS-01 验证，即使 NAT VPS 也能 100% 成功颁发 Let's Encrypt 证书！

### 4. 纯 IPv6 独轮车 VPS 自动注入 NAT64 / DNS64
- 无独立 IPv4 地址的 Pure IPv6 VPS 无法直接访问 GitHub、IPv4 源服务器或管理面板；
- 控制台在环境检测阶段自动探测 IPv4 连通性，对于 IPv6-Only 主机自动注入知名稳定 DNS64 解析服务（如 Hetzner / Trex），打通 IPv4 流量出口，无缝拉取 GitHub 脚本与官方二进制文件。

### 5. 极低内存 (128M / 256M / 512M) 自动挂载 1GB Swapfile
- 小内存 VPS 编译或并发下载解压时极易被 Linux OOM Killer 杀掉；
- 部署流水线自动识别物理内存 `< 1GB` 且未开启 Swap 的机器，自动安全创建并启用 1GB `/swapfile` 并写入 `/etc/fstab`，确保 Sing-box 与脚本平稳运行不闪退。

### 6. 甲骨文 / AWS 等云原生自带防火墙自动清障
- Oracle Cloud、AWS 镜像预置严格的 `iptables` / `ip6tables` REJECT 或 DROP 规则，导致即使安全组放行端口仍无法连通；
- 系统在环境自举中自动清理系统内阻碍入站流量的 DROP/REJECT 规则，并将默认 INPUT 策略设为 ACCEPT；部署前先执行端口占用排查，避免同端口冲突。

### 7. BBRv3 / BBR + fq 拥塞控制深度适配与持久化
- 优先探测并激活最新的 BBRv3（如高版本 XanMod / 原生支持内核），若不支持则自动激活标准 BBR + fq；
- 规则固化至 `/etc/sysctl.d/99-bbr.conf`，彻底杜绝 cloud-init 覆盖或重启失效。

### 8. Reality 专属伪装域名 (SNI) 单机定制与测速优选
- 默认支持并发对国内友好 CDN 节点进行 TLS 1.3 握手时延测速优选；
- 支持为每台 VPS 单独指定专属伪装域名（例如 `swdist.apple.com`、`dl.google.com`），满足不同机房、不同阻断策略的灵活应对。

### 9. 最小化 Linux 与 Alpine 环境自举 & NTP 时钟校准
- 纯净机或 Alpine 镜像缺少 `curl`, `wget`, `jq`, `tar`, `bash` 等基础工具；
- 控制台在部署前自动执行底层自举脚本：
  - 兼容 `apk` (Alpine Linux)、`apt-get` (Debian/Ubuntu)、`dnf`/`yum` (CentOS/Alma/Rocky/Fedora)、`pacman` (Arch)；
  - 自动通过 `chrony` 或 `ntpdate` 执行 NTP 毫秒级对时（彻底避免 Reality 协议因时间偏差 >90s 导致客户端无法握手）；
  - 自动适配 Alpine 的 `openrc` 与标准系统的 `systemd`。

### 10. VPS 多选机制与底部悬浮批量操作栏
- **灵活多选自定义**：支持单选勾选、当前页全选、反向选择，满足对特定子集 VPS 进行批量调度的需求；
- **智能悬浮操作坞 (Floating Dock)**：勾选任意主机后，底部自动呼出半透明赛博悬浮操作栏，支持针对已选主机一键发起：
  - 🚀 并发批量部署
  - 🩺 并发批量巡检
  - 🔄 批量重启 Sing-box 服务
  - ⬆️ 批量升级 Sing-box 核心
  - ⚡ 批量刷新激活 BBR+fq
  - 🗑️ 批量彻底卸载节点全部组件
  - ❌ 批量从面板列表移除记录

### 11. 一键彻底深度卸载（删除节点脚本安装的所有东西）
- 一键停止并禁用 `sing-box`、`cloudflared` (Argo)、`sb-hop` 等所有关联服务与进程；
- 彻底清理 `/usr/local/bin/sing-box`、`/usr/local/bin/sb`、`/usr/local/bin/cloudflared` 等所有二进制执行文件与软链接；
- 彻底删除 `/etc/sing-box` 配置与状态、`/etc/cloudflared` 隧道、SSL 证书、所有日志及 Crontab 计划任务；
- 彻底清除 `iptables` / `ip6tables` / `nftables` 中的端口跳跃重定向规则；
- 重置 VPS 在控制台中的状态为「⚪ 待部署」，面板和 VPS 均恢复初始干净状态。

### 12. 实时可视化终端抽屉与流媒体解锁检测
- 部署任务具备独立实时日志通道，前台黑色终端控制台自动逐行滚动展示远端构建日志；
- 一键集群健康巡检：Ping 延迟、Sing-box 进程、监听端口、Argo 状态；
- 一键流媒体与 AI 解锁测试：自动检测 ChatGPT、YouTube Premium、Netflix 解锁等级。

---

## 🛠️ 快速启动（默认安装到 /opt 目录，端口 5001）

### 方式一：Linux 一键安装脚本（推荐，自动注册开机自启）

```bash
curl -fsSL https://raw.githubusercontent.com/kzhx666/vps-cluster-manager/main/install.sh | bash
```
> 安装完成后可直接使用快捷命令管理：`vps-cluster-manager start/stop/restart/status/logs/update/uninstall`。

### 方式二：Docker Compose（推荐）

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/kzhx666/vps-cluster-manager.git
cd /opt/vps-cluster-manager
docker compose up -d --build
```
打开浏览器访问：`http://你的服务器IP:5001`

### 方式三：Python 原生直接运行

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/kzhx666/vps-cluster-manager.git
cd /opt/vps-cluster-manager
pip install -r requirements.txt
python app.py
```
默认监听 `http://0.0.0.0:5001`（端口可通过环境变量 `PORT` 修改）。

---

## 🔄 一键更新与升级指南（保留所有已有 VPS 配置）

面板内置多重防丢失保障机制，无论通过以下哪种方式升级，所有已录入的 VPS 节点数据与全局设置均完好保留：

### 方法一：快捷管理命令（推荐，最简便）
如果此前使用一键脚本安装，直接在终端执行：
```bash
vps-cluster-manager update
```
系统将自动同步 GitHub 最新代码、安装新依赖并平滑重启服务。

### 方法二：手动单行命令更新（通用无损升级）
```bash
cd /opt/vps-cluster-manager && git fetch --all && git reset --hard origin/main && systemctl restart vps-cluster-manager
```

### 方法三：重新执行一键安装脚本（安全覆盖升级）
```bash
curl -fsSL https://raw.githubusercontent.com/kzhx666/vps-cluster-manager/main/install.sh | bash
```
> 安装脚本在拉取代码前会自动将当前配置备份至 `/var/backups`，升级完成后自动还原历史 VPS 节点。

### 方法四：Docker 容器升级
```bash
cd /opt/vps-cluster-manager && git pull && docker compose up -d --build
```

---

### 💡 历史 VPS 数据找回提示
如果您此前在更改默认安装路径（`/opt`）之前添加过 VPS，旧数据通常保存在原运行目录（例如 `/root/vps-cluster-manager/data/config.json`），只需一条命令复制并重启即可找回：
```bash
cp /root/vps-cluster-manager/data/config.json /opt/vps-cluster-manager/data/config.json && systemctl restart vps-cluster-manager
```
或者全局搜索服务器上的备份文件：
```bash
find / -name "config.json" 2>/dev/null
```

## 📋 目录架构

```
vps-cluster-manager/
├── app.py                     # Flask 后端核心，多线程 SSH 部署引擎与 API
├── install.sh                 # Linux 一键安装与系统服务注册脚本 (默认装到 /opt)
├── requirements.txt           # Python 核心依赖 (Flask, paramiko, requests)
├── Dockerfile                 # 容器构建配置
├── docker-compose.yml         # 容器一键编排 (映射 5001 端口)
├── data/                      # 持久化存储目录
│   └── config.json            # 集群服务器凭据与全局策略
├── static/
│   ├── css/
│   │   └── style.css          # Cyber Dark 现代毛玻璃 UI 样式库
│   └── js/
│       └── main.js            # 响应式交互、多选与实时终端日志轮询引擎
├── templates/
│   └── index.html             # 单页管理控制台
└── README.md                  # 项目使用与配置说明文档
```

---

## ⚙️ 批量导入格式与连通性测试

在控制台点击 **「批量导入」**，支持设置**统一名称前缀**（多台自动按 名称-1、名称-2 递增，单台直接使用该名称），并可在导入前点击 **「⚡ 连通性测试」** 并发测试 SSH 握手、延迟与系统类型：

```text
# 格式 1: IP:端口:用户:密码 (推荐，例如配合统一名称前缀)
1.2.3.4:22:root:MyPassword123
5.6.7.8:22022:root:NatPassword456

# 格式 2: 自定义名称:IP:端口:用户:密码 (单行指定独立别名)
香港CN2:1.2.3.4:22:root:MyPass123
哈基猫美区:5.6.7.8:22022:root:SecPass456

# 格式 3: IP:端口:用户:密码:自定义名称 (尾部指定名称)
1.2.3.4:22:root:MyPass123:日本软银

# 格式 4: IP:密码 (默认端口 22，用户 root)
2.3.4.5:RootPassword789

# 格式 5: 用户:密码@IP:端口
root:MyPassword123@1.2.3.4:22
```
> 💡 点击 **「连通性测试」** 可实时查看每台主机的 SSH 连通状态、网络时延 (ms) 及操作系统版本；测试完成后可选择 **「仅导入连通成功的主机」** 或 **「确认导入全部」**。

---

## 🤝 联动生态

- **[sb-install-plus](https://github.com/kzhx666/sb-install-plus)**: 极速单机与集群底层脚本，无缝兼容自动化静默安装与交互式菜单
- **[cf-node-manager-plus](https://github.com/kzhx666/cf-node-manager-plus)**: 现代化节点管理平台与订阅转换中心，支持优选域名智能匹配与探针流量联动
