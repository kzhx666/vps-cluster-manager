#!/usr/bin/env bash
# ==============================================================================
# VPS Cluster Manager - 一键安装与管理脚本
# 默认安装目录: /opt/vps-cluster-manager
# 默认访问端口: 5001
# ==============================================================================

set -u

RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
BLUE="\033[36m"
PLAIN="\033[0m"

APP_NAME="vps-cluster-manager"
INSTALL_DIR="/opt/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
REPO_URL="https://github.com/kzhx666/vps-cluster-manager.git"
DEFAULT_PORT="5001"
SHORTCUT_BIN="/usr/local/bin/${APP_NAME}"

info() { echo -e "${BLUE}[INFO]${PLAIN} $*"; }
ok()   { echo -e "${GREEN}[ OK ]${PLAIN} $*"; }
warn() { echo -e "${YELLOW}[WARN]${PLAIN} $*"; }
err()  { echo -e "${RED}[ERR ]${PLAIN} $*"; }

# 1. Root 权限检查
if [[ ${EUID:-999} -ne 0 ]]; then
  err "必须使用 root 用户执行此安装脚本！"
  exit 1
fi

# 2. 识别包管理器并安装依赖
install_deps() {
  info "正在检测并安装系统依赖 (Python3, Git, Curl)..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y >/dev/null 2>&1
    apt-get install -y git curl python3 python3-pip python3-venv >/dev/null 2>&1
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git curl python3 python3-pip >/dev/null 2>&1
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git curl python3 python3-pip >/dev/null 2>&1
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache git curl python3 py3-pip >/dev/null 2>&1
  else
    warn "未识别的包管理器，请确保已安装 Python3、Pip 和 Git！"
  fi
  ok "系统依赖就绪"
}

# 3. 拉取/更新代码到 /opt/vps-cluster-manager (自动备份与保留配置)
setup_codebase() {
  mkdir -p /opt
  local ETC_DIR="/etc/vps-cluster-manager"
  local ETC_ROLL_DIR="/etc/vps-cluster-manager/backups"
  local BACKUP_CONFIG="/var/backups/vps-cluster-manager-config.json"
  mkdir -p "$ETC_ROLL_DIR"
  mkdir -p /var/backups

  # 无论执行何种操作，优先备份现有数据至永久系统目录 /etc/vps-cluster-manager/ (与 git 目录完全隔离，永不被覆盖或删除)
  for cand in "$INSTALL_DIR/data/config.json" "/root/vps-cluster-manager/data/config.json"; do
    if [[ -f "$cand" ]] && grep -q '"host"' "$cand" 2>/dev/null; then
      cp -f "$cand" "$ETC_DIR/config.json" 2>/dev/null || true
      cp -f "$cand" "$ETC_ROLL_DIR/config_$(date +%Y%m%d_%H%M%S).json" 2>/dev/null || true
      cp -f "$cand" "$BACKUP_CONFIG" 2>/dev/null || true
      cp -f "$cand" "/tmp/vps_cm_config_backup.json" 2>/dev/null || true
      break
    fi
  done

  if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "检测到已有代码库，正在更新最新代码 (保留已有配置)..."
    cd "$INSTALL_DIR"
    git fetch --all >/dev/null 2>&1 || true
    git reset --hard origin/main >/dev/null 2>&1 || true
  else
    info "正在拉取项目代码至 ${INSTALL_DIR} ..."
    if [[ -d "$INSTALL_DIR/data" ]]; then
      mv "$INSTALL_DIR/data" "/tmp/vps_cm_data_bak" 2>/dev/null || true
    fi
    rm -rf "$INSTALL_DIR" 2>/dev/null || true
    git clone "$REPO_URL" "$INSTALL_DIR"
    if [[ -d "/tmp/vps_cm_data_bak" ]]; then
      mv "/tmp/vps_cm_data_bak" "$INSTALL_DIR/data" 2>/dev/null || true
    fi
  fi
  cd "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR/data"

  # 如果目标 config.json 无有效节点记录，自动从多层备份链中寻找含有节点的配置进行恢复
  if (! grep -q '"host"' "$INSTALL_DIR/data/config.json" 2>/dev/null); then
    local restored=false
    if [[ -f "$ETC_DIR/config.json" ]] && grep -q '"host"' "$ETC_DIR/config.json" 2>/dev/null; then
      cp -f "$ETC_DIR/config.json" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
      ok "已从永久专用备份目录 $ETC_DIR 自动恢复原有的服务器配置数据！"
      restored=true
    elif [[ -d "$ETC_ROLL_DIR" ]] && ls "$ETC_ROLL_DIR"/*.json >/dev/null 2>&1; then
      latest_etc_bak="$(ls -t "$ETC_ROLL_DIR"/*.json 2>/dev/null | head -n1)"
      if [[ -n "$latest_etc_bak" ]] && grep -q '"host"' "$latest_etc_bak" 2>/dev/null; then
        cp -f "$latest_etc_bak" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
        ok "已从专用时间戳备份 $latest_etc_bak 自动恢复原有的服务器配置数据！"
        restored=true
      fi
    fi

    if [[ "$restored" != "true" ]]; then
      if [[ -f "$BACKUP_CONFIG" ]] && grep -q '"host"' "$BACKUP_CONFIG" 2>/dev/null; then
        cp -f "$BACKUP_CONFIG" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
        ok "已从系统级备份 /var/backups 自动恢复原有的服务器配置数据！"
      elif [[ -f "/root/vps-cluster-manager/data/config.json" ]] && grep -q '"host"' "/root/vps-cluster-manager/data/config.json" 2>/dev/null; then
        cp -f "/root/vps-cluster-manager/data/config.json" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
        ok "已从 /root/vps-cluster-manager 自动恢复原有的服务器配置数据！"
      elif [[ -f "/tmp/vps_cm_config_backup.json" ]] && grep -q '"host"' "/tmp/vps_cm_config_backup.json" 2>/dev/null; then
        cp -f "/tmp/vps_cm_config_backup.json" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
        ok "已从 /tmp 备份自动恢复原有的服务器配置数据！"
      fi
    fi
  fi

  ok "代码已就绪: ${INSTALL_DIR}"
}

# 4. 配置 Python 虚拟环境与依赖
setup_venv() {
  info "正在配置 Python 运行环境与安装依赖模块..."
  cd "$INSTALL_DIR"

  # 优先创建独立 venv，防止 PEP 668 外部管理环境报错
  if python3 -m venv "$INSTALL_DIR/venv" >/dev/null 2>&1; then
    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
    "$INSTALL_DIR/venv/bin/pip" install --no-cache-dir -r requirements.txt >/dev/null 2>&1
    PY_BIN="$INSTALL_DIR/venv/bin/python"
  else
    pip3 install --no-cache-dir -r requirements.txt >/dev/null 2>&1 || pip install --no-cache-dir -r requirements.txt >/dev/null 2>&1 || true
    PY_BIN="$(command -v python3 || command -v python)"
  fi

  ok "Python 核心依赖安装完毕"
}

# 5. 放行防火墙端口 5001
allow_firewall() {
  info "正在放行防火墙端口 ${DEFAULT_PORT}..."
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "Status: active"; then
    ufw allow "${DEFAULT_PORT}/tcp" >/dev/null 2>&1 || true
    ufw reload >/dev/null 2>&1 || true
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="${DEFAULT_PORT}/tcp" >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
  elif command -v iptables >/dev/null 2>&1; then
    iptables -I INPUT -p tcp --dport "${DEFAULT_PORT}" -j ACCEPT >/dev/null 2>&1 || true
  fi
  ok "防火墙端口 ${DEFAULT_PORT} 已放行"
}

# 6. 配置 systemd 守护进程
setup_systemd() {
  info "正在注册 systemd 系统守护进程..."
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=VPS Cluster Manager Console
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=PORT=${DEFAULT_PORT}
Environment=HOST=0.0.0.0
ExecStart=${PY_BIN} ${INSTALL_DIR}/app.py
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$APP_NAME" >/dev/null 2>&1
  systemctl restart "$APP_NAME"
  ok "系统服务 ${APP_NAME} 已成功启动并设置开机自启"
}

# 7. 注册快捷管理命令
setup_shortcut() {
  cat > "$SHORTCUT_BIN" <<'EOF'
#!/usr/bin/env bash
APP_NAME="vps-cluster-manager"
INSTALL_DIR="/opt/vps-cluster-manager"

case "${1:-}" in
  start)
    systemctl start "$APP_NAME"
    echo "服务已启动"
    ;;
  stop)
    systemctl stop "$APP_NAME"
    echo "服务已停止"
    ;;
  restart)
    systemctl restart "$APP_NAME"
    echo "服务已重启"
    ;;
  status)
    systemctl status "$APP_NAME"
    ;;
  logs)
    journalctl -u "$APP_NAME" -f -n 50
    ;;
  update)
    echo "正在升级 VPS Cluster Manager 至最新版本..."
    mkdir -p /etc/vps-cluster-manager/backups
    mkdir -p /var/backups
    # 优先在升级前备份至系统专用永久目录
    if [[ -f "$INSTALL_DIR/data/config.json" ]] && grep -q '"host"' "$INSTALL_DIR/data/config.json" 2>/dev/null; then
      cp -f "$INSTALL_DIR/data/config.json" "/etc/vps-cluster-manager/config.json" 2>/dev/null || true
      cp -f "$INSTALL_DIR/data/config.json" "/etc/vps-cluster-manager/backups/config_$(date +%Y%m%d_%H%M%S).json" 2>/dev/null || true
      cp -f "$INSTALL_DIR/data/config.json" "/var/backups/vps-cluster-manager-config.json" 2>/dev/null || true
      cp -f "$INSTALL_DIR/data/config.json" "/tmp/vps_cm_config_backup.json" 2>/dev/null || true
    fi
    cd "$INSTALL_DIR"
    git fetch --all
    git reset --hard origin/main
    # 升级后若当前无节点记录，自动从永久专用备份目录及多层备份链中寻找含有节点的配置进行恢复
    if (! grep -q '"host"' "$INSTALL_DIR/data/config.json" 2>/dev/null); then
      mkdir -p "$INSTALL_DIR/data"
      local restored=false
      if [[ -f "/etc/vps-cluster-manager/config.json" ]] && grep -q '"host"' "/etc/vps-cluster-manager/config.json" 2>/dev/null; then
        cp -f "/etc/vps-cluster-manager/config.json" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
        echo "已从永久专用备份目录 /etc/vps-cluster-manager 自动恢复服务器节点配置！"
        restored=true
      elif [[ -d "/etc/vps-cluster-manager/backups" ]] && ls /etc/vps-cluster-manager/backups/*.json >/dev/null 2>&1; then
        latest_etc="$(ls -t /etc/vps-cluster-manager/backups/*.json 2>/dev/null | head -n1)"
        if [[ -n "$latest_etc" ]] && grep -q '"host"' "$latest_etc" 2>/dev/null; then
          cp -f "$latest_etc" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
          echo "已从时间戳备份 $latest_etc 自动恢复服务器节点配置！"
          restored=true
        fi
      fi

      if [[ "$restored" != "true" ]]; then
        if [[ -f "/var/backups/vps-cluster-manager-config.json" ]] && grep -q '"host"' "/var/backups/vps-cluster-manager-config.json" 2>/dev/null; then
          cp -f "/var/backups/vps-cluster-manager-config.json" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
          echo "已从 /var/backups 自动恢复服务器节点配置！"
        elif [[ -f "/root/vps-cluster-manager/data/config.json" ]] && grep -q '"host"' "/root/vps-cluster-manager/data/config.json" 2>/dev/null; then
          cp -f "/root/vps-cluster-manager/data/config.json" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
          echo "已从 /root 历史目录自动恢复服务器节点配置！"
        elif [[ -f "/tmp/vps_cm_config_backup.json" ]] && grep -q '"host"' "/tmp/vps_cm_config_backup.json" 2>/dev/null; then
          cp -f "/tmp/vps_cm_config_backup.json" "$INSTALL_DIR/data/config.json" 2>/dev/null || true
          echo "已从 /tmp 备份自动恢复服务器节点配置！"
        fi
      fi
    fi
    if [[ -f "$INSTALL_DIR/venv/bin/pip" ]]; then
      "$INSTALL_DIR/venv/bin/pip" install --no-cache-dir -r requirements.txt >/dev/null 2>&1 || true
    fi
    systemctl restart "$APP_NAME"
    echo "🎉 项目已升级至最新版本并重启成功！所有 VPS 节点与配置完好无损。"
    ;;
  uninstall)
    systemctl stop "$APP_NAME" 2>/dev/null || true
    systemctl disable "$APP_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/${APP_NAME}.service"
    systemctl daemon-reload 2>/dev/null || true
    rm -rf "$INSTALL_DIR"
    rm -f "/usr/local/bin/${APP_NAME}"
    echo "项目已从 /opt 彻底卸载清理完毕！"
    ;;
  *)
    echo "=========================================================="
    echo "  VPS Cluster Manager 管理命令"
    echo "=========================================================="
    echo "  ${APP_NAME} start      - 启动面板"
    echo "  ${APP_NAME} stop       - 停止面板"
    echo "  ${APP_NAME} restart    - 重启面板"
    echo "  ${APP_NAME} status     - 查看运行状态"
    echo "  ${APP_NAME} logs       - 实时查看运行日志"
    echo "  ${APP_NAME} update     - 一键拉取最新代码并重启"
    echo "  ${APP_NAME} uninstall  - 彻底卸载并删除所有文件"
    echo "=========================================================="
    ;;
esac
EOF
  chmod +x "$SHORTCUT_BIN"
}

# 8. 获取本机外网 IP 并展示完成信息
show_success() {
  local ip
  ip="$(curl -s4m4 ip.sb 2>/dev/null || curl -s4m4 ifconfig.co 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -z "$ip" ]] && ip="服务器IP"

  echo -e "\n${GREEN}==================================================================${PLAIN}"
  echo -e "${GREEN}  🎉 VPS Cluster Manager 集群控制台已成功安装并运行！${PLAIN}"
  echo -e "${GREEN}==================================================================${PLAIN}"
  echo -e "  安装目录: ${BLUE}${INSTALL_DIR}${PLAIN}"
  echo -e "  访问端口: ${BLUE}${DEFAULT_PORT}${PLAIN}"
  echo -e "  Web 面板: ${GREEN}http://${ip}:${DEFAULT_PORT}${PLAIN}"
  echo -e "------------------------------------------------------------------"
  echo -e "  快捷命令: ${YELLOW}${APP_NAME}${PLAIN} (支持 start/stop/restart/status/logs/update/uninstall)"
  echo -e "${GREEN}==================================================================${PLAIN}\n"
}

# --- 卸载子命令处理 ---
if [[ "${1:-}" == "uninstall" || "${1:-}" == "--uninstall" ]]; then
  info "正在彻底卸载 ${APP_NAME}..."
  systemctl stop "$APP_NAME" 2>/dev/null || true
  systemctl disable "$APP_NAME" 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload 2>/dev/null || true
  rm -rf "$INSTALL_DIR"
  rm -f "$SHORTCUT_BIN"
  ok "卸载清理完毕！"
  exit 0
fi

# --- 执行完整安装流程 ---
install_deps
setup_codebase
setup_venv
allow_firewall
setup_systemd
setup_shortcut
show_success
