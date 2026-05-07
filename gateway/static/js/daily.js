/* daily.js – Daily Life journal + Character State + Random Events + NPC (P1) */

const MOOD_EMOJI = {
  happy:   '😊', neutral: '😐', sad:     '😢',
  excited: '🌟', tired:   '😴', anxious: '😟', calm: '🌿',
};
const LEVEL_EMOJI  = { green: '🟢', yellow: '🟡', orange: '🟠', red: '🔴' };
const REL_EMOJI    = { romantic: '💕', friend: '👥', family: '🏠', acquaintance: '🤝', rival: '⚔️' };

// ── Tab router ─────────────────────────────────────────────────────────────
let _dailySubTab     = 'journal';
let _journalAgentId  = '';
let _evtAgentId      = '';   // '' = all agents
let _evtScene        = '';   // '' = all scenes
let _skeletonAgentId = '';
let _screenTimeAgentId = '';

// Only character-type agents use the daily system
function _charAgents() {
  if (typeof allAgents === 'undefined') return [];
  if (typeof agentTypes === 'undefined' || !Object.keys(agentTypes).length) return allAgents;
  return allAgents.filter(a => agentTypes[a] === 'character');
}

async function loadDailyTab() {
  document.getElementById('area').innerHTML = '<div class="page-loading">Loading…</div>';
  document.getElementById('area').classList.remove('read-mode');
  _renderDailyShell();
  _switchDailySub(_dailySubTab);
}

function _renderDailyShell() {
  document.getElementById('area').innerHTML = `
    <div class="d-tabs">
      <button class="d-tab" id="dtab-journal"    onclick="_switchDailySub('journal')">📔 Journal</button>
      <button class="d-tab" id="dtab-state"      onclick="_switchDailySub('state')">🎭 State</button>
      <button class="d-tab" id="dtab-events"     onclick="_switchDailySub('events')">🎲 Events</button>
      <button class="d-tab" id="dtab-npcs"       onclick="_switchDailySub('npcs')">👥 NPCs</button>
      <button class="d-tab" id="dtab-skeleton"   onclick="_switchDailySub('skeleton')">🗓 Skeleton</button>
      <button class="d-tab" id="dtab-screentime" onclick="_switchDailySub('screentime')">📱 Screen Rules</button>
    </div>
    <div id="daily-sub"></div>`;
}

function _switchDailySub(tab) {
  _dailySubTab = tab;
  ['journal','state','events','npcs','skeleton','screentime'].forEach(t => {
    document.getElementById('dtab-'+t)?.classList.toggle('active', t === tab);
  });
  const sub = document.getElementById('daily-sub');
  if (!sub) return;
  sub.innerHTML = '<div class="page-loading">Loading…</div>';
  if (tab === 'journal')    _loadJournal();
  else if (tab === 'state')      _loadState();
  else if (tab === 'events')     _loadEvents();
  else if (tab === 'npcs')       _loadNpcs();
  else if (tab === 'skeleton')   _loadSkeleton();
  else if (tab === 'screentime') _loadScreenTime();
}

// ── Journal ────────────────────────────────────────────────────────────────
async function _loadJournal() {
  // read agent from live select if it already exists, otherwise keep state
  const sel = document.getElementById('dailyAgentId');
  if (sel) _journalAgentId = sel.value || _journalAgentId;
  if (!_journalAgentId) _journalAgentId = _charAgents()[0] || 'default';
  try {
    const data = await apiFetch(`/admin/api/daily?limit=60&agent_id=${encodeURIComponent(_journalAgentId)}`);
    const events = data.events || [];
    document.getElementById('n-daily').textContent = events.length;
    document.getElementById('daily-sub').innerHTML = _buildJournalPanel(events);
    // restore selection after re-render
    const newSel = document.getElementById('dailyAgentId');
    if (newSel) newSel.value = _journalAgentId;
  } catch (e) {
    document.getElementById('daily-sub').innerHTML =
      `<div class="page-loading" style="color:var(--danger)">Error: ${e}</div>`;
  }
}

function _buildJournalPanel(events) {
  const agents = _charAgents();
  const agentOpts = agents.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
  const genForm = `
    <div class="daily-toolbar">
      <div class="daily-gen-group">
        <select class="daily-input" id="dailyAgentId" style="min-width:120px"
          onchange="_journalAgentId=this.value;_loadJournal()">${agentOpts}</select>
        <input class="daily-input" id="dailyDate" placeholder="YYYY-MM-DD (today)" style="width:140px">
        <input class="daily-input" id="dailyExtraPrompt" placeholder="Extra guidance…" style="flex:2">
        <button class="btn btn-s btn-accent" onclick="triggerDailyGenerate()">✨ Generate</button>
      </div>
      <div id="dailyGenResult" class="daily-gen-result" style="display:none"></div>
    </div>
    <div class="ov" id="dailyEditOv" onclick="if(event.target===this)_closeDailyEdit()">
      <div class="modal" onclick="event.stopPropagation()" style="width:480px">
        <div class="modal-title">Edit Entry</div>
        <div class="form-g">
          <label class="form-lbl">Date</label>
          <input class="form-in" id="deDate" placeholder="YYYY-MM-DD">
        </div>
        <div class="form-g">
          <label class="form-lbl">Mood</label>
          <select class="form-sel" id="deMood">
            ${['happy','neutral','sad','excited','tired','anxious','calm'].map(m =>
              `<option value="${m}">${MOOD_EMOJI[m]||''} ${m}</option>`).join('')}
          </select>
        </div>
        <div class="form-g">
          <label class="form-lbl">Relation Type</label>
          <select class="form-sel" id="deRelType">
            <option value="living_together">🏠 同居 living together</option>
            <option value="long_distance">✈️ 异地 long distance</option>
          </select>
        </div>
        <div class="form-g">
          <label class="form-lbl">Time of Day</label>
          <input class="form-in" id="deTimeOfDay" placeholder="morning / 14:00 / evening">
        </div>
        <div class="form-g">
          <label class="form-lbl">Summary</label>
          <textarea class="form-ta" id="deSummary" style="min-height:100px"></textarea>
        </div>
        <div class="form-g">
          <label class="form-lbl">Carry Over</label>
          <input class="form-in" id="deCarryOver" placeholder="Things to remember tomorrow…">
        </div>
        <div class="modal-acts">
          <button class="btn btn-g" onclick="_closeDailyEdit()">Cancel</button>
          <button class="btn btn-p" onclick="_saveDailyEdit()">Save</button>
        </div>
        <input type="hidden" id="deId">
      </div>
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

const REL_TYPE_LABEL = { living_together: '🏠 同居', long_distance: '✈️ 异地' };

function buildDailyCard(e) {
  const emoji = MOOD_EMOJI[e.mood] || '😐';
  const srcMap = { auto: '🤖', manual: '✏️', auto_extract: '🧠', random_event: '🎲' };
  const src = srcMap[e.source] || '📝';
  const relLabel = REL_TYPE_LABEL[e.relation_type] || '';
  return `<div class="daily-card" id="daily-${esc(e.id)}">
    <div class="daily-card-header">
      <span class="daily-mood">${emoji} ${esc(e.mood)}</span>
      ${e.time_of_day ? `<span class="daily-time">${esc(e.time_of_day)}</span>` : ''}
      ${relLabel ? `<span class="daily-rel-badge daily-rel-${esc(e.relation_type||'')}">${relLabel}</span>` : ''}
      <span class="daily-source" title="${esc(e.source)}">${src}</span>
      <button class="btn-icon" onclick="_openDailyEdit(${JSON.stringify(e)})" title="Edit">✎</button>
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

function _openDailyEdit(e) {
  document.getElementById('deId').value         = e.id;
  document.getElementById('deDate').value       = e.date || '';
  document.getElementById('deMood').value       = e.mood || 'neutral';
  document.getElementById('deRelType').value    = e.relation_type || 'living_together';
  document.getElementById('deTimeOfDay').value  = e.time_of_day || '';
  document.getElementById('deSummary').value    = e.summary || '';
  document.getElementById('deCarryOver').value  = e.carry_over || '';
  document.getElementById('dailyEditOv').classList.add('open');
}

function _closeDailyEdit() {
  document.getElementById('dailyEditOv').classList.remove('open');
}

async function _saveDailyEdit() {
  const id = document.getElementById('deId').value;
  if (!id) return;
  const body = {
    date:          document.getElementById('deDate').value.trim(),
    mood:          document.getElementById('deMood').value,
    relation_type: document.getElementById('deRelType').value,
    time_of_day:   document.getElementById('deTimeOfDay').value.trim(),
    summary:       document.getElementById('deSummary').value.trim(),
    carry_over:    document.getElementById('deCarryOver').value.trim(),
  };
  try {
    await apiFetch('/admin/api/daily/'+id, { method:'PUT', body: JSON.stringify(body) });
    _closeDailyEdit();
    _loadJournal();
    toast('Entry updated ✓');
  } catch (e) { toast('Error: '+e); }
}

// ── Character State ────────────────────────────────────────────────────────
async function _loadState() {
  const sub = document.getElementById('daily-sub');
  const agents = _charAgents();
  const agentOpts = agents.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">🎭 Character State
        <select class="daily-input" id="stateAgentId" style="min-width:120px;margin-left:12px"
          onchange="_fetchState()">${agentOpts}</select>
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
    const resp = await apiFetch(`/admin/api/character-state/${encodeURIComponent(aid)}`);
    // Newer endpoint wraps in {state:…}; older returns flat — handle both
    const s = resp.state || resp;
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
const SCENE_LABEL = { '': '🌐 Global', daily: '📔 日常', long_distance: '✈️ 异地', cohabitation: '🏠 同居' };

const _SP_LABEL = { always:'必发', likely:'大概率', maybe:'可能', rarely:'偶尔', never:'仅内部', threshold:'蓄水池' };
const _SP_COLOR = { always:'#22c55e', likely:'#86efac', maybe:'#fbbf24', rarely:'#94a3b8', never:'#64748b', threshold:'#a78bfa' };
const _WX_OPTS  = ['sunny','clear','cloudy','overcast','light_rain','rain','heavy_rain','thunderstorm','snow','fog','hail'];
const _TOD_OPTS = ['morning','afternoon','evening','night','late_night'];
const _DT_OPTS  = ['workday','weekend','holiday'];
const _MODE_OPTS= ['long_distance','cohabitation'];
const _SZN_OPTS = ['spring','summer','autumn','winter'];

let _allEvts    = [];
let _evtEditId  = null;   // null = new event

async function _loadEvents() {
  const sub    = document.getElementById('daily-sub');
  const agents = _charAgents();
  const agOpts = ['<option value="">🌐 All agents</option>',
    ...agents.map(a=>`<option value="${esc(a)}">${esc(a)}</option>`)].join('');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">🎲 Random Events Pool
        <button class="btn btn-s btn-g" style="margin-left:auto" onclick="_rollEvent()">🎲 Roll</button>
        <button class="btn btn-s btn-accent" onclick="_openEvtModal(null)">+ Add</button>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0 10px">
        <select class="form-sel" id="evtFilterAgent" style="font-size:11px;min-width:120px"
          onchange="_evtAgentId=this.value;_renderEvtList()">${agOpts}</select>
        <div class="tl-filter-pills" id="evtScenePills" style="flex-wrap:wrap">
          ${Object.entries(SCENE_LABEL).map(([v,l]) =>
            `<button class="tl-pill${_evtScene===v?' active':''}"
              onclick="_evtScene='${v}';_renderEvtList();document.querySelectorAll('#evtScenePills .tl-pill').forEach(b=>b.classList.remove('active'));this.classList.add('active')"
            >${l}</button>`).join('')}
        </div>
      </div>
      <div id="rollResult" style="display:none;margin:8px 0;padding:10px;border-radius:8px;background:var(--ghost-bg);font-size:12px"></div>
      <div id="eventsList" style="margin-top:4px"></div>
    </div>
    <!-- Event edit modal -->
    <div class="ov" id="evtModalOv" onclick="if(event.target===this)_closeEvtModal()">
      <div class="modal" onclick="event.stopPropagation()" style="width:600px;max-height:88vh;overflow-y:auto">
        <div class="modal-title" id="evtModalTitle">Add Event</div>

        <!-- ① 基础 -->
        <div class="evt-modal-section">
          <div class="evt-modal-sec-head">📝 基础信息</div>
          <div class="form-g">
            <label class="form-lbl">事件内容</label>
            <input class="form-in" id="emContent" placeholder="e.g. 忘带伞，在便利店躲雨">
          </div>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <div style="flex:1;min-width:100px">
              <label class="form-lbl">Level</label>
              <select class="form-sel" id="emLevel">
                <option value="green">🟢 green</option>
                <option value="yellow">🟡 yellow</option>
                <option value="orange">🟠 orange</option>
                <option value="red">🔴 red</option>
              </select>
            </div>
            <div style="flex:1;min-width:120px">
              <label class="form-lbl">场景</label>
              <select class="form-sel" id="emScene">
                ${Object.entries(SCENE_LABEL).map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}
              </select>
            </div>
            <div style="min-width:70px">
              <label class="form-lbl">Weight</label>
              <input class="form-in" id="emWeight" type="number" min="0.1" step="0.5" value="1" style="width:70px">
            </div>
            <div style="flex:1;min-width:110px">
              <label class="form-lbl">角色</label>
              <select class="form-sel" id="emAgent">${agOpts}</select>
            </div>
          </div>
        </div>

        <!-- ② 发送策略 -->
        <div class="evt-modal-section">
          <div class="evt-modal-sec-head">📤 发送策略</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
            <div style="flex:1;min-width:160px">
              <label class="form-lbl">策略</label>
              <select class="form-sel" id="emSendPolicy" onchange="_evtPolicyChange()">
                ${Object.entries(_SP_LABEL).map(([v,l])=>`<option value="${v}">${l} (${v})</option>`).join('')}
              </select>
            </div>
            <div id="emSendProbWrap" style="min-width:130px">
              <label class="form-lbl">发送概率 <span style="opacity:.5">(0–1)</span></label>
              <input class="form-in" id="emSendProb" type="number" min="0" max="1" step="0.05" value="0.4" style="width:80px">
            </div>
          </div>
          <div style="font-size:10px;color:var(--muted);margin-top:4px">
            always=必发 · likely=70% · maybe=40% · rarely=15% · never=仅改变内部状态 · threshold=蓄水池满才触发
          </div>
        </div>

        <!-- ③ 条件标签 -->
        <div class="evt-modal-section">
          <div class="evt-modal-sec-head">🏷 触发条件 <span style="font-size:10px;font-weight:400;opacity:.6">（空=不限制）</span></div>
          <div style="display:flex;gap:12px;flex-wrap:wrap">
            <div style="flex:1;min-width:200px">
              <label class="form-lbl">天气 weather</label>
              <div class="evt-chips" id="emCondWeather">
                ${_WX_OPTS.map(w=>`<button class="evt-chip" data-v="${w}" onclick="_toggleChip(this)">${w}</button>`).join('')}
              </div>
            </div>
            <div style="flex:1;min-width:180px">
              <label class="form-lbl">时段 time_of_day</label>
              <div class="evt-chips" id="emCondTod">
                ${_TOD_OPTS.map(t=>`<button class="evt-chip" data-v="${t}" onclick="_toggleChip(this)">${t}</button>`).join('')}
              </div>
            </div>
            <div style="min-width:150px">
              <label class="form-lbl">日类型 day_type</label>
              <div class="evt-chips" id="emCondDt">
                ${_DT_OPTS.map(t=>`<button class="evt-chip" data-v="${t}" onclick="_toggleChip(this)">${t}</button>`).join('')}
              </div>
            </div>
            <div style="min-width:160px">
              <label class="form-lbl">关系模式 mode</label>
              <div class="evt-chips" id="emCondMode">
                ${_MODE_OPTS.map(t=>`<button class="evt-chip" data-v="${t}" onclick="_toggleChip(this)">${t}</button>`).join('')}
              </div>
            </div>
            <div style="min-width:200px">
              <label class="form-lbl">季节 season</label>
              <div class="evt-chips" id="emCondSeason">
                ${_SZN_OPTS.map(t=>`<button class="evt-chip" data-v="${t}" onclick="_toggleChip(this)">${t}</button>`).join('')}
              </div>
            </div>
            <div style="min-width:140px">
              <label class="form-lbl">疲劳度 ≥ fatigue_above</label>
              <input class="form-in" id="emCondFatigue" type="number" min="0" max="5" step="1"
                placeholder="空=不限" style="width:80px">
            </div>
          </div>
        </div>

        <!-- ④ 状态影响 -->
        <div class="evt-modal-section">
          <div class="evt-modal-sec-head">💫 状态影响</div>
          <div style="display:flex;gap:12px;flex-wrap:wrap">
            <div>
              <label class="form-lbl">情绪效价 mood.valence <span style="opacity:.5">(-1~+1)</span></label>
              <input class="form-in" id="emMoodValence" type="number" min="-1" max="1" step="0.05" value="0" style="width:80px">
            </div>
            <div>
              <label class="form-lbl">活跃度 mood.energy <span style="opacity:.5">(-1~+1)</span></label>
              <input class="form-in" id="emMoodEnergy" type="number" min="-1" max="1" step="0.05" value="0" style="width:80px">
            </div>
            <div>
              <label class="form-lbl">思念值 miss_you <span style="opacity:.5">(±)</span></label>
              <input class="form-in" id="emAccMiss" type="number" step="0.1" value="0" style="width:70px">
            </div>
            <div>
              <label class="form-lbl">低落值 low_mood <span style="opacity:.5">(±)</span></label>
              <input class="form-in" id="emAccLow" type="number" step="0.1" value="0" style="width:70px">
            </div>
            <div>
              <label class="form-lbl">烦躁值 irritable <span style="opacity:.5">(±)</span></label>
              <input class="form-in" id="emAccIrr" type="number" step="0.1" value="0" style="width:70px">
            </div>
          </div>
        </div>

        <!-- ⑤ 连锁事件 -->
        <div class="evt-modal-section">
          <div class="evt-modal-sec-head">⛓ 连锁事件 <span style="font-size:10px;font-weight:400;opacity:.6">（JSON，可为空）</span></div>
          <div style="font-size:10px;color:var(--muted);margin-bottom:6px">
            格式：{"event":"…","probability":0.3,"delay":"within_1h","mood_effect":{"valence":-0.15},"accumulator_effect":{"low_mood":1.5},"send_policy":"maybe"}<br>
            delay 可选：immediate / within_1h / within_12h / next_morning
          </div>
          <textarea class="form-ta" id="emChain" style="min-height:70px;font-family:monospace;font-size:11px"
            placeholder='{"event":"淋雨后感觉有点着凉","probability":0.30,"delay":"within_1h","mood_effect":{"valence":-0.15,"energy":-0.10},"accumulator_effect":{"low_mood":1.5},"send_policy":"maybe"}'></textarea>
        </div>

        <div class="modal-acts">
          <button class="btn btn-d" id="emDeleteBtn" onclick="_deleteEvtFromModal()" style="display:none">Delete</button>
          <div style="display:flex;gap:8px">
            <button class="btn btn-g" onclick="_closeEvtModal()">Cancel</button>
            <button class="btn btn-p" onclick="_saveEvtModal()">Save</button>
          </div>
        </div>
      </div>
    </div>`;
  _fetchEvents();
}

function _evtPolicyChange() {
  const p = document.getElementById('emSendPolicy')?.value;
  const wrap = document.getElementById('emSendProbWrap');
  if (wrap) wrap.style.opacity = (p === 'never' || p === 'threshold') ? '.4' : '1';
}

function _toggleChip(btn) {
  btn.classList.toggle('active');
}

function _getChips(containerId) {
  return [...document.querySelectorAll(`#${containerId} .evt-chip.active`)].map(b => b.dataset.v);
}

function _setChips(containerId, values) {
  document.querySelectorAll(`#${containerId} .evt-chip`).forEach(b => {
    b.classList.toggle('active', (values||[]).includes(b.dataset.v));
  });
}

function _openEvtModal(evt) {
  _evtEditId = evt ? evt.id : null;
  document.getElementById('evtModalTitle').textContent = evt ? 'Edit Event' : 'Add Event';
  document.getElementById('emDeleteBtn').style.display = evt ? '' : 'none';

  // Parse stored JSON fields safely
  const parsej = (v, def={}) => { try { return typeof v==='string' ? JSON.parse(v||'{}') : (v||def); } catch(_) { return def; } };
  const conditions       = parsej(evt?.conditions, {});
  const mood_effect      = parsej(evt?.mood_effect, {});
  const acc_effect       = parsej(evt?.accumulator_effect, {});
  let   chain            = '';
  if (evt?.chain_definition && evt.chain_definition.trim() && evt.chain_definition !== '{}') {
    try { chain = JSON.stringify(JSON.parse(evt.chain_definition), null, 2); } catch(_) { chain = evt.chain_definition; }
  }

  document.getElementById('emContent').value      = evt?.content || '';
  document.getElementById('emLevel').value        = evt?.level   || 'green';
  document.getElementById('emScene').value        = evt?.scene   || '';
  document.getElementById('emWeight').value       = evt?.weight  ?? 1;
  document.getElementById('emAgent').value        = evt?.agent_id|| '';
  document.getElementById('emSendPolicy').value   = evt?.send_policy || 'maybe';
  document.getElementById('emSendProb').value     = evt?.send_probability ?? 0.40;
  document.getElementById('emMoodValence').value  = mood_effect.valence ?? 0;
  document.getElementById('emMoodEnergy').value   = mood_effect.energy  ?? 0;
  document.getElementById('emAccMiss').value      = acc_effect.miss_you  ?? 0;
  document.getElementById('emAccLow').value       = acc_effect.low_mood  ?? 0;
  document.getElementById('emAccIrr').value       = acc_effect.irritable ?? 0;
  document.getElementById('emChain').value        = chain;
  const fa = conditions.fatigue_above;
  document.getElementById('emCondFatigue').value  = fa != null ? fa : '';

  _setChips('emCondWeather', conditions.weather   || []);
  _setChips('emCondTod',     conditions.time_of_day || []);
  _setChips('emCondDt',      conditions.day_type   || []);
  _setChips('emCondMode',    conditions.mode       || []);
  _setChips('emCondSeason',  conditions.season     || []);

  _evtPolicyChange();
  document.getElementById('evtModalOv').classList.add('open');
}

function _closeEvtModal() {
  document.getElementById('evtModalOv').classList.remove('open');
  _evtEditId = null;
}

async function _saveEvtModal() {
  const content = document.getElementById('emContent').value.trim();
  if (!content) return toast('内容不能为空');

  // Build conditions object — only include keys that have selections
  const conds = {};
  const wx  = _getChips('emCondWeather');  if (wx.length)  conds.weather     = wx;
  const tod = _getChips('emCondTod');      if (tod.length) conds.time_of_day = tod;
  const dt  = _getChips('emCondDt');       if (dt.length)  conds.day_type    = dt;
  const md  = _getChips('emCondMode');     if (md.length)  conds.mode        = md;
  const szn = _getChips('emCondSeason');   if (szn.length) conds.season      = szn;
  const fa  = document.getElementById('emCondFatigue').value.trim();
  if (fa !== '')  conds.fatigue_above = parseInt(fa);

  const valence = parseFloat(document.getElementById('emMoodValence').value) || 0;
  const energy  = parseFloat(document.getElementById('emMoodEnergy').value)  || 0;
  const miss    = parseFloat(document.getElementById('emAccMiss').value) || 0;
  const low     = parseFloat(document.getElementById('emAccLow').value)  || 0;
  const irr     = parseFloat(document.getElementById('emAccIrr').value)  || 0;

  let chainDef = '';
  const chainRaw = document.getElementById('emChain').value.trim();
  if (chainRaw) {
    try { JSON.parse(chainRaw); chainDef = chainRaw; }
    catch(_) { return toast('连锁定义 JSON 格式错误'); }
  }

  const body = {
    content,
    level:              document.getElementById('emLevel').value,
    scene:              document.getElementById('emScene').value,
    weight:             parseFloat(document.getElementById('emWeight').value) || 1,
    agent_id:           document.getElementById('emAgent').value,
    send_policy:        document.getElementById('emSendPolicy').value,
    send_probability:   parseFloat(document.getElementById('emSendProb').value) || 0.4,
    conditions:         JSON.stringify(conds),
    mood_effect:        JSON.stringify({ valence, energy }),
    accumulator_effect: JSON.stringify({ miss_you: miss, low_mood: low, irritable: irr }),
    chain_definition:   chainDef,
  };

  try {
    if (_evtEditId) {
      await apiFetch(`/admin/api/random-events/${_evtEditId}`, { method:'PUT', body: JSON.stringify(body) });
      toast('已更新 ✓');
    } else {
      await apiFetch('/admin/api/random-events', { method:'POST', body: JSON.stringify(body) });
      toast('已添加 ✓');
    }
    _closeEvtModal();
    _fetchEvents();
  } catch(e) { toast('Error: '+e); }
}

async function _deleteEvtFromModal() {
  if (!_evtEditId || !confirm('Delete this event?')) return;
  try {
    await apiFetch('/admin/api/random-events/'+_evtEditId, { method:'DELETE' });
    _closeEvtModal();
    _fetchEvents();
  } catch(e) { toast('Error: '+e); }
}

function _renderEvtList() {
  const el = document.getElementById('eventsList');
  if (!el) return;
  let evts = _allEvts;
  if (_evtAgentId) evts = evts.filter(e => e.agent_id === _evtAgentId);
  if (_evtScene)   evts = evts.filter(e => e.scene === _evtScene);
  if (!evts.length) { el.innerHTML = '<div class="daily-empty">No events match the filter.</div>'; return; }
  const byLevel = {};
  for (const e of evts) (byLevel[e.level] = byLevel[e.level] || []).push(e);
  el.innerHTML = ['green','yellow','orange','red'].map(lvl => {
    if (!byLevel[lvl]) return '';
    const items = byLevel[lvl].map(e => {
      const sceneLabel = SCENE_LABEL[e.scene || ''] || '';
      const policy     = e.send_policy || 'maybe';
      const pColor     = _SP_COLOR[policy] || '#888';
      const pLabel     = _SP_LABEL[policy] || policy;

      // Condition tags
      let condTags = '';
      try {
        const c = typeof e.conditions === 'string' ? JSON.parse(e.conditions || '{}') : (e.conditions || {});
        if (c.weather?.length)     condTags += `<span class="evt-cond-tag">☁ ${c.weather.join('/')}</span>`;
        if (c.time_of_day?.length) condTags += `<span class="evt-cond-tag">⏰ ${c.time_of_day.join('/')}</span>`;
        if (c.day_type?.length)    condTags += `<span class="evt-cond-tag">📅 ${c.day_type.join('/')}</span>`;
        if (c.mode?.length)        condTags += `<span class="evt-cond-tag">🗺 ${c.mode.join('/')}</span>`;
        if (c.season?.length)      condTags += `<span class="evt-cond-tag">🌸 ${c.season.join('/')}</span>`;
        if (c.fatigue_above != null) condTags += `<span class="evt-cond-tag">😴≥${c.fatigue_above}</span>`;
      } catch(_) {}

      // Effect summary
      let effectStr = '';
      try {
        const m = typeof e.mood_effect === 'string' ? JSON.parse(e.mood_effect || '{}') : (e.mood_effect || {});
        const a = typeof e.accumulator_effect === 'string' ? JSON.parse(e.accumulator_effect || '{}') : (e.accumulator_effect || {});
        const parts = [];
        if (m.valence) parts.push(`情绪${m.valence>0?'+':''}${m.valence}`);
        if (a.miss_you) parts.push(`思念${a.miss_you>0?'+':''}${a.miss_you}`);
        if (a.low_mood) parts.push(`低落${a.low_mood>0?'+':''}${a.low_mood}`);
        if (a.irritable) parts.push(`烦躁${a.irritable>0?'+':''}${a.irritable}`);
        if (parts.length) effectStr = parts.join(' ');
      } catch(_) {}

      const hasChain = !!(e.chain_definition && e.chain_definition.trim() && e.chain_definition !== '{}' && e.chain_definition !== '');

      return `
        <div class="evt-row evt-row-rich" onclick="_openEvtModal(${JSON.stringify(e).replace(/"/g,'&quot;')})">
          <span class="evt-lvl">${LEVEL_EMOJI[lvl]||''}</span>
          <span class="evt-content">${esc(e.content)}</span>
          ${e.scene ? `<span class="evt-scene-badge">${sceneLabel}</span>` : ''}
          <span class="evt-policy-badge" style="background:${pColor}22;color:${pColor};border-color:${pColor}44">${pLabel}</span>
          ${hasChain ? '<span class="evt-cond-tag" style="color:#a78bfa;border-color:#a78bfa44">⛓ chain</span>' : ''}
          <div class="evt-cond-row">${condTags}</div>
          ${effectStr ? `<span class="evt-effect">${esc(effectStr)}</span>` : ''}
          <button class="btn-icon" onclick="event.stopPropagation();_deleteEvtDirect('${esc(e.id)}')" title="Delete">✕</button>
        </div>`;
    }).join('');
    return `<div class="evt-group"><div class="evt-group-head">${LEVEL_EMOJI[lvl]} ${lvl} (${byLevel[lvl].length})</div>${items}</div>`;
  }).join('');
}

async function _fetchEvents() {
  const el = document.getElementById('eventsList');
  if (!el) return;
  try {
    const data = await apiFetch('/admin/api/random-events');
    _allEvts = data.events || [];
    if (!_allEvts.length) { el.innerHTML = '<div class="daily-empty">No events in pool.</div>'; return; }
    _renderEvtList();
  } catch(e) { el.innerHTML = `<span style="color:var(--danger);font-size:11px">${e}</span>`; }
}

async function _deleteEvtDirect(id) {
  if (!confirm('Delete this event?')) return;
  try {
    await apiFetch('/admin/api/random-events/'+id, {method:'DELETE'});
    _fetchEvents();
  } catch(e) { toast('Error: '+e); }
}

// keep old name as alias for any inline callers
const _deleteEvent = _deleteEvtDirect;

async function _rollEvent() {
  const el = document.getElementById('rollResult');
  if (!el) return;
  const aid = document.getElementById('evtFilterAgent')?.value || '';
  try {
    const e = await apiFetch('/admin/api/random-events/roll', {method:'POST', body: JSON.stringify({agent_id:aid})});
    el.style.display='';
    const policy = e.send_policy || 'maybe';
    const pColor = _SP_COLOR[policy] || '#888';
    const pLabel = _SP_LABEL[policy] || policy;
    el.innerHTML = `${LEVEL_EMOJI[e.level]||''} <strong>${esc(e.content)}</strong>
      &nbsp;<span style="font-size:10px;color:${pColor}">${pLabel}</span>
      ${e.chain_definition && e.chain_definition !== '{}' ? ' <span style="font-size:10px;color:#a78bfa">⛓ has chain</span>' : ''}`;
  } catch(_) { toast('No events to roll'); }
}

// ── NPCs ───────────────────────────────────────────────────────────────────
async function _loadNpcs() {
  const sub = document.getElementById('daily-sub');
  const agents = _charAgents();
  const agentOpts = agents.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">👥 Social Network
        <select class="daily-input" id="npcAgentId" style="min-width:120px;margin-left:12px"
          onchange="_fetchNpcs()">${agentOpts}</select>
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

// ── Daily Skeleton ─────────────────────────────────────────────────────────
async function _loadSkeleton() {
  const sub = document.getElementById('daily-sub');
  const agents = _charAgents();
  const agentOpts = agents.map(a =>
    `<option value="${esc(a)}" ${a===_skeletonAgentId?'selected':''}>${esc(a)}</option>`).join('');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">🗓 Daily Skeleton
        <select class="daily-input" id="skAgentId" style="min-width:120px;margin-left:12px"
          onchange="_skeletonAgentId=this.value;_fetchSkeleton()">${agentOpts}</select>
        <button class="btn btn-s btn-p" style="margin-left:auto" onclick="_saveSkeleton()">Save</button>
      </div>
      <div id="skeletonBody" style="margin-top:14px"><div class="page-loading" style="font-size:11px">Loading…</div></div>
    </div>`;
  _fetchSkeleton();
}

async function _fetchSkeleton() {
  const body = document.getElementById('skeletonBody');
  if (!body) return;
  body.innerHTML = '<div class="page-loading" style="font-size:11px">Loading…</div>';
  try {
    const s = await apiFetch(`/admin/api/config/daily-skeleton?agent_id=${encodeURIComponent(_skeletonAgentId)}`);
    body.innerHTML = _buildSkeletonForm(s);
  } catch(e) {
    body.innerHTML = `<span style="color:var(--danger);font-size:11px">${e}</span>`;
  }
}

function _buildSkeletonForm(s) {
  const tpls = ['freelancer','student','office','custom'];
  const styles = ['remote','office','hybrid','flexible'];
  const wu = s.wake_up || {};
  const sl = s.sleep   || {};
  const habits = Array.isArray(s.habits) ? s.habits.join(', ') : (s.habits||'');
  return `
    <div class="state-grid">
      <div class="state-field">
        <label class="form-lbl">Template</label>
        <select class="form-sel" id="skTpl">
          ${tpls.map(t => `<option value="${t}" ${s.template===t?'selected':''}>${t}</option>`).join('')}
        </select>
      </div>
      <div class="state-field">
        <label class="form-lbl">Work Style</label>
        <select class="form-sel" id="skStyle">
          ${styles.map(t => `<option value="${t}" ${s.work_style===t?'selected':''}>${t}</option>`).join('')}
        </select>
      </div>
      <div class="state-field">
        <label class="form-lbl">Wake-up range</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="form-in" id="skWuFrom" value="${esc((wu.range||[])[0]||'08:00')}" placeholder="08:00" style="width:80px">
          <span style="opacity:.5">–</span>
          <input class="form-in" id="skWuTo"   value="${esc((wu.range||[])[1]||'11:00')}" placeholder="11:00" style="width:80px">
          <select class="form-sel" id="skWuBias" style="width:90px">
            ${['normal','early','late'].map(b => `<option value="${b}" ${(wu.bias||'normal')===b?'selected':''}>${b}</option>`).join('')}
          </select>
        </div>
      </div>
      <div class="state-field">
        <label class="form-lbl">Sleep range</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="form-in" id="skSlFrom" value="${esc((sl.range||[])[0]||'23:00')}" placeholder="23:00" style="width:80px">
          <span style="opacity:.5">–</span>
          <input class="form-in" id="skSlTo"   value="${esc((sl.range||[])[1]||'02:00')}" placeholder="02:00" style="width:80px">
        </div>
      </div>
      <div class="state-field" style="grid-column:span 2">
        <label class="form-lbl">Habits <span style="opacity:.5">(comma-separated)</span></label>
        <input class="form-in" id="skHabits" value="${esc(habits)}" placeholder="喝咖啡, 午睡, 加班" style="width:100%">
      </div>
    </div>`;
}

async function _saveSkeleton() {
  const habitsRaw = document.getElementById('skHabits').value.trim();
  const body = {
    agent_id:   _skeletonAgentId,
    template:   document.getElementById('skTpl').value,
    work_style: document.getElementById('skStyle').value,
    wake_up: {
      range: [
        document.getElementById('skWuFrom').value.trim(),
        document.getElementById('skWuTo').value.trim(),
      ],
      bias: document.getElementById('skWuBias').value,
    },
    sleep: {
      range: [
        document.getElementById('skSlFrom').value.trim(),
        document.getElementById('skSlTo').value.trim(),
      ],
    },
    habits: habitsRaw ? habitsRaw.split(',').map(h => h.trim()).filter(Boolean) : [],
  };
  try {
    await apiFetch('/admin/api/config/daily-skeleton', { method:'POST', body: JSON.stringify(body) });
    toast(`Skeleton saved for ${_skeletonAgentId} ✓`);
  } catch(e) { toast('Error: '+e); }
}


// ── Screen Time Rules ──────────────────────────────────────────────────────
async function _loadScreenTime() {
  const sub = document.getElementById('daily-sub');
  const agents = _charAgents();
  if (!_screenTimeAgentId) _screenTimeAgentId = agents[0] || '';
  const agentOpts = agents.map(a =>
    `<option value="${esc(a)}" ${a===_screenTimeAgentId?'selected':''}>${esc(a)}</option>`).join('');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">📱 Screen Time Rules
        <select class="daily-input" id="screenTimeAgentId" style="min-width:120px;margin-left:12px"
          onchange="_screenTimeAgentId=this.value;_fetchScreenRules()">${agentOpts}</select>
        <button class="btn btn-s btn-accent" style="margin-left:auto" onclick="_addScreenRule()">+ Rule</button>
        <button class="btn btn-s btn-p"      style="margin-left:4px"  onclick="_saveScreenRules()">Save All</button>
      </div>
      <div style="font-size:10px;color:var(--muted);margin:6px 0 10px">
        Condition syntax: <code>category:游戏 &gt; 120</code> · <code>app:王者荣耀 &gt; 60</code> · <code>any AND hour &gt;= 1</code><br>
        Variables in push: <code>{app}</code> <code>{category}</code> <code>{minutes}</code>
      </div>
      <div id="screenRulesList"></div>
      <div id="screenRulesMsg" style="font-size:11px;margin-top:6px"></div>
    </div>`;
  _fetchScreenRules();
}

let _screenRules = [];

async function _fetchScreenRules() {
  const el = document.getElementById('screenRulesList');
  if (!el) return;
  try {
    const url = _screenTimeAgentId
      ? `/admin/api/config/screen-time-rules?agent_id=${encodeURIComponent(_screenTimeAgentId)}`
      : '/admin/api/config/screen-time-rules';
    const data = await apiFetch(url);
    _screenRules = data.rules || [];
    _renderScreenRules();
  } catch(e) {
    el.innerHTML = `<span style="color:var(--danger);font-size:11px">${e}</span>`;
  }
}

function _renderScreenRules() {
  const el = document.getElementById('screenRulesList');
  if (!el) return;
  if (!_screenRules.length) {
    el.innerHTML = '<div class="daily-empty">No rules. Click + Rule to add.</div>';
    return;
  }
  el.innerHTML = _screenRules.map((r, i) => `
    <div class="screen-rule-row" id="srule-${i}">
      <div class="sr-idx">${i+1}</div>
      <div class="sr-fields">
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <div style="flex:3;min-width:180px">
            <label class="form-lbl">Condition</label>
            <input class="form-in sr-cond" data-i="${i}" value="${esc(r.condition||'')}"
              placeholder="category:游戏 > 120" style="width:100%">
          </div>
          <div style="flex:3;min-width:180px">
            <label class="form-lbl">Push message</label>
            <input class="form-in sr-push" data-i="${i}" value="${esc(r.push||'')}"
              placeholder="还在打{app}？" style="width:100%">
          </div>
          <div style="flex:1;min-width:120px">
            <label class="form-lbl">Cooldown category</label>
            <input class="form-in sr-cd" data-i="${i}" value="${esc(r.cooldown_category||'game_check')}"
              placeholder="game_check" style="width:100%">
          </div>
          <div style="flex:1;min-width:100px">
            <label class="form-lbl">Bark sound <span style="opacity:.5">(opt)</span></label>
            <input class="form-in sr-sound" data-i="${i}" value="${esc(r.bark_sound||'')}"
              placeholder="" style="width:100%">
          </div>
        </div>
      </div>
      <button class="btn-icon" style="align-self:center;flex-shrink:0" onclick="_removeScreenRule(${i})" title="Remove">✕</button>
    </div>`).join('');
}

function _collectScreenRules() {
  return _screenRules.map((_, i) => ({
    condition:        document.querySelector(`.sr-cond[data-i="${i}"]`)?.value.trim() || '',
    push:             document.querySelector(`.sr-push[data-i="${i}"]`)?.value.trim() || '',
    cooldown_category: document.querySelector(`.sr-cd[data-i="${i}"]`)?.value.trim() || 'game_check',
    bark_sound:       document.querySelector(`.sr-sound[data-i="${i}"]`)?.value.trim() || '',
  })).filter(r => r.condition && r.push);
}

function _addScreenRule() {
  // flush current edits first
  _screenRules = _collectScreenRules();
  _screenRules.push({ condition: '', push: '', cooldown_category: 'game_check', bark_sound: '' });
  _renderScreenRules();
  // focus new condition field
  const newCond = document.querySelector(`.sr-cond[data-i="${_screenRules.length-1}"]`);
  newCond?.focus();
}

function _removeScreenRule(i) {
  _screenRules = _collectScreenRules();
  _screenRules.splice(i, 1);
  _renderScreenRules();
}

async function _saveScreenRules() {
  const rules = _collectScreenRules();
  const msg = document.getElementById('screenRulesMsg');
  try {
    const r = await apiFetch('/admin/api/config/screen-time-rules',
      { method:'POST', body: JSON.stringify({ rules, agent_id: _screenTimeAgentId }) });
    _screenRules = rules;
    if (msg) { msg.style.color='var(--ok)'; msg.textContent=`✓ ${r.count} rules saved`; }
    toast('Screen rules saved ✓');
  } catch(e) {
    if (msg) { msg.style.color='var(--danger)'; msg.textContent='✗ '+e; }
    toast('Error: '+e);
  }
}
