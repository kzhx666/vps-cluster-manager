import os
import sys
import json
import uuid
import time
import re
import socket
import threading
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file, Response

app = Flask(__name__, static_folder='static', template_folder='templates')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

os.makedirs(DATA_DIR, exist_ok=True)

# Try importing paramiko; if unavailable, fallback runner will be used
try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

config_lock = threading.Lock()
tasks = {}
tasks_lock = threading.Lock()

DEFAULT_CONFIG = {
    "global": {
        "push_url": "http://23.254.150.14:5000",
        "push_token": "",
        "auto_push": True,
        "cf_dns_token": "",
        "default_protocols": {
            "reality": True,
            "hy2": True,
            "tuic": True
        },
        "default_ssh_port": 22,
        "default_ssh_user": "root",
        "install_script_url": "https://raw.githubusercontent.com/kzhx666/sb-install-plus/main/install.sh",
        "concurrency": 5,
        "auto_inspect": False,
        "auto_inspect_interval": 60
    },
    "servers": []
}

def load_config():
    with config_lock:
        data = None
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[ERROR] Failed to load config from {CONFIG_FILE}: {e}")

        # If data is missing or has empty servers list, check historical paths and backup files
        if not data or not data.get("servers"):
            candidates = [
                "/root/vps-cluster-manager/data/config.json",
                "/root/vps-cluster-manager/config.json",
                "/var/backups/vps-cluster-manager-config.json",
                os.path.join(DATA_DIR, "config.json.bak"),
                "/tmp/vps_cm_config_backup.json"
            ]
            for cand in candidates:
                if os.path.isfile(cand) and cand != CONFIG_FILE:
                    try:
                        with open(cand, 'r', encoding='utf-8') as f:
                            cand_data = json.load(f)
                            if cand_data.get("servers"):
                                print(f"[INFO] Auto-migrated {len(cand_data['servers'])} servers from historical backup {cand}")
                                data = cand_data
                                save_config_unlocked(data)
                                break
                    except Exception:
                        pass

        if not data:
            data = json.loads(json.dumps(DEFAULT_CONFIG))
            save_config_unlocked(data)
            return data

        # merge with defaults if keys missing
        for k, v in DEFAULT_CONFIG["global"].items():
            if k not in data.get("global", {}):
                data.setdefault("global", {})[k] = v

        # Auto-migrate and normalize legacy server subscription tags:
        # tag_main must be [name]本机, tag_argo must be [name]优选
        modified_tags = False
        for s in data.get("servers", []):
            base_name = (s.get("name") or s.get("host") or "").strip()
            if not base_name:
                continue

            tag_m = (s.get("tag_main") or "").strip()
            if not tag_m or tag_m == base_name or tag_m == s.get("host", "").strip():
                s["tag_main"] = f"{base_name}本机"
                modified_tags = True

            tag_a = (s.get("tag_argo") or "").strip()
            if not tag_a or tag_a.endswith("-Argo") or tag_a == base_name or tag_a == s.get("host", "").strip():
                s["tag_argo"] = f"{base_name}优选"
                modified_tags = True

        if modified_tags:
            save_config_unlocked(data)

        return data

def save_config_unlocked(data):
    try:
        temp_file = CONFIG_FILE + '.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
        os.rename(temp_file, CONFIG_FILE)

        # Dual backup persistence: directory backup + system-wide backup
        try:
            bak_file = os.path.join(DATA_DIR, 'config.json.bak')
            with open(bak_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.makedirs('/var/backups', exist_ok=True)
            with open('/var/backups/vps-cluster-manager-config.json', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    except Exception as e:
        print(f"[ERROR] Failed to save config: {e}")

def save_config(data):
    with config_lock:
        save_config_unlocked(data)

# ------------------------------------------------------------------------------
# SSH Runner Helper
# ------------------------------------------------------------------------------
class SSHRunner:
    def __init__(self, host, port=22, user='root', password=None, private_key=None, timeout=12):
        self.host = host
        self.port = int(port or 22)
        self.user = user or 'root'
        self.password = password or ''
        self.private_key = private_key or ''
        self.timeout = timeout
        self.client = None

    def connect(self):
        if PARAMIKO_AVAILABLE:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = {
                'hostname': self.host,
                'port': self.port,
                'username': self.user,
                'timeout': self.timeout,
                'banner_timeout': self.timeout,
                'auth_timeout': self.timeout
            }
            if self.private_key and self.private_key.strip():
                import io
                key_file = io.StringIO(self.private_key.strip())
                pkey = None
                for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                    key_file.seek(0)
                    try:
                        pkey = key_cls.from_private_key(key_file)
                        break
                    except Exception:
                        continue
                if pkey:
                    kwargs['pkey'] = pkey
                else:
                    raise Exception("无法解析提供的私钥，请确认私钥格式(RSA/Ed25519/ECDSA)")
            else:
                kwargs['password'] = self.password

            client.connect(**kwargs)
            self.client = client
            return True
        else:
            # Test TCP connection first
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            result = sock.connect_ex((self.host, self.port))
            sock.close()
            if result != 0:
                raise Exception(f"无法连接目标服务器端口 {self.host}:{self.port}")
            return True

    def run_cmd(self, cmd, log_callback=None, timeout=300):
        if PARAMIKO_AVAILABLE and self.client:
            transport = self.client.get_transport()
            channel = transport.open_session()
            channel.set_combine_stderr(True)
            channel.get_pty()
            channel.settimeout(timeout)
            channel.exec_command(cmd)

            output_lines = []
            buf = ""
            start_time = time.time()

            while True:
                if channel.recv_ready():
                    data = channel.recv(4096).decode('utf-8', errors='replace')
                    if not data:
                        break
                    buf += data
                    while '\n' in buf:
                        line, buf = buf.split('\n', 1)
                        clean_line = line.replace('\r', '')
                        output_lines.append(clean_line)
                        if log_callback:
                            log_callback(clean_line)
                elif channel.exit_status_ready():
                    if buf:
                        clean_line = buf.replace('\r', '')
                        output_lines.append(clean_line)
                        if log_callback:
                            log_callback(clean_line)
                    break
                else:
                    time.sleep(0.05)
                if time.time() - start_time > timeout:
                    channel.close()
                    raise TimeoutError(f"命令执行超时 ({timeout}s)")

            exit_code = channel.recv_exit_status()
            channel.close()
            return exit_code, "\n".join(output_lines)
        else:
            # Fallback to local ssh subprocess
            ssh_cmd = [
                'ssh',
                '-o', 'StrictHostKeyChecking=no',
                '-o', f'ConnectTimeout={self.timeout}',
                '-p', str(self.port),
                f'{self.user}@{self.host}',
                cmd
            ]
            proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
            output_lines = []
            for line in proc.stdout:
                clean_line = line.rstrip('\r\n')
                output_lines.append(clean_line)
                if log_callback:
                    log_callback(clean_line)
            proc.wait()
            return proc.returncode, "\n".join(output_lines)

    def close(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

# ------------------------------------------------------------------------------
# Task Tracker Helper
# ------------------------------------------------------------------------------
def create_task(task_type, server_id, server_name=""):
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "type": task_type,
        "server_id": server_id,
        "server_name": server_name,
        "status": "running",
        "progress": 5,
        "step": "准备执行任务...",
        "logs": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "error": None
    }
    with tasks_lock:
        tasks[task_id] = task
    return task_id

def append_task_log(task_id, message, step=None, progress=None):
    with tasks_lock:
        if task_id in tasks:
            ts = datetime.now().strftime("%H:%M:%S")
            tasks[task_id]["logs"].append(f"[{ts}] {message}")
            if step is not None:
                tasks[task_id]["step"] = step
            if progress is not None:
                tasks[task_id]["progress"] = progress

def finish_task(task_id, success=True, error=None):
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id]["status"] = "success" if success else "error"
            tasks[task_id]["progress"] = 100 if success else tasks[task_id]["progress"]
            tasks[task_id]["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if error:
                tasks[task_id]["error"] = str(error)
                ts = datetime.now().strftime("%H:%M:%S")
                tasks[task_id]["logs"].append(f"[{ts}] [ERR] {error}")

# ------------------------------------------------------------------------------
# Helpers: Token Cleaning
# ------------------------------------------------------------------------------
def clean_argo_token(val):
    """Clean Argo Tunnel token by stripping command prefixes and trailing options."""
    if not val:
        return ""
    val = str(val).strip()
    idx = val.find("eyJ")
    if idx != -1:
        val = val[idx:]
        val = val.split()[0].strip("'\"`;")
    return val.strip()

# ------------------------------------------------------------------------------
# NAT Port Allocation Calculator
# ------------------------------------------------------------------------------
def calculate_ports(server, global_cfg):
    port_mode = server.get('port_mode', 'auto')
    port_range = server.get('port_range', '').strip()
    protocols = global_cfg.get('default_protocols', {})
    enable_r = 1 if protocols.get('reality', True) else 0
    enable_h = 1 if protocols.get('hy2', True) else 0
    enable_t = 1 if protocols.get('tuic', True) else 0

    if port_mode == 'nat' and port_range:
        # Expected format: "10020-10039" or "10020,10021,10022..."
        if '-' in port_range:
            parts = port_range.split('-')
            start_p = int(parts[0].strip())
            end_p = int(parts[1].strip())
            port_list = list(range(start_p, end_p + 1))
        elif ',' in port_range:
            port_list = [int(p.strip()) for p in port_range.split(',') if p.strip().isdigit()]
        else:
            port_list = [int(port_range.strip())]

        needed_ports = enable_r + enable_h + enable_t
        if len(port_list) < needed_ports:
            raise ValueError(f"NAT 端口池数量不足！已启用 {needed_ports} 个协议，但仅提供了 {len(port_list)} 个端口。")

        idx = 0
        p_reality = port_list[idx] if enable_r else 0
        if enable_r: idx += 1
        p_hy2 = port_list[idx] if enable_h else 0
        if enable_h: idx += 1
        p_tuic = port_list[idx] if enable_t else 0
        if enable_t: idx += 1

        remaining_ports = port_list[idx:]
        hop_range = ""
        if len(remaining_ports) >= 3 and enable_h:
            # Build hop range string
            hop_range = f"{remaining_ports[0]}-{remaining_ports[-1]}"

        return {
            "mode": "nat",
            "p_reality": p_reality,
            "p_hy2": p_hy2,
            "p_tuic": p_tuic,
            "hop_range": hop_range,
            "allocated_summary": f"Reality:{p_reality} Hy2:{p_hy2} TUIC:{p_tuic} 跳跃:{hop_range or '无'}"
        }
    else:
        # Standard auto mode: find free ports or leave empty for installer auto-detect
        return {
            "mode": "auto",
            "p_reality": 0,
            "p_hy2": 0,
            "p_tuic": 0,
            "hop_range": "",
            "allocated_summary": "标准全端口 (安装脚本动态随机安全高位端口 + 默认跳跃)"
        }

# ------------------------------------------------------------------------------
# Distro Bootstrap & Time Calibration Shell Script
# ------------------------------------------------------------------------------
BOOTSTRAP_SCRIPT = r'''#!/bin/sh
set -e
info() { echo "[BOOTSTRAP] $*"; }

OS_NAME="unknown"
OS_VER="unknown"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  OS_NAME="${ID:-unknown}"
  OS_VER="${VERSION_ID:-unknown}"
fi
ARCH="$(uname -m)"

info "Detected OS: $OS_NAME $OS_VER ($ARCH)"

# ==============================================================================
# 1. 纯 IPv6 VPS 检测与公共 NAT64 / DNS64 自动打通
# ==============================================================================
if ! curl -s4m2 https://1.1.1.1 >/dev/null 2>&1 && ! ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
  if curl -s6m2 https://[2606:4700:4700::1111] >/dev/null 2>&1 || ping6 -c 1 -W 2 2606:4700:4700::1111 >/dev/null 2>&1; then
    info "检测到服务器为纯 IPv6 VPS (无直接 IPv4 外网出站路由)！"
    info "正在自动配置公共 NAT64 / DNS64 翻译解析，打通全球 IPv4 与 GitHub 访问通道..."
    if ! grep -q "2a01:4f9:c010:3f02::1" /etc/resolv.conf 2>/dev/null; then
      cp /etc/resolv.conf /etc/resolv.conf.bak 2>/dev/null || true
      cat > /tmp/resolv.conf <<'EOF_DNS'
nameserver 2a01:4f9:c010:3f02::1
nameserver 2a00:1098:2b::1
nameserver 2a00:1098:2c::1
EOF_DNS
      cat /etc/resolv.conf >> /tmp/resolv.conf 2>/dev/null || true
      cp /tmp/resolv.conf /etc/resolv.conf 2>/dev/null || true
      rm -f /tmp/resolv.conf
      info "NAT64 / DNS64 注入完成，纯 IPv6 主机已获得 IPv4 通信能力！"
    fi
  fi
fi

# ==============================================================================
# 2. 内存检测与智能虚拟内存 (Swap) 自动挂载 (防小内存 OOM 爆死)
# ==============================================================================
MEM_TOTAL=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
SWAP_TOTAL=$(awk '/SwapTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$MEM_TOTAL" -lt 1048576 ] && [ "$SWAP_TOTAL" -lt 524288 ]; then
  info "检测到服务器物理内存不足 1GB ($((MEM_TOTAL/1024))MB) 且缺少 Swap！"
  info "正在创建 1GB 虚拟内存 (Swapfile) 以防止包管理和进程 OOM 爆内存卡死..."
  if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 2>/dev/null || true
    if [ -f /swapfile ]; then
      chmod 600 /swapfile
      mkswap /swapfile >/dev/null 2>&1 || true
      swapon /swapfile >/dev/null 2>&1 || true
      if ! grep -q "/swapfile" /etc/fstab 2>/dev/null; then
        echo "/swapfile swap swap defaults 0 0" >> /etc/fstab 2>/dev/null || true
      fi
      info "1GB 虚拟内存 (Swapfile) 已成功激活挂载！"
    fi
  fi
fi

# ==============================================================================
# 3. 清理云厂商 (如甲骨文 Oracle、AWS) 默认入站阻断链并放行
# ==============================================================================
info "正在优化防火墙入站规则，清理 Oracle/AWS 默认 REJECT 阻断策略..."
if command -v iptables >/dev/null 2>&1; then
  iptables -D INPUT -j REJECT 2>/dev/null || true
  iptables -D FORWARD -j REJECT 2>/dev/null || true
  iptables -P INPUT ACCEPT 2>/dev/null || true
  iptables -P FORWARD ACCEPT 2>/dev/null || true
fi
if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -D INPUT -j REJECT 2>/dev/null || true
  ip6tables -D FORWARD -j REJECT 2>/dev/null || true
  ip6tables -P INPUT ACCEPT 2>/dev/null || true
  ip6tables -P FORWARD ACCEPT 2>/dev/null || true
fi

# Package Manager Bootstrap
if command -v apk >/dev/null 2>&1; then
  info "Configuring Alpine Linux (apk)..."
  apk update >/dev/null 2>&1 || true
  apk add --no-cache curl wget jq tar ca-certificates bash openrc tzdata chrony iptables ip6tables >/dev/null 2>&1 || true
elif command -v apt-get >/dev/null 2>&1; then
  info "Configuring Debian/Ubuntu (apt-get)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq curl wget jq tar ca-certificates bash chrony iptables iproute2 dnsutils >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
  info "Configuring RHEL/Fedora/Rocky (dnf)..."
  dnf install -y -q curl wget jq tar ca-certificates bash chrony iptables iproute bind-utils >/dev/null 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  info "Configuring CentOS/RHEL (yum)..."
  yum install -y -q curl wget jq tar ca-certificates bash chrony iptables iproute bind-utils >/dev/null 2>&1 || true
elif command -v pacman >/dev/null 2>&1; then
  info "Configuring Arch Linux (pacman)..."
  pacman -Sy --noconfirm curl wget jq tar ca-certificates bash chrony iptables iproute2 >/dev/null 2>&1 || true
fi

# Ensure /bin/bash exists
if [ ! -x /bin/bash ] && command -v bash >/dev/null 2>&1; then
  ln -sf "$(command -v bash)" /bin/bash 2>/dev/null || true
fi

# Time Calibration (Reality requires time sync)
info "Calibrating server time via NTP..."
if command -v chronyd >/dev/null 2>&1; then
  chronyd -q 'server pool.ntp.org iburst' >/dev/null 2>&1 || true
elif command -v ntpdate >/dev/null 2>&1; then
  ntpdate -u pool.ntp.org >/dev/null 2>&1 || true
fi

# ==============================================================================
# BBR + fq 智能检测与启用 (支持优先启用 BBRv3 / BBR)
# ==============================================================================
info "正在检查 TCP 拥塞控制与队列算法 (BBR / fq)..."
CUR_CC="$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || echo '')"
CUR_QDISC="$(sysctl -n net.core.default_qdisc 2>/dev/null || cat /proc/sys/net/core/default_qdisc 2>/dev/null || echo '')"

BBR_STATUS=""
if echo "$CUR_CC" | grep -Eq '^(bbr|bbr3|bbrplus)$' && echo "$CUR_QDISC" | grep -Eq '^(fq|fq_pie|cake)$'; then
  info "检测到系统已开启 ${CUR_CC} + ${CUR_QDISC}，性能已达标，跳过安装！"
  BBR_STATUS="${CUR_CC}+${CUR_QDISC}"
else
  info "当前拥塞算法: ${CUR_CC:-未知}, 队列: ${CUR_QDISC:-未知}，准备激活最新 BBRv3 / BBR + fq..."

  # 1. 尝试探测并加载 BBRv3 / BBR 内核模块
  modprobe tcp_bbr3 2>/dev/null || true
  modprobe tcp_bbr 2>/dev/null || true

  # 2. 写入模块自启加载
  if [ -d /etc/modules-load.d ]; then
    echo "tcp_bbr3" > /etc/modules-load.d/99-bbr.conf 2>/dev/null || true
    echo "tcp_bbr" >> /etc/modules-load.d/99-bbr.conf 2>/dev/null || true
  fi
  if [ -f /etc/modules ]; then
    grep -q "tcp_bbr" /etc/modules 2>/dev/null || echo "tcp_bbr" >> /etc/modules 2>/dev/null || true
  fi

  # 3. 优选算法: bbr3 > bbr > bbrplus
  AVAIL_CC="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || cat /proc/sys/net/ipv4/tcp_available_congestion_control 2>/dev/null || echo '')"
  TARGET_CC="bbr"
  if echo "$AVAIL_CC" | grep -qw "bbr3"; then
    TARGET_CC="bbr3"
    info "检测到内核原生支持最新一代 BBRv3，优先选定 bbr3！"
  elif echo "$AVAIL_CC" | grep -qw "bbr"; then
    TARGET_CC="bbr"
    info "选定 BBR 极速拥塞控制算法！"
  elif echo "$AVAIL_CC" | grep -qw "bbrplus"; then
    TARGET_CC="bbrplus"
    info "选定 BBRplus 拥塞控制算法！"
  else
    modprobe tcp_bbr 2>/dev/null || true
    TARGET_CC="bbr"
  fi

  # 4. 写入 sysctl 内核调优配置
  mkdir -p /etc/sysctl.d 2>/dev/null || true
  cat > /etc/sysctl.d/99-bbr.conf <<EOF
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = ${TARGET_CC}
EOF

  if [ -f /etc/sysctl.conf ]; then
    sed -i '/net.core.default_qdisc/d' /etc/sysctl.conf 2>/dev/null || true
    sed -i '/net.ipv4.tcp_congestion_control/d' /etc/sysctl.conf 2>/dev/null || true
    echo "net.core.default_qdisc = fq" >> /etc/sysctl.conf
    echo "net.ipv4.tcp_congestion_control = ${TARGET_CC}" >> /etc/sysctl.conf
  fi

  # 5. 应用生效
  sysctl -p /etc/sysctl.d/99-bbr.conf >/dev/null 2>&1 || sysctl -p >/dev/null 2>&1 || sysctl --system >/dev/null 2>&1 || true

  # 6. 回读并校验
  FINAL_CC="$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || echo '')"
  FINAL_QDISC="$(sysctl -n net.core.default_qdisc 2>/dev/null || cat /proc/sys/net/core/default_qdisc 2>/dev/null || echo '')"
  if echo "$FINAL_CC" | grep -Eq '^(bbr|bbr3|bbrplus)$'; then
    info "BBR 激活成功！当前内核运行状态: ${FINAL_CC} + ${FINAL_QDISC}"
    BBR_STATUS="${FINAL_CC}+${FINAL_QDISC}"
  else
    info "已配置 BBR+fq (受限容器环境反馈: ${FINAL_CC:-未知}+${FINAL_QDISC:-未知})"
    BBR_STATUS="${FINAL_CC}+${FINAL_QDISC}"
  fi
fi

NOW_UTC="$(date -u +%s)"
echo "PREFLIGHT_RESULT:{\"os\":\"$OS_NAME\",\"version\":\"$OS_VER\",\"arch\":\"$ARCH\",\"time\":$NOW_UTC,\"bbr\":\"$BBR_STATUS\"}"
'''

# ------------------------------------------------------------------------------
# Worker: Single Server Deployment
# ------------------------------------------------------------------------------
def deploy_server_worker(task_id, server_id):
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        finish_task(task_id, success=False, error=f"未找到服务器 ID: {server_id}")
        return

    global_cfg = cfg.get("global", {})
    host = server.get("host")
    port = server.get("port", 22)
    user = server.get("user", "root")
    pwd = server.get("password")
    key = server.get("private_key")
    server_name = server.get("name", host)

    append_task_log(task_id, f"正在连接目标服务器 [{server_name}] ({host}:{port})...", step="连接服务器", progress=10)

    runner = SSHRunner(host, port, user, pwd, key)
    try:
        runner.connect()
        append_task_log(task_id, "SSH 连接成功！开始环境检查与底层依赖自举...", step="底层环境自举", progress=20)

        # 1. Run Pre-flight Bootstrap
        def log_cb(line):
            append_task_log(task_id, line)

        code, out = runner.run_cmd(BOOTSTRAP_SCRIPT, log_callback=log_cb, timeout=180)
        if code != 0:
            raise Exception(f"环境自举脚本执行失败 (退出代码: {code})")

        # Parse preflight result
        preflight_info = {}
        for line in out.splitlines():
            if line.startswith("PREFLIGHT_RESULT:"):
                try:
                    preflight_info = json.loads(line.replace("PREFLIGHT_RESULT:", "").strip())
                except Exception:
                    pass

        os_str = f"{preflight_info.get('os', 'Linux')} {preflight_info.get('version', '')}".strip()
        arch_str = preflight_info.get('arch', 'x86_64')
        bbr_str = preflight_info.get('bbr', '')
        bbr_log = f", 内核拥塞控制 [{bbr_str}]" if bbr_str else ""
        append_task_log(task_id, f"环境自举完成: 操作系统 [{os_str}], 架构 [{arch_str}]{bbr_log}, 时间已完成 NTP 校准", step="端口与环境准备", progress=40)

        # 2. Port Calculation
        port_info = calculate_ports(server, global_cfg)
        append_task_log(task_id, f"端口策略已就绪: {port_info['allocated_summary']}", progress=50)

        # 3. Assemble Environment Variables
        script_url = global_cfg.get("install_script_url", "https://raw.githubusercontent.com/kzhx666/sb-install-plus/main/install.sh")
        push_url = global_cfg.get("push_url", "")
        push_token = global_cfg.get("push_token", "")
        auto_push = 1 if global_cfg.get("auto_push", True) else 0

        tag_main = server.get("tag_main", "").strip()
        if not tag_main or tag_main == server_name or tag_main == server.get("host"):
            tag_main = f"{server_name}本机"

        tag_argo = server.get("tag_argo", "").strip()
        if not tag_argo or tag_argo.endswith("-Argo") or tag_argo == server_name or tag_argo == server.get("host"):
            tag_argo = f"{server_name}优选"

        custom_domain = server.get("domain", "").strip()
        cf_dns_token = global_cfg.get("cf_dns_token", "").strip()

        argo_enabled = server.get("argo_enabled", False)
        argo_domain = server.get("argo_domain", "").strip()
        argo_token = clean_argo_token(server.get("argo_token", ""))

        env_exports = [
            "export AUTO_INSTALL=1",
            f"export AUTO_PUSH={auto_push}",
            f"export PUSH_URL=\"{push_url}\"",
            f"export PUSH_TOKEN=\"{push_token}\"",
            f"export PUSH_TAG_MAIN=\"{tag_main}\"",
            f"export PUSH_TAG_ARGO=\"{tag_argo}\""
        ]

        # Port mapping
        if port_info["mode"] == "nat":
            env_exports.append(f"export PORT_REALITY={port_info['p_reality']}")
            env_exports.append(f"export PORT_HY2={port_info['p_hy2']}")
            env_exports.append(f"export PORT_TUIC={port_info['p_tuic']}")
            if port_info["hop_range"]:
                env_exports.append(f"export SB_HOP_RANGE=\"{port_info['hop_range']}\"")
            else:
                env_exports.append("export SB_HOP_RANGE=\"\"")

        # Protocols
        protocols = global_cfg.get("default_protocols", {})
        env_exports.append(f"export SB_ENABLE_REALITY={1 if protocols.get('reality', True) else 0}")
        env_exports.append(f"export SB_ENABLE_HY2={1 if protocols.get('hy2', True) else 0}")
        env_exports.append(f"export SB_ENABLE_TUIC={1 if protocols.get('tuic', True) else 0}")

        # Reality SNI (custom or auto-select)
        reality_sni = server.get("reality_sni", "").strip()
        if reality_sni:
            env_exports.append(f"export REALITY_SNI=\"{reality_sni}\"")

        # Domain & ACME
        if custom_domain:
            env_exports.append(f"export INPUT_DOMAIN=\"{custom_domain}\"")
            if cf_dns_token:
                env_exports.append("export SB_ACME_MODE=\"dns\"")
                env_exports.append(f"export SB_CF_TOKEN=\"{cf_dns_token}\"")
            else:
                env_exports.append("export SB_ACME_MODE=\"standalone\"")

        # Argo
        if argo_enabled and argo_domain and argo_token:
            env_exports.append(f"export SB_ARGO_DOMAIN=\"{argo_domain}\"")
            env_exports.append(f"export SB_ARGO_TOKEN=\"{argo_token}\"")

        # Command to run installer
        deploy_cmd = f"""
{chr(10).join(env_exports)}
curl -fsSL "{script_url}" -o /tmp/sb_install.sh || wget -qO /tmp/sb_install.sh "{script_url}"
chmod +x /tmp/sb_install.sh
/bin/bash /tmp/sb_install.sh
rm -f /tmp/sb_install.sh
"""
        append_task_log(task_id, "开始执行 Sing-box 核心协议集群自动化部署...", step="部署 Sing-box 极速核心", progress=60)
        code, out = runner.run_cmd(deploy_cmd, log_callback=log_cb, timeout=400)
        if code != 0:
            raise Exception(f"安装脚本执行返回非 0 状态码 ({code})")

        append_task_log(task_id, "部署执行完毕！正在验证服务运行状态与提取节点凭证...", step="验证服务状态", progress=90)

        # Check service status
        check_cmd = "systemctl is-active sing-box 2>/dev/null || rc-service sing-box status 2>/dev/null || pgrep -f sing-box"
        c_code, c_out = runner.run_cmd(check_cmd, timeout=10)
        is_running = ("active" in c_out.lower() or "running" in c_out.lower() or (c_code == 0 and c_out.strip()))

        # Update server state in config
        with config_lock:
            cur_cfg = load_config()
            for s in cur_cfg.get("servers", []):
                if s.get("id") == server_id:
                    s["status"] = "online" if is_running else "warning"
                    s["os_info"] = f"{os_str} ({arch_str})"
                    if bbr_str:
                        s["bbr_info"] = bbr_str
                    s["last_deployed"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    s["last_check_result"] = {
                        "singbox_running": is_running,
                        "bbr": bbr_str,
                        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    break
            save_config_unlocked(cur_cfg)

        status_msg = "服务正常运行并成功推送到面板！" if is_running else "服务部署已完成，但进程状态需关注！"
        append_task_log(task_id, f"恭喜！[{server_name}] {status_msg}", step="部署完成", progress=100)
        finish_task(task_id, success=True)

    except Exception as e:
        append_task_log(task_id, f"部署遇到错误: {str(e)}", step="部署异常", progress=100)
        finish_task(task_id, success=False, error=str(e))
        with config_lock:
            cur_cfg = load_config()
            for s in cur_cfg.get("servers", []):
                if s.get("id") == server_id:
                    s["status"] = "error"
                    s["last_error"] = str(e)
                    break
            save_config_unlocked(cur_cfg)
    finally:
        runner.close()

# ------------------------------------------------------------------------------
# Routes: Web UI
# ------------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html', js_version=int(time.time()))

# ------------------------------------------------------------------------------
# Routes: Config Management
# ------------------------------------------------------------------------------
@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(load_config())

@app.route('/api/config/global', methods=['POST'])
def update_global_config():
    payload = request.get_json() or {}
    cfg = load_config()
    cfg["global"].update(payload)
    save_config(cfg)
    return jsonify({"success": True, "data": cfg["global"]})

@app.route('/api/test_push_target', methods=['POST'])
def test_push_target():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip().rstrip('/')
    token = (data.get('token') or '').strip()

    if not url:
        return jsonify({"success": False, "message": "面板 URL 不能为空"}), 400

    if not REQUESTS_AVAILABLE:
        return jsonify({"success": True, "message": "requests 模块未安装，跳过预检"}), 200

    try:
        # Test connecting to cf-node-manager-plus API
        test_url = f"{url}/api/sources"
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        resp = requests.get(test_url, headers=headers, timeout=5)
        if resp.status_code == 200:
            return jsonify({"success": True, "message": "成功连通节点管理面板！Token 鉴权通过"})
        elif resp.status_code == 401 or resp.status_code == 403:
            return jsonify({"success": False, "message": f"连通管理面板，但密码/Token 错误 (HTTP {resp.status_code})"})
        else:
            return jsonify({"success": True, "message": f"管理面板可达 (HTTP {resp.status_code})"})
    except Exception as e:
        return jsonify({"success": False, "message": f"无法连通目标管理面板: {str(e)}"})

# ------------------------------------------------------------------------------
# Routes: Server Management
# ------------------------------------------------------------------------------
@app.route('/api/server/add', methods=['POST'])
def add_server():
    data = request.get_json() or {}
    host = (data.get('host') or '').strip()
    if not host:
        return jsonify({"success": False, "message": "VPS IP 或域名不能为空"}), 400

    server_id = str(uuid.uuid4())
    srv_name = (data.get('name') or host).strip()
    tag_main = (data.get('tag_main') or '').strip()
    if not tag_main or tag_main == srv_name or tag_main == host:
        tag_main = f"{srv_name}本机"

    tag_argo = (data.get('tag_argo') or '').strip()
    if not tag_argo or tag_argo.endswith("-Argo") or tag_argo == srv_name or tag_argo == host:
        tag_argo = f"{srv_name}优选"

    server = {
        "id": server_id,
        "name": srv_name,
        "host": host,
        "port": int(data.get('port') or 22),
        "user": (data.get('user') or 'root').strip(),
        "auth_type": data.get('auth_type', 'password'),
        "password": data.get('password', ''),
        "private_key": data.get('private_key', ''),
        "tag_main": tag_main,
        "tag_argo": tag_argo,
        "port_mode": data.get('port_mode', 'auto'),
        "port_range": data.get('port_range', '').strip(),
        "domain": data.get('domain', '').strip(),
        "reality_sni": data.get('reality_sni', '').strip(),
        "argo_enabled": bool(data.get('argo_enabled', False)),
        "argo_domain": data.get('argo_domain', '').strip(),
        "argo_token": clean_argo_token(data.get('argo_token', '')),
        "status": "idle",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    cfg = load_config()
    cfg["servers"].append(server)
    save_config(cfg)
    return jsonify({"success": True, "data": server})

@app.route('/api/server/update/<server_id>', methods=['POST'])
def update_server(server_id):
    data = request.get_json() or {}
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if str(s.get("id")) == str(server_id)), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    updatable_fields = [
        'name', 'host', 'port', 'user', 'auth_type', 'password', 'private_key',
        'tag_main', 'tag_argo', 'port_mode', 'port_range', 'domain', 'reality_sni',
        'argo_enabled', 'argo_domain', 'argo_token'
    ]
    for field in updatable_fields:
        if field in data:
            if field == 'port':
                server[field] = int(data[field] or 22)
            elif field == 'argo_enabled':
                server[field] = bool(data[field])
            elif field == 'argo_token':
                server[field] = clean_argo_token(data[field])
            elif field == 'name':
                server['name'] = str(data['name']).strip() or server.get('host', '')
            elif field == 'tag_main':
                val = str(data['tag_main']).strip()
                s_name = server.get('name', server.get('host'))
                if not val or val == s_name:
                    val = f"{s_name}本机"
                server['tag_main'] = val
            elif field == 'tag_argo':
                val = str(data['tag_argo']).strip()
                s_name = server.get('name', server.get('host'))
                if not val or val.endswith('-Argo') or val == s_name:
                    val = f"{s_name}优选"
                server['tag_argo'] = val
            else:
                server[field] = data[field]

    s_name = server.get('name', server.get('host'))
    if not server.get('tag_main') or server.get('tag_main') == s_name:
        server['tag_main'] = f"{s_name}本机"
    if not server.get('tag_argo') or server.get('tag_argo', '').endswith('-Argo') or server.get('tag_argo') == s_name:
        server['tag_argo'] = f"{s_name}优选"

    save_config(cfg)
    return jsonify({"success": True, "data": server})

@app.route('/api/server/delete/<server_id>', methods=['POST'])
def delete_server(server_id):
    cfg = load_config()
    orig_len = len(cfg.get("servers", []))
    cfg["servers"] = [s for s in cfg.get("servers", []) if s.get("id") != server_id]
    if len(cfg["servers"]) == orig_len:
        return jsonify({"success": False, "message": "未找到要删除的服务器"}), 404
    save_config(cfg)
    return jsonify({"success": True})

@app.route('/api/server/batch_delete', methods=['POST'])
def batch_delete_servers():
    data = request.get_json() or {}
    sids = data.get('server_ids', [])
    if not sids:
        return jsonify({"success": False, "message": "未选定任何 VPS"}), 400
    with config_lock:
        cur = load_config()
        original_count = len(cur.get("servers", []))
        cur["servers"] = [s for s in cur.get("servers", []) if s.get("id") not in sids]
        deleted_count = original_count - len(cur["servers"])
        save_config_unlocked(cur)
    return jsonify({"success": True, "message": f"已成功从集群管理面板移除 {deleted_count} 台 VPS", "count": deleted_count})

# ------------------------------------------------------------------------------
# Routes: Pre-flight Check & Deployment Tasks
# ------------------------------------------------------------------------------
@app.route('/api/check_env/<server_id>', methods=['POST'])
def check_env(server_id):
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    host = server.get("host")
    port = server.get("port", 22)
    user = server.get("user", "root")
    pwd = server.get("password")
    key = server.get("private_key")

    runner = SSHRunner(host, port, user, pwd, key, timeout=8)
    start_t = time.time()
    try:
        runner.connect()
        latency_ms = round((time.time() - start_t) * 1000, 1)

        code, out = runner.run_cmd(
            "uname -m; cat /etc/os-release 2>/dev/null | grep -E '^(ID|VERSION_ID)=' || true; echo '---BBR_CHECK---'; sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo 'unknown'; sysctl -n net.core.default_qdisc 2>/dev/null || echo 'unknown'",
            timeout=15
        )
        lines = out.strip().splitlines()
        arch = lines[0] if len(lines) > 0 else "unknown"
        os_id = "Linux"
        bbr_cc = "unknown"
        bbr_qdisc = "unknown"

        in_bbr = False
        bbr_lines = []
        for line in lines[1:]:
            if "---BBR_CHECK---" in line:
                in_bbr = True
                continue
            if in_bbr:
                bbr_lines.append(line.strip())
            else:
                if line.startswith("ID="):
                    os_id = line.replace("ID=", "").strip('"\n ')

        if len(bbr_lines) >= 1 and bbr_lines[0]:
            bbr_cc = bbr_lines[0]
        if len(bbr_lines) >= 2 and bbr_lines[1]:
            bbr_qdisc = bbr_lines[1]

        bbr_str = f"{bbr_cc}+{bbr_qdisc}"

        # Update server latency and bbr
        with config_lock:
            cur_cfg = load_config()
            for s in cur_cfg.get("servers", []):
                if s.get("id") == server_id:
                    s["latency_ms"] = latency_ms
                    s["os_info"] = f"{os_id} ({arch})"
                    s["bbr_info"] = bbr_str
                    break
            save_config_unlocked(cur_cfg)

        return jsonify({
            "success": True,
            "latency_ms": latency_ms,
            "os": os_id,
            "arch": arch,
            "bbr": bbr_str,
            "message": f"连接成功！延迟 {latency_ms}ms, 系统: {os_id} ({arch}), BBR: {bbr_str}"
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"SSH 连接失败: {str(e)}"}), 500
    finally:
        runner.close()

@app.route('/api/deploy/<server_id>', methods=['POST'])
def deploy_server(server_id):
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    task_id = create_task("deploy", server_id, server.get("name", server.get("host")))
    t = threading.Thread(target=deploy_server_worker, args=(task_id, server_id), daemon=True)
    t.start()
    return jsonify({"success": True, "task_id": task_id})

@app.route('/api/deploy_all', methods=['POST'])
def deploy_all():
    data = request.get_json(silent=True) or {}
    req_sids = data.get('server_ids', [])
    cfg = load_config()
    servers = cfg.get("servers", [])
    if req_sids:
        servers = [s for s in servers if s.get("id") in req_sids]
    if not servers:
        return jsonify({"success": False, "message": "当前没有选定任何 VPS 或集群列表为空"}), 400

    task_ids = []
    concurrency = int(cfg.get("global", {}).get("concurrency", 5))
    semaphore = threading.Semaphore(concurrency)

    def worker_wrapper(tid, sid):
        with semaphore:
            deploy_server_worker(tid, sid)

    for server in servers:
        sid = server.get("id")
        tid = create_task("deploy", sid, server.get("name", server.get("host")))
        task_ids.append(tid)
        t = threading.Thread(target=worker_wrapper, args=(tid, sid), daemon=True)
        t.start()

    return jsonify({"success": True, "task_ids": task_ids, "count": len(task_ids)})

@app.route('/api/tasks/<task_id>', methods=['GET'])
def get_task_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return jsonify({"success": False, "message": "任务不存在"}), 404
        return jsonify({"success": True, "data": task})

# ------------------------------------------------------------------------------
# Routes: Cluster Health Inspection & Unlock Testing
# ------------------------------------------------------------------------------
def inspect_server_sync(server):
    host = server.get("host")
    port = server.get("port", 22)
    user = server.get("user", "root")
    pwd = server.get("password")
    key = server.get("private_key")

    runner = SSHRunner(host, port, user, pwd, key, timeout=8)
    start_t = time.time()
    try:
        runner.connect()
        latency = round((time.time() - start_t) * 1000, 1)

        inspect_script = r'''
echo "---SB_STATUS---"
systemctl is-active sing-box 2>/dev/null || rc-service sing-box status 2>/dev/null || pgrep -f sing-box 2>/dev/null || echo "inactive"
echo "---ARGO_STATUS---"
systemctl is-active cloudflared 2>/dev/null || pgrep -f cloudflared 2>/dev/null || echo "inactive"
echo "---PORTS---"
ss -lntu 2>/dev/null | awk '{print $1,$5}' || netstat -lntu 2>/dev/null | awk '{print $1,$4}'
echo "---IPS---"
curl -s4m4 ip.sb 2>/dev/null || true
echo ""
curl -s6m4 ip.sb 2>/dev/null || true
echo "---BBR---"
sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || echo "unknown"
sysctl -n net.core.default_qdisc 2>/dev/null || cat /proc/sys/net/core/default_qdisc 2>/dev/null || echo "unknown"
'''
        code, out = runner.run_cmd(inspect_script, timeout=25)

        sb_status = "inactive"
        argo_status = "inactive"
        ports = []
        ipv4 = ""
        ipv6 = ""
        bbr_lines = []

        current_sec = ""
        for line in out.splitlines():
            line_str = line.strip()
            if line_str.startswith("---") and line_str.endswith("---"):
                current_sec = line_str
                continue
            if current_sec == "---SB_STATUS---" and line_str:
                if "active" in line_str.lower() or "running" in line_str.lower() or line_str.isdigit():
                    sb_status = "active"
            elif current_sec == "---ARGO_STATUS---" and line_str:
                if "active" in line_str.lower() or "running" in line_str.lower() or line_str.isdigit():
                    argo_status = "active"
            elif current_sec == "---PORTS---" and line_str:
                ports.append(line_str)
            elif current_sec == "---IPS---" and line_str:
                if "." in line_str and not ipv4:
                    ipv4 = line_str
                elif ":" in line_str and not ipv6:
                    ipv6 = line_str
            elif current_sec == "---BBR---" and line_str:
                bbr_lines.append(line_str)

        bbr_str = ""
        if len(bbr_lines) >= 2:
            bbr_str = f"{bbr_lines[0]}+{bbr_lines[1]}"
        elif len(bbr_lines) == 1:
            bbr_str = bbr_lines[0]

        return {
            "success": True,
            "latency_ms": latency,
            "singbox_running": (sb_status == "active"),
            "argo_running": (argo_status == "active"),
            "ipv4": ipv4,
            "ipv6": ipv6,
            "bbr": bbr_str,
            "ports_count": len(ports),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    finally:
        runner.close()

@app.route('/api/inspect/<server_id>', methods=['POST'])
def inspect_single(server_id):
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    res = inspect_server_sync(server)
    with config_lock:
        cur_cfg = load_config()
        for s in cur_cfg.get("servers", []):
            if s.get("id") == server_id:
                s["last_check_result"] = res
                if res.get("success"):
                    s["status"] = "online" if res.get("singbox_running") else "warning"
                    s["latency_ms"] = res.get("latency_ms")
                    if res.get("bbr"):
                        s["bbr_info"] = res.get("bbr")
                else:
                    s["status"] = "error"
                break
        save_config_unlocked(cur_cfg)

    return jsonify({"success": True, "data": res})

@app.route('/api/inspect_all', methods=['POST'])
def inspect_all():
    data = request.get_json(silent=True) or {}
    req_sids = data.get('server_ids', [])
    cfg = load_config()
    servers = cfg.get("servers", [])
    if req_sids:
        servers = [s for s in servers if s.get("id") in req_sids]
    if not servers:
        return jsonify({"success": False, "message": "当前没有选定任何 VPS 或集群列表为空"}), 400

    def run_all_inspect():
        for s in servers:
            sid = s.get("id")
            res = inspect_server_sync(s)
            with config_lock:
                cur = load_config()
                for target in cur.get("servers", []):
                    if target.get("id") == sid:
                        target["last_check_result"] = res
                        if res.get("success"):
                            target["status"] = "online" if res.get("singbox_running") else "warning"
                            target["latency_ms"] = res.get("latency_ms")
                            if res.get("bbr"):
                                target["bbr_info"] = res.get("bbr")
                        else:
                            target["status"] = "error"
                        break
                save_config_unlocked(cur)

    t = threading.Thread(target=run_all_inspect, daemon=True)
    t.start()
    return jsonify({"success": True, "message": "已在后台启动全集群并发巡检！"})

@app.route('/api/test_unlock/<server_id>', methods=['POST'])
def test_unlock(server_id):
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    host = server.get("host")
    port = server.get("port", 22)
    user = server.get("user", "root")
    pwd = server.get("password")
    key = server.get("private_key")

    runner = SSHRunner(host, port, user, pwd, key, timeout=8)
    try:
        runner.connect()
        test_script = r'''
# Test OpenAI
OPENAI_CODE="$(curl -s4m6 -o /dev/null -w "%{http_code}" https://android.chat.openai.com/api/v1/rgstr 2>/dev/null || echo "000")"
# Test YouTube
YT_CODE="$(curl -s4m6 -o /dev/null -w "%{http_code}" https://www.youtube.com 2>/dev/null || echo "000")"
# Test Netflix
NF_CODE="$(curl -s4m6 -o /dev/null -w "%{http_code}" https://www.netflix.com/title/80018499 2>/dev/null || echo "000")"

echo "UNLOCK_RES:{\"openai\":\"$OPENAI_CODE\",\"youtube\":\"$YT_CODE\",\"netflix\":\"$NF_CODE\"}"
'''
        code, out = runner.run_cmd(test_script, timeout=25)
        unlock_info = {}
        for line in out.splitlines():
            if line.startswith("UNLOCK_RES:"):
                try:
                    unlock_info = json.loads(line.replace("UNLOCK_RES:", "").strip())
                except Exception:
                    pass

        # Interpret status
        openai_status = "原生支持" if unlock_info.get("openai") in ["200", "403"] else "受限/阻断"
        youtube_status = "解锁" if unlock_info.get("youtube") == "200" else "受限"
        netflix_status = "完整解锁" if unlock_info.get("netflix") == "200" else "仅自制剧/受限"

        result = {
            "openai": openai_status,
            "youtube": youtube_status,
            "netflix": netflix_status,
            "raw": unlock_info
        }

        with config_lock:
            cur_cfg = load_config()
            for s in cur_cfg.get("servers", []):
                if s.get("id") == server_id:
                    s.setdefault("last_check_result", {})["unlock"] = result
                    break
            save_config_unlocked(cur_cfg)

        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "message": f"解锁测试失败: {str(e)}"}), 500
    finally:
        runner.close()

# ------------------------------------------------------------------------------
# Routes: Batch Import & Export
# ------------------------------------------------------------------------------
def is_ip_or_domain(val: str) -> bool:
    val = val.strip()
    parts = val.split('.')
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return True
    if ':' in val or re.match(r'^[a-zA-Z0-9][-a-zA-Z0-9.]+\.[a-zA-Z]{2,}$', val):
        return True
    return False

def parse_import_line(line: str, name_prefix: str = "", index: int = 1, total_count: int = 1):
    line = line.strip()
    if not line or line.startswith('#'):
        return None

    custom_name = ""
    host, port, user, pwd = "", 22, "root", ""

    if '#' in line and not line.startswith('#'):
        custom_name, line = line.split('#', 1)
        custom_name = custom_name.strip()
        line = line.strip()

    if "@" in line:
        auth_part, host_part = line.split("@", 1)
        if ":" in auth_part:
            aparts = auth_part.split(":")
            if len(aparts) == 3 and not is_ip_or_domain(aparts[0]):
                custom_name, user, pwd = aparts[0], aparts[1], aparts[2]
            else:
                user, pwd = aparts[0], ":".join(aparts[1:])
        else:
            user = auth_part

        if ":" in host_part:
            hparts = host_part.split(":")
            host = hparts[0]
            port = int(hparts[1]) if hparts[1].isdigit() else 22
            if len(hparts) >= 3 and not custom_name:
                custom_name = ":".join(hparts[2:])
        else:
            host = host_part

    elif ":" in line:
        parts = [p.strip() for p in line.split(":")]
        if len(parts) >= 5:
            if not is_ip_or_domain(parts[0]) and is_ip_or_domain(parts[1]):
                custom_name = parts[0]
                host = parts[1]
                port = int(parts[2]) if parts[2].isdigit() else 22
                user = parts[3]
                pwd = ":".join(parts[4:])
            elif is_ip_or_domain(parts[0]):
                host = parts[0]
                port = int(parts[1]) if parts[1].isdigit() else 22
                user = parts[2]
                pwd = parts[3]
                custom_name = ":".join(parts[4:])
            else:
                custom_name = parts[0]
                host = parts[1]
                port = int(parts[2]) if parts[2].isdigit() else 22
                user = parts[3]
                pwd = ":".join(parts[4:])
        elif len(parts) == 4:
            if not is_ip_or_domain(parts[0]) and is_ip_or_domain(parts[1]):
                custom_name, host, port_str, pwd = parts[0], parts[1], parts[2], parts[3]
                port = int(port_str) if port_str.isdigit() else 22
                user = "root"
            else:
                host, port_str, user, pwd = parts[0], parts[1], parts[2], parts[3]
                port = int(port_str) if port_str.isdigit() else 22
        elif len(parts) == 3:
            if not is_ip_or_domain(parts[0]) and is_ip_or_domain(parts[1]):
                custom_name, host, pwd = parts[0], parts[1], parts[2]
                port, user = 22, "root"
            else:
                host, port_str, pwd = parts[0], parts[1], parts[2]
                port = int(port_str) if port_str.isdigit() else 22
                user = "root"
        elif len(parts) == 2:
            host, pwd = parts[0], parts[1]
            port, user = 22, "root"
    else:
        parts = line.split()
        if len(parts) >= 5:
            if not is_ip_or_domain(parts[0]) and is_ip_or_domain(parts[1]):
                custom_name, host, port_str, user, pwd = parts[0], parts[1], parts[2], parts[3], parts[4]
                port = int(port_str) if port_str.isdigit() else 22
            else:
                host, port_str, user, pwd, custom_name = parts[0], parts[1], parts[2], parts[3], " ".join(parts[4:])
                port = int(port_str) if port_str.isdigit() else 22
        elif len(parts) == 4:
            host, port_str, user, pwd = parts[0], parts[1], parts[2], parts[3]
            port = int(port_str) if port_str.isdigit() else 22
        elif len(parts) == 3:
            host, port_str, pwd = parts[0], parts[1], parts[2]
            port = int(port_str) if port_str.isdigit() else 22
        elif len(parts) == 2:
            host, pwd = parts[0], parts[1]

    if not host:
        return None

    if custom_name:
        final_name = custom_name.strip()
    elif name_prefix.strip():
        prefix = name_prefix.strip()
        final_name = prefix if total_count == 1 else f"{prefix}-{index}"
    else:
        final_name = f"VPS-{host.split('.')[-1] if '.' in host else host[:6]}"

    return {
        "name": final_name,
        "host": host.strip(),
        "port": port,
        "user": user.strip(),
        "password": pwd.strip()
    }

def test_ssh_connectivity(host, port=22, user="root", pwd="", key="", timeout=7):
    """Test SSH connectivity and return latency and OS info."""
    start_t = time.time()
    runner = SSHRunner(host, port, user, pwd, key, timeout=timeout)
    try:
        runner.connect()
        latency = round((time.time() - start_t) * 1000, 1)
        code, out = runner.run_cmd("cat /etc/os-release 2>/dev/null | grep -E '^(PRETTY_NAME|NAME)=' | head -n1 | cut -d= -f2 | tr -d '\"' || uname -sr", timeout=5)
        os_info = out.strip() if out and code == 0 else "Linux"
        return {
            "success": True,
            "latency": latency,
            "os_info": os_info,
            "message": "连接正常"
        }
    except Exception as e:
        latency = round((time.time() - start_t) * 1000, 1)
        err_msg = str(e)
        if "Authentication failed" in err_msg or "password" in err_msg.lower():
            friendly_err = "认证失败 (密码错误)"
        elif "timed out" in err_msg.lower() or "timeout" in err_msg.lower():
            friendly_err = "连接超时 (端口未响应)"
        elif "Connection refused" in err_msg:
            friendly_err = "连接被拒 (端口未开放)"
        else:
            friendly_err = f"连接失败: {err_msg[:30]}"
        return {
            "success": False,
            "latency": latency,
            "os_info": "",
            "message": friendly_err
        }
    finally:
        runner.close()

@app.route('/api/batch_test_connection', methods=['POST'])
def batch_test_connection():
    data = request.get_json() or {}
    text = data.get("text", "")
    name_prefix = data.get("name_prefix", "").strip()

    lines = [l.strip() for l in text.strip().splitlines() if l.strip() and not l.strip().startswith('#')]
    parsed_items = []
    for idx, line in enumerate(lines, 1):
        item = parse_import_line(line, name_prefix=name_prefix, index=idx, total_count=len(lines))
        if item:
            parsed_items.append(item)

    if not parsed_items:
        return jsonify({"success": False, "message": "未能识别出有效的 VPS 主机条目，请检查输入格式"}), 400

    from concurrent.futures import ThreadPoolExecutor

    def worker(item):
        res = test_ssh_connectivity(
            host=item["host"],
            port=item["port"],
            user=item["user"],
            pwd=item["password"],
            timeout=7
        )
        return {
            "name": item["name"],
            "host": item["host"],
            "port": item["port"],
            "user": item["user"],
            "password": item["password"],
            "success": res["success"],
            "latency": res.get("latency", 0),
            "os_info": res.get("os_info", ""),
            "message": res.get("message", "")
        }

    with ThreadPoolExecutor(max_workers=min(12, len(parsed_items))) as executor:
        results = list(executor.map(worker, parsed_items))

    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count

    return jsonify({
        "success": True,
        "results": results,
        "total": len(results),
        "success_count": success_count,
        "fail_count": fail_count
    })

@app.route('/api/batch_import', methods=['POST'])
def batch_import():
    data = request.get_json() or {}
    items = data.get("items")
    text = data.get("text", "")
    name_prefix = data.get("name_prefix", "").strip()

    to_import = []
    if isinstance(items, list) and items:
        for it in items:
            if it.get("host"):
                to_import.append(it)
    elif text:
        lines = [l.strip() for l in text.strip().splitlines() if l.strip() and not l.strip().startswith('#')]
        for idx, line in enumerate(lines, 1):
            parsed = parse_import_line(line, name_prefix=name_prefix, index=idx, total_count=len(lines))
            if parsed:
                to_import.append(parsed)

    if not to_import:
        return jsonify({"success": False, "message": "未能识别出有效的 VPS 主机条目"}), 400

    cfg = load_config()
    existing_hosts = {(s.get("host"), int(s.get("port", 22))) for s in cfg.get("servers", [])}

    imported = []
    skipped = 0
    for item in to_import:
        host = item.get("host")
        port = int(item.get("port", 22))
        if (host, port) in existing_hosts:
            skipped += 1
            continue

        server_id = str(uuid.uuid4())
        alias = item.get("name") or f"VPS-{host.split('.')[-1] if '.' in host else host[:6]}"
        new_server = {
            "id": server_id,
            "name": alias,
            "host": host,
            "port": port,
            "user": item.get("user", "root"),
            "auth_type": "password",
            "password": item.get("password", ""),
            "private_key": "",
            "tag_main": f"{alias}本机",
            "tag_argo": f"{alias}优选",
            "port_mode": "auto",
            "port_range": "",
            "domain": "",
            "reality_sni": "",
            "argo_enabled": False,
            "argo_domain": "",
            "argo_token": "",
            "status": "online" if item.get("success") else "idle",
            "latency": item.get("latency", 0),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        if item.get("os_info"):
            new_server["last_check_result"] = {
                "os": item["os_info"],
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

        cfg["servers"].append(new_server)
        existing_hosts.add((host, port))
        imported.append(new_server)

    save_config(cfg)
    msg = f"成功导入 {len(imported)} 台 VPS！"
    if skipped > 0:
        msg += f" (已跳过 {skipped} 台重复主机)"
    return jsonify({"success": True, "count": len(imported), "skipped": skipped, "message": msg, "imported": imported})

@app.route('/api/export', methods=['GET'])
def export_config():
    cfg = load_config()
    content = json.dumps(cfg, ensure_ascii=False, indent=2)
    return Response(
        content,
        mimetype="application/json",
        headers={"Content-disposition": "attachment; filename=vps_cluster_config.json"}
    )

@app.route('/api/import', methods=['POST'])
def import_config():
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "没有上传文件"}), 400
    f = request.files['file']
    try:
        data = json.load(f)
        if "servers" not in data or "global" not in data:
            return jsonify({"success": False, "message": "配置文件格式不合法"}), 400
        save_config(data)
        return jsonify({"success": True, "message": f"成功恢复配置！包含 {len(data['servers'])} 台服务器"})
    except Exception as e:
        return jsonify({"success": False, "message": f"解析配置文件失败: {str(e)}"}), 400

# ------------------------------------------------------------------------------
# Routes: Batch Operations (Restart, Update Core, Reapply BBR, Uninstall)
# ------------------------------------------------------------------------------
@app.route('/api/batch_ops', methods=['POST'])
def batch_ops():
    data = request.get_json() or {}
    action = data.get("action", "")  # 'restart' | 'update_core' | 'reapply_bbr' | 'uninstall'
    server_ids = data.get("server_ids")  # list or empty for all

    cfg = load_config()
    servers = cfg.get("servers", [])
    if server_ids and len(server_ids) > 0:
        targets = [s for s in servers if s.get("id") in server_ids]
    else:
        targets = servers

    if not targets:
        return jsonify({"success": False, "message": "没有选定任何目标 VPS"}), 400

    action_names = {
        "restart": "批量重启 Sing-box 服务",
        "update_core": "批量更新 Sing-box 核心至最新版",
        "reapply_bbr": "批量刷新并激活 BBR+fq",
        "uninstall": "批量清理卸载 Sing-box"
    }
    action_desc = action_names.get(action, f"批量执行: {action}")
    task_id = create_task(f"batch_{action}", "cluster", f"{action_desc} ({len(targets)}台)")

    def run_ops():
        append_task_log(task_id, f"正在对 {len(targets)} 台 VPS 并发下发: [{action_desc}]...", progress=10)
        success_count = 0
        concurrency = int(cfg.get("global", {}).get("concurrency", 5))
        sem = threading.Semaphore(concurrency)

        def worker(target_srv):
            nonlocal success_count
            with sem:
                host = target_srv.get("host")
                port = target_srv.get("port", 22)
                user = target_srv.get("user", "root")
                pwd = target_srv.get("password")
                key = target_srv.get("private_key")
                name = target_srv.get("name", host)
                runner = SSHRunner(host, port, user, pwd, key, timeout=10)
                try:
                    runner.connect()
                    if action == "restart":
                        cmd = "systemctl restart sing-box 2>/dev/null || rc-service sing-box restart 2>/dev/null; sleep 1; systemctl is-active sing-box 2>/dev/null || rc-service sing-box status 2>/dev/null || pgrep -f sing-box"
                        _, out = runner.run_cmd(cmd, timeout=15)
                        status_ok = "active" in out.lower() or "running" in out.lower()
                        append_task_log(task_id, f"[{name}] 重启完毕: {'🟢 服务运行中' if status_ok else '🟡 状态待确认'}")
                        success_count += 1
                    elif action == "update_core":
                        cmd = r'''
LATEST="$(curl -s4m6 https://api.github.com/repos/SagerNet/sing-box/releases/latest | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/' | sed 's/^v//' || echo "")"
ARCH="$(uname -m)"
case "$ARCH" in x86_64) A="amd64" ;; aarch64|arm64) A="arm64" ;; *) A="" ;; esac
if [ -n "$LATEST" ] && [ -n "$A" ]; then
  wget -qO /tmp/sb.tar.gz "https://github.com/SagerNet/sing-box/releases/download/v${LATEST}/sing-box-${LATEST}-linux-${A}.tar.gz" || true
  if [ -s /tmp/sb.tar.gz ]; then
    tar -xzf /tmp/sb.tar.gz -C /tmp/
    cp -f /tmp/sing-box-${LATEST}-linux-${A}/sing-box /usr/local/bin/sing-box
    chmod +x /usr/local/bin/sing-box
    rm -rf /tmp/sb.tar.gz /tmp/sing-box-${LATEST}-linux-${A}
    systemctl restart sing-box 2>/dev/null || rc-service sing-box restart 2>/dev/null || true
    echo "SUCCESS: v${LATEST}"
  else
    echo "FAIL: 下载核心失败"
  fi
else
  echo "FAIL: 无法获取最新版本号或未知架构"
fi
'''
                        _, out = runner.run_cmd(cmd, timeout=60)
                        append_task_log(task_id, f"[{name}] 核心更新: {out.strip()[:80]}")
                        success_count += 1
                    elif action == "reapply_bbr":
                        cmd = r'''
modprobe tcp_bbr3 2>/dev/null || true
modprobe tcp_bbr 2>/dev/null || true
AVAIL="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || cat /proc/sys/net/ipv4/tcp_available_congestion_control 2>/dev/null || echo '')"
TARGET="bbr"
if echo "$AVAIL" | grep -qw "bbr3"; then TARGET="bbr3"; fi
sysctl -w net.core.default_qdisc=fq >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_congestion_control=${TARGET} >/dev/null 2>&1 || true
echo "BBR: $(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)+$(sysctl -n net.core.default_qdisc 2>/dev/null)"
'''
                        _, out = runner.run_cmd(cmd, timeout=15)
                        append_task_log(task_id, f"[{name}] {out.strip()}")
                        success_count += 1
                    elif action == "uninstall":
                        cmd = r'''
# 1. 停止并禁用所有相关服务
systemctl stop sing-box 2>/dev/null || true
systemctl disable sing-box 2>/dev/null || true
rc-service sing-box stop 2>/dev/null || true
rc-update del sing-box default 2>/dev/null || true

systemctl stop cloudflared 2>/dev/null || true
systemctl disable cloudflared 2>/dev/null || true
rc-service cloudflared stop 2>/dev/null || true
rc-update del cloudflared default 2>/dev/null || true
if command -v cloudflared >/dev/null 2>&1; then
  cloudflared service uninstall >/dev/null 2>&1 || true
fi

systemctl stop sb-hop.service 2>/dev/null || true
systemctl disable sb-hop.service 2>/dev/null || true

pkill -9 -f sing-box 2>/dev/null || true
pkill -9 -f cloudflared 2>/dev/null || true

# 2. 清理系统服务单元文件
rm -f /etc/systemd/system/sing-box.service* 2>/dev/null || true
rm -rf /etc/systemd/system/sing-box.service.d 2>/dev/null || true
rm -f /etc/systemd/system/cloudflared.service* 2>/dev/null || true
rm -rf /etc/systemd/system/cloudflared.service.d 2>/dev/null || true
rm -f /etc/systemd/system/sb-hop.service* 2>/dev/null || true
rm -f /etc/init.d/sing-box /etc/init.d/cloudflared 2>/dev/null || true

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload 2>/dev/null || true
  systemctl reset-failed 2>/dev/null || true
fi

# 3. 删除二进制可执行文件与快捷脚本
rm -f /usr/local/bin/sing-box /usr/bin/sing-box 2>/dev/null || true
rm -f /usr/local/bin/sb /usr/bin/sb 2>/dev/null || true
rm -f /usr/local/bin/sb-hop.sh /usr/local/bin/sb-selfcheck.sh 2>/dev/null || true
rm -f /usr/local/bin/cloudflared /usr/bin/cloudflared 2>/dev/null || true

# 4. 删除配置文件、状态文件、证书、日志与临时缓存
rm -rf /etc/sing-box 2>/dev/null || true
rm -rf /etc/cloudflared 2>/dev/null || true
rm -rf /var/log/sing-box* 2>/dev/null || true
rm -rf /tmp/sb* /tmp/sing-box* /tmp/cloudflared* 2>/dev/null || true

# 5. 清理 Crontab 计划任务残留
if command -v crontab >/dev/null 2>&1; then
  crontab -l 2>/dev/null | grep -v 'sing-box' | grep -v 'sb-' | grep -v 'cloudflared' | crontab - 2>/dev/null || true
fi

# 6. 清理 iptables / ip6tables 端口跳跃规则与链
if command -v iptables >/dev/null 2>&1; then
  iptables -t nat -D PREROUTING -p udp -j SB_HOP 2>/dev/null || true
  iptables -t nat -D PREROUTING -p udp -j SB_HOP 2>/dev/null || true
  iptables -t nat -F SB_HOP 2>/dev/null || true
  iptables -t nat -X SB_HOP 2>/dev/null || true
fi
if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -t nat -D PREROUTING -p udp -j SB_HOP6 2>/dev/null || true
  ip6tables -t nat -D PREROUTING -p udp -j SB_HOP6 2>/dev/null || true
  ip6tables -t nat -F SB_HOP6 2>/dev/null || true
  ip6tables -t nat -X SB_HOP6 2>/dev/null || true
fi

rm -f /etc/sysctl.d/99-singbox-speed.conf 2>/dev/null || true
sysctl -p 2>/dev/null || true
echo "UNINSTALLED_OK"
'''
                        runner.run_cmd(cmd, timeout=30)
                        append_task_log(task_id, f"[{name}] Sing-box、Argo 隧道、证书与所有关联配置已彻底清理卸载完毕！")
                        success_count += 1
                        with config_lock:
                            cur = load_config()
                            for s in cur.get("servers", []):
                                if s.get("id") == target_srv.get("id"):
                                    s["status"] = "idle"
                                    s["last_check_result"] = {}
                                    s["last_deployed"] = "已彻底卸载"
                                    break
                            save_config_unlocked(cur)
                except Exception as e:
                    append_task_log(task_id, f"[{name}] 执行失败: {str(e)}")
                finally:
                    runner.close()

        threads = []
        for s in targets:
            th = threading.Thread(target=worker, args=(s,), daemon=True)
            threads.append(th)
            th.start()

        for th in threads:
            th.join()

        finish_task(task_id, success=True)
        append_task_log(task_id, f"[{action_desc}] 全部执行完毕！成功完成: {success_count}/{len(targets)} 台", progress=100)

    t = threading.Thread(target=run_ops, daemon=True)
    t.start()
    return jsonify({"success": True, "task_id": task_id})

# ------------------------------------------------------------------------------
# Periodic Background Inspector Worker
# ------------------------------------------------------------------------------
def background_periodic_inspector():
    while True:
        try:
            cfg = load_config()
            g = cfg.get("global", {})
            if g.get("auto_inspect", False):
                interval_min = int(g.get("auto_inspect_interval", 60))
                servers = cfg.get("servers", [])
                if servers:
                    for s in servers:
                        sid = s.get("id")
                        res = inspect_server_sync(s)
                        with config_lock:
                            cur = load_config()
                            for target in cur.get("servers", []):
                                if target.get("id") == sid:
                                    target["last_check_result"] = res
                                    if res.get("success"):
                                        target["status"] = "online" if res.get("singbox_running") else "warning"
                                        target["latency_ms"] = res.get("latency_ms")
                                        if res.get("bbr"):
                                            target["bbr_info"] = res.get("bbr")
                                    else:
                                        target["status"] = "error"
                                    break
                            save_config_unlocked(cur)
                time.sleep(max(interval_min * 60, 60))
            else:
                time.sleep(30)
        except Exception:
            time.sleep(30)

inspector_thread = threading.Thread(target=background_periodic_inspector, daemon=True)
inspector_thread.start()

# ------------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    host = os.environ.get('HOST', '0.0.0.0')
    print(f"==================================================================")
    print(f"  多 VPS 集群一键批量安装与巡检控制台 (vps-cluster-manager)")
    print(f"  运行地址: http://127.0.0.1:{port} (监听所有网卡: {host}:{port})")
    print(f"  Paramiko SSH 支持: {'已启用 (原生极速)' if PARAMIKO_AVAILABLE else '未安装 (将自动降级为系统原生 SSH 命令)'}")
    print(f"==================================================================")
    app.run(host=host, port=port, debug=False)
