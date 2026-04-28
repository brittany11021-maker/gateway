// ── Agents ────────────────────────────────────────────────────────────────────
async function loadAgents() {
  try {
    const d = await api('/admin/api/agents');
    allAgents  = d.agents.length ? d.agents : ['default'];
    agentTypes = d.agent_types || {};
  } catch(e) {
    allAgents = []; agentTypes = {};
  }
  const nAgent = allAgents.filter(a => (agentTypes[a] || 'agent') === 'agent').length;
  const nChar  = allAgents.filter(a => agentTypes[a] === 'character').length;
  const elA = document.getElementById('n-agent');
  const elC = document.getElementById('n-character');
  if (elA) elA.textContent = nAgent;
  if (elC) elC.textContent = nChar;
  agentItems = {}; agentPages = {}; agentLoaded.clear();
  _agentPageAid = null;
  renderPage();
}

// ── Agent list (top-level, lightweight cards) ─────────────────────────────────
function renderPage() {
  _agentPageAid = null;
  _setToolbarMode('list');

  if (!allAgents.length) {
    setArea('<div class="page-empty"><div class="page-empty-ico">◌</div>'
          + '<div class="page-empty-lbl">No agents yet — click + New to create</div></div>');
    return;
  }

  let visibleAgents = allAgents;
  if (S.tab === 'agent')     visibleAgents = allAgents.filter(a => (agentTypes[a] || 'agent') === 'agent');
  if (S.tab === 'character') visibleAgents = allAgents.filter(a => agentTypes[a] === 'character');
  if (S.tab === 'project')   visibleAgents = allAgents.filter(a => (agentTypes[a] || 'agent') === 'agent');

  if (!visibleAgents.length) {
    const typeLabel = S.tab === 'character' ? 'character' : 'agent';
    setArea(`<div class="page-empty"><div class="page-empty-ico">◌</div>`
          + `<div class="page-empty-lbl">No ${typeLabel}s yet</div></div>`);
    return;
  }

  const cards = visibleAgents.map((aid, i) => {
    const avBg = S.night ? AV_NIGHT[i % AV_NIGHT.length] : AV_DAY[i % AV_DAY.length];
    const initial = [...aid][0]?.toUpperCase() || '?';
    const atype = agentTypes[aid] || 'agent';
    const badge = atype === 'character'
      ? '<span style="font-size:9px;background:rgba(180,100,220,.15);color:#b464dc;border-radius:4px;padding:2px 6px;margin-left:6px">character</span>'
      : '<span style="font-size:9px;background:rgba(60,140,255,.12);color:#3c8cff;border-radius:4px;padding:2px 6px;margin-left:6px">agent</span>';
    return `
    <div class="agent-card" data-aid="${aid}" onclick="openAgentPage('${aid.replace(/'/g,"\\'")}')">
      <div class="u-avatar u-av-init" id="uav${i}" style="background:${avBg}">${initial}</div>
      <div class="agent-card-info">
        <span class="u-name">${esc(aid)}</span>${badge}
      </div>
      <button class="settings-btn" onclick="openSettingsByAid('${aid.replace(/'/g,"\\'")}',event)" title="Settings">⚙</button>
    </div>`;
  }).join('');

  setArea(`<div class="agent-card-list">${cards}</div>`);
  visibleAgents.forEach((aid, i) => restoreAvatar(aid, i));
}

// ── Avatar helpers ─────────────────────────────────────────────────────────────
async function restoreAvatar(aid, i) {
  const cacheKey = `av_${aid}`;
  let avatar = sessionStorage.getItem(cacheKey);
  if (avatar === null) {
    try {
      const d = await api(`/admin/api/agents/${enc(aid)}/settings`);
      avatar = d.avatar || '';
      sessionStorage.setItem(cacheKey, avatar);
    } catch { avatar = ''; }
  }
  if (avatar) applyAvatarEl(i, avatar, aid);
}

function applyAvatarEl(i, avatarUrl, aid) {
  const el = document.getElementById(`uav${i}`);
  if (!el) return;
  if (avatarUrl) {
    el.innerHTML = `<img src="${esc(avatarUrl)}" alt="${esc(aid)}">`;
    el.style.background = '';
  }
}

// ── Agent detail page ─────────────────────────────────────────────────────────
let _agentPageAid  = null;
let _agentDetailTab = 'l1';
let _agentDetailItems = {};  // tab → items array

const TIER_CFG = {
  l1: { col:'memory_profile',  label:'L1 — Profile', icon:'◉', cls:'type-profile',
        desc:'永久记忆·角色/关系' },
  l2: { col:'memory_project',  label:'L2 — Project', icon:'◈', cls:'type-project',
        desc:'中期·项目/知识' },
  l3: { col:'memory_recent',   label:'L3 — Recent',  icon:'◑', cls:'type-recent',
        desc:'近期·~30天' },
  history: { col:null, label:'History', icon:'⟳', cls:'type-recent', desc:'原始对话' },
  daily:   { col:null, label:'Daily',   icon:'✦', cls:'type-profile', desc:'日记事件' },
};

async function openAgentPage(aid) {
  _agentPageAid   = aid;
  _agentDetailTab = 'l1';
  _agentDetailItems = {};
  _setToolbarMode('detail', aid);

  const atype = agentTypes[aid] || 'agent';
  const tabs = ['l1', 'l2', 'l3', 'history'];
  if (atype === 'character') tabs.splice(3, 0, 'daily');

  const tabsHtml = tabs.map(t => {
    const cfg = TIER_CFG[t];
    return `<button class="detail-tab ${t==='l1'?'active':''}" data-tab="${t}"
      onclick="switchAgentDetailTab('${t}')">${cfg.icon} ${cfg.label}</button>`;
  }).join('');

  const badge = atype === 'character'
    ? '<span style="font-size:10px;background:rgba(180,100,220,.15);color:#b464dc;border-radius:4px;padding:2px 8px">character</span>'
    : '<span style="font-size:10px;background:rgba(60,140,255,.12);color:#3c8cff;border-radius:4px;padding:2px 8px">agent</span>';

  setArea(`
    <div class="detail-page">
      <div class="detail-header">
        <div style="display:flex;align-items:center;gap:10px">
          <div class="u-avatar u-av-init" id="detail-av" style="width:38px;height:38px;font-size:15px;
            background:${S.night ? AV_NIGHT[0] : AV_DAY[0]}">${[...aid][0]?.toUpperCase()||'?'}</div>
          <div>
            <span style="font-size:15px;font-weight:700">${esc(aid)}</span>
            ${badge}
          </div>
          <button class="settings-btn" onclick="openSettingsByAid('${aid.replace(/'/g,"\\'")}',event)"
            style="margin-left:4px" title="Settings">⚙</button>
        </div>
        <div class="detail-tabs">${tabsHtml}</div>
      </div>
      <div id="detail-body"><div class="u-loading">Loading…</div></div>
    </div>`);

  // Restore avatar for detail header
  const cacheKey = `av_${aid}`;
  const cached = sessionStorage.getItem(cacheKey);
  if (cached) {
    const el = document.getElementById('detail-av');
    if (el && cached) { el.innerHTML = `<img src="${esc(cached)}" style="width:100%;height:100%;object-fit:cover;border-radius:50%">`; el.style.background=''; }
  }

  await loadDetailTab('l1');
}

function closeAgentPage() {
  _agentPageAid = null;
  S.q = '';
  document.getElementById('searchIn').value = '';
  renderPage();
}

async function switchAgentDetailTab(tab) {
  _agentDetailTab = tab;
  document.querySelectorAll('.detail-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  await loadDetailTab(tab);
}

async function loadDetailTab(tab) {
  const body = document.getElementById('detail-body');
  if (!body) return;
  body.innerHTML = '<div class="u-loading">Loading…</div>';

  const aid = _agentPageAid;
  if (!aid) return;

  if (tab === 'history') { await loadDetailHistory(aid); return; }
  if (tab === 'daily')   { await loadDetailDaily(aid);   return; }

  const cfg = TIER_CFG[tab];
  try {
    const params = new URLSearchParams({ collection: cfg.col, agent_id: aid, limit: 500 });
    if (S.q) params.set('q', S.q);
    const d = await api('/admin/api/memories?' + params);
    _agentDetailItems[tab] = d.items;
    d.items.forEach(it => { CACHE[it.id] = { ...it, _col: cfg.col, _aid: aid }; });
    renderDetailCards(tab);
  } catch(e) {
    body.innerHTML = `<div class="u-empty">Error: ${e.message}</div>`;
  }
}

function renderDetailCards(tab) {
  const body = document.getElementById('detail-body');
  if (!body) return;
  const cfg   = TIER_CFG[tab];
  const items = _agentDetailItems[tab] || [];
  const page  = agentPages[tab] || 0;
  const total = items.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const slice = items.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const countHtml = `<div style="font-size:10px;color:var(--muted);margin-bottom:12px;padding:0 2px">
    ${cfg.desc} · ${total} entries</div>`;

  if (!total) {
    body.innerHTML = countHtml + '<div class="u-empty">No memories yet</div>'; return;
  }

  const cards = slice.map(it => `
    <div class="mem-card ${cfg.cls}" onclick="toggleCardExpand(this)">
      <div class="mc-acts" onclick="event.stopPropagation()">
        <button class="mc-act" onclick="openEditModal('${it.id}')">✎</button>
        <button class="mc-act del" onclick="delMemoryDetail('${it.id}','${cfg.col}')">✕</button>
      </div>
      <div class="mc-label">${cfg.label}</div>
      <div class="mc-text">${esc(it.text || '')}</div>
      <div class="mc-footer">
        <span class="mc-icon">${cfg.icon}</span>
        <span class="mc-date">${fmtTs(it.created_ts)}</span>
      </div>
    </div>`).join('');

  const pager = pages > 1 ? `
    <div class="pager">
      <button class="pager-btn" onclick="changeDetailPage('${tab}',-1)" ${page===0?'disabled':''}>‹</button>
      <span class="pager-info">${page+1} / ${pages}</span>
      <button class="pager-btn" onclick="changeDetailPage('${tab}',+1)" ${page>=pages-1?'disabled':''}>›</button>
    </div>` : '';

  body.innerHTML = countHtml + `<div class="mem-grid">${cards}</div>${pager}`;
}

function changeDetailPage(tab, delta) {
  const items = _agentDetailItems[tab] || [];
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  agentPages[tab] = Math.max(0, Math.min(pages - 1, (agentPages[tab] || 0) + delta));
  renderDetailCards(tab);
}

async function loadDetailHistory(aid) {
  const body = document.getElementById('detail-body');
  try {
    const d = await api(`/admin/api/conversations?agent_id=${enc(aid)}&limit=200`);
    const items = d.items || [];
    if (!items.length) { body.innerHTML = '<div class="u-empty">No conversations yet</div>'; return; }

    const rows = items.map((c, idx) => {
      const msgs = (c.messages || []).filter(m => m.role !== 'system');
      return `<div class="conv-card" id="cv-${c.id}">
        <div class="conv-hd" onclick="toggleConv('${c.id}')">
          <span class="conv-idx">#${idx+1}</span>
          <span class="conv-time">${fmtIsoTime(c.created_at)} · ${fmtIsoDate(c.created_at)}</span>
          <span class="conv-meta">${msgs.length} msg</span>
          <span class="conv-arr" title="${esc(c.id)}">▾</span>
        </div>
        <div class="conv-bd">${msgs.map(m => `
          <div class="msg">
            <div class="msg-role ${m.role}">${m.role}</div>
            <div class="msg-body">${esc(typeof m.content==='string'?m.content:JSON.stringify(m.content))}</div>
          </div>`).join('')}
        </div>
      </div>`;
    }).join('');
    body.innerHTML = `<div style="font-size:10px;color:var(--muted);margin-bottom:12px;padding:0 2px">
      原始对话 · ${items.length} conversations</div>
      <div class="conv-list">${rows}</div>`;
  } catch(e) {
    body.innerHTML = `<div class="u-empty">Error: ${e.message}</div>`;
  }
}

async function loadDetailDaily(aid) {
  const body = document.getElementById('detail-body');
  try {
    const d = await api(`/admin/api/daily-life?agent_id=${enc(aid)}&limit=100`);
    const items = d.items || d.events || [];
    if (!items.length) { body.innerHTML = '<div class="u-empty">No daily events yet</div>'; return; }
    const cards = items.map(it => `
      <div class="mem-card type-profile" onclick="toggleCardExpand(this)">
        <div class="mc-label">Daily · ${it.mood || ''}</div>
        <div class="mc-text">${esc(it.summary || it.content || '')}</div>
        <div class="mc-footer">
          <span class="mc-icon">✦</span>
          <span class="mc-date">${fmtIsoDate(it.created_at || it.date || '')}</span>
        </div>
      </div>`).join('');
    body.innerHTML = `<div style="font-size:10px;color:var(--muted);margin-bottom:12px;padding:0 2px">
      日记事件 · ${items.length} entries</div>
      <div class="mem-grid">${cards}</div>`;
  } catch(e) {
    body.innerHTML = `<div class="u-empty">Error loading daily: ${e.message}</div>`;
  }
}

async function delMemoryDetail(id, col) {
  if (!confirm('Delete this memory?')) return;
  try {
    await api(`/admin/api/memories/${id}?collection=${col}`, { method:'DELETE' });
    toast('Deleted');
    loadGlobalStats();
    delete _agentDetailItems[_agentDetailTab];
    await loadDetailTab(_agentDetailTab);
  } catch(e) { toast('Error: '+e.message); }
}

function toggleCardExpand(el) { el.classList.toggle('expanded'); }
function toggleConv(id) { document.getElementById('cv-'+id)?.classList.toggle('open'); }

// ── Toolbar mode ──────────────────────────────────────────────────────────────
function _setToolbarMode(mode, aid) {
  const toolbar   = document.querySelector('.toolbar');
  const backBtn   = document.getElementById('backBtn');
  const newBtn    = document.getElementById('newBtn');
  const addBtn    = document.getElementById('addBtn');
  const searchWrp = document.getElementById('searchWrap');

  if (!toolbar) return;
  toolbar.style.display = '';
  if (mode === 'detail') {
    if (backBtn)   backBtn.style.display = '';
    if (newBtn)    newBtn.style.display  = 'none';
    if (addBtn) { addBtn.style.display = ''; addBtn.textContent = '+ Memory'; }
    if (searchWrp) searchWrp.style.display = '';
  } else {
    if (backBtn)   backBtn.style.display = 'none';
    if (newBtn)  { newBtn.style.display  = ''; newBtn.textContent = '+ New'; }
    if (addBtn) addBtn.style.display = 'none';
    if (searchWrp) searchWrp.style.display = 'none';
  }
}

// ── Tab switch ────────────────────────────────────────────────────────────────
function switchTab(tab) {
  // If we're in an agent detail page, close it first
  if (_agentPageAid) { _agentPageAid = null; _agentDetailItems = {}; }

  S.tab = tab; S.q = '';
  document.getElementById('searchIn').value = '';
  document.querySelectorAll('.s-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-'+tab)?.classList.add('active');

  if (tab === 'books') {
    document.querySelector('.toolbar').style.display = 'none';
    loadBooksTab(); return;
  }
  if (tab === 'mcp') {
    document.querySelector('.toolbar').style.display = 'none';
    if (typeof loadMcpTools === 'function') loadMcpTools(); return;
  }
  if (tab === 'daily') {
    document.querySelector('.toolbar').style.display = 'none';
    if (typeof loadDailyTab === 'function') loadDailyTab(); return;
  }
  if (tab === 'world') {
    document.querySelector('.toolbar').style.display = 'none';
    if (typeof loadWorldbookTab === 'function') loadWorldbookTab(); return;
  }
  if (tab === 'user') {
    document.querySelector('.toolbar').style.display = 'none';
    if (typeof loadUserProfileTab === 'function') loadUserProfileTab(); return;
  }

  document.getElementById('area').classList.remove('read-mode');
  agentItems = {}; agentPages = {}; agentLoaded.clear();
  renderPage();
}

// ── Search ────────────────────────────────────────────────────────────────────
function onSearch(q) {
  S.q = q;
  clearTimeout(S.timer);
  if (!_agentPageAid) return;
  const tab = _agentDetailTab;
  if (tab === 'history' || tab === 'daily') return;
  S.timer = setTimeout(async () => {
    delete _agentDetailItems[tab];
    await loadDetailTab(tab);
  }, 380);
}

// ── Settings modal ────────────────────────────────────────────────────────────
function openSettingsByAid(aid, event) {
  if (event) event.stopPropagation();
  settingsAid = aid;
  document.getElementById('settingsLbl').textContent = aid;
  document.getElementById('sAvatar').value = '';
  document.getElementById('sModel').value  = '';
  document.getElementById('sChain').value  = '';
  document.getElementById('sNotes').value  = '';
  refreshAvPreview('', aid);

  api(`/admin/api/agents/${enc(aid)}/settings`).then(d => {
    document.getElementById('sAvatar').value = d.avatar    || '';
    document.getElementById('sModel').value  = d.llm_model || '';
    document.getElementById('sChain').value  = d.api_chain || '';
    document.getElementById('sNotes').value  = d.notes     || '';
    refreshAvPreview(d.avatar || '', aid);
    document.getElementById('sSysPrompt').value = d.system_prompt || '';
    const atype = d.agent_type || 'agent';
    document.getElementById('sAgentType').value = atype;
    document.getElementById('sMcpEnabled').checked = d.mcp_enabled !== false;
    document.getElementById('sAutoMemory').checked = !!d.auto_memory;
    const pcfg = d.mcp_proxy_config || {};
    document.getElementById('sProxyCfg').value = Object.keys(pcfg).length
      ? JSON.stringify(pcfg, null, 2) : '';
    onAgentTypeChange(atype);
  }).catch(() => {});

  loadEnvChainForAgent(aid);
  document.getElementById('settingsOv').classList.add('open');
}

// Keep old openSettings signature for compatibility
async function openSettings(i, event) {
  if (event) event.stopPropagation();
  const sec = document.querySelector(`.user-section[data-idx="${i}"]`);
  const aid = (sec && sec.dataset.aid) || allAgents[i];
  openSettingsByAid(aid, null);
}

function closeSettingsOv() { document.getElementById('settingsOv').classList.remove('open'); }
function onAgentTypeChange(val) {
  const g = document.getElementById('sProxyCfgG');
  if (g) g.style.display = val === 'character' ? '' : 'none';
}

function previewAvatar(url) { refreshAvPreview(url.trim(), settingsAid || '?'); }
function clearAvatar() {
  document.getElementById('sAvatar').value = '';
  refreshAvPreview('', settingsAid || '?');
}
function refreshAvPreview(url, aid) {
  const el = document.getElementById('avPreview');
  if (!el) return;
  if (url) {
    el.innerHTML = `<img src="${esc(url)}" style="width:100%;height:100%;object-fit:cover;border-radius:50%">`;
    el.style.background = '';
  } else {
    const i = allAgents.indexOf(aid);
    const bg = i >= 0 ? (S.night ? AV_NIGHT[i % AV_NIGHT.length] : AV_DAY[i % AV_DAY.length]) : '#ddd';
    el.innerHTML = ([...aid][0]?.toUpperCase() || '?');
    el.style.background = bg;
  }
}
function loadAvatarFile(input) {
  const file = input.files[0];
  if (!file) return;
  if (file.size > 800 * 1024) { toast('Image too large (max 800 KB)'); return; }
  const reader = new FileReader();
  reader.onload = e => {
    document.getElementById('sAvatar').value = e.target.result;
    refreshAvPreview(e.target.result, settingsAid || '?');
  };
  reader.readAsDataURL(file);
}

async function saveSettings() {
  if (!settingsAid) return;
  let mcp_proxy_config = {};
  try { mcp_proxy_config = JSON.parse(document.getElementById('sProxyCfg').value || '{}'); } catch {}
  const body = {
    llm_model:        document.getElementById('sModel').value.trim(),
    api_chain:        document.getElementById('sChain').value.trim(),
    notes:            document.getElementById('sNotes').value.trim(),
    avatar:           document.getElementById('sAvatar').value.trim(),
    agent_type:       document.getElementById('sAgentType').value,
    mcp_enabled:      document.getElementById('sMcpEnabled').checked,
    auto_memory:      document.getElementById('sAutoMemory').checked,
    mcp_proxy_config,
    system_prompt:    document.getElementById('sSysPrompt').value,
  };
  try {
    await api(`/admin/api/agents/${enc(settingsAid)}/settings`, { method:'POST', body });
    sessionStorage.removeItem(`av_${settingsAid}`);
    toast('Settings saved');
    closeSettingsOv();
    const i = allAgents.indexOf(settingsAid);
    if (i >= 0) applyAvatarEl(i, body.avatar, settingsAid);
    if (_agentPageAid === settingsAid) agentTypes[settingsAid] = body.agent_type;
  } catch(e) { toast('Error: '+e.message); }
}

// ── Distill history ───────────────────────────────────────────────────────────
async function distillHistory() {
  if (!settingsAid) return;
  const btn = document.getElementById('distillBtn');
  const msg = document.getElementById('distillMsg');
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = 'Running…';
  try {
    const d = await api(`/admin/api/agents/${enc(settingsAid)}/distill-history`, { method:'POST' });
    const txt = `✓ ${d.processed} convs, +${d.memories_added} memories`;
    if (msg) msg.textContent = txt;
    toast(txt);
    loadGlobalStats();
    // Refresh current detail page if open for this agent
    if (_agentPageAid === settingsAid) {
      delete _agentDetailItems[_agentDetailTab];
      await loadDetailTab(_agentDetailTab);
    }
  } catch(e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
    toast('Distill error: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteAgentSettings() {
  if (!settingsAid || !confirm(`Delete settings for "${settingsAid}"?\n(Memory data is kept)`)) return;
  try {
    await api(`/admin/api/agents/${enc(settingsAid)}/settings`, { method:'DELETE' });
    sessionStorage.removeItem(`av_${settingsAid}`);
    toast('Settings deleted');
    closeSettingsOv();
  } catch(e) { toast('Error: '+e.message); }
}

// ── New agent modal ───────────────────────────────────────────────────────────
let _newAgentType = 'agent';

function setNewType(type) {
  _newAgentType = type;
  const btnA = document.getElementById('newTypeAgent');
  const btnC = document.getElementById('newTypeChar');
  if (!btnA || !btnC) return;
  btnA.className = type === 'agent'     ? 'btn btn-p' : 'btn btn-g';
  btnC.className = type === 'character' ? 'btn btn-p' : 'btn btn-g';
  [btnA, btnC].forEach(b => { b.style.flex='1'; b.style.fontSize='11px'; b.style.padding='8px'; });
}

function openNewAgentModal() {
  const defaultType = S.tab === 'character' ? 'character' : 'agent';
  _newAgentType = defaultType;
  document.getElementById('newAid').value   = '';
  document.getElementById('newNotes').value = '';
  document.getElementById('newAgentOv').classList.add('open');
  setTimeout(() => { setNewType(defaultType); document.getElementById('newAid').focus(); }, 50);
}
function closeNewAgentOv() { document.getElementById('newAgentOv').classList.remove('open'); }

async function createAgent() {
  const aid   = document.getElementById('newAid').value.trim();
  const notes = document.getElementById('newNotes').value.trim();
  if (!aid) return;
  try {
    await api(`/admin/api/agents/${enc(aid)}/settings`, {
      method:'POST', body:{
        api_source:'nvidia', llm_model:'', notes, avatar:'',
        agent_type: _newAgentType,
        mcp_enabled: _newAgentType === 'agent',
      }
    });
    toast(`${_newAgentType === 'character' ? 'Character' : 'Agent'} "${aid}" created`);
    closeNewAgentOv();
    await loadAgents();
  } catch(e) { toast('Error: '+e.message); }
}

// ── Add / edit memory modal ───────────────────────────────────────────────────
function openAddModal() {
  editId = null; editCol = null; editAid = null;
  document.getElementById('memTitle').textContent = 'Add Memory';

  // Pre-select collection based on current detail tab
  const tabToCol = { l1:'memory_profile', l2:'memory_project', l3:'memory_recent' };
  const defaultCol = tabToCol[_agentDetailTab] || 'memory_profile';
  document.getElementById('memCol').value = defaultCol;
  document.getElementById('memCol').disabled = false;
  document.getElementById('memTxt').value = '';

  // Pre-select agent if in detail page
  const userGroup = document.getElementById('memUserG');
  const sel = document.getElementById('memUser');
  if (_agentPageAid) {
    userGroup.style.display = 'none';
    sel.innerHTML = `<option value="${esc(_agentPageAid)}">${esc(_agentPageAid)}</option>`;
    sel.value = _agentPageAid;
  } else {
    userGroup.style.display = '';
    sel.innerHTML = allAgents.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
  }

  document.getElementById('memOv').classList.add('open');
  setTimeout(() => document.getElementById('memTxt').focus(), 50);
}


// ── Auto-classify memory tier ─────────────────────────────────────────────────
async function classifyMemory() {
  const text = document.getElementById('memTxt').value.trim();
  const btn  = document.getElementById('classifyBtn');
  const res  = document.getElementById('classifyResult');
  if (!text) { res.textContent = '请先输入内容'; res.style.color = 'var(--muted)'; return; }
  btn.disabled = true; btn.textContent = '⏳';
  res.textContent = '分析中…'; res.style.color = 'var(--muted)';
  const aid = (document.getElementById('memUser') || {}).value || _agentPageAid || '';
  try {
    const r = await api('/admin/api/memories/classify', {
      method: 'POST', body: { text, agent_id: aid }
    });
    document.getElementById('memCol').value = r.collection;
    const icons = { l1:'◉', l2:'◈', l3:'◑' };
    res.innerHTML = `<span style="color:var(--text)">${icons[r.tier]||''} ${esc(r.label)}</span>` +
                    `<span style="color:var(--muted);margin-left:6px">${esc(r.reason)}</span>`;
    res.style.color = '';
  } catch(e) {
    res.textContent = '分类失败: ' + e.message; res.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false; btn.textContent = '🤖 分类';
  }
}

function openEditModal(id) {
  const it = CACHE[id]; if (!it) return;
  editId = id; editCol = it._col; editAid = it._aid;
  document.getElementById('memTitle').textContent = 'Edit Memory';
  document.getElementById('memCol').value = it._col;
  document.getElementById('memCol').disabled = true;
  document.getElementById('memTxt').value = it.text || '';
  document.getElementById('memUserG').style.display = 'none';
  document.getElementById('memOv').classList.add('open');
  setTimeout(() => document.getElementById('memTxt').focus(), 50);
}
function closeMemOv() { document.getElementById('memOv').classList.remove('open'); }

async function saveMemory() {
  const text = document.getElementById('memTxt').value.trim();
  const col  = document.getElementById('memCol').value;
  if (!text) return;
  try {
    if (editId) {
      await api(`/admin/api/memories/${editId}`, { method:'PUT', body:{ collection:editCol, text } });
      toast('Updated');
      if (_agentPageAid && editAid === _agentPageAid) {
        delete _agentDetailItems[_agentDetailTab];
        await loadDetailTab(_agentDetailTab);
      }
    } else {
      const aid = document.getElementById('memUser').value || _agentPageAid;
      await api('/admin/api/memories', { method:'POST', body:{ collection:col, text, agent_id:aid } });
      toast('Added');
      if (_agentPageAid === aid) {
        // Refresh the relevant tier tab
        const colToTab = { memory_profile:'l1', memory_project:'l2', memory_recent:'l3' };
        const targetTab = colToTab[col];
        if (targetTab) {
          delete _agentDetailItems[targetTab];
          if (_agentDetailTab === targetTab) await loadDetailTab(targetTab);
        }
      }
    }
    closeMemOv();
    loadGlobalStats();
  } catch(e) { toast('Error: '+e.message); }
}

async function delMemory(id, col) {
  if (!confirm('Delete this memory?')) return;
  const aid = CACHE[id]?._aid;
  try {
    await api(`/admin/api/memories/${id}?collection=${col}`, { method:'DELETE' });
    toast('Deleted');
    loadGlobalStats();
    if (_agentPageAid && aid === _agentPageAid) {
      delete _agentDetailItems[_agentDetailTab];
      await loadDetailTab(_agentDetailTab);
    }
  } catch(e) { toast('Error: '+e.message); }
}
