// ==============================================================================
// VPS CLUSTER MANAGER - CLIENT-SIDE APPLICATION SCRIPT
// ==============================================================================

const state = {
  config: { global: {}, servers: [] },
  filter: 'all',
  search: '',
  activeTaskId: null,
  pollTimer: null,
  activeServerId: null
};

// ------------------------------------------------------------------------------
// Utility: Toast Notifications
// ------------------------------------------------------------------------------
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span>${message}</span>
  `;
  container.appendChild(toast);

  // Trigger animation
  requestAnimationFrame(() => toast.classList.add('show'));

  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// ------------------------------------------------------------------------------
// Modal Controls
// ------------------------------------------------------------------------------
function openModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    modal.style.display = 'flex';
    requestAnimationFrame(() => modal.classList.add('open'));
  }
}

function closeModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    modal.classList.remove('open');
    setTimeout(() => {
      modal.style.display = 'none';
      if (modalId === 'modal-terminal') {
        stopTerminalPolling();
      }
    }, 200);
  }
}

// ------------------------------------------------------------------------------
// Data Fetch & Synchronization
// ------------------------------------------------------------------------------
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    state.config = data;
    updateStats();
    renderServers();
  } catch (err) {
    showToast(`加载集群配置失败: ${err.message}`, 'error');
  }
}

function updateStats() {
  const servers = state.config.servers || [];
  const total = servers.length;
  const online = servers.filter(s => s.status === 'online').length;
  const idle = servers.filter(s => s.status === 'idle').length;
  const error = servers.filter(s => s.status === 'error' || s.status === 'warning').length;

  document.getElementById('stat-total').textContent = total;
  document.getElementById('stat-online').textContent = online;
  document.getElementById('stat-idle').textContent = idle;
  document.getElementById('stat-error').textContent = error;

  document.getElementById('count-all').textContent = total;
  document.getElementById('count-online').textContent = online;
  document.getElementById('count-idle').textContent = idle;
  document.getElementById('count-error').textContent = error;
}

// ------------------------------------------------------------------------------
// Server List Rendering
// ------------------------------------------------------------------------------
function renderServers() {
  const grid = document.getElementById('server-grid');
  const emptyState = document.getElementById('empty-state');
  if (!grid) return;

  grid.innerHTML = '';
  const servers = state.config.servers || [];

  const filtered = servers.filter(s => {
    // Status filter
    if (state.filter === 'online' && s.status !== 'online') return false;
    if (state.filter === 'idle' && s.status !== 'idle') return false;
    if (state.filter === 'error' && (s.status !== 'error' && s.status !== 'warning')) return false;

    // Search query
    if (state.search) {
      const q = state.search.toLowerCase();
      const matchName = (s.name || '').toLowerCase().includes(q);
      const matchHost = (s.host || '').toLowerCase().includes(q);
      const matchTag = (s.tag_main || '').toLowerCase().includes(q);
      const matchDomain = (s.domain || '').toLowerCase().includes(q);
      return matchName || matchHost || matchTag || matchDomain;
    }
    return true;
  });

  if (servers.length === 0) {
    emptyState.style.display = 'flex';
    grid.style.display = 'none';
    return;
  } else {
    emptyState.style.display = 'none';
    grid.style.display = 'grid';
  }

  if (filtered.length === 0) {
    grid.innerHTML = `
      <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-dim);">
        🔍 未找到符合条件的 VPS 主机
      </div>
    `;
    return;
  }

  filtered.forEach(s => {
    const card = document.createElement('div');
    card.className = 'server-card';

    // Status Pill
    let statusClass = 'idle';
    let statusText = '⚪ 待部署';
    if (s.status === 'online') {
      statusClass = 'online';
      statusText = '🟢 在线运行';
    } else if (s.status === 'warning') {
      statusClass = 'warning';
      statusText = '🟡 进程异常';
    } else if (s.status === 'error') {
      statusClass = 'error';
      statusText = '🔴 连接异常';
    }

    // NAT tag
    let portTagHtml = '';
    if (s.port_mode === 'nat' && s.port_range) {
      portTagHtml = `<span class="pill-tag tag-nat">NAT [${s.port_range}]</span>`;
    } else {
      portTagHtml = `<span class="pill-tag">标准全端口</span>`;
    }

    // Domain tag
    let domainTagHtml = s.domain 
      ? `<span class="pill-tag tag-domain">TLS: ${s.domain}</span>` 
      : `<span class="pill-tag">ECC 自签伪装</span>`;

    // Argo tag
    let argoTagHtml = s.argo_enabled && s.argo_domain 
      ? `<span class="pill-tag tag-argo">Argo: ${s.argo_domain}</span>` 
      : '';

    // BBR badge
    let bbrBadgeHtml = '';
    const bbrVal = s.bbr_info || (s.last_check_result && s.last_check_result.bbr) || '';
    if (bbrVal && bbrVal !== 'unknown+unknown') {
      const isBbr3 = bbrVal.includes('bbr3');
      const isBbr = bbrVal.includes('bbr');
      const bColor = isBbr3 ? '#67e8f9' : (isBbr ? '#6ee7b7' : 'var(--text-dim)');
      const bBg = isBbr3 ? 'rgba(6,182,212,0.15)' : (isBbr ? 'rgba(16,185,129,0.15)' : 'rgba(255,255,255,0.05)');
      bbrBadgeHtml = `<span class="pill-tag" style="background:${bBg}; border:1px solid ${bColor}; color:${bColor};">⚡ ${escapeHtml(bbrVal)}</span>`;
    }

    // Latency badge
    const latencyHtml = s.latency_ms ? `<span class="badge badge-info">${s.latency_ms}ms</span>` : '';
    const osHtml = s.os_info ? `<span class="badge badge-info">${s.os_info}</span>` : '';

    // Unlock summary
    let unlockHtml = '';
    if (s.last_check_result && s.last_check_result.unlock) {
      const u = s.last_check_result.unlock;
      unlockHtml = `
        <div style="display:flex; gap:6px; margin-top:4px;">
          <span class="badge badge-${u.openai.includes('解锁') || u.openai.includes('原生') ? 'success' : 'danger'}">OpenAI</span>
          <span class="badge badge-${u.youtube.includes('解锁') ? 'success' : 'danger'}">YouTube</span>
          <span class="badge badge-${u.netflix.includes('解锁') ? 'success' : 'warning'}">Netflix</span>
        </div>
      `;
    }

    card.innerHTML = `
      <div class="server-card-header">
        <div class="server-title-group">
          <div class="server-name">
            ${escapeHtml(s.name || s.host)}
            ${osHtml}
          </div>
          <div class="server-endpoint" title="点击复制 IP 和端口" onclick="copyToClipboard('${s.host}:${s.port}')">
            <span>${s.host}:${s.port}</span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
          </div>
        </div>
        <div style="display:flex; flex-direction:column; align-items:flex-end; gap:4px;">
          <span class="status-badge ${statusClass}">
            <span class="status-dot"></span>
            ${statusText}
          </span>
          ${latencyHtml}
        </div>
      </div>

      <div class="server-card-body">
        <div class="detail-row">
          <span class="detail-label">订阅源标签:</span>
          <span class="detail-value text-cyan">${escapeHtml(s.tag_main || s.name)}</span>
        </div>
        <div class="detail-row">
          <span class="detail-label">端口与加速:</span>
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            ${portTagHtml}
            ${domainTagHtml}
            ${bbrBadgeHtml}
          </div>
        </div>
        ${argoTagHtml ? `
        <div class="detail-row">
          <span class="detail-label">隧道穿透:</span>
          <div>${argoTagHtml}</div>
        </div>` : ''}
        <div class="detail-row">
          <span class="detail-label">最近部署:</span>
          <span class="detail-value">${s.last_deployed || '未记录'}</span>
        </div>
        ${unlockHtml ? `
        <div class="detail-row" style="border:none; padding-bottom:0;">
          <span class="detail-label">流媒体解锁:</span>
          ${unlockHtml}
        </div>` : ''}
      </div>

      <div class="server-card-actions">
        <button class="btn btn-sm btn-primary" onclick="deploySingleServer('${s.id}')">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
          部署
        </button>
        <button class="btn btn-sm btn-secondary" onclick="checkEnvServer('${s.id}')" title="测试 SSH 连通性与底层系统信息">
          🔍 测延迟
        </button>
        <button class="btn btn-sm btn-secondary" onclick="inspectSingleServer('${s.id}')" title="健康巡检">
          🩺 巡检
        </button>
        <button class="btn btn-sm btn-secondary" onclick="testUnlockServer('${s.id}')" title="测试 ChatGPT / YouTube / Netflix 解锁">
          🧪 解锁
        </button>
        <button class="btn btn-sm btn-ghost" onclick="editServer('${s.id}')" title="修改配置">
          ✏️ 编辑
        </button>
        <button class="btn btn-sm btn-danger-ghost" onclick="deleteServer('${s.id}')" title="删除">
          🗑️
        </button>
      </div>
    `;

    grid.appendChild(card);
  });
}

function escapeHtml(text) {
  if (!text) return '';
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
  return String(text).replace(/[&<>"']/g, m => map[m]);
}

function copyToClipboard(text) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => {
      showToast(`已复制: ${text}`, 'info');
    });
  }
}

// ------------------------------------------------------------------------------
// Event Listeners: Search & Filters
// ------------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  loadConfig();

  // Search input
  const searchInput = document.getElementById('search-input');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      state.search = e.target.value.trim();
      renderServers();
    });
  }

  // Filter pills
  const pills = document.querySelectorAll('.filter-pill');
  pills.forEach(pill => {
    pill.addEventListener('click', () => {
      pills.forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      state.filter = pill.dataset.filter;
      renderServers();
    });
  });

  // Top action buttons
  document.getElementById('btn-global-config')?.addEventListener('click', openGlobalConfigModal);
  document.getElementById('btn-save-global-config')?.addEventListener('click', saveGlobalConfig);
  document.getElementById('btn-test-push-target')?.addEventListener('click', testPushTarget);

  document.getElementById('btn-add-server')?.addEventListener('click', () => openServerModal(null));
  document.getElementById('btn-save-server')?.addEventListener('click', saveServer);

  document.getElementById('btn-batch-import')?.addEventListener('click', () => {
    document.getElementById('batch-import-text').value = '';
    document.getElementById('batch-preview-container').style.display = 'none';
    openModal('modal-batch-import');
  });
  document.getElementById('btn-do-batch-import')?.addEventListener('click', doBatchImport);

  document.getElementById('btn-deploy-all')?.addEventListener('click', deployAllServers);
  document.getElementById('btn-inspect-all')?.addEventListener('click', inspectAllServers);

  // Batch ops dropdown menu toggle
  const batchOpsToggle = document.getElementById('btn-batch-ops-toggle');
  const batchOpsMenu = document.getElementById('batch-ops-menu');
  if (batchOpsToggle && batchOpsMenu) {
    batchOpsToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      batchOpsMenu.classList.toggle('show');
    });
    document.addEventListener('click', () => {
      batchOpsMenu.classList.remove('show');
    });
  }

  document.getElementById('btn-export-config')?.addEventListener('click', () => {
    window.location.href = '/api/export';
  });

  const importFileInput = document.getElementById('import-file-input');
  if (importFileInput) {
    importFileInput.addEventListener('change', uploadConfigFile);
  }

  // NAT port input preview dynamic calculation
  const srvPortMode = document.getElementById('srv-port-mode');
  const srvPortRange = document.getElementById('srv-port-range');
  if (srvPortMode && srvPortRange) {
    srvPortMode.addEventListener('change', updateNatPreview);
    srvPortRange.addEventListener('input', updateNatPreview);
  }

  // Argo toggle
  const srvArgoEnabled = document.getElementById('srv-argo-enabled');
  if (srvArgoEnabled) {
    srvArgoEnabled.addEventListener('change', (e) => {
      document.getElementById('grp-argo-details').style.display = e.target.checked ? 'block' : 'none';
    });
  }

  // Auth type toggle
  const srvAuthType = document.getElementById('srv-auth-type');
  if (srvAuthType) {
    srvAuthType.addEventListener('change', (e) => {
      const isKey = e.target.value === 'private_key';
      document.getElementById('grp-password').style.display = isKey ? 'none' : 'block';
      document.getElementById('grp-private-key').style.display = isKey ? 'block' : 'none';
    });
  }

  // Copy logs button
  document.getElementById('btn-copy-logs')?.addEventListener('click', () => {
    const lines = document.getElementById('terminal-lines').innerText;
    copyToClipboard(lines);
  });
});

// ------------------------------------------------------------------------------
// Global Config Modal Logic
// ------------------------------------------------------------------------------
function openGlobalConfigModal() {
  const g = state.config.global || {};
  document.getElementById('cfg-push-url').value = g.push_url || '';
  document.getElementById('cfg-push-token').value = g.push_token || '';
  document.getElementById('cfg-auto-push').checked = g.auto_push !== false;
  document.getElementById('cfg-cf-dns-token').value = g.cf_dns_token || '';

  const protos = g.default_protocols || {};
  document.getElementById('cfg-proto-reality').checked = protos.reality !== false;
  document.getElementById('cfg-proto-hy2').checked = protos.hy2 !== false;
  document.getElementById('cfg-proto-tuic').checked = protos.tuic !== false;

  document.getElementById('cfg-script-url').value = g.install_script_url || '';
  document.getElementById('cfg-concurrency').value = g.concurrency || 5;

  document.getElementById('cfg-auto-inspect').checked = g.auto_inspect === true;
  document.getElementById('cfg-auto-inspect-interval').value = g.auto_inspect_interval || 60;

  openModal('modal-global-config');
}

async function saveGlobalConfig() {
  const payload = {
    push_url: document.getElementById('cfg-push-url').value.trim(),
    push_token: document.getElementById('cfg-push-token').value.trim(),
    auto_push: document.getElementById('cfg-auto-push').checked,
    cf_dns_token: document.getElementById('cfg-cf-dns-token').value.trim(),
    default_protocols: {
      reality: document.getElementById('cfg-proto-reality').checked,
      hy2: document.getElementById('cfg-proto-hy2').checked,
      tuic: document.getElementById('cfg-proto-tuic').checked
    },
    install_script_url: document.getElementById('cfg-script-url').value.trim(),
    concurrency: parseInt(document.getElementById('cfg-concurrency').value) || 5,
    auto_inspect: document.getElementById('cfg-auto-inspect').checked,
    auto_inspect_interval: parseInt(document.getElementById('cfg-auto-inspect-interval').value) || 60
  };

  try {
    const res = await fetch('/api/config/global', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.success) {
      showToast('全局统一配置已成功保存！', 'success');
      state.config.global = data.data;
      closeModal('modal-global-config');
    } else {
      showToast(`保存失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`保存出错: ${err.message}`, 'error');
  }
}

async function testPushTarget() {
  const url = document.getElementById('cfg-push-url').value.trim();
  const token = document.getElementById('cfg-push-token').value.trim();
  if (!url) {
    showToast('请先输入管理平台地址', 'error');
    return;
  }

  showToast('正在测试与管理平台的网络连通性与凭证...', 'info');
  try {
    const res = await fetch('/api/test_push_target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, token })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
    } else {
      showToast(data.message, 'error');
    }
  } catch (err) {
    showToast(`测试连通性出错: ${err.message}`, 'error');
  }
}

// ------------------------------------------------------------------------------
// Add / Edit Server Logic
// ------------------------------------------------------------------------------
function openServerModal(serverId = null) {
  state.activeServerId = serverId;
  const isEdit = !!serverId;
  document.getElementById('server-modal-title').textContent = isEdit ? '编辑 VPS 主机配置' : '添加 VPS 主机';
  document.getElementById('edit-server-id').value = serverId || '';

  if (isEdit) {
    const server = (state.config.servers || []).find(s => s.id === serverId);
    if (!server) return;
    document.getElementById('srv-name').value = server.name || '';
    document.getElementById('srv-host').value = server.host || '';
    document.getElementById('srv-port').value = server.port || 22;
    document.getElementById('srv-user').value = server.user || 'root';
    document.getElementById('srv-auth-type').value = server.auth_type || 'password';
    document.getElementById('srv-password').value = server.password || '';
    document.getElementById('srv-key').value = server.private_key || '';

    document.getElementById('srv-tag-main').value = server.tag_main || '';
    document.getElementById('srv-tag-argo').value = server.tag_argo || '';

    document.getElementById('srv-port-mode').value = server.port_mode || 'auto';
    document.getElementById('srv-port-range').value = server.port_range || '';
    document.getElementById('srv-domain').value = server.domain || '';
    document.getElementById('srv-reality-sni').value = server.reality_sni || '';

    document.getElementById('srv-argo-enabled').checked = !!server.argo_enabled;
    document.getElementById('srv-argo-domain').value = server.argo_domain || '';
    document.getElementById('srv-argo-token').value = server.argo_token || '';

    const isKey = server.auth_type === 'private_key';
    document.getElementById('grp-password').style.display = isKey ? 'none' : 'block';
    document.getElementById('grp-private-key').style.display = isKey ? 'block' : 'none';
    document.getElementById('grp-argo-details').style.display = server.argo_enabled ? 'block' : 'none';
  } else {
    // New server defaults
    document.getElementById('srv-name').value = '';
    document.getElementById('srv-host').value = '';
    document.getElementById('srv-port').value = 22;
    document.getElementById('srv-user').value = 'root';
    document.getElementById('srv-auth-type').value = 'password';
    document.getElementById('srv-password').value = '';
    document.getElementById('srv-key').value = '';

    document.getElementById('srv-tag-main').value = '';
    document.getElementById('srv-tag-argo').value = '';

    document.getElementById('srv-port-mode').value = 'auto';
    document.getElementById('srv-port-range').value = '';
    document.getElementById('srv-domain').value = '';
    document.getElementById('srv-reality-sni').value = '';

    document.getElementById('srv-argo-enabled').checked = false;
    document.getElementById('srv-argo-domain').value = '';
    document.getElementById('srv-argo-token').value = '';

    document.getElementById('grp-password').style.display = 'block';
    document.getElementById('grp-private-key').style.display = 'none';
    document.getElementById('grp-argo-details').style.display = 'none';
  }

  updateNatPreview();
  openModal('modal-server-edit');
}

function updateNatPreview() {
  const mode = document.getElementById('srv-port-mode').value;
  const rangeInput = document.getElementById('srv-port-range').value.trim();
  const preview = document.getElementById('nat-calc-preview');

  if (mode !== 'nat') {
    preview.style.display = 'none';
    return;
  }
  preview.style.display = 'block';

  if (!rangeInput || !rangeInput.includes('-')) {
    preview.textContent = '💡 请输入连续范围 (例如: 10020-10039)';
    return;
  }

  const parts = rangeInput.split('-');
  const startP = parseInt(parts[0].trim());
  const endP = parseInt(parts[1].trim());

  if (isNaN(startP) || isNaN(endP) || endP <= startP) {
    preview.textContent = '⚠️ 端口范围格式不正确，请输入例如 10020-10039';
    return;
  }

  const total = endP - startP + 1;
  const pReality = startP;
  const pHy2 = startP + 1;
  const pTuic = startP + 2;
  const remStart = startP + 3;

  if (total < 3) {
    preview.textContent = `❌ 端口总数只有 ${total} 个，不足以分配三大协议 (至少需 3 个端口)`;
    return;
  }

  let hopStr = '无 (端口不足5个)';
  if (remStart <= endP) {
    hopStr = `${remStart}-${endP}`;
  }

  preview.textContent = `💡 规划结果: Reality=${pReality}, Hy2=${pHy2}, TUIC=${pTuic}, Hy2跳跃池=${hopStr} (共${total}端口)`;
}

async function saveServer() {
  const serverId = document.getElementById('edit-server-id').value;
  const isEdit = !!serverId;
  const host = document.getElementById('srv-host').value.trim();

  if (!host) {
    showToast('请输入 VPS IP 或域名', 'error');
    return;
  }

  const payload = {
    name: document.getElementById('srv-name').value.trim() || host,
    host: host,
    port: parseInt(document.getElementById('srv-port').value) || 22,
    user: document.getElementById('srv-user').value.trim() || 'root',
    auth_type: document.getElementById('srv-auth-type').value,
    password: document.getElementById('srv-password').value,
    private_key: document.getElementById('srv-key').value,
    tag_main: document.getElementById('srv-tag-main').value.trim(),
    tag_argo: document.getElementById('srv-tag-argo').value.trim(),
    port_mode: document.getElementById('srv-port-mode').value,
    port_range: document.getElementById('srv-port-range').value.trim(),
    domain: document.getElementById('srv-domain').value.trim(),
    reality_sni: document.getElementById('srv-reality-sni').value.trim(),
    argo_enabled: document.getElementById('srv-argo-enabled').checked,
    argo_domain: document.getElementById('srv-argo-domain').value.trim(),
    argo_token: document.getElementById('srv-argo-token').value.trim()
  };

  const endpoint = isEdit ? `/api/server/update/${serverId}` : '/api/server/add';

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.success) {
      showToast(isEdit ? 'VPS 配置更新成功！' : 'VPS 添加成功！', 'success');
      closeModal('modal-server-edit');
      loadConfig();
    } else {
      showToast(`保存失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`保存出错: ${err.message}`, 'error');
  }
}

function editServer(serverId) {
  openServerModal(serverId);
}

async function deleteServer(serverId) {
  const s = (state.config.servers || []).find(i => i.id === serverId);
  const name = s ? (s.name || s.host) : '此服务器';

  if (!confirm(`确定要从集群中移除 [${name}] 吗？`)) {
    return;
  }

  try {
    const res = await fetch(`/api/server/delete/${serverId}`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`已删除 [${name}]`, 'success');
      loadConfig();
    } else {
      showToast(`删除失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`删除出错: ${err.message}`, 'error');
  }
}

// ------------------------------------------------------------------------------
// Single Actions: Deploy, Check Env, Inspect, Unlock
// ------------------------------------------------------------------------------
async function deploySingleServer(serverId) {
  const s = (state.config.servers || []).find(i => i.id === serverId);
  const name = s ? (s.name || s.host) : 'VPS';

  showToast(`正在启动 [${name}] 的自动化一键部署任务...`, 'info');

  try {
    const res = await fetch(`/api/deploy/${serverId}`, { method: 'POST' });
    const data = await res.json();
    if (data.success && data.task_id) {
      openTerminal(data.task_id, `部署 Sing-box 核心协议: [${name}]`);
    } else {
      showToast(`启动部署失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`部署请求出错: ${err.message}`, 'error');
  }
}

async function checkEnvServer(serverId) {
  const s = (state.config.servers || []).find(i => i.id === serverId);
  const name = s ? (s.name || s.host) : 'VPS';

  showToast(`正在连接 [${name}] 测试延迟与环境...`, 'info');
  try {
    const res = await fetch(`/api/check_env/${serverId}`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`[${name}] ${data.message}`, 'success');
      loadConfig();
    } else {
      showToast(`[${name}] 检测失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`检测出错: ${err.message}`, 'error');
  }
}

async function inspectSingleServer(serverId) {
  const s = (state.config.servers || []).find(i => i.id === serverId);
  const name = s ? (s.name || s.host) : 'VPS';

  showToast(`正在对 [${name}] 执行服务状态与网络巡检...`, 'info');
  try {
    const res = await fetch(`/api/inspect/${serverId}`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      const d = data.data;
      const statusStr = d.singbox_running ? '🟢 服务运行中' : '🔴 核心未在运行';
      showToast(`[${name}] 巡检完成: ${statusStr}, 延迟: ${d.latency_ms}ms`, d.singbox_running ? 'success' : 'warning');
      loadConfig();
    } else {
      showToast(`巡检失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`巡检请求异常: ${err.message}`, 'error');
  }
}

async function testUnlockServer(serverId) {
  const s = (state.config.servers || []).find(i => i.id === serverId);
  const name = s ? (s.name || s.host) : 'VPS';

  document.getElementById('unlock-modal-title').textContent = `流媒体与 AI 解锁报告: [${name}]`;
  document.getElementById('unlock-modal-subtitle').textContent = `正在从 ${s.host} 远程执行测速与风控穿透校验...`;
  document.getElementById('unlock-val-openai').textContent = '测试中...';
  document.getElementById('unlock-val-youtube').textContent = '测试中...';
  document.getElementById('unlock-val-netflix').textContent = '测试中...';
  openModal('modal-unlock');

  try {
    const res = await fetch(`/api/test_unlock/${serverId}`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      const u = data.data;
      document.getElementById('unlock-modal-subtitle').textContent = `测试完成于 ${new Date().toLocaleTimeString()}`;
      
      const setVal = (elId, text) => {
        const el = document.getElementById(elId);
        el.textContent = text;
        el.className = 'unlock-card-status ' + (text.includes('解锁') || text.includes('原生') ? 'badge-success' : 'badge-danger');
      };

      setVal('unlock-val-openai', u.openai);
      setVal('unlock-val-youtube', u.youtube);
      setVal('unlock-val-netflix', u.netflix);
      loadConfig();
    } else {
      document.getElementById('unlock-modal-subtitle').textContent = `测试失败: ${data.message}`;
    }
  } catch (err) {
    document.getElementById('unlock-modal-subtitle').textContent = `请求失败: ${err.message}`;
  }
}

// ------------------------------------------------------------------------------
// Batch Actions: Deploy All, Inspect All, Batch Import
// ------------------------------------------------------------------------------
async function deployAllServers() {
  const count = (state.config.servers || []).length;
  if (count === 0) {
    showToast('当前集群暂无 VPS，请先添加或导入！', 'error');
    return;
  }

  if (!confirm(`确定要并发一键部署集群内的全部 ${count} 台 VPS 吗？`)) {
    return;
  }

  showToast(`已向后台提交全集群 ${count} 台 VPS 批量部署流水线！`, 'info');
  try {
    const res = await fetch('/api/deploy_all', { method: 'POST' });
    const data = await res.json();
    if (data.success && data.task_ids && data.task_ids.length > 0) {
      showToast(`已成功下发 ${data.count} 个并行部署任务！`, 'success');
      // Open terminal for first task
      openTerminal(data.task_ids[0], `全量批量部署任务 (首台主机实时日志)`);
    } else {
      showToast(`批量部署失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`批量部署出错: ${err.message}`, 'error');
  }
}

async function inspectAllServers() {
  showToast('已向后台触发全集群并发巡检，正在后台探测各主机...', 'info');
  try {
    const res = await fetch('/api/inspect_all', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      setTimeout(loadConfig, 4000);
    } else {
      showToast(`巡检失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`巡检出错: ${err.message}`, 'error');
  }
}

async function triggerBatchOp(action) {
  const batchOpsMenu = document.getElementById('batch-ops-menu');
  if (batchOpsMenu) batchOpsMenu.classList.remove('show');

  const count = (state.config.servers || []).length;
  if (count === 0) {
    showToast('当前集群内暂无 VPS，请先添加主机', 'error');
    return;
  }

  const actionMap = {
    restart: { title: '批量重启 Sing-box 服务', confirm: `确定要向全部 ${count} 台 VPS 发送指令，批量重启 Sing-box 核心进程吗？` },
    update_core: { title: '批量升级 Sing-box 核心', confirm: `确定要在全部 ${count} 台 VPS 上并发执行官方核心升级任务吗？` },
    reapply_bbr: { title: '批量刷新激活 BBR+fq', confirm: `确定要在全部 ${count} 台 VPS 上刷新并永久激活 BBRv3 / BBR + fq 拥塞控制吗？` },
    uninstall: { title: '批量清理卸载 Sing-box', confirm: `⚠️ 高危操作：确定要在全部 ${count} 台 VPS 上彻底停止并卸载 Sing-box 吗？` }
  };

  const meta = actionMap[action] || { title: '批量运维任务', confirm: `确定执行批量操作 [${action}] 吗？` };
  if (!confirm(meta.confirm)) return;

  showToast(`正在向集群提交【${meta.title}】批量任务...`, 'info');
  try {
    const res = await fetch('/api/batch_ops', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, server_ids: [] })
    });
    const data = await res.json();
    if (data.success && data.task_id) {
      openTerminal(data.task_id, `${meta.title} (集群实时控制台)`);
    } else {
      showToast(`下发批量任务失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`批量任务请求异常: ${err.message}`, 'error');
  }
}
window.triggerBatchOp = triggerBatchOp;

async function doBatchImport() {
  const text = document.getElementById('batch-import-text').value.trim();
  if (!text) {
    showToast('请粘贴 VPS 列表文本', 'error');
    return;
  }

  try {
    const res = await fetch('/api/batch_import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
    const data = await res.json();
    if (data.success) {
      showToast(`成功解析并导入 ${data.count} 台 VPS！`, 'success');
      closeModal('modal-batch-import');
      loadConfig();
    } else {
      showToast(`导入失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`导入出错: ${err.message}`, 'error');
  }
}

async function uploadConfigFile(e) {
  const file = e.target.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/api/import', { method: 'POST', body: formData });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      loadConfig();
    } else {
      showToast(`导入失败: ${data.message}`, 'error');
    }
  } catch (err) {
    showToast(`导入出错: ${err.message}`, 'error');
  } finally {
    e.target.value = '';
  }
}

// ------------------------------------------------------------------------------
// Real-time Terminal Log Viewer Engine
// ------------------------------------------------------------------------------
function openTerminal(taskId, title = '任务控制台') {
  state.activeTaskId = taskId;
  document.getElementById('terminal-title').textContent = title;
  document.getElementById('terminal-step').textContent = '连接并执行中...';
  document.getElementById('terminal-lines').innerHTML = '';
  document.getElementById('terminal-progress-bar').style.width = '10%';
  document.getElementById('terminal-status-text').textContent = '正在实时接收远端输出流...';

  openModal('modal-terminal');
  pollTaskLogs();
  state.pollTimer = setInterval(pollTaskLogs, 1200);
}

function stopTerminalPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  state.activeTaskId = null;
}

async function pollTaskLogs() {
  if (!state.activeTaskId) return;

  try {
    const res = await fetch(`/api/tasks/${state.activeTaskId}`);
    const data = await res.json();
    if (!data.success || !data.data) return;

    const task = data.data;
    document.getElementById('terminal-step').textContent = task.step || '执行中';
    document.getElementById('terminal-server-name').textContent = task.server_name || '';

    // Update Progress Bar
    const progress = Math.min(Math.max(task.progress || 10, 5), 100);
    document.getElementById('terminal-progress-bar').style.width = `${progress}%`;

    // Render Logs
    const linesContainer = document.getElementById('terminal-lines');
    const existingCount = linesContainer.children.length;
    const newLogs = task.logs || [];

    if (newLogs.length > existingCount) {
      for (let i = existingCount; i < newLogs.length; i++) {
        const lineText = newLogs[i];
        const lineDiv = document.createElement('div');
        lineDiv.className = 'terminal-line';

        if (lineText.includes('[OK]') || lineText.includes('成功') || lineText.includes('完成')) {
          lineDiv.classList.add('log-ok');
        } else if (lineText.includes('[ERR]') || lineText.includes('错误') || lineText.includes('失败')) {
          lineDiv.classList.add('log-err');
        } else if (lineText.includes('[WARN]') || lineText.includes('警告')) {
          lineDiv.classList.add('log-warn');
        } else if (lineText.includes('[INFO]')) {
          lineDiv.classList.add('log-info');
        }

        lineDiv.textContent = lineText;
        linesContainer.appendChild(lineDiv);
      }

      // Auto-scroll
      const chk = document.getElementById('chk-autoscroll');
      if (chk && chk.checked) {
        const win = document.getElementById('terminal-window');
        win.scrollTop = win.scrollHeight;
      }
    }

    if (task.status === 'success') {
      document.getElementById('terminal-status-text').textContent = '✅ 任务执行成功已完成！';
      document.getElementById('terminal-progress-bar').style.width = '100%';
      document.getElementById('terminal-step').className = 'badge badge-success';
      stopTerminalPolling();
      loadConfig();
    } else if (task.status === 'error') {
      document.getElementById('terminal-status-text').textContent = `❌ 任务执行异常: ${task.error || '未知错误'}`;
      document.getElementById('terminal-step').className = 'badge badge-danger';
      stopTerminalPolling();
      loadConfig();
    }
  } catch (err) {
    console.error('Task poll error:', err);
  }
}
