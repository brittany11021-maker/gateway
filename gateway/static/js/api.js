// ── Constants ─────────────────────────────────────────────────────────────────
const COL   = { profile:'memory_profile', project:'memory_project', recent:'memory_recent' };
const LABEL = { memory_profile:'角色', memory_project:'Project', memory_recent:'Recent' };
const ICON  = { memory_profile:'◉', memory_project:'◈', memory_recent:'◑' };
const TYPE  = { memory_profile:'type-profile', memory_project:'type-project', memory_recent:'type-recent' };
const PAGE_SIZE = 20;

const AV_DAY   = ['#D2D5FF','#BEF0D5','#FFD4C8','#FFE5B4','#D5E5FF','#FFD5E8','#C8F0F0','#F5E0C8'];
const AV_NIGHT = ['rgba(90,100,220,.38)','rgba(40,148,90,.35)','rgba(210,80,70,.35)',
                  'rgba(180,130,40,.35)','rgba(60,100,200,.35)','rgba(180,60,120,.35)',
                  'rgba(40,160,160,.35)','rgba(200,120,50,.35)'];

// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  key:   localStorage.getItem('mgw_k') || '',
  tab:   'user',
  night: localStorage.getItem('mgw_n') === '1',
  q:     '',
  timer: null,
};

// ── Shared mutable globals (written by memory.js) ─────────────────────────────
let allAgents   = [];
let agentTypes  = {}; // aid -> 'agent'|'character'
let agentItems  = {};
let agentPages  = {};
let agentLoaded = new Set();
let settingsAid = null;
let editId = null, editCol = null, editAid = null;
const CACHE = {};

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  applyTheme();
  if (!S.key) { openKeyModal(); return; }
  boot();
});

async function boot() {
  updateKeyChip();
  // Probe the key before loading anything — if stale/invalid, bounce to the modal.
  try {
    const r = await fetch('/admin/api/agents', {
      headers: { 'Authorization': `Bearer ${S.key}` },
    });
    if (r.status === 401 || r.status === 403) {
      localStorage.removeItem('mgw_k');
      S.key = '';
      updateKeyChip();
      openKeyModal();
      const err = document.getElementById('keyErr');
      if (err) err.textContent = 'Saved key is no longer valid';
      return;
    }
  } catch (e) { /* network issue — let normal loaders surface it */ }
  await Promise.all([loadGlobalStats(), loadAgents()]);
}

// ── API util ──────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: {
      'Authorization': `Bearer ${S.key}`,
      'Content-Type':  'application/json',
      ...(opts.headers || {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401 || r.status === 403) {
    localStorage.removeItem('mgw_k');
    S.key = '';
    updateKeyChip();
    openKeyModal();
    const err = document.getElementById('keyErr');
    if (err) err.textContent = 'Key rejected — please re-enter';
    throw new Error(r.status);
  }
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// Alias for modules that pre-stringify their body (mcp.js, daily.js)
async function apiFetch(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: {
      'Authorization': `Bearer ${S.key}`,
      'Content-Type':  'application/json',
      ...(opts.headers || {}),
    },
    body: opts.body || undefined,
  });
  if (r.status === 401 || r.status === 403) {
    S.key = ''; localStorage.removeItem('mgw_k');
    openKeyModal(); throw new Error(r.status);
  }
  if (!r.ok) {
    const txt = await r.text().catch(() => r.statusText);
    throw new Error(txt || r.status);
  }
  return r.json();
}

// ── Global stats ──────────────────────────────────────────────────────────────
async function loadGlobalStats() {
  try {
    const d = await api('/admin/api/stats/global');
    document.getElementById('n-profile').textContent       = d.memory_profile  ?? 0;
    document.getElementById('n-project').textContent       = d.memory_project  ?? 0;
    document.getElementById('n-recent').textContent        = d.memory_recent   ?? 0;
    document.getElementById('n-conversations').textContent = d.conversations   ?? 0;
    if (d.books     !== undefined) document.getElementById('n-books').textContent = d.books;
    if (d.daily     !== undefined) document.getElementById('n-daily').textContent = d.daily;
    if (d.mcp_tools !== undefined) document.getElementById('n-mcp').textContent   = d.mcp_tools;
  } catch(e) { console.error(e); }
}

// ── Theme ─────────────────────────────────────────────────────────────────────
function applyTheme() {
  document.body.classList.toggle('night', S.night);
  document.getElementById('themeL').textContent = S.night ? 'NIGHT' : 'DAY';
  document.querySelectorAll('.u-av-init').forEach(el => {
    if (el.querySelector('img')) return;
    const i = parseInt(el.id.replace('uav',''));
    el.style.background = S.night ? AV_NIGHT[i % AV_NIGHT.length] : AV_DAY[i % AV_DAY.length];
  });
}

function toggleTheme() {
  S.night = !S.night;
  localStorage.setItem('mgw_n', S.night ? '1' : '0');
  applyTheme();
}

// ── Key modal ─────────────────────────────────────────────────────────────────
function openKeyModal() {
  document.getElementById('keyIn').value = S.key;
  const err = document.getElementById('keyErr');
  if (err) err.textContent = '';
  document.getElementById('keyOv').classList.add('open');
  setTimeout(() => document.getElementById('keyIn').focus(), 50);
}

async function saveKey() {
  const k   = document.getElementById('keyIn').value.trim();
  const err = document.getElementById('keyErr');
  if (err) err.textContent = '';
  if (!k) { if (err) err.textContent = 'Key cannot be empty'; return; }

  // Verify key against backend before saving — reject invalid keys.
  let ok = false;
  try {
    const r = await fetch('/admin/api/agents', {
      headers: { 'Authorization': `Bearer ${k}` },
    });
    ok = r.ok;
  } catch (e) { ok = false; }

  if (!ok) {
    if (err) err.textContent = 'Invalid key — access denied';
    return;
  }

  S.key = k;
  localStorage.setItem('mgw_k', k);
  document.getElementById('keyOv').classList.remove('open');
  updateKeyChip(); boot();
}

function updateKeyChip() {
  document.getElementById('keyChip').textContent = S.key ? `KEY ••${S.key.slice(-4)}` : 'SET KEY';
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg) {
  const w = document.getElementById('toastWrap');
  const el = document.createElement('div');
  el.className = 'toast'; el.textContent = msg;
  w.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// ── DOM helpers ───────────────────────────────────────────────────────────────
function setArea(html) { document.getElementById('area').innerHTML = html; }
function enc(s) { return encodeURIComponent(s); }
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtTs(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('zh-CN',{year:'2-digit',month:'2-digit',day:'2-digit'});
}
function fmtIsoTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
}
function fmtIsoDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'});
}
