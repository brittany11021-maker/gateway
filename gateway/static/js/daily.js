/* daily.js – Daily Life journal + Character State + Random Events + NPC (P1) */

const MOOD_EMOJI = {
  happy:   '😊', neutral: '😐', sad:     '😢',
  excited: '🌟', tired:   '😴', anxious: '😟', calm: '🌿',
};
const LEVEL_EMOJI  = { green: '🟢', yellow: '🟡', orange: '🟠', red: '🔴' };
const REL_EMOJI    = { romantic: '💕', friend: '👥', family: '🏠', acquaintance: '🤝', rival: '⚔️' };

// ── Tab router ─────────────────────────────────────────────────────────────
let _dailySubTab = 'journal';

async function loadDailyTab() {
  document.getElementById('area').innerHTML = '<div class="page-loading">Loading…</div>';
  document.getElementById('area').classList.remove('read-mode');
  _renderDailyShell();
  _switchDailySub(_dailySubTab);
}

function _renderDailyShell() {
  document.getElementById('area').innerHTML = `
    <div class="d-tabs">
      <button class="d-tab" id="dtab-journal"  onclick="_switchDailySub('journal')">📔 Journal</button>
      <button class="d-tab" id="dtab-state"    onclick="_switchDailySub('state')">🎭 State</button>
      <button class="d-tab" id="dtab-events"   onclick="_switchDailySub('events')">🎲 Events</button>
      <button class="d-tab" id="dtab-npcs"     onclick="_switchDailySub('npcs')">👥 NPCs</button>
    </div>
    <div id="daily-sub"></div>`;
}

function _switchDailySub(tab) {
  _dailySubTab = tab;
  ['journal','state','events','npcs'].forEach(t => {
    document.getElementById('dtab-'+t)?.classList.toggle('active', t === tab);
  });
  const sub = document.getElementById('daily-sub');
  if (!sub) return;
  sub.innerHTML = '<div class="page-loading">Loading…</div>';
  if (tab === 'journal') _loadJournal();
  else if (tab === 'state')   _loadState();
  else if (tab === 'events')  _loadEvents();
  else if (tab === 'npcs')    _loadNpcs();
}

// ── Journal ────────────────────────────────────────────────────────────────
async function _loadJournal() {
  try {
    const data = await apiFetch('/admin/api/daily?limit=60');
    const events = data.events || [];
    document.getElementById('n-daily').textContent = events.length;
    document.getElementById('daily-sub').innerHTML = _buildJournalPanel(events);
  } catch (e) {
    document.getElementById('daily-sub').innerHTML =
      `<div class="page-loading" style="color:var(--danger)">Error: ${e}</div>`;
  }
}

function _buildJournalPanel(events) {
  const genForm = `
    <div class="daily-toolbar">
      <div class="daily-gen-group">
        <input class="daily-input" id="dailyAgentId" placeholder="agent_id" value="default">
        <input class="daily-input" id="dailyDate" placeholder="YYYY-MM-DD (today)" style="width:140px">
        <input class="daily-input" id="dailyExtraPrompt" placeholder="Extra guidance…" style="flex:2">
        <button class="btn btn-s btn-accent" onclick="triggerDailyGenerate()">✨ Generate</button>
      </div>
      <div id="dailyGenResult" class="daily-gen-result" style="display:none"></div>
    </div>`;
  if (!events.length)
    return genForm + '<div class="daily-empty">No journal entries yet.</div>';
  const byDate = {};
  for (const e of events) (byDate[e.date] = byDate[e.date] || []).push(e);
  const days = Object.keys(byDate).sort().reverse().map(date => {
    return `<div class="daily-day">
      <div class="daily-day-header">${esc(date)}</div>
      ${byDate[date].map(buildDailyCard).join('')}
    </div>`;
  }).join('');
  return genForm + `<div class="daily-feed">${days}</div>`;
}

function buildDailyCard(e) {
  const emoji = MOOD_EMOJI[e.mood] || '😐';
  const srcMap = { auto: '🤖', manual: '✏️', auto_extract: '🧠', random_event: '🎲' };
  const src = srcMap[e.source] || '📝';
  return `<div class="daily-card" id="daily-${esc(e.id)}">
    <div class="daily-card-header">
      <span class="daily-mood">${emoji} ${esc(e.mood)}</span>
      ${e.time_of_day ? `<span class="daily-time">${esc(e.time_of_day)}</span>` : ''}
      <span class="daily-source" title="${esc(e.source)}">${src}</span>
      <button class="btn-icon daily-del" onclick="deleteDaily('${esc(e.id)}')" title="Delete">✕</button>
    </div>
    <div class="daily-summary">${esc(e.summary)}</div>
    ${e.carry_over ? `<div class="daily-carryover">→ ${esc(e.carry_over)}</div>` : ''}
  </div>`;
}

async function triggerDailyGenerate() {
  const agentId     = document.getElementById('dailyAgentId')?.value.trim() || 'default';
  const date        = document.getElementById('dailyDate')?.value.trim() || '';
  const extraPrompt = document.getElementById('dailyExtraPrompt')?.value.trim() || '';
  const resultEl    = document.getElementById('dailyGenResult');
  if (resultEl) { resultEl.style.display='block'; resultEl.className='daily-gen-result info'; resultEl.textContent='✨ Generating…'; }
  try {
    const r = await apiFetch('/admin/api/mcp/daily-generate', {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId, date, extra_prompt: extraPrompt }),
    });
    if (resultEl) { resultEl.className='daily-gen-result ok'; resultEl.textContent='✓ '+(r.result||'Generated'); }
    setTimeout(_loadJournal, 800);
  } catch (e) {
    if (resultEl) { resultEl.className='daily-gen-result err'; resultEl.textContent='✗ '+e; }
  }
}

async function deleteDaily(id) {
  if (!confirm('Delete this journal entry?')) return;
  try {
    await apiFetch('/admin/api/daily/'+id, { method:'DELETE' });
    document.getElementById('daily-'+id)?.remove();
  } catch (e) { alert('Delete failed: '+e); }
}

// ── Character State ────────────────────────────────────────────────────────
async function _loadState() {
  const sub = document.getElementById('daily-sub');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">🎭 Character State
        <input class="daily-input" id="stateAgentId" placeholder="agent_id" value="default" style="width:140px;margin-left:12px">
        <button class="btn btn-s btn-g" onclick="_fetchState()">Load</button>
      </div>
      <div id="stateBody" style="margin-top:14px"></div>
    </div>`;
  _fetchState();
}

async function _fetchState() {
  const aid = document.getElementById('stateAgentId')?.value.trim() || 'default';
  const body = document.getElementById('stateBody');
  if (!body) return;
  body.innerHTML = '<div class="page-loading" style="font-size:11px">Loading…</div>';
  try {
    const s = await apiFetch(`/admin/api/character-state/${encodeURIComponent(aid)}`);
    body.innerHTML = _buildStateForm(s);
  } catch(e) { body.innerHTML = `<span style="color:var(--danger);font-size:11px">${e}</span>`; }
}

function _buildStateForm(s) {
  const moodPct = ((s.mood_score + 100) / 200 * 100).toFixed(0);
  const fatPct  = s.fatigue;
  return `
    <div class="state-grid">
      <div class="state-field">
        <label class="form-lbl">Mood Score <span style="opacity:.5">(-100…+100)</span></label>
        <div style="display:flex;align-items:center;gap:10px">
          <input type="range" id="sMoodScore" min="-100" max="100" value="${s.mood_score}"
            oninput="document.getElementById('sMoodVal').textContent=this.value"
            style="flex:1;accent-color:var(--accent)">
          <span id="sMoodVal" style="min-width:36px;text-align:right;font-size:12px;font-weight:600">${s.mood_score}</span>
        </div>
      </div>
      <div class="state-field">
        <label class="form-lbl">Mood Label</label>
        <select class="form-sel" id="sMoodLabel">
          ${['happy','neutral','sad','excited','tired','anxious','calm'].map(m =>
            `<option value="${m}" ${s.mood_label===m?'selected':''}>${m}</option>`).join('')}
        </select>
      </div>
      <div class="state-field">
        <label class="form-lbl">Fatigue <span style="opacity:.5">(0=fresh … 100=exhausted)</span></label>
        <div style="display:flex;align-items:center;gap:10px">
          <input type="range" id="sFatigue" min="0" max="100" value="${s.fatigue}"
            oninput="document.getElementById('sFatVal').textContent=this.value"
            style="flex:1;accent-color:#f59e0b">
          <span id="sFatVal" style="min-width:28px;text-align:right;font-size:12px;font-weight:600">${s.fatigue}</span>
        </div>
      </div>
      <div class="state-field">
        <label class="form-lbl">Scene</label>
        <select class="form-sel" id="sScene">
          <option value="daily" ${s.scene==='daily'?'selected':''}>daily</option>
          <option value="long_distance" ${s.scene==='long_distance'?'selected':''}>long_distance</option>
          <option value="cohabitation" ${s.scene==='cohabitation'?'selected':''}>cohabitation</option>
        </select>
      </div>
      <div class="state-field" style="grid-column:span 2">
        <label class="form-lbl">Scene Note</label>
        <input class="form-in" id="sSceneNote" value="${esc(s.scene_note||'')}" placeholder="e.g. traveling for work this week">
      </div>
      <div class="state-field">
        <label class="form-lbl">Cooldown <span style="opacity:.5">(minutes, 0=off)</span></label>
        <input class="form-in" id="sCooldown" type="number" min="0" value="${s.cooldown_minutes||0}" style="width:100px">
      </div>
      <div class="state-field" style="grid-column:span 2">
        <label class="form-lbl">Cooldown Message <span style="opacity:.5">(留空则自动生成回应)</span></label>
        <input class="form-in" id="sCooldownMsg" value="${esc(s.cooldown_message||'')}"
          placeholder="e.g. 我去睡觉了，明天再聊" style="width:100%">
      </div>
      <div class="state-field" style="display:flex;align-items:flex-end">
        <button class="btn btn-p" onclick="_saveState()">Save State</button>
      </div>
    </div>
    <div style="font-size:10px;color:var(--muted);margin-top:8px">Last active: ${esc(s.last_active||'—')}</div>`;
}

async function _saveState() {
  const aid = document.getElementById('stateAgentId')?.value.trim() || 'default';
  const body = {
    mood_score:       parseInt(document.getElementById('sMoodScore').value),
    mood_label:       document.getElementById('sMoodLabel').value,
    fatigue:          parseInt(document.getElementById('sFatigue').value),
    scene:            document.getElementById('sScene').value,
    scene_note:       document.getElementById('sSceneNote').value.trim(),
    cooldown_minutes: parseInt(document.getElementById('sCooldown').value)||0,
    cooldown_message: document.getElementById('sCooldownMsg').value.trim(),
  };
  try {
    await apiFetch(`/admin/api/character-state/${encodeURIComponent(aid)}`,
      { method:'POST', body: JSON.stringify(body) });
    toast('State saved');
  } catch(e) { toast('Error: '+e); }
}

// ── Random Events ──────────────────────────────────────────────────────────
async function _loadEvents() {
  const sub = document.getElementById('daily-sub');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">🎲 Random Events Pool
        <button class="btn btn-s btn-g" style="margin-left:auto" onclick="_rollEvent()">🎲 Roll</button>
        <button class="btn btn-s btn-accent" onclick="_showAddEvent()">+ Add</button>
      </div>
      <div id="rollResult" style="display:none;margin:8px 0;padding:10px;border-radius:8px;background:var(--ghost-bg);font-size:12px"></div>
      <div id="addEventForm" style="display:none;margin:10px 0;padding:12px;border:1px solid var(--border);border-radius:10px">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
          <div style="flex:3;min-width:200px">
            <label class="form-lbl">Content</label>
            <input class="form-in" id="newEvtContent" placeholder="Event description…">
          </div>
          <div>
            <label class="form-lbl">Level</label>
            <select class="form-sel" id="newEvtLevel" style="width:110px">
              <option value="green">🟢 green</option>
              <option value="yellow">🟡 yellow</option>
              <option value="orange">🟠 orange</option>
              <option value="red">🔴 red</option>
            </select>
          </div>
          <div>
            <label class="form-lbl">Weight</label>
            <input class="form-in" id="newEvtWeight" type="number" min="0.1" step="0.5" value="1" style="width:70px">
          </div>
          <div>
            <label class="form-lbl">Agent (blank=global)</label>
            <input class="form-in" id="newEvtAgent" placeholder="agent_id" style="width:120px">
          </div>
          <button class="btn btn-p" onclick="_addEvent()">Add</button>
          <button class="btn btn-g" onclick="_hideAddEvent()">Cancel</button>
        </div>
      </div>
      <div id="eventsList" style="margin-top:8px"></div>
    </div>`;
  _fetchEvents();
}

async function _fetchEvents() {
  const el = document.getElementById('eventsList');
  if (!el) return;
  try {
    const data = await apiFetch('/admin/api/random-events');
    const evts = data.events || [];
    if (!evts.length) { el.innerHTML = '<div class="daily-empty">No events in pool.</div>'; return; }
    const byLevel = {};
    for (const e of evts) (byLevel[e.level] = byLevel[e.level]||[]).push(e);
    el.innerHTML = ['green','yellow','orange','red'].map(lvl => {
      if (!byLevel[lvl]) return '';
      const items = byLevel[lvl].map(e => `
        <div class="evt-row">
          <span class="evt-lvl">${LEVEL_EMOJI[lvl]||''}</span>
          <span class="evt-content">${esc(e.content)}</span>
          <span class="evt-weight" title="weight">${e.weight}×</span>
          ${e.agent_id ? `<span class="evt-agent">${esc(e.agent_id)}</span>` : ''}
          <button class="btn-icon" onclick="_deleteEvent('${esc(e.id)}')" title="Delete">✕</button>
        </div>`).join('');
      return `<div class="evt-group"><div class="evt-group-head">${LEVEL_EMOJI[lvl]} ${lvl} (${byLevel[lvl].length})</div>${items}</div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<span style="color:var(--danger);font-size:11px">${e}</span>`; }
}

function _showAddEvent() { document.getElementById('addEventForm').style.display=''; }
function _hideAddEvent() { document.getElementById('addEventForm').style.display='none'; }

async function _addEvent() {
  const content = document.getElementById('newEvtContent').value.trim();
  if (!content) return toast('Content required');
  try {
    await apiFetch('/admin/api/random-events', { method:'POST', body: JSON.stringify({
      content, level: document.getElementById('newEvtLevel').value,
      weight: parseFloat(document.getElementById('newEvtWeight').value)||1,
      agent_id: document.getElementById('newEvtAgent').value.trim(),
    })});
    _hideAddEvent(); _fetchEvents(); toast('Event added');
  } catch(e) { toast('Error: '+e); }
}

async function _deleteEvent(id) {
  if (!confirm('Delete this event?')) return;
  try {
    await apiFetch('/admin/api/random-events/'+id, {method:'DELETE'});
    _fetchEvents();
  } catch(e) { toast('Error: '+e); }
}

async function _rollEvent() {
  const el = document.getElementById('rollResult');
  if (!el) return;
  try {
    const e = await apiFetch('/admin/api/random-events/roll', {method:'POST', body:'{}'});
    el.style.display='';
    el.innerHTML = `${LEVEL_EMOJI[e.level]||''} <strong>[${e.level}]</strong> ${esc(e.content)}`;
  } catch(e) { toast('No events to roll'); }
}

// ── NPCs ───────────────────────────────────────────────────────────────────
async function _loadNpcs() {
  const sub = document.getElementById('daily-sub');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">👥 Social Network
        <input class="daily-input" id="npcAgentId" placeholder="agent_id" value="default" style="width:140px;margin-left:12px">
        <button class="btn btn-s btn-g" onclick="_fetchNpcs()">Load</button>
        <button class="btn btn-s btn-accent" style="margin-left:4px" onclick="_showAddNpc()">+ Add NPC</button>
      </div>
      <div id="addNpcForm" style="display:none;margin:10px 0;padding:12px;border:1px solid var(--border);border-radius:10px">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
          <div><label class="form-lbl">Name</label>
            <input class="form-in" id="npcName" placeholder="Name"></div>
          <div><label class="form-lbl">Relationship</label>
            <select class="form-sel" id="npcRel" style="width:140px">
              ${['acquaintance','friend','family','romantic','rival'].map(r=>
                `<option value="${r}">${REL_EMOJI[r]||''} ${r}</option>`).join('')}
            </select></div>
          <div><label class="form-lbl">Affinity (-100…+100)</label>
            <input class="form-in" id="npcAffinity" type="number" min="-100" max="100" value="0" style="width:80px"></div>
          <div style="flex:2;min-width:160px"><label class="form-lbl">Notes</label>
            <input class="form-in" id="npcNotes" placeholder="Brief description…"></div>
          <button class="btn btn-p" onclick="_saveNpc()">Save</button>
          <button class="btn btn-g" onclick="_hideAddNpc()">Cancel</button>
        </div>
      </div>
      <div id="npcsList" style="margin-top:10px"></div>
    </div>`;
  _fetchNpcs();
}

function _showAddNpc() { document.getElementById('addNpcForm').style.display=''; }
function _hideAddNpc() { document.getElementById('addNpcForm').style.display='none'; }

async function _fetchNpcs() {
  const aid = document.getElementById('npcAgentId')?.value.trim()||'default';
  const el = document.getElementById('npcsList');
  if (!el) return;
  try {
    const data = await apiFetch(`/admin/api/npcs/${encodeURIComponent(aid)}`);
    const npcs = data.npcs || [];
    if (!npcs.length) { el.innerHTML = '<div class="daily-empty">No NPCs yet.</div>'; return; }
    el.innerHTML = npcs.map(n => {
      const af = n.affinity >= 0 ? '+'+n.affinity : ''+n.affinity;
      const afColor = n.affinity > 30 ? 'var(--ok)' : n.affinity < -30 ? 'var(--danger)' : 'var(--muted)';
      return `<div class="npc-row">
        <span class="npc-rel">${REL_EMOJI[n.relationship]||'👤'}</span>
        <span class="npc-name">${esc(n.name)}</span>
        <span class="npc-rel-lbl">${esc(n.relationship)}</span>
        <span style="font-size:11px;font-weight:700;color:${afColor}">${af}</span>
        <span class="npc-notes">${esc(n.notes||'—')}</span>
        <button class="btn-icon" onclick="_deleteNpc('${esc(aid)}','${esc(n.name)}')" title="Delete">✕</button>
      </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<span style="color:var(--danger);font-size:11px">${e}</span>`; }
}

async function _saveNpc() {
  const aid = document.getElementById('npcAgentId')?.value.trim()||'default';
  const body = {
    name:         document.getElementById('npcName').value.trim(),
    relationship: document.getElementById('npcRel').value,
    affinity:     parseInt(document.getElementById('npcAffinity').value)||0,
    notes:        document.getElementById('npcNotes').value.trim(),
  };
  if (!body.name) return toast('Name required');
  try {
    await apiFetch(`/admin/api/npcs/${encodeURIComponent(aid)}`,
      { method:'POST', body: JSON.stringify(body) });
    _hideAddNpc(); _fetchNpcs(); toast('NPC saved');
  } catch(e) { toast('Error: '+e); }
}

async function _deleteNpc(aid, name) {
  if (!confirm(`Remove ${name}?`)) return;
  try {
    await apiFetch(`/admin/api/npcs/${encodeURIComponent(aid)}/${encodeURIComponent(name)}`,
      {method:'DELETE'});
    _fetchNpcs();
  } catch(e) { toast('Error: '+e); }
}

function esc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
