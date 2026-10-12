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
        "concurrency": 5
    },
    "servers": []
}

def load_config():
    with config_lock:
        if not os.path.exists(CONFIG_FILE):
            save_config_unlocked(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # merge with defaults if keys missing
                for k, v in DEFAULT_CONFIG["global"].items():
                    if k not in data.get("global", {}):
                        data.setdefault("global", {})[k] = v
                return data
        except Exception as e:
            print(f"[ERROR] Failed to load config: {e}")
            return json.loads(json.dumps(DEFAULT_CONFIG))

def save_config_unlocked(data):
    try:
        temp_file = CONFIG_FILE + '.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
        os.rename(temp_file, CONFIG_FILE)
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

        tag_main = server.get("tag_main", "").strip() or server_name
        tag_argo = server.get("tag_argo", "").strip() or f"{tag_main}-Argo"

        custom_domain = server.get("domain", "").strip()
        cf_dns_token = global_cfg.get("cf_dns_token", "").strip()

        argo_enabled = server.get("argo_enabled", False)
        argo_domain = server.get("argo_domain", "").strip()
        argo_token = server.get("argo_token", "").strip()

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
    return render_template('index.html')

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
    server = {
        "id": server_id,
        "name": (data.get('name') or host).strip(),
        "host": host,
        "port": int(data.get('port') or 22),
        "user": (data.get('user') or 'root').strip(),
        "auth_type": data.get('auth_type', 'password'),
        "password": data.get('password', ''),
        "private_key": data.get('private_key', ''),
        "tag_main": data.get('tag_main', '').strip(),
        "tag_argo": data.get('tag_argo', '').strip(),
        "port_mode": data.get('port_mode', 'auto'),
        "port_range": data.get('port_range', '').strip(),
        "domain": data.get('domain', '').strip(),
        "argo_enabled": bool(data.get('argo_enabled', False)),
        "argo_domain": data.get('argo_domain', '').strip(),
        "argo_token": data.get('argo_token', '').strip(),
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
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    updatable_fields = [
        'name', 'host', 'port', 'user', 'auth_type', 'password', 'private_key',
        'tag_main', 'tag_argo', 'port_mode', 'port_range', 'domain',
        'argo_enabled', 'argo_domain', 'argo_token'
    ]
    for field in updatable_fields:
        if field in data:
            if field == 'port':
                server[field] = int(data[field] or 22)
            elif field == 'argo_enabled':
                server[field] = bool(data[field])
            else:
                server[field] = data[field]

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
    cfg = load_config()
    servers = cfg.get("servers", [])
    if not servers:
        return jsonify({"success": False, "message": "当前集群没有 VPS"}), 400

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
    cfg = load_config()
    servers = cfg.get("servers", [])
    if not servers:
        return jsonify({"success": False, "message": "集群中暂无 VPS"}), 400

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
@app.route('/api/batch_import', methods=['POST'])
def batch_import():
    data = request.get_json() or {}
    text = data.get("text", "")
    lines = text.strip().splitlines()

    imported = []
    cfg = load_config()
    existing_hosts = {s.get("host") for s in cfg.get("servers", [])}

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # Format 1: host:port:user:password
        # Format 2: host:password (default port 22, user root)
        # Format 3: user:password@host:port
        # Format 4: host port user password (space delimited)
        host, port, user, pwd = "", 22, "root", ""

        if "@" in line:
            # user:pwd@host:port
            auth_part, host_part = line.split("@", 1)
            if ":" in auth_part:
                user, pwd = auth_part.split(":", 1)
            else:
                user = auth_part
            if ":" in host_part:
                host, port_str = host_part.split(":", 1)
                port = int(port_str) if port_str.isdigit() else 22
            else:
                host = host_part
        elif ":" in line:
            parts = line.split(":")
            if len(parts) >= 4:
                host, port_str, user, pwd = parts[0], parts[1], parts[2], ":".join(parts[3:])
                port = int(port_str) if port_str.isdigit() else 22
            elif len(parts) == 2:
                host, pwd = parts[0], parts[1]
                port = 22
                user = "root"
            elif len(parts) == 3:
                host, port_str, pwd = parts[0], parts[1], parts[2]
                port = int(port_str) if port_str.isdigit() else 22
                user = "root"
        else:
            parts = line.split()
            if len(parts) >= 4:
                host, port_str, user, pwd = parts[0], parts[1], parts[2], parts[3]
                port = int(port_str) if port_str.isdigit() else 22
            elif len(parts) == 2:
                host, pwd = parts[0], parts[1]

        if not host:
            continue

        server_id = str(uuid.uuid4())
        alias = f"VPS-{host.split('.')[-1] if '.' in host else host[:6]}"
        new_server = {
            "id": server_id,
            "name": alias,
            "host": host,
            "port": port,
            "user": user,
            "auth_type": "password",
            "password": pwd,
            "private_key": "",
            "tag_main": alias,
            "tag_argo": f"{alias}-Argo",
            "port_mode": "auto",
            "port_range": "",
            "domain": "",
            "argo_enabled": False,
            "argo_domain": "",
            "argo_token": "",
            "status": "idle",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        cfg["servers"].append(new_server)
        imported.append(new_server)

    save_config(cfg)
    return jsonify({"success": True, "count": len(imported), "imported": imported})

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
# Entrypoint
# ------------------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5005))
    host = os.environ.get('HOST', '0.0.0.0')
    print(f"==================================================================")
    print(f"  多 VPS 集群一键批量安装与巡检控制台 (vps-cluster-manager)")
    print(f"  运行地址: http://127.0.0.1:{port} (监听所有网卡: {host}:{port})")
    print(f"  Paramiko SSH 支持: {'已启用 (原生极速)' if PARAMIKO_AVAILABLE else '未安装 (将自动降级为系统原生 SSH 命令)'}")
    print(f"==================================================================")
    app.run(host=host, port=port, debug=False)
