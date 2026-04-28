/* mcp.js v2 – MCP tool management + Telegram control panel */

const MCP_GROUP_ICONS = {
  'Memory':        '🧠',
  'Books':         '📚',
  'Notifications': '🔔',
  'Devices':       '🎮',
  'Calendar':      '📅',
  'Health':        '❤️',
  'Screentime':    '📱',
  'Environment':   '🌤',
  'Productivity':  '✅',
  'Notion':        '📝',
  'Character':     '✨',
  'Other':         '⚙️',
};

const MCP_GROUP_ORDER = [
  'Memory', 'Character', 'Environment', 'Productivity',
  'Notifications', 'Devices', 'Calendar', 'Books',
  'Health', 'Screentime', 'Other',
];

// ── main loader ───────────────────────────────────────────────────────────────

async function loadMcpTools() {
  const area = document.getElementById('area');
  area.innerHTML = '<div class="page-loading">Loading MCP tools…</div>';
  area.classList.remove('read-mode');

  try {
    const [data, tgStatus] = await Promise.all([
      apiFetch('/admin/api/mcp/tools'),
      apiFetch('/admin/api/telegram/status').catch(() => null),
    ]);

    const total = data.count || 0;
    document.getElementById('n-mcp').textContent = total;

    const cfgSection  = buildConfigSection(tgStatus);

    const groups     = data.groups || {};
    const orderedKeys = [
      ...MCP_GROUP_ORDER.filter(g => groups[g]),
      ...Object.keys(groups).filter(g => !MCP_GROUP_ORDER.includes(g)),
    ];

    const groupsHtml = orderedKeys
      .map(g => buildGroupCard(g, MCP_GROUP_ICONS[g] || '⚙️', groups[g]))
      .join('');

    area.innerHTML = `
      <div class="mcp-panel">
        ${cfgSection}
        <div class="mcp-groups">${groupsHtml}</div>
      </div>`;

    // Restore saved URLs
    const savedBark = localStorage.getItem('barkUrl') || '';
    const savedInti = localStorage.getItem('intifaceUrl') || '';
    if (savedBark) document.getElementById('barkUrl').value = savedBark;
    if (savedInti) document.getElementById('intifaceUrl').value = savedInti;

    // Wire Telegram agent selector if present
    _bindTelegramSelector(tgStatus);

  } catch (e) {
    area.innerHTML = `<div class="page-loading" style="color:var(--danger)">Error: ${e}</div>`;
  }
}

// ── config section ────────────────────────────────────────────────────────────

function buildConfigSection(tgStatus) {
  const tgBlock = buildTelegramBlock(tgStatus);
  return `
    <div class="mcp-config-bar">
      <div class="mcp-config-item">
        <span class="mcp-config-label">🔔 Bark URL</span>
        <input class="mcp-config-input" id="barkUrl"
          placeholder="https://api.day.app/your-token"
          onchange="saveBarkUrl(this.value)">
        <button class="btn btn-s btn-g" onclick="testBark()">Test</button>
      </div>
      <div class="mcp-config-item">
        <span class="mcp-config-label">🎮 Intiface URL</span>
        <input class="mcp-config-input" id="intifaceUrl"
          placeholder="ws://host.docker.internal:12345"
          onchange="saveIntifaceUrl(this.value)">
        <button class="btn btn-s btn-g" onclick="scanDevices()">Scan</button>
      </div>
    </div>
    ${tgBlock}
    <div id="mcpActionResult" class="mcp-action-result" style="display:none"></div>`;
}

// ── Telegram block ────────────────────────────────────────────────────────────

function buildTelegramBlock(st) {
  if (!st) return '';                          // API not available
  if (!st.configured) return `
    <div class="tg-panel tg-unconfigured">
      <span class="tg-icon">✈️</span>
      <span style="color:var(--muted);font-size:12px">Telegram not configured
        (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)</span>
    </div>`;

  const msgCount = st.session_messages || 0;
  const chatId   = st.chat_id   || '—';
  const curAgent = st.current_agent || st.default_agent || '—';

  return `
    <div class="tg-panel">
      <div class="tg-header">
        <span class="tg-icon">✈️</span>
        <span class="tg-title">Telegram Bot</span>
        <span class="tg-badge tg-online">● online</span>
        <span class="tg-meta">chat_id: ${esc(chatId)}</span>
      </div>
      <div class="tg-row">
        <div class="tg-field">
          <label class="tg-label">当前对话角色</label>
          <select class="mcp-config-input tg-agent-sel" id="tgAgentSel"
                  onchange="switchTelegramAgent(this.value)">
            <option value="${esc(curAgent)}">${esc(curAgent)}</option>
          </select>
        </div>
        <div class="tg-field tg-field-sm">
          <label class="tg-label">上下文</label>
          <span class="tg-count" id="tgMsgCount">${msgCount} 条</span>
        </div>
        <div class="tg-actions">
          <button class="btn btn-s btn-d" onclick="clearTelegramSession()"
                  title="清空当前对话的上下文（不影响记忆）">🗑 清空前文</button>
        </div>
      </div>
      <div class="tg-hint">
        Bot 指令：/start [agent_id] · /switch &lt;id&gt; · /list · /clear
      </div>
    </div>`;
}

// Populate agent selector after DOM is ready
function _bindTelegramSelector(st) {
  const sel = document.getElementById('tgAgentSel');
  if (!sel || !st || !st.configured) return;

  const curAgent = st.current_agent || st.default_agent || '';

  // Populate from global agentTypes (loaded by loadAgents)
  const allAgents = Object.keys(agentTypes || {}).sort();
  sel.innerHTML = '';
  if (!allAgents.length) {
    const opt = document.createElement('option');
    opt.value = curAgent; opt.textContent = curAgent;
    sel.appendChild(opt);
    return;
  }
  allAgents.forEach(aid => {
    const opt = document.createElement('option');
    opt.value = aid;
    opt.textContent = aid;
    if (aid === curAgent) opt.selected = true;
    sel.appendChild(opt);
  });
}

// ── Telegram actions ──────────────────────────────────────────────────────────

async function clearTelegramSession() {
  showMcpResult('清空中…', 'info');
  try {
    const r = await apiFetch('/admin/api/telegram/clear', { method: 'POST', body: '{}' });
    showMcpResult('✓ 对话上下文已清空', 'ok');
    document.getElementById('tgMsgCount').textContent = '0 条';
  } catch (e) {
    showMcpResult('✗ ' + e, 'err');
  }
}

async function switchTelegramAgent(agentId) {
  if (!agentId) return;
  showMcpResult('切换中…', 'info');
  try {
    const r = await apiFetch('/admin/api/telegram/switch', {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId }),
    });
    showMcpResult(`✓ 已切换到 ${agentId}，上下文已清空`, 'ok');
    document.getElementById('tgMsgCount').textContent = '0 条';
  } catch (e) {
    showMcpResult('✗ ' + e, 'err');
    // Revert selector on error
    loadMcpTools();
  }
}

// ── Group card ────────────────────────────────────────────────────────────────

function buildGroupCard(groupName, icon, tools) {
  const chips = tools.map(t => `
    <div class="mcp-tool-chip" title="${esc(t.description)}">
      <span class="mcp-tool-name">${esc(t.name)}</span>
      <span class="mcp-tool-desc">${esc(t.description)}</span>
    </div>`).join('');

  return `
    <div class="mcp-group-card">
      <div class="mcp-group-header">
        <span class="mcp-group-icon">${icon}</span>
        <span class="mcp-group-name">${esc(groupName)}</span>
        <span class="mcp-group-count">${tools.length}</span>
      </div>
      <div class="mcp-tool-list">${chips}</div>
    </div>`;
}

// ── Bark / Intiface ───────────────────────────────────────────────────────────

async function testBark() {
  showMcpResult('Sending test notification…', 'info');
  try {
    const r = await apiFetch('/admin/api/mcp/bark-test', {
      method: 'POST',
      body: JSON.stringify({ title: 'Palimpsest', body: 'Connection test ✓' }),
    });
    showMcpResult('✓ ' + r.result, 'ok');
  } catch (e) {
    showMcpResult('✗ ' + e, 'err');
  }
}

async function scanDevices() {
  showMcpResult('Scanning Intiface devices…', 'info');
  try {
    const r = await apiFetch('/admin/api/mcp/intiface-devices');
    showMcpResult(r.result, 'ok');
  } catch (e) {
    showMcpResult('✗ ' + e, 'err');
  }
}

function saveBarkUrl(val) {
  localStorage.setItem('barkUrl', val);
  apiFetch('/admin/api/gateway-config', {
    method: 'POST',
    body: JSON.stringify({ BARK_URL: val }),
  }).catch(() => {});
}

function saveIntifaceUrl(val) {
  localStorage.setItem('intifaceUrl', val);
  apiFetch('/admin/api/gateway-config', {
    method: 'POST',
    body: JSON.stringify({ INTIFACE_URL: val }),
  }).catch(() => {});
}

// ── Result display ────────────────────────────────────────────────────────────

function showMcpResult(msg, type) {
  const el = document.getElementById('mcpActionResult');
  if (!el) return;
  el.textContent = msg;
  el.className   = 'mcp-action-result mcp-result-' + type;
  el.style.display = 'block';
  if (type === 'ok') setTimeout(() => { el.style.display = 'none'; }, 3000);
}

function esc(s) {
  return (s || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
