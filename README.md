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

### 10. 后台定时静默巡检与批量运维工具箱
- **定时自动健康巡检**：可在全局配置中自定义巡检周期（如 60 分钟），后台守护线程定时探测各 VPS 进程与延迟，面板随时展示最新健康状况；
- **批量运维下拉菜单**：一键并发执行批量重启 Sing-box、批量升级 Sing-box 官方核心、批量刷新激活 BBRv3、批量彻底卸载清理，实时日志控制台全程滚动输出。

### 11. 实时可视化终端抽屉与流媒体解锁检测
- 部署任务具备独立实时日志通道，前台黑色终端控制台自动逐行滚动展示远端构建日志；
- 一键集群健康巡检：Ping 延迟、Sing-box 进程、监听端口、Argo 状态；
- 一键流媒体与 AI 解锁测试：自动检测 ChatGPT、YouTube Premium、Netflix 解锁等级。

---

## 🛠️ 快速启动

### 方式一：Docker Compose（推荐，一行命令）

```bash
cd vps-cluster-manager
docker compose up -d
```
打开浏览器访问：`http://你的服务器IP:5005`

### 方式二：Python 原生运行

```bash
cd vps-cluster-manager
pip install -r requirements.txt
python app.py
```
默认监听 `http://0.0.0.0:5005`（端口可通过环境变量 `PORT` 修改）。

---

## 📋 目录架构

```
vps-cluster-manager/
├── app.py                     # Flask 后端核心，多线程 SSH 部署引擎与 API
├── requirements.txt           # Python 核心依赖 (Flask, paramiko, requests)
├── Dockerfile                 # 容器构建配置
├── docker-compose.yml         # 容器一键编排
├── data/                      # 持久化存储目录
│   └── config.json            # 集群服务器凭据与全局策略
├── static/
│   ├── css/
│   │   └── style.css          # Cyber Dark 现代毛玻璃 UI 样式库
│   └── js/
│       └── main.js            # 响应式交互、实时终端日志轮询引擎
├── templates/
│   └── index.html             # 单页管理控制台
└── README.md                  # 项目使用与配置说明文档
```

---

## ⚙️ 批量导入格式支持

在控制台点击 **「批量导入」**，支持多行直接粘贴：

```text
# 格式 1: IP:端口:用户:密码 (推荐)
1.2.3.4:22:root:MyPassword123
5.6.7.8:22022:root:NatPassword456

# 格式 2: IP:密码 (默认端口 22，用户 root)
2.3.4.5:RootPassword789

# 格式 3: 用户:密码@IP:端口
root:MyPassword123@1.2.3.4:22
```

---

## 🤝 联动生态

- **[sb-install-plus](https://github.com/kzhx666/sb-install-plus)**: 极速单机与集群底层脚本，无缝兼容自动化静默安装与交互式菜单
- **[cf-node-manager-plus](https://github.com/kzhx666/cf-node-manager-plus)**: 现代化节点管理平台与订阅转换中心，支持优选域名智能匹配与探针流量联动
