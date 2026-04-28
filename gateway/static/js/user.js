// ── User Profile Tab ─────────────────────────────────────────────────────────
// Manages user_profiles table via /admin/api/user-profiles

let _upCurrentAid = '';

async function loadUserProfileTab() {
  document.getElementById('area').classList.remove('read-mode');
  setArea('<div class="page-loading">Loading\u2026</div>');

  try {
    const [agentsResp, profilesResp] = await Promise.all([
      api('/admin/api/agents'),
      api('/admin/api/user-profiles'),
    ]);
    const agents   = agentsResp.agents || [];
    const profiles = {};
    (profilesResp.profiles || []).forEach(p => { profiles[p.agent_id] = p; });

    // Build agent selector options (global '' first, then all agents)
    const opts = [{ id: '', label: '(global default)' }, ...agents.map(a => ({ id: a, label: a }))];

    const html = `
<div style="max-width:640px;margin:0 auto;padding:24px 16px">
  <div style="font-size:11px;color:var(--muted);margin-bottom:18px;line-height:1.6">
    User profiles inject the user's name and context into every conversation for a specific agent,
    or globally (agent = global default). Agent-specific profiles take priority over the global default.
  </div>

  <div class="form-g" style="margin-bottom:20px">
    <label class="form-lbl">Agent</label>
    <select class="form-sel" id="upAgentSel" onchange="loadUserProfileFor(this.value)">
      ${opts.map(o => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('')}
    </select>
  </div>

  <div id="upFormArea"></div>
</div>`;

    setArea(html);
    // Load global default on start
    await loadUserProfileFor('');
  } catch(e) {
    setArea(`<div class="u-empty">Error: ${e.message}</div>`);
  }
}

async function loadUserProfileFor(aid) {
  _upCurrentAid = aid;
  const area = document.getElementById('upFormArea');
  if (!area) return;
  area.innerHTML = '<div class="u-loading">Loading\u2026</div>';

  try {
    const key = aid === '' ? '__global__' : enc(aid);
    const d = await api(`/admin/api/user-profiles/${aid === '' ? '__global__' : enc(aid)}`);
    renderUserProfileForm(d, aid);
  } catch(e) {
    area.innerHTML = `<div class="u-empty">Error: ${e.message}</div>`;
  }
}

function renderUserProfileForm(d, aid) {
  const area = document.getElementById('upFormArea');
  if (!area) return;
  const hasData = d && (d.user_name || d.content);
  area.innerHTML = `
<div style="border:1px solid var(--border);border-radius:12px;padding:20px;background:var(--card-bg,var(--ghost-bg))">
  <div class="form-g">
    <label class="form-lbl">User Name <span style="opacity:.5;font-weight:400">(called in messages as \u7528\u6237\u540d\u5b57)</span></label>
    <input class="form-in" id="upUserName" placeholder="e.g. \u59d0\u59d0 / Alex" value="${esc(d.user_name || '')}">
  </div>
  <div class="form-g">
    <label class="form-lbl">Profile Content <span style="opacity:.5;font-weight:400">(injected as system context before worldbook)</span></label>
    <textarea class="form-ta" id="upContent" style="min-height:140px;font-size:12px"
      placeholder="Describe the user: personality, preferences, relationship to the character, etc.">${esc(d.content || '')}</textarea>
  </div>
  <div class="form-g" style="display:flex;gap:20px;align-items:center">
    <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px">
      <input type="checkbox" id="upEnabled" style="width:14px;height:14px" ${(d.enabled !== false) ? 'checked' : ''}>
      <span>Enabled</span>
    </label>
    <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px">
      <input type="checkbox" id="upConstant" style="width:14px;height:14px" ${(d.constant !== false) ? 'checked' : ''}>
      <span>Constant <span style="opacity:.5">(always inject, not vector-searched)</span></span>
    </label>
  </div>
  <div style="display:flex;gap:10px;margin-top:20px;align-items:center">
    <button class="btn btn-p" onclick="saveUserProfile()">Save</button>
    ${hasData ? `<button class="btn btn-d" onclick="deleteUserProfile()">Delete</button>` : ''}
    <span id="upMsg" style="font-size:11px;color:var(--muted)"></span>
  </div>
</div>`;
}

async function saveUserProfile() {
  const aid = _upCurrentAid;
  const body = {
    user_name: document.getElementById('upUserName').value.trim(),
    content:   document.getElementById('upContent').value.trim(),
    enabled:   document.getElementById('upEnabled').checked,
    constant:  document.getElementById('upConstant').checked,
    priority:  1,
  };
  const msg = document.getElementById('upMsg');
  try {
    const key = aid === '' ? '__global__' : enc(aid);
    await api(`/admin/api/user-profiles/${aid === '' ? '__global__' : enc(aid)}`, { method:'POST', body });
    if (msg) { msg.textContent = '\u2713 Saved'; setTimeout(() => { if(msg) msg.textContent=''; }, 2000); }
    toast('User profile saved');
    // Update n-user count
    try {
      const r = await api('/admin/api/user-profiles');
      const n = document.getElementById('n-user');
      if (n) n.textContent = (r.profiles || []).length;
    } catch {}
  } catch(e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
    toast('Error: ' + e.message);
  }
}

async function deleteUserProfile() {
  if (!confirm('Delete this user profile?')) return;
  const aid = _upCurrentAid;
  const msg = document.getElementById('upMsg');
  try {
    const key = aid === '' ? '__global__' : enc(aid);
    await api(`/admin/api/user-profiles/${aid === '' ? '__global__' : enc(aid)}`, { method:'DELETE' });
    toast('Profile deleted');
    await loadUserProfileFor(aid);
  } catch(e) {
    if (msg) msg.textContent = 'Error: ' + e.message;
  }
}
