import os
import sys
import json
import uuid
import time
import re
import socket
import threading
import subprocess
import hmac
import hashlib
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

if len(sys.argv) > 1 and sys.argv[1] in ('passwd', 'password'):
    print("==========================================================")
    print("  VPS Cluster Manager - 管理员密码与入口路径维护")
    print("==========================================================")
    cfg_path = CONFIG_FILE if os.path.exists(CONFIG_FILE) else "/etc/vps-cluster-manager/config.json"
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    g = cfg.setdefault("global", {})
    cur_pwd = g.get("admin_password") or "[尚未设定 / 首次初始化模式]"
    cur_path = g.get("login_path") or "login"
    cur_stealth = "已开启 (防扫描隐蔽模式，未授权访问根路径返回404)" if g.get("stealth_mode") else "未开启 (常规模式)"
    print(f"当前私密管理入口: /{cur_path}")
    print(f"防扫描隐蔽状态:   {cur_stealth}")
    print(f"当前管理员密码:   {cur_pwd}")
    print("----------------------------------------------------------")
    try:
        new_p = input("请输入新的管理员密码 (直接回车保持不变): ").strip()
        if new_p:
            if len(new_p) < 5:
                print("错误: 为保障安全，密码长度不能少于 5 位！")
                sys.exit(1)
            g["admin_password"] = new_p
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            print(f"[ OK ] 管理员密码已成功重设为: {new_p}")

        new_path = input("请输入新的自定义管理入口路径 (如: my_secret_admin，直接回车保持不变): ").strip().lstrip('/')
        if new_path:
            clean_path = re.sub(r'[^a-zA-Z0-9_\-]', '', new_path)
            if clean_path:
                g["login_path"] = clean_path
                os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
                print(f"[ OK ] 管理入口路径已成功设置为: /{clean_path}")
    except (KeyboardInterrupt, EOFError):
        print("\n操作已取消")
    sys.exit(0)

from flask import Flask, request, jsonify, render_template, send_file, Response, make_response, redirect

app = Flask(__name__, static_folder='static', template_folder='templates')
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

config_lock = threading.RLock()
tasks = {}
tasks_lock = threading.RLock()

# Dedicated permanent system backup directory (completely outside git repository)
ETC_BACKUP_DIR = "/etc/vps-cluster-manager"
ETC_ROLL_DIR = "/etc/vps-cluster-manager/backups"

DEFAULT_CONFIG = {
    "global": {
        "admin_password": "",
        "login_path": "login",
        "stealth_mode": False,
        "push_url": "http://23.254.150.14:5000",
        "push_token": "",
        "auto_push": True,
        "cf_dns_token": "",
        "default_protocols": {
            "reality": True,
            "hy2": True,
            "tuic": True,
            "xhttp": True,
            "awg": True
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

def find_all_backup_configs():
    """Scans all possible disk locations and returns list of candidate file paths."""
    candidates = []
    
    # 1. Known primary and historical paths (including dedicated /etc/ paths)
    static_paths = [
        os.path.join(ETC_BACKUP_DIR, "config.json"),
        os.path.join(ETC_BACKUP_DIR, "config.json.bak"),
        os.path.join(DATA_DIR, "config.json.bak"),
        os.path.join(DATA_DIR, "config.json.tmp"),
        "/var/backups/vps-cluster-manager-config.json",
        "/tmp/vps_cm_config_backup.json",
        "/tmp/vps_cm_data_bak/config.json",
        "/tmp/vps_cm_data_bak/data/config.json",
        "/root/vps-cluster-manager/data/config.json",
        "/root/vps-cluster-manager/data/config.json.bak",
        "/root/vps-cluster-manager/data/config.json.tmp",
        "/root/vps-cluster-manager/config.json",
        "/opt/vps-cluster-manager/data/config.json.bak",
        "/opt/vps-cluster-manager/data/config.json.tmp"
    ]
    for p in static_paths:
        if os.path.isfile(p):
            candidates.append(p)

    # 2. Check dedicated permanent /etc/vps-cluster-manager/backups/
    if os.path.isdir(ETC_ROLL_DIR):
        try:
            for f in os.listdir(ETC_ROLL_DIR):
                if f.endswith('.json'):
                    fp = os.path.join(ETC_ROLL_DIR, f)
                    if os.path.isfile(fp) and fp not in candidates:
                        candidates.append(fp)
        except Exception:
            pass

    # 3. Check /opt/vps-cluster-manager/data/backups/
    roll_dir = os.path.join(DATA_DIR, 'backups')
    if os.path.isdir(roll_dir):
        try:
            for f in os.listdir(roll_dir):
                if f.endswith('.json'):
                    fp = os.path.join(roll_dir, f)
                    if os.path.isfile(fp) and fp not in candidates:
                        candidates.append(fp)
        except Exception:
            pass

    # 4. Check /var/backups/
    if os.path.isdir('/var/backups'):
        try:
            for f in os.listdir('/var/backups'):
                if ('vps' in f.lower() or 'cluster' in f.lower() or 'config' in f.lower()) and f.endswith('.json'):
                    fp = os.path.join('/var/backups', f)
                    if os.path.isfile(fp) and fp not in candidates:
                        candidates.append(fp)
        except Exception:
            pass

    # 5. Check /tmp/
    if os.path.isdir('/tmp'):
        try:
            for f in os.listdir('/tmp'):
                if ('vps' in f.lower() or 'config' in f.lower()) and f.endswith('.json'):
                    fp = os.path.join('/tmp', f)
                    if os.path.isfile(fp) and fp not in candidates:
                        candidates.append(fp)
        except Exception:
            pass

    return candidates

def load_config():
    with config_lock:
        data = None
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[ERROR] Failed to load config from {CONFIG_FILE}: {e}")

        # If data is missing or has empty servers list, exhaustively search all historical paths and backups
        if not data or not data.get("servers"):
            best_servers = []
            best_global = {}
            for cand in find_all_backup_configs():
                if cand != CONFIG_FILE:
                    try:
                        with open(cand, 'r', encoding='utf-8') as f:
                            cand_data = json.load(f)
                            if isinstance(cand_data, dict):
                                srvs = cand_data.get("servers", [])
                                if len(srvs) > len(best_servers):
                                    best_servers = srvs
                                    if cand_data.get("global"):
                                        best_global = cand_data.get("global")
                                    print(f"[INFO] Auto-discovered {len(srvs)} servers from historical backup {cand}")
                    except Exception:
                        pass

            if best_servers:
                if not data:
                    data = json.loads(json.dumps(DEFAULT_CONFIG))
                data["servers"] = best_servers
                if best_global:
                    data.setdefault("global", {}).update(best_global)
                print(f"[INFO] Successfully auto-recovered {len(best_servers)} VPS nodes into active config!")
                save_config_unlocked(data)

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
        os.makedirs(DATA_DIR, exist_ok=True)
        temp_file = CONFIG_FILE + '.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Atomic file replacement without deletion window
        os.replace(temp_file, CONFIG_FILE)

        # Multi-layer rolling backups (Never wipe non-empty backups with empty ones)
        curr_servers = data.get("servers", [])
        if curr_servers:
            # 1. Update directory backup
            try:
                bak_file = os.path.join(DATA_DIR, 'config.json.bak')
                with open(bak_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            # 2. Update system-wide backup in /var/backups
            try:
                os.makedirs('/var/backups', exist_ok=True)
                sys_bak = '/var/backups/vps-cluster-manager-config.json'
                with open(sys_bak, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            # 3. Create persistent rolling backup with timestamp in data/backups
            try:
                roll_dir = os.path.join(DATA_DIR, 'backups')
                os.makedirs(roll_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                with open(os.path.join(roll_dir, f"config_{ts}.json"), 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                # Keep up to 30 rolling backups
                all_baks = sorted([os.path.join(roll_dir, f) for f in os.listdir(roll_dir) if f.endswith('.json')])
                while len(all_baks) > 30:
                    oldest = all_baks.pop(0)
                    try:
                        os.remove(oldest)
                    except Exception:
                        pass
            except Exception:
                pass

            # 4. Update dedicated system backup in /etc/vps-cluster-manager (never affected by git updates)
            try:
                os.makedirs(ETC_ROLL_DIR, exist_ok=True)
                etc_cfg = os.path.join(ETC_BACKUP_DIR, "config.json")
                with open(etc_cfg, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                etc_roll_file = os.path.join(ETC_ROLL_DIR, f"config_{ts}.json")
                with open(etc_roll_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                # Keep up to 30 rolling backups in /etc/vps-cluster-manager/backups
                etc_baks = sorted([os.path.join(ETC_ROLL_DIR, f) for f in os.listdir(ETC_ROLL_DIR) if f.endswith('.json')])
                while len(etc_baks) > 30:
                    oldest = etc_baks.pop(0)
                    try:
                        os.remove(oldest)
                    except Exception:
                        pass
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
        if len(tasks) > 500:
            finished = [k for k, v in tasks.items() if v.get("status") in ("success", "error")]
            if len(finished) > 100:
                sorted_old = sorted(finished, key=lambda k: tasks[k].get("created_at", ""))
                for old_k in sorted_old[:50]:
                    tasks.pop(old_k, None)
        tasks[task_id] = task
    return task_id

ANSI_ESCAPE = re.compile(r'(\x1b\[[0-9;?]*[a-zA-Z]|\ufffd\[[0-9]+(;[0-9]+)*[a-zA-Z]|\[[0-9]{1,2}(;[0-9]{1,2})*m)')

def strip_ansi(text):
    if not text:
        return ""
    clean = ANSI_ESCAPE.sub('', str(text))
    return clean.replace('\ufffd', '')

def append_task_log(task_id, message, step=None, progress=None):
    with tasks_lock:
        if task_id in tasks:
            ts = datetime.now().strftime("%H:%M:%S")
            clean_msg = strip_ansi(str(message))
            tasks[task_id]["logs"].append(f"[{ts}] {clean_msg}")
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
    enable_x = 1 if protocols.get('xhttp', True) else 0
    enable_awg = 1 if protocols.get('awg', True) else 0

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

        needed_ports = enable_r + enable_h + enable_t + enable_x + enable_awg
        if len(port_list) < needed_ports:
            raise ValueError(f"NAT 端口池数量不足！已启用 {needed_ports} 个协议，但仅提供了 {len(port_list)} 个端口。")

        idx = 0
        p_reality = port_list[idx] if enable_r else 0
        if enable_r: idx += 1
        p_hy2 = port_list[idx] if enable_h else 0
        if enable_h: idx += 1
        p_tuic = port_list[idx] if enable_t else 0
        if enable_t: idx += 1
        p_xhttp = port_list[idx] if enable_x else 0
        if enable_x: idx += 1
        p_awg = port_list[idx] if enable_awg else 0
        if enable_awg: idx += 1

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
            "p_xhttp": p_xhttp,
            "p_awg": p_awg,
            "hop_range": hop_range,
            "allocated_summary": f"Reality:{p_reality} Hy2:{p_hy2} TUIC:{p_tuic} xHTTP:{p_xhttp} AWG:{p_awg} 跳跃:{hop_range or '无'}"
        }
    else:
        # Standard auto mode: find free ports or leave empty for installer auto-detect
        return {
            "mode": "auto",
            "p_reality": 0,
            "p_hy2": 0,
            "p_tuic": 0,
            "p_xhttp": 0,
            "p_awg": 0,
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
# 2. 根分区磁盘安全检查与智能动态虚拟内存 (Swap) 挂载 (杜绝爆磁盘/爆内存)
# ==============================================================================
# 2.1 紧急磁盘自救: 若磁盘可用空间告急 (<300MB) 且已存在过大的 /swapfile，立刻释放以恢复磁盘可用空间
DISK_FREE_MB=$(df -m / 2>/dev/null | awk 'END {print $(NF-2)}')
if [ "${DISK_FREE_MB:-0}" -lt 300 ] && [ -f /swapfile ]; then
  info "检测到根分区可用空间告急 (${DISK_FREE_MB:-0}MB < 300MB) 且存在旧 Swapfile，正在释放以恢复磁盘配额..."
  swapoff /swapfile 2>/dev/null || true
  rm -f /swapfile 2>/dev/null || true
  sed -i '/\/swapfile/d' /etc/fstab 2>/dev/null || true
  DISK_FREE_MB=$(df -m / 2>/dev/null | awk 'END {print $(NF-2)}')
  info "旧 Swapfile 已成功释放！当前磁盘已恢复可用空间: ${DISK_FREE_MB:-0}MB"
fi

# 2.2 智能根据物理内存和磁盘剩余动态规划 Swap 大小
MEM_TOTAL=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
SWAP_TOTAL=$(awk '/SwapTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)

# 仅在物理内存不足 1GB 且当前无有效 Swap 的机器上按需轻量分配
if [ "$MEM_TOTAL" -lt 1048576 ] && [ "$SWAP_TOTAL" -lt 262144 ]; then
  SWAP_SIZE_MB=0
  if [ "${DISK_FREE_MB:-0}" -ge 2500 ]; then
    SWAP_SIZE_MB=1024
  elif [ "${DISK_FREE_MB:-0}" -ge 1200 ]; then
    SWAP_SIZE_MB=512
  elif [ "${DISK_FREE_MB:-0}" -ge 600 ]; then
    SWAP_SIZE_MB=256
  fi

  if [ "$SWAP_SIZE_MB" -gt 0 ] && [ ! -f /swapfile ]; then
    info "检测到服务器物理内存 $((MEM_TOTAL/1024))MB，磁盘可用 ${DISK_FREE_MB}MB，智能安全分配 ${SWAP_SIZE_MB}MB Swapfile..."
    fallocate -l ${SWAP_SIZE_MB}M /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=${SWAP_SIZE_MB} 2>/dev/null || true
    if [ -f /swapfile ]; then
      chmod 600 /swapfile
      mkswap /swapfile >/dev/null 2>&1 || true
      if swapon /swapfile 2>/dev/null; then
        if ! grep -q "/swapfile" /etc/fstab 2>/dev/null; then
          echo "/swapfile swap swap defaults 0 0" >> /etc/fstab 2>/dev/null || true
        fi
        info "${SWAP_SIZE_MB}MB 虚拟内存 (Swapfile) 已成功激活挂载！"
      else
        # 部分 OpenVZ / LXC 容器内核禁止 swapon，立即清理掉文件，不白占磁盘
        rm -f /swapfile 2>/dev/null || true
        info "受限虚拟化环境禁止 swapon，已自动回收释放 ${SWAP_SIZE_MB}MB 磁盘空间！"
      fi
    fi
  else
    info "当前磁盘可用空间紧凑 (${DISK_FREE_MB:-0}MB)，跳过分配 Swapfile，优先确保系统运行及核心下载所需磁盘空间安全！"
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
  rm -rf /var/cache/apk/* 2>/dev/null || true
elif command -v apt-get >/dev/null 2>&1; then
  info "Configuring Debian/Ubuntu (apt-get)..."
  export DEBIAN_FRONTEND=noninteractive
  export NEEDRESTART_MODE=a
  export NEEDRESTART_SUSPEND=1
  if [ -f /etc/needrestart/needrestart.conf ]; then
    sed -i "s/#\$nrconf{restart} = 'i';/\$nrconf{restart} = 'a';/g" /etc/needrestart/needrestart.conf 2>/dev/null || true
  fi

  # 检查基础运行依赖是否完备 (curl/wget, bash, tar)
  NEED_INSTALL=0
  if (! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1) || ! command -v bash >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
    NEED_INSTALL=1
  fi

  if [ "$NEED_INSTALL" -eq 1 ]; then
    info "检测到缺失核心组件，正在清理锁并更新软件源..."
    systemctl stop unattended-upgrades apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
    killall -9 apt apt-get dpkg unattended-upgrade 2>/dev/null || true
    rm -f /var/lib/dpkg/lock* /var/lib/apt/lists/lock /var/cache/apt/archives/lock 2>/dev/null || true
    dpkg --configure -a 2>/dev/null || true

    APT_OPTS="-o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10 -o Acquire::Retries=1 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
    if command -v timeout >/dev/null 2>&1; then
      timeout 30 apt-get update -qq $APT_OPTS 2>/dev/null || true
      timeout 60 apt-get install -y -qq $APT_OPTS curl wget jq tar ca-certificates bash chrony iptables iproute2 dnsutils 2>/dev/null || true
    else
      apt-get update -qq $APT_OPTS 2>/dev/null || true
      apt-get install -y -qq $APT_OPTS curl wget jq tar ca-certificates bash chrony iptables iproute2 dnsutils 2>/dev/null || true
    fi
  else
    info "Debian/Ubuntu 基础环境完备 (curl/tar/bash 就绪)，跳过 apt 阻塞更新！"
  fi
elif command -v dnf >/dev/null 2>&1; then
  info "Configuring RHEL/Fedora/Rocky (dnf)..."
  dnf install -y -q curl wget jq tar ca-certificates bash chrony iptables iproute bind-utils >/dev/null 2>&1 || true
  dnf clean all >/dev/null 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  info "Configuring CentOS/RHEL (yum)..."
  yum install -y -q curl wget jq tar ca-certificates bash chrony iptables iproute bind-utils >/dev/null 2>&1 || true
  yum clean all >/dev/null 2>&1 || true
elif command -v pacman >/dev/null 2>&1; then
  info "Configuring Arch Linux (pacman)..."
  pacman -Sy --noconfirm curl wget jq tar ca-certificates bash chrony iptables iproute2 >/dev/null 2>&1 || true
  pacman -Sc --noconfirm >/dev/null 2>&1 || true
fi

# Ensure /bin/bash exists
if [ ! -x /bin/bash ] && command -v bash >/dev/null 2>&1; then
  ln -sf "$(command -v bash)" /bin/bash 2>/dev/null || true
fi

# Time Calibration (Reality requires time sync)
info "Calibrating server time via NTP..."
if command -v chronyd >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 chronyd -q 'server pool.ntp.org iburst' >/dev/null 2>&1 || true
  else
    chronyd -q 'server pool.ntp.org iburst' >/dev/null 2>&1 || true
  fi
elif command -v ntpdate >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 ntpdate -u pool.ntp.org >/dev/null 2>&1 || true
  else
    ntpdate -u pool.ntp.org >/dev/null 2>&1 || true
  fi
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

  # 4. 写入 sysctl 内核调优配置 (容错保护)
  mkdir -p /etc/sysctl.d 2>/dev/null || true
  cat > /etc/sysctl.d/99-bbr.conf <<EOF 2>/dev/null || true
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = ${TARGET_CC}
EOF

  if [ -f /etc/sysctl.conf ]; then
    sed -i '/net.core.default_qdisc/d' /etc/sysctl.conf 2>/dev/null || true
    sed -i '/net.ipv4.tcp_congestion_control/d' /etc/sysctl.conf 2>/dev/null || true
    echo "net.core.default_qdisc = fq" >> /etc/sysctl.conf 2>/dev/null || true
    echo "net.ipv4.tcp_congestion_control = ${TARGET_CC}" >> /etc/sysctl.conf 2>/dev/null || true
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
    server = next((s for s in cfg.get("servers", []) if str(s.get("id")) == str(server_id)), None)
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
        argo_is_on = bool(argo_enabled) and str(argo_enabled).lower() not in ('false', '0', '')

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
            env_exports.append(f"export PORT_XHTTP_DIRECT={port_info.get('p_xhttp', 0)}")
            env_exports.append(f"export PORT_AWG={port_info.get('p_awg', 0)}")
            if port_info["hop_range"]:
                env_exports.append(f"export SB_HOP_RANGE=\"{port_info['hop_range']}\"")
            else:
                env_exports.append("export SB_HOP_RANGE=\"\"")

        # Protocols
        protocols = global_cfg.get("default_protocols", {})
        env_exports.append(f"export SB_ENABLE_REALITY={1 if protocols.get('reality', True) else 0}")
        env_exports.append(f"export SB_ENABLE_HY2={1 if protocols.get('hy2', True) else 0}")
        env_exports.append(f"export SB_ENABLE_TUIC={1 if protocols.get('tuic', True) else 0}")
        env_exports.append(f"export SB_ENABLE_XHTTP_DIRECT={1 if protocols.get('xhttp', True) else 0}")
        env_exports.append(f"export SB_ENABLE_AWG={1 if protocols.get('awg', True) else 0}")

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

        # Argo (Dual export for maximum compatibility)
        if argo_is_on and argo_domain and argo_token:
            argo_trans = server.get('argo_transport', 'xhttp')
            env_exports.append(f"export SB_ARGO_DOMAIN=\"{argo_domain}\"")
            env_exports.append(f"export SB_ARGO_TOKEN=\"{argo_token}\"")
            env_exports.append(f"export SB_ARGO_TRANSPORT=\"{argo_trans}\"")
            env_exports.append(f"export ARGO_DOMAIN=\"{argo_domain}\"")
            env_exports.append(f"export ARGO_TOKEN=\"{argo_token}\"")
            env_exports.append(f"export ARGO_TRANSPORT=\"{argo_trans}\"")

        argo_status_desc = f"已启用 ({argo_domain} | {server.get('argo_transport', 'xhttp')})" if (argo_is_on and argo_domain and argo_token) else "未启用"
        append_task_log(task_id, f"部署参数就绪: 端口策略 [{port_info['allocated_summary']}], Argo 隧道 [{argo_status_desc}]", progress=55)

        # Command to run installer with multi-CDN mirror fallback and strict exit code verification
        deploy_cmd = f"""
{chr(10).join(env_exports)}
INSTALL_URLS="{script_url} https://fastly.jsdelivr.net/gh/kzhx666/sb-install-plus@main/install.sh https://cdn.jsdelivr.net/gh/kzhx666/sb-install-plus@main/install.sh https://mirror.ghproxy.com/{script_url} https://ghproxy.net/{script_url}"

DL_OK=0
for u in $INSTALL_URLS; do
  echo "正在获取部署脚本: $u ..."
  if curl -fsSL --connect-timeout 8 -m 30 "$u" -o /tmp/sb_install.sh 2>/dev/null || wget -q -T 20 -O /tmp/sb_install.sh "$u" 2>/dev/null; then
    if [ -s /tmp/sb_install.sh ] && grep -q "Sing-box" /tmp/sb_install.sh 2>/dev/null; then
      DL_OK=1
      echo "部署脚本拉取成功并校验通过！"
      break
    fi
  fi
done

if [ "$DL_OK" -ne 1 ]; then
  echo "错误: 所有镜像源均无法拉取 Sing-box 安装脚本，请检查 VPS 国际互联网连接！"
  exit 1
fi

chmod +x /tmp/sb_install.sh
/bin/bash /tmp/sb_install.sh --auto
INSTALL_CODE=$?
rm -f /tmp/sb_install.sh
if [ "$INSTALL_CODE" -ne 0 ]; then
  echo "部署脚本执行失败，退出代码: $INSTALL_CODE"
  exit "$INSTALL_CODE"
fi
"""
        append_task_log(task_id, "开始执行 Sing-box 核心协议集群自动化部署...", step="部署 Sing-box 极速核心", progress=60)
        code, out = runner.run_cmd(deploy_cmd, log_callback=log_cb, timeout=800)
        if code != 0:
            raise Exception(f"安装脚本执行返回非 0 状态码 ({code})")

        append_task_log(task_id, "部署执行完毕！正在验证服务运行状态与提取节点凭证...", step="验证服务状态", progress=90)

        # Check service status
        check_cmd = r'''
if systemctl is-active sing-box >/dev/null 2>&1; then echo "active";
elif command -v rc-service >/dev/null 2>&1 && rc-service sing-box status 2>/dev/null | grep -qi "started"; then echo "active";
elif [ -x /etc/init.d/sing-box ] && /etc/init.d/sing-box status 2>/dev/null | grep -qi "started"; then echo "active";
elif pidof sing-box >/dev/null 2>&1 || pgrep -f "sing-box" >/dev/null 2>&1; then echo "active";
elif [ -f /run/sing-box.pid ] && kill -0 "$(cat /run/sing-box.pid 2>/dev/null)" 2>/dev/null; then echo "active";
else rc-service sing-box status 2>/dev/null || systemctl is-active sing-box 2>/dev/null || echo "inactive"; fi
'''
        c_code, c_out = runner.run_cmd(check_cmd, timeout=10)
        c_lower = c_out.lower()
        is_running = any(k in c_lower for k in ("active", "started", "running")) or any(s.strip().isdigit() for s in c_out.splitlines() if s.strip())

        # Check cloudflared service if Argo is enabled
        if argo_is_on and argo_domain and argo_token:
            cf_check = r'''
if systemctl is-active cloudflared >/dev/null 2>&1; then echo "active";
elif command -v rc-service >/dev/null 2>&1 && rc-service cloudflared status 2>/dev/null | grep -qi "started"; then echo "active";
elif [ -x /etc/init.d/cloudflared ] && /etc/init.d/cloudflared status 2>/dev/null | grep -qi "started"; then echo "active";
elif pidof cloudflared >/dev/null 2>&1 || pgrep -f "cloudflared" >/dev/null 2>&1; then echo "active";
else rc-service cloudflared status 2>/dev/null || systemctl is-active cloudflared 2>/dev/null || echo "inactive"; fi
'''
            _, cf_out = runner.run_cmd(cf_check, timeout=10)
            cf_lower = cf_out.lower()
            cf_running = any(k in cf_lower for k in ("active", "started", "running")) or any(s.strip().isdigit() for s in cf_out.splitlines() if s.strip())
            if cf_running:
                append_task_log(task_id, "Cloudflare Argo 隧道守护进程已连接并正常在线！", progress=95)
            else:
                append_task_log(task_id, "提示: Cloudflare Argo 隧道守护进程未处于 active 状态，请检查 Token 权限或服务日志", progress=95)

        # Update server state in config
        with config_lock:
            cur_cfg = load_config()
            for s in cur_cfg.get("servers", []):
                if str(s.get("id")) == str(server_id):
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
                if str(s.get("id")) == str(server_id):
                    s["status"] = "error"
                    s["last_error"] = str(e)
                    break
            save_config_unlocked(cur_cfg)
    finally:
        runner.close()

# ------------------------------------------------------------------------------
# Security Infrastructure: Authentication, Sessions & Anti-Scanning Rate Limiting
# ------------------------------------------------------------------------------
def get_secret_key():
    key_paths = [
        os.path.join(ETC_BACKUP_DIR, '.secret_key'),
        os.path.join(DATA_DIR, '.secret_key')
    ]
    for p in key_paths:
        if os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    k = f.read().strip()
                    if k:
                        return k
            except Exception:
                pass
    k = uuid.uuid4().hex + uuid.uuid4().hex
    for p in key_paths:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as f:
                f.write(k)
            break
        except Exception:
            pass
    return k

SECRET_KEY = get_secret_key()

FAILED_ATTEMPTS = {}
FAILED_LOCKS = {}
RATE_LIMIT_LOCK = threading.Lock()

def get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

def check_ip_rate_limit(ip):
    now = time.time()
    with RATE_LIMIT_LOCK:
        if ip in FAILED_LOCKS:
            if now < FAILED_LOCKS[ip]:
                remaining = int(FAILED_LOCKS[ip] - now)
                return False, f"登录失败过多，安全网关已临时阻断该 IP，请 {remaining} 秒后再试"
            else:
                del FAILED_LOCKS[ip]
                FAILED_ATTEMPTS.pop(ip, None)
    return True, ""

def record_failed_attempt(ip):
    now = time.time()
    with RATE_LIMIT_LOCK:
        attempts = [t for t in FAILED_ATTEMPTS.get(ip, []) if now - t < 60]
        attempts.append(now)
        FAILED_ATTEMPTS[ip] = attempts
        if len(attempts) >= 5:
            FAILED_LOCKS[ip] = now + 60
            return 60
    return 0

def record_successful_attempt(ip):
    with RATE_LIMIT_LOCK:
        FAILED_ATTEMPTS.pop(ip, None)
        FAILED_LOCKS.pop(ip, None)

def get_admin_password():
    env_pwd = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pwd:
        return env_pwd
    cfg = load_config()
    return (cfg.get("global", {}).get("admin_password") or "").strip()

def is_auth_configured():
    return bool(get_admin_password())

def get_login_path():
    cfg = load_config()
    p = (cfg.get("global", {}).get("login_path") or "login").strip().lstrip('/')
    return p if p else "login"

def is_stealth_mode():
    cfg = load_config()
    return bool(cfg.get("global", {}).get("stealth_mode", False))

def create_auth_token(password):
    ts = str(int(time.time()))
    pwd_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()[:16]
    payload = f"{ts}:{pwd_hash}"
    sig = hmac.new(SECRET_KEY.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    return f"{payload}:{sig}"

def verify_auth_token(token, password):
    if not token or not password:
        return False
    parts = token.split(':')
    if len(parts) != 3:
        return False
    ts, pwd_hash, sig = parts
    try:
        created_time = int(ts)
    except ValueError:
        return False
    if time.time() - created_time > 86400 * 30:
        return False
    expected_payload = f"{ts}:{pwd_hash}"
    expected_sig = hmac.new(SECRET_KEY.encode('utf-8'), expected_payload.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected_sig):
        return False
    current_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()[:16]
    return hmac.compare_digest(pwd_hash, current_hash)

def is_authenticated():
    current_pwd = get_admin_password()
    if not current_pwd:
        return False

    # 1. Cookie Session
    cookie_token = request.cookies.get('auth_session')
    if cookie_token and verify_auth_token(cookie_token, current_pwd):
        return True

    # 2. Authorization Header
    auth_h = request.headers.get('Authorization', '').strip()
    if auth_h:
        token = auth_h[7:].strip() if auth_h.lower().startswith('bearer ') else auth_h
        if token:
            if hmac.compare_digest(token, current_pwd):
                return True
            if verify_auth_token(token, current_pwd):
                return True

    # 3. Custom Headers
    api_key = request.headers.get('X-API-Key', '').strip() or request.headers.get('X-Token', '').strip()
    if api_key and hmac.compare_digest(api_key, current_pwd):
        return True

    # 4. URL query: ?token=<token>
    query_tok = request.args.get('token', '').strip()
    if query_tok and hmac.compare_digest(query_tok, current_pwd):
        return True

    return False

@app.before_request
def require_authentication():
    # Always allow static files & browser favicon
    if request.path.startswith('/static') or request.path == '/favicon.ico':
        return None

    cur_login_path = '/' + get_login_path()

    # Dynamic check for configured login path
    if request.path == cur_login_path:
        if is_authenticated():
            return redirect('/')
        return render_template('login.html', is_configured=is_auth_configured(), login_path=get_login_path())

    # Authentication API endpoints & logout
    if request.path in ['/api/login', '/api/init_auth', '/logout']:
        return None

    # Authenticated user handling
    if is_authenticated():
        # If accessing disabled /login path when a custom secret path is active, return 404
        if request.path == '/login' and cur_login_path != '/login':
            return "404 Not Found", 404
        return None

    # Block unauthenticated API access immediately
    if request.path.startswith('/api/'):
        return jsonify({
            "success": False,
            "message": "未授权访问，请先通过管理密码登录",
            "need_login": True
        }), 401

    # Root path / handling:
    # Only redirect / to /login when login_path is default 'login' and stealth mode is OFF
    if request.path == '/' and cur_login_path == '/login' and not is_stealth_mode():
        return redirect('/login')

    # For ALL other unauthenticated paths (custom path active, stealth mode on, previous secret paths, scanner probes):
    # Strictly return 404! Never redirect to cur_login_path!
    return "404 Not Found", 404

@app.route('/login', methods=['GET'])
def login_route():
    cur_login_path = '/' + get_login_path()
    if cur_login_path != '/login':
        return "404 Not Found", 404
    if is_authenticated():
        return redirect('/')
    return render_template('login.html', is_configured=is_auth_configured(), login_path=get_login_path())

@app.route('/api/init_auth', methods=['POST'])
def api_init_auth():
    if is_auth_configured():
        return jsonify({"success": False, "message": "管理员密码已设定，无法重复初始化"}), 403

    data = request.get_json() or {}
    password = (data.get("password") or "").strip()
    if not password:
        return jsonify({"success": False, "message": "密码不能为空"}), 400
    if len(password) < 5:
        return jsonify({"success": False, "message": "为保障安全，密码长度不能少于 5 个字符"}), 400

    cfg = load_config()
    cfg.setdefault("global", {})["admin_password"] = password
    save_config(cfg)

    record_successful_attempt(get_client_ip())
    token = create_auth_token(password)
    resp = make_response(jsonify({"success": True, "message": "管理员密码初始化成功"}))
    resp.set_cookie('auth_session', token, max_age=86400 * 30, httponly=True, samesite='Lax')
    return resp

@app.route('/api/login', methods=['POST'])
def api_login():
    ip = get_client_ip()
    allowed, err_msg = check_ip_rate_limit(ip)
    if not allowed:
        return jsonify({"success": False, "message": err_msg}), 429

    if not is_auth_configured():
        return jsonify({"success": False, "message": "面板尚未初始化管理密码，请先完成安全初始化"}), 400

    data = request.get_json() or {}
    password = (data.get("password") or "").strip()
    remember = bool(data.get("remember", True))
    admin_pwd = get_admin_password()

    if hmac.compare_digest(password, admin_pwd):
        record_successful_attempt(ip)
        token = create_auth_token(admin_pwd)
        max_age = 86400 * 30 if remember else None
        resp = make_response(jsonify({"success": True, "message": "登录成功"}))
        resp.set_cookie('auth_session', token, max_age=max_age, httponly=True, samesite='Lax')
        return resp
    else:
        lock_sec = record_failed_attempt(ip)
        if lock_sec > 0:
            return jsonify({"success": False, "message": f"密码错误！连续失败次数过多，安全网关已锁定该 IP {lock_sec} 秒"}), 429
        return jsonify({"success": False, "message": "管理员密码错误，请重新输入"}), 400

@app.route('/api/auth/change_password', methods=['POST'])
def api_change_password():
    if not is_authenticated():
        return jsonify({"success": False, "message": "未登录"}), 401

    data = request.get_json() or {}
    old_pwd = (data.get("old_password") or "").strip()
    new_pwd = (data.get("new_password") or "").strip()
    admin_pwd = get_admin_password()

    if not hmac.compare_digest(old_pwd, admin_pwd):
        return jsonify({"success": False, "message": "当前原密码输入错误"}), 400
    if not new_pwd or len(new_pwd) < 5:
        return jsonify({"success": False, "message": "新密码长度不能少于 5 个字符"}), 400

    cfg = load_config()
    cfg.setdefault("global", {})["admin_password"] = new_pwd
    save_config(cfg)

    new_token = create_auth_token(new_pwd)
    resp = make_response(jsonify({"success": True, "message": "管理密码修改成功"}))
    resp.set_cookie('auth_session', new_token, max_age=86400 * 30, httponly=True, samesite='Lax')
    return resp

@app.route('/logout', methods=['GET'])
def logout_route():
    cur_login_path = '/' + get_login_path()
    resp = make_response(redirect(cur_login_path))
    resp.delete_cookie('auth_session')
    return resp

@app.after_request
def add_no_cache_headers(response):
    if request.path == '/' or request.path.endswith('.html') or request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

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
    cfg = load_config()
    cfg_copy = json.loads(json.dumps(cfg))
    cfg_copy.setdefault("global", {})["admin_password_set"] = bool(cfg.get("global", {}).get("admin_password"))
    cfg_copy.setdefault("global", {})["admin_password"] = ""
    return jsonify(cfg_copy)

@app.route('/api/config/global', methods=['POST'])
def update_global_config():
    payload = request.get_json() or {}
    cfg = load_config()
    if "admin_password" in payload:
        new_p = str(payload["admin_password"]).strip()
        if new_p:
            cfg["global"]["admin_password"] = new_p
        payload.pop("admin_password", None)
    if "login_path" in payload:
        clean_lp = re.sub(r'[^a-zA-Z0-9_\-]', '', str(payload["login_path"])).strip().lstrip('/')
        payload["login_path"] = clean_lp if clean_lp else "login"
    if "stealth_mode" in payload:
        payload["stealth_mode"] = bool(payload["stealth_mode"])
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
        "argo_transport": data.get('argo_transport', 'xhttp'),
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
        'argo_enabled', 'argo_domain', 'argo_transport', 'argo_token'
    ]
    for field in updatable_fields:
        if field in data:
            if field == 'port':
                server[field] = int(data[field] or 22)
            elif field == 'argo_enabled':
                server[field] = bool(data[field])
            elif field == 'argo_transport':
                server['argo_transport'] = 'ws' if str(data[field]).lower() == 'ws' else 'xhttp'
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
    cfg["servers"] = [s for s in cfg.get("servers", []) if str(s.get("id")) != str(server_id)]
    if len(cfg["servers"]) == orig_len:
        return jsonify({"success": False, "message": "未找到要删除的服务器"}), 404
    save_config(cfg)
    return jsonify({"success": True})

@app.route('/api/server/batch_delete', methods=['POST'])
def batch_delete_servers():
    data = request.get_json() or {}
    sids = [str(x) for x in (data.get('server_ids') or [])]
    if not sids:
        return jsonify({"success": False, "message": "未选定任何 VPS"}), 400
    with config_lock:
        cur = load_config()
        original_count = len(cur.get("servers", []))
        cur["servers"] = [s for s in cur.get("servers", []) if str(s.get("id")) not in sids]
        deleted_count = original_count - len(cur["servers"])
        save_config_unlocked(cur)
    return jsonify({"success": True, "message": f"已成功从集群管理面板移除 {deleted_count} 台 VPS", "count": deleted_count})

@app.route('/api/server/sync_client_backup', methods=['POST'])
def sync_client_backup():
    """Restores servers from client browser localStorage backup."""
    data = request.get_json() or {}
    client_servers = data.get("servers", [])
    if not isinstance(client_servers, list) or not client_servers:
        return jsonify({"success": False, "message": "备份数据为空或格式不正确"}), 400

    with config_lock:
        cfg = load_config()
        existing_keys = {(s.get("host"), int(s.get("port", 22))) for s in cfg.get("servers", [])}
        restored_count = 0

        for s in client_servers:
            if not s.get("host"):
                continue
            key = (s.get("host"), int(s.get("port", 22)))
            if key not in existing_keys:
                name = s.get("name") or s.get("host")
                if not s.get("tag_main") or s.get("tag_main") == name:
                    s["tag_main"] = f"{name}本机"
                if not s.get("tag_argo") or s.get("tag_argo", "").endswith("-Argo") or s.get("tag_argo") == name:
                    s["tag_argo"] = f"{name}优选"
                cfg["servers"].append(s)
                existing_keys.add(key)
                restored_count += 1

        if restored_count > 0:
            save_config_unlocked(cfg)

    return jsonify({
        "success": True,
        "restored_count": restored_count,
        "total_count": len(cfg.get("servers", [])),
        "message": f"成功从浏览器本地备份中恢复 {restored_count} 台 VPS 主机！"
    })

@app.route('/api/server/backup_status', methods=['GET'])
def backup_status():
    """Returns available backup files and their server counts for user diagnostics."""
    backups = []
    for cand in find_all_backup_configs():
        try:
            sz = os.path.getsize(cand)
            with open(cand, 'r', encoding='utf-8') as f:
                d = json.load(f)
                srvs = d.get("servers", []) if isinstance(d, dict) else []
                backups.append({
                    "path": cand,
                    "size_bytes": sz,
                    "servers_count": len(srvs),
                    "modified_time": datetime.fromtimestamp(os.path.getmtime(cand)).strftime("%Y-%m-%d %H:%M:%S")
                })
        except Exception:
            pass
    return jsonify({"success": True, "backups": backups})


# ------------------------------------------------------------------------------
# Routes: Pre-flight Check & Deployment Tasks
# ------------------------------------------------------------------------------
@app.route('/api/check_env/<server_id>', methods=['POST'])
def check_env(server_id):
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if str(s.get("id")) == str(server_id)), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    sname = server.get("name") or server.get("host")
    task_id = create_task("check_env", server_id, f"测环境: {sname}")
    append_task_log(task_id, f"发起对 [{sname}] ({server.get('host')}:{server.get('port', 22)}) 的系统环境与网络延迟测试...", step="准备连接", progress=10)

    def worker():
        host = server.get("host")
        port = server.get("port", 22)
        user = server.get("user", "root")
        pwd = server.get("password")
        key = server.get("private_key")

        runner = SSHRunner(host, port, user, pwd, key, timeout=10)
        start_t = time.time()
        try:
            append_task_log(task_id, f"正在建立 SSH 连接到 {host}:{port} (用户: {user})...", step="建立SSH连接", progress=25)
            runner.connect()
            latency_ms = round((time.time() - start_t) * 1000, 1)
            append_task_log(task_id, f"[OK] SSH 连接成功！TCP/SSH往返延迟: {latency_ms}ms", step="检测系统参数", progress=45)

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
            append_task_log(task_id, f"[OK] 系统发行版: {os_id} | CPU架构: {arch} | 网络拥塞算法: {bbr_str}", step="更新配置记录", progress=85)

            with config_lock:
                cur_cfg = load_config()
                for s in cur_cfg.get("servers", []):
                    if str(s.get("id")) == str(server_id):
                        s["latency_ms"] = latency_ms
                        s["os_info"] = f"{os_id} ({arch})"
                        s["bbr_info"] = bbr_str
                        break
                save_config_unlocked(cur_cfg)

            append_task_log(task_id, f"🎉 [{sname}] 环境与延迟检测全部完成！延迟: {latency_ms}ms", step="测试完成", progress=100)
            finish_task(task_id, success=True)
        except Exception as e:
            append_task_log(task_id, f"[ERR] 连接检测失败: {str(e)}", step="连接异常", progress=100)
            finish_task(task_id, success=False, error=str(e))
        finally:
            runner.close()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return jsonify({"success": True, "task_id": task_id, "message": f"已对 [{sname}] 启动环境检测任务"})

@app.route('/api/deploy/<server_id>', methods=['POST'])
def deploy_server(server_id):
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if str(s.get("id")) == str(server_id)), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    task_id = create_task("deploy", server_id, server.get("name", server.get("host")))
    t = threading.Thread(target=deploy_server_worker, args=(task_id, server_id), daemon=True)
    t.start()
    return jsonify({"success": True, "task_id": task_id})

@app.route('/api/deploy_all', methods=['POST'])
def deploy_all():
    data = request.get_json(silent=True) or {}
    req_sids = [str(x) for x in (data.get('server_ids') or [])]
    cfg = load_config()
    servers = cfg.get("servers", [])
    if req_sids:
        servers = [s for s in servers if str(s.get("id")) in req_sids]
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

@app.route('/api/tasks/recent', methods=['GET'])
def get_recent_tasks():
    with tasks_lock:
        limit_arg = request.args.get('limit', '200')
        try:
            limit = int(limit_arg)
        except Exception:
            limit = 200
        sorted_tasks = sorted(tasks.values(), key=lambda t: t.get('created_at', ''), reverse=True)
        recent = []
        target_tasks = sorted_tasks[:limit] if limit > 0 else sorted_tasks
        for t in target_tasks:
            recent.append({
                "id": t["id"],
                "type": t.get("type"),
                "server_id": t.get("server_id"),
                "server_name": t.get("server_name"),
                "status": t.get("status"),
                "progress": t.get("progress"),
                "step": t.get("step"),
                "created_at": t.get("created_at"),
                "log_count": len(t.get("logs", []))
            })
        latest_id = sorted_tasks[0]["id"] if sorted_tasks else None
        return jsonify({"success": True, "tasks": recent, "latest_id": latest_id})

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
if systemctl is-active sing-box >/dev/null 2>&1; then
    echo "active"
elif command -v rc-service >/dev/null 2>&1 && rc-service sing-box status 2>/dev/null | grep -qi "started"; then
    echo "active"
elif [ -x /etc/init.d/sing-box ] && /etc/init.d/sing-box status 2>/dev/null | grep -qi "started"; then
    echo "active"
elif pidof sing-box >/dev/null 2>&1; then
    echo "active"
elif pgrep -f "sing-box" >/dev/null 2>&1; then
    echo "active"
elif [ -f /run/sing-box.pid ] && kill -0 "$(cat /run/sing-box.pid 2>/dev/null)" 2>/dev/null; then
    echo "active"
else
    rc-service sing-box status 2>/dev/null || systemctl is-active sing-box 2>/dev/null || pgrep -f sing-box 2>/dev/null || echo "inactive"
fi
echo "---ARGO_STATUS---"
if systemctl is-active cloudflared >/dev/null 2>&1; then
    echo "active"
elif command -v rc-service >/dev/null 2>&1 && rc-service cloudflared status 2>/dev/null | grep -qi "started"; then
    echo "active"
elif [ -x /etc/init.d/cloudflared ] && /etc/init.d/cloudflared status 2>/dev/null | grep -qi "started"; then
    echo "active"
elif pidof cloudflared >/dev/null 2>&1 || pgrep -f "cloudflared" >/dev/null 2>&1; then
    echo "active"
else
    rc-service cloudflared status 2>/dev/null || systemctl is-active cloudflared 2>/dev/null || echo "inactive"
fi
echo "---PORTS---"
ss -lntu 2>/dev/null | awk '{print $1,$5}' || netstat -lntu 2>/dev/null | awk '{print $1,$4}'
echo "---IPS---"
curl -s4m4 ip.sb 2>/dev/null || wget -qO- -T 4 -4 ip.sb 2>/dev/null || true
echo ""
curl -s6m4 ip.sb 2>/dev/null || wget -qO- -T 4 -6 ip.sb 2>/dev/null || true
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
                lower = line_str.lower()
                if any(k in lower for k in ("active", "started", "running")) or line_str.isdigit():
                    sb_status = "active"
            elif current_sec == "---ARGO_STATUS---" and line_str:
                lower = line_str.lower()
                if any(k in lower for k in ("active", "started", "running")) or line_str.isdigit():
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
    server = next((s for s in cfg.get("servers", []) if str(s.get("id")) == str(server_id)), None)
    if not server:
        return jsonify({"success": False, "message": "服务器不存在"}), 404

    res = inspect_server_sync(server)
    with config_lock:
        cur_cfg = load_config()
        for s in cur_cfg.get("servers", []):
            if str(s.get("id")) == str(server_id):
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
    req_sids = [str(x) for x in (data.get('server_ids') or [])]
    cfg = load_config()
    servers = cfg.get("servers", [])
    if req_sids:
        servers = [s for s in servers if str(s.get("id")) in req_sids]
    if not servers:
        return jsonify({"success": False, "message": "当前没有选定任何 VPS 或集群列表为空"}), 400

    task_desc = f"健康巡检 ({len(servers)}台)" if len(servers) > 1 else f"单机巡检: {servers[0].get('name') or servers[0].get('host')}"
    task_id = create_task("inspect", "cluster", task_desc)
    append_task_log(task_id, f"正在对 {len(servers)} 台 VPS 启动健康巡检与延迟探测...", step="准备巡检", progress=5)

    def run_all_inspect():
        total = len(servers)
        online_count = 0
        for idx, s in enumerate(servers):
            sid = s.get("id")
            sname = s.get("name") or s.get("host")
            pct = int(10 + (idx / total) * 80)
            append_task_log(task_id, f"[{sname}] ({s.get('host')}:{s.get('port', 22)}) 连接中...", step=f"巡检中 ({idx+1}/{total})", progress=pct)
            res = inspect_server_sync(s)
            with config_lock:
                cur = load_config()
                for target in cur.get("servers", []):
                    if str(target.get("id")) == str(sid):
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

            if res.get("success"):
                status_desc = "🟢 服务运行中" if res.get("singbox_running") else "🟡 核心进程未在运行"
                bbr_desc = res.get("bbr") or "未知"
                append_task_log(task_id, f"[OK] [{sname}] 巡检完成: {status_desc} | 延迟: {res.get('latency_ms')}ms | BBR: {bbr_desc}")
                if res.get("singbox_running"):
                    online_count += 1
            else:
                append_task_log(task_id, f"[ERR] [{sname}] 巡检失败: {res.get('error')}")

        append_task_log(task_id, f"🎉 巡检任务全部完成！正常运行: {online_count}/{total} 台", step="巡检完毕", progress=100)
        finish_task(task_id, success=True)

    t = threading.Thread(target=run_all_inspect, daemon=True)
    t.start()
    return jsonify({"success": True, "task_id": task_id, "message": "已启动健康巡检任务"})

@app.route('/api/test_unlock/<server_id>', methods=['POST'])
def test_unlock(server_id):
    cfg = load_config()
    server = next((s for s in cfg.get("servers", []) if str(s.get("id")) == str(server_id)), None)
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
                if str(s.get("id")) == str(server_id):
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
            "argo_transport": "xhttp",
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
        str_sids = [str(x) for x in server_ids]
        targets = [s for s in servers if str(s.get("id")) in str_sids]
    else:
        targets = servers

    if not targets:
        return jsonify({"success": False, "message": "没有选定任何目标 VPS"}), 400

    single_mode = len(targets) == 1
    s_single_name = targets[0].get("name", targets[0].get("host")) if single_mode else ""
    action_names = {
        "restart": f"重启服务: [{s_single_name}]" if single_mode else f"批量重启服务 ({len(targets)}台)",
        "update_core": f"更新Sing-box核心(含AWG): [{s_single_name}]" if single_mode else f"批量更新Sing-box核心(含AWG) ({len(targets)}台)",
        "update_cloudflared": f"更新CF隧道核心: [{s_single_name}]" if single_mode else f"批量更新CF隧道核心 ({len(targets)}台)",
        "update_xray": f"更新Xray核心: [{s_single_name}]" if single_mode else f"批量更新Xray核心 ({len(targets)}台)",
        "push_nodes": f"推送节点至订阅管理平台: [{s_single_name}]" if single_mode else f"批量推送节点至订阅管理平台 ({len(targets)}台)",
        "update_script": f"更新sb本地管理脚本: [{s_single_name}]" if single_mode else f"批量更新sb本地管理脚本 ({len(targets)}台)",
        "reapply_bbr": f"刷新激活BBR: [{s_single_name}]" if single_mode else f"批量刷新BBR ({len(targets)}台)",
        "uninstall": f"彻底卸载清理: [{s_single_name}]" if single_mode else f"批量彻底卸载清理 ({len(targets)}台)"
    }
    action_desc = action_names.get(action, f"执行操作: {action}")
    task_id = create_task(f"batch_{action}", "cluster", action_desc)

    def run_ops():
        append_task_log(task_id, f"正在对 {len(targets)} 台 VPS 下发任务: [{action_desc}]...", step="下发指令", progress=10)
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
                        cmd = r'''
systemctl restart sing-box 2>/dev/null || rc-service sing-box restart 2>/dev/null || /etc/init.d/sing-box restart 2>/dev/null || true
sleep 1
if systemctl is-active sing-box >/dev/null 2>&1; then echo "active";
elif command -v rc-service >/dev/null 2>&1 && rc-service sing-box status 2>/dev/null | grep -qi "started"; then echo "active";
elif [ -x /etc/init.d/sing-box ] && /etc/init.d/sing-box status 2>/dev/null | grep -qi "started"; then echo "active";
elif pidof sing-box >/dev/null 2>&1 || pgrep -f "sing-box" >/dev/null 2>&1; then echo "active";
elif [ -f /run/sing-box.pid ] && kill -0 "$(cat /run/sing-box.pid 2>/dev/null)" 2>/dev/null; then echo "active";
else rc-service sing-box status 2>/dev/null || systemctl is-active sing-box 2>/dev/null || echo "inactive"; fi
'''
                        _, out = runner.run_cmd(cmd, timeout=15)
                        status_ok = any(w in out.lower() for w in ("active", "started", "running")) or any(s.strip().isdigit() for s in out.splitlines() if s.strip())
                        append_task_log(task_id, f"[{name}] 重启完毕: {'🟢 服务运行中' if status_ok else '🟡 状态待确认'}")
                        success_count += 1
                    elif action == "update_core":
                        cmd = r'''
ARCH="$(uname -m)"
case "$ARCH" in x86_64) A="amd64" ;; aarch64|arm64) A="arm64" ;; armv7*) A="armv7" ;; 386|i686) A="386" ;; *) A="amd64" ;; esac
LX_TAG="$(curl -s4m6 https://api.github.com/repos/Leadaxe/sing-box-lx/releases/latest 2>/dev/null | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/' || echo "")"
[ -z "$LX_TAG" ] && LX_TAG="v1.14.3-lx.22"

TMP_DIR="$(mktemp -d /tmp/sb_lx_XXXXXX 2>/dev/null || mktemp -d)"
LX_FILE="sing-box-${LX_TAG#v}-linux-${A}.tar.gz"
LX_URL="https://github.com/Leadaxe/sing-box-lx/releases/download/${LX_TAG}/${LX_FILE}"

curl -fsSL --connect-timeout 8 -m 60 "https://hub.gitmirror.com/$LX_URL" -o "$TMP_DIR/sb.tar.gz" 2>/dev/null || \
curl -fsSL --connect-timeout 8 -m 60 "https://ghproxy.net/$LX_URL" -o "$TMP_DIR/sb.tar.gz" 2>/dev/null || \
curl -fsSL --connect-timeout 8 -m 60 "$LX_URL" -o "$TMP_DIR/sb.tar.gz" 2>/dev/null || \
wget -q -T 30 -O "$TMP_DIR/sb.tar.gz" "$LX_URL" || true

if [ -s "$TMP_DIR/sb.tar.gz" ]; then
  tar -xzf "$TMP_DIR/sb.tar.gz" -C "$TMP_DIR"
  SB_BIN="$(find "$TMP_DIR" -type f -name "sing-box" | head -n1)"
  if [ -n "$SB_BIN" ] && ([ -x "$SB_BIN" ] || chmod +x "$SB_BIN" 2>/dev/null); then
    cp -f "$SB_BIN" /usr/local/bin/sing-box
    chmod +x /usr/local/bin/sing-box
    command -v setcap >/dev/null 2>&1 && setcap 'cap_net_bind_service,cap_net_admin=+ep' /usr/local/bin/sing-box 2>/dev/null || true
    systemctl restart sing-box 2>/dev/null || rc-service sing-box restart 2>/dev/null || true
    VER="$(/usr/local/bin/sing-box version 2>/dev/null | head -n1)"
    echo "SUCCESS: $VER (支持AWG混淆特性)"
  else
    echo "FAIL: 解压未找到 sing-box 二进制文件"
  fi
  rm -rf "$TMP_DIR"
else
  rm -rf "$TMP_DIR"
  echo "FAIL: 下载 Sing-box-lx ($LX_TAG) 核心失败"
fi
'''
                        _, out = runner.run_cmd(cmd, timeout=90)
                        append_task_log(task_id, f"[{name}] Sing-box核心(含AWG): {out.strip()[:100]}")
                        success_count += 1
                    elif action == "update_cloudflared":
                        cmd = r'''
ARCH="$(uname -m)"
case "$ARCH" in x86_64) A="amd64" ;; aarch64|arm64) A="arm64" ;; armv7*) A="arm" ;; 386|i686) A="386" ;; *) A="amd64" ;; esac
CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${A}"
TMP_CF="/tmp/cf_${A}_$(date +%s)"

curl -fsSL --connect-timeout 8 -m 60 "https://ghproxy.net/$CF_URL" -o "$TMP_CF" 2>/dev/null || \
curl -fsSL --connect-timeout 8 -m 60 "https://hub.gitmirror.com/$CF_URL" -o "$TMP_CF" 2>/dev/null || \
curl -fsSL --connect-timeout 8 -m 60 "$CF_URL" -o "$TMP_CF" 2>/dev/null || \
wget -q -T 30 -O "$TMP_CF" "$CF_URL" || true

if [ -s "$TMP_CF" ]; then
  chmod +x "$TMP_CF"
  if "$TMP_CF" --version >/dev/null 2>&1; then
    mv -f "$TMP_CF" /usr/local/bin/cloudflared
    chmod +x /usr/local/bin/cloudflared
    systemctl restart cloudflared 2>/dev/null || rc-service cloudflared restart 2>/dev/null || true
    VER="$(/usr/local/bin/cloudflared --version 2>/dev/null | head -n1)"
    echo "SUCCESS: $VER"
  else
    rm -f "$TMP_CF"
    echo "FAIL: cloudflared 二进制校验失败"
  fi
else
  rm -f "$TMP_CF"
  echo "FAIL: 下载 cloudflared 失败"
fi
'''
                        _, out = runner.run_cmd(cmd, timeout=90)
                        append_task_log(task_id, f"[{name}] Cloudflare隧道核心: {out.strip()[:100]}")
                        success_count += 1
                    elif action == "update_xray":
                        cmd = r'''
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) XARCH="64" ;;
  aarch64|arm64) XARCH="arm64-v8a" ;;
  armv7*) XARCH="arm32-v7a" ;;
  386|i686) XARCH="32" ;;
  *) XARCH="64" ;;
esac
TMP_DIR="$(mktemp -d /tmp/xray_XXXXXX 2>/dev/null || mktemp -d)"
XTAG="$(curl -s4m6 https://api.github.com/repos/XTLS/Xray-core/releases/latest 2>/dev/null | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/' || echo "")"
[ -z "$XTAG" ] && XTAG="v25.1.30"
XURL="https://github.com/XTLS/Xray-core/releases/download/${XTAG}/Xray-linux-${XARCH}.zip"

curl -fsSL --connect-timeout 8 -m 60 "https://ghproxy.net/$XURL" -o "$TMP_DIR/xray.zip" 2>/dev/null || \
curl -fsSL --connect-timeout 8 -m 60 "https://hub.gitmirror.com/$XURL" -o "$TMP_DIR/xray.zip" 2>/dev/null || \
curl -fsSL --connect-timeout 8 -m 60 "$XURL" -o "$TMP_DIR/xray.zip" 2>/dev/null || \
wget -q -T 30 -O "$TMP_DIR/xray.zip" "$XURL" || true

if [ -s "$TMP_DIR/xray.zip" ]; then
  unzip -q -o "$TMP_DIR/xray.zip" -d "$TMP_DIR" 2>/dev/null || busybox unzip -q -o "$TMP_DIR/xray.zip" -d "$TMP_DIR" 2>/dev/null || true
  if [ -f "$TMP_DIR/xray" ]; then
    chmod +x "$TMP_DIR/xray"
    cp -f "$TMP_DIR/xray" /usr/local/bin/xray-argo
    chmod +x /usr/local/bin/xray-argo
    systemctl restart xray-argo 2>/dev/null || rc-service xray-argo restart 2>/dev/null || true
    VER="$(/usr/local/bin/xray-argo version 2>/dev/null | head -n1)"
    echo "SUCCESS: $VER"
  else
    echo "FAIL: 解压后未找到 xray 可执行文件"
  fi
  rm -rf "$TMP_DIR"
else
  rm -rf "$TMP_DIR"
  echo "FAIL: 下载 Xray-core 失败"
fi
'''
                        _, out = runner.run_cmd(cmd, timeout=90)
                        append_task_log(task_id, f"[{name}] Xray核心更新: {out.strip()[:100]}")
                        success_count += 1
                    elif action == "push_nodes":
                        cmd = r'''
if [ -x /usr/local/bin/sb ]; then
  /usr/local/bin/sb push 2>&1
elif [ -f /usr/local/bin/sb ]; then
  bash /usr/local/bin/sb push 2>&1
else
  echo "FAIL: 未找到 /usr/local/bin/sb 管理脚本"
fi
'''
                        _, out = runner.run_cmd(cmd, timeout=40)
                        append_task_log(task_id, f"[{name}] 同步节点: {out.strip()[:120]}")
                        success_count += 1
                    elif action == "update_script":
                        cmd = r'''
URL="https://raw.githubusercontent.com/kzhx666/sb-install-plus/main/install.sh"
curl -fsSL --connect-timeout 8 -m 30 "https://ghproxy.net/$URL" -o /usr/local/bin/sb 2>/dev/null || \
curl -fsSL --connect-timeout 8 -m 30 "https://hub.gitmirror.com/$URL" -o /usr/local/bin/sb 2>/dev/null || \
curl -fsSL --connect-timeout 8 -m 30 "$URL" -o /usr/local/bin/sb 2>/dev/null || \
wget -q -T 20 -O /usr/local/bin/sb "$URL" || true
if [ -s /usr/local/bin/sb ] && grep -q "Sing-box" /usr/local/bin/sb 2>/dev/null; then
  chmod +x /usr/local/bin/sb
  echo "SUCCESS: /usr/local/bin/sb 脚本已更新至 GitHub 最新版"
else
  echo "FAIL: 获取最新安装脚本失败"
fi
'''
                        _, out = runner.run_cmd(cmd, timeout=30)
                        append_task_log(task_id, f"[{name}] 运维脚本更新: {out.strip()[:100]}")
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

systemctl stop xray-argo 2>/dev/null || true
systemctl disable xray-argo 2>/dev/null || true
rc-service xray-argo stop 2>/dev/null || true
rc-update del xray-argo default 2>/dev/null || true

systemctl stop sb-hop.service 2>/dev/null || true
systemctl disable sb-hop.service 2>/dev/null || true

pkill -9 -f sing-box 2>/dev/null || true
pkill -9 -f cloudflared 2>/dev/null || true
pkill -9 -f xray-argo 2>/dev/null || true

# 2. 清理系统服务单元文件
rm -f /etc/systemd/system/sing-box.service* 2>/dev/null || true
rm -rf /etc/systemd/system/sing-box.service.d 2>/dev/null || true
rm -f /etc/systemd/system/cloudflared.service* 2>/dev/null || true
rm -rf /etc/systemd/system/cloudflared.service.d 2>/dev/null || true
rm -f /etc/systemd/system/xray-argo.service* 2>/dev/null || true
rm -f /etc/systemd/system/sb-hop.service* 2>/dev/null || true
rm -f /etc/init.d/sing-box /etc/init.d/cloudflared /etc/init.d/xray-argo 2>/dev/null || true

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload 2>/dev/null || true
  systemctl reset-failed 2>/dev/null || true
fi

# 3. 删除二进制可执行文件与快捷脚本
rm -f /usr/local/bin/sing-box /usr/bin/sing-box 2>/dev/null || true
rm -f /usr/local/bin/sb /usr/bin/sb 2>/dev/null || true
rm -f /usr/local/bin/sb-hop.sh /usr/local/bin/sb-selfcheck.sh 2>/dev/null || true
rm -f /usr/local/bin/cloudflared /usr/bin/cloudflared 2>/dev/null || true
rm -f /usr/local/bin/xray-argo /usr/local/bin/xray 2>/dev/null || true

# 4. 删除配置文件、状态文件、证书、日志与临时缓存
rm -rf /etc/sing-box 2>/dev/null || true
rm -rf /etc/cloudflared 2>/dev/null || true
rm -rf /var/log/sing-box* 2>/dev/null || true
rm -rf /tmp/sb* /tmp/sing-box* /tmp/cloudflared* /tmp/xray* 2>/dev/null || true

# 5. 清理 Crontab 计划任务残留
if command -v crontab >/dev/null 2>&1; then
  crontab -l 2>/dev/null | grep -v 'sing-box' | grep -v 'sb-' | grep -v 'cloudflared' | grep -v 'xray-argo' | crontab - 2>/dev/null || true
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

                        # 同步通知节点管理平台注销并清理已推送的订阅源
                        cur_cfg = load_config()
                        push_target = cur_cfg.get("global", {}).get("push_target", {})
                        p_url = (push_target.get("url") or "").strip().rstrip('/')
                        p_tok = (push_target.get("token") or "").strip()
                        if p_url and REQUESTS_AVAILABLE:
                            try:
                                tags_to_clean = [f"{name}本机", f"{name}优选", f"{name}"]
                                headers = {"Authorization": f"Bearer {p_tok}"} if p_tok else {}
                                requests.post(
                                    f"{p_url}/api/delete_pushed_nodes",
                                    json={"tags": tags_to_clean, "token": p_tok},
                                    headers=headers,
                                    timeout=5
                                )
                                append_task_log(task_id, f"[{name}] 已向节点管理平台同步清理订阅源: {', '.join(tags_to_clean)}")
                            except Exception as ex_del:
                                append_task_log(task_id, f"[{name}] 提示: 向节点管理平台请求清理订阅源 (可忽略): {ex_del}")

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
# Routes: Reorder Servers & Custom Shell Command Execution
# ------------------------------------------------------------------------------
@app.route('/api/servers/reorder', methods=['POST'])
def reorder_servers():
    """Update order of servers in config.json to match user-defined drag-and-drop order."""
    data = request.get_json() or {}
    new_ids = data.get("server_ids", [])
    if not isinstance(new_ids, list):
        return jsonify({"success": False, "message": "参数格式错误"}), 400

    with config_lock:
        cfg = load_config()
        id_map = {str(s.get("id")): s for s in cfg.get("servers", [])}
        reordered = []
        for sid in new_ids:
            sid_str = str(sid)
            if sid_str in id_map:
                reordered.append(id_map.pop(sid_str))
        # Keep any remaining servers not explicitly in new_ids at the end
        for remaining in id_map.values():
            reordered.append(remaining)

        cfg["servers"] = reordered
        save_config_unlocked(cfg)

    return jsonify({"success": True, "message": "VPS 排序已更新保存"})

@app.route('/api/server/exec_cmd', methods=['POST'])
def exec_custom_cmd():
    """Execute arbitrary shell command on specified or all VPSs concurrently and return structured outputs."""
    data = request.get_json() or {}
    cmd = (data.get("cmd") or "").strip()
    server_ids = data.get("server_ids")
    timeout = int(data.get("timeout") or 30)

    if not cmd:
        return jsonify({"success": False, "message": "执行命令不能为空"}), 400

    cfg = load_config()
    servers = cfg.get("servers", [])
    if server_ids and len(server_ids) > 0:
        str_sids = [str(x) for x in server_ids]
        targets = [s for s in servers if str(s.get("id")) in str_sids]
    else:
        targets = servers

    if not targets:
        return jsonify({"success": False, "message": "未找到任何目标 VPS"}), 400

    from concurrent.futures import ThreadPoolExecutor

    def run_single(srv):
        s_id = srv.get("id")
        s_name = srv.get("name") or srv.get("host")
        s_host = srv.get("host")
        s_port = srv.get("port", 22)
        user = srv.get("user", "root")
        pwd = srv.get("password")
        key = srv.get("private_key")

        runner = SSHRunner(s_host, s_port, user, pwd, key, timeout=10)
        t_start = time.time()
        try:
            runner.connect()
            code, out = runner.run_cmd(cmd, timeout=timeout)
            duration = round((time.time() - t_start) * 1000)
            return {
                "id": s_id,
                "name": s_name,
                "host": s_host,
                "port": s_port,
                "success": (code == 0),
                "code": code,
                "output": strip_ansi(out),
                "duration_ms": duration,
                "error": ""
            }
        except Exception as ex:
            duration = round((time.time() - t_start) * 1000)
            return {
                "id": s_id,
                "name": s_name,
                "host": s_host,
                "port": s_port,
                "success": False,
                "code": -1,
                "output": "",
                "duration_ms": duration,
                "error": str(ex)
            }
        finally:
            runner.close()

    max_w = min(16, max(1, len(targets)))
    with ThreadPoolExecutor(max_workers=max_w) as executor:
        results = list(executor.map(run_single, targets))

    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count

    return jsonify({
        "success": True,
        "cmd": cmd,
        "total": len(targets),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results
    })



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
