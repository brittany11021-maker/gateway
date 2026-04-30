/* timeline.js v1 – Palimpsest L1-L5 + Daily events timeline */

const LAYER_COLOR = { L1:'#a78bfa', L2:'#60a5fa', L3:'#34d399', L4:'#fbbf24', L5:'#f87171' };
const LAYER_LABEL = { L1:'Identity', L2:'Background', L3:'Episode', L4:'Moment', L5:'Rewrite' };
const LAYER_ORDER = ['L1','L2','L3','L4','L5'];

let _tlAgentId   = 'default';
let _tlFilter    = '';       // '' = all layers, or 'L1'…'L5' or 'daily'
let _tlMemories  = [];
let _tlDaily     = [];

async function loadTimelineTab() {
  const area = document.getElementById('area');
  area.classList.remove('read-mode');
  area.innerHTML = `
    <div class="tl-shell">
      <div class="tl-toolbar">
        <input class="tl-agent-in" id="tlAgentIn" value="${_tlAgentId}"
          placeholder="agent_id" onchange="_tlSetAgent(this.value)">
        <div class="tl-filter-pills">
          <button class="tl-pill${_tlFilter===''?' active':''}"     onclick="_tlSetFilter('')">All</button>
          ${LAYER_ORDER.map(l =>
            `<button class="tl-pill${_tlFilter===l?' active':''}"
              style="--lc:${LAYER_COLOR[l]}"
              onclick="_tlSetFilter('${l}')">${l} ${LAYER_LABEL[l]}</button>`).join('')}
          <button class="tl-pill tl-pill-daily${_tlFilter==='daily'?' active':''}"
            onclick="_tlSetFilter('daily')">📔 Daily</button>
        </div>
        <button class="btn btn-s btn-g" onclick="_tlLoad()">↺ Refresh</button>
      </div>
      <div id="tlBody" class="tl-body"><div class="page-loading">Loading…</div></div>
    </div>`;
  _tlLoad();
}

function _tlSetAgent(v) {
  _tlAgentId = v.trim() || 'default';
  _tlLoad();
}
function _tlSetFilter(f) {
  _tlFilter = f;
  document.querySelectorAll('.tl-pill').forEach(el => {
    const match = (f === '' && !el.dataset.f && !el.classList.contains('tl-pill-daily'))
      || el.textContent.startsWith(f)
      || (f === 'daily' && el.classList.contains('tl-pill-daily'))
      || (f === '' && el.textContent === 'All');
    el.classList.toggle('active', el.textContent.trim() === (f === '' ? 'All' : f.startsWith('L') ? f + ' ' + LAYER_LABEL[f] : '📔 Daily'));
  });
  _tlRender();
}

async function _tlLoad() {
  const body = document.getElementById('tlBody');
  if (!body) return;
  body.innerHTML = '<div class="page-loading">Loading…</div>';
  try {
    const [memData, dayData] = await Promise.all([
      apiFetch(`/admin/api/palimpsest?agent_id=${encodeURIComponent(_tlAgentId)}&limit=200&importance_min=1`),
      apiFetch(`/admin/api/daily?agent_id=${encodeURIComponent(_tlAgentId)}&limit=60`).catch(() => ({events:[]})),
    ]);
    _tlMemories = memData.memories || [];
    _tlDaily    = (dayData.events || []);
    _tlRender();
  } catch(e) {
    body.innerHTML = `<div class="page-loading" style="color:var(--danger)">Error: ${e}</div>`;
  }
}

function _tlRender() {
  const body = document.getElementById('tlBody');
  if (!body) return;

  // Build unified event list
  let events = [];

  if (_tlFilter !== 'daily') {
    for (const m of _tlMemories) {
      if (_tlFilter && m.layer !== _tlFilter) continue;
      events.push({
        type:    'memory',
        layer:   m.layer,
        date:    (m.created_at || '').slice(0, 10),
        ts:      m.created_at || '',
        content: m.content,
        id:      m.id,
        imp:     m.importance || 1,
      });
    }
  }
  if (_tlFilter === '' || _tlFilter === 'daily') {
    for (const d of _tlDaily) {
      events.push({
        type:    'daily',
        layer:   'daily',
        date:    d.date || '',
        ts:      d.date || '',
        content: d.summary || '',
        mood:    d.mood,
        id:      d.id,
        imp:     2,
      });
    }
  }

  if (!events.length) {
    body.innerHTML = '<div class="tl-empty">No events found.</div>';
    return;
  }

  // Group by date descending
  events.sort((a, b) => (b.ts > a.ts ? 1 : -1));
  const byDate = {};
  for (const e of events) {
    const d = e.date || 'unknown';
    (byDate[d] = byDate[d] || []).push(e);
  }

  const MOOD_EMOJI = {happy:'😊',neutral:'😐',sad:'😢',excited:'🌟',tired:'😴',anxious:'😟',calm:'🌿'};

  const html = Object.entries(byDate).map(([date, evs]) => {
    const items = evs.map(e => {
      if (e.type === 'daily') {
        const emoji = MOOD_EMOJI[e.mood] || '📔';
        return `<div class="tl-event tl-event-daily">
          <div class="tl-dot" style="background:#94a3b8"></div>
          <div class="tl-event-body">
            <span class="tl-tag" style="background:#475569">${emoji} Daily</span>
            <span class="tl-content">${_tlEsc(e.content)}</span>
          </div>
        </div>`;
      }
      const color = LAYER_COLOR[e.layer] || '#94a3b8';
      const label = LAYER_LABEL[e.layer] || e.layer;
      const imp = '★'.repeat(Math.min(e.imp, 5));
      return `<div class="tl-event">
        <div class="tl-dot" style="background:${color}"></div>
        <div class="tl-event-body">
          <span class="tl-tag" style="background:${color}22;color:${color};border:1px solid ${color}44">${label}</span>
          <span class="tl-imp" title="importance ${e.imp}">${imp}</span>
          <span class="tl-content">${_tlEsc(e.content)}</span>
        </div>
      </div>`;
    }).join('');

    return `<div class="tl-day">
      <div class="tl-date-marker"><span>${date}</span></div>
      <div class="tl-day-events">${items}</div>
    </div>`;
  }).join('');

  body.innerHTML = `<div class="tl-feed">${html}</div>`;
}

function _tlEsc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
