// ── Providers modal state ─────────────────────────────────────────────────────
let _provData = null;   // { providers, default_chain, embed_provider }
let _editingProvider = null;  // name of provider being edited, or null for new

async function openProvidersModal() {
  document.getElementById('providersOv').classList.add('open');
  await _loadProviders();
}

function closeProvidersModal() {
  document.getElementById('providersOv').classList.remove('open');
  _editingProvider = null;
}

async function _loadProviders() {
  document.getElementById('providersList').innerHTML = '<div class="u-loading">Loading…</div>';
  try {
    _provData = await api('/admin/api/providers');
    _renderProviders();
  } catch(e) {
    document.getElementById('providersList').innerHTML =
      `<div class="u-empty">Error: ${e.message}</div>`;
  }
}

function _renderProviders() {
  const { providers, default_chain, embed_provider, distill_model, distill_providers } = _provData;

  const defaultChainArr = default_chain.split(',').map(s=>s.trim()).filter(Boolean);
  const provRows = providers.map((p, idx) => {
    const gwIdx = defaultChainArr.indexOf(p.name);
    const gwLabel = gwIdx === 0 ? 'gateway1·主' : gwIdx === 1 ? 'gateway2·备1' : gwIdx === 2 ? 'gateway3·备2' : gwIdx > 2 ? `gateway${gwIdx+1}` : '';
    return `
    <div class="prov-row" id="prow-${esc(p.name)}">
      <div class="prov-info">
        <span class="prov-name">${esc(p.name)}</span>
        ${gwLabel ? `<span style="font-size:9px;background:rgba(60,140,255,.12);color:#3c8cff;border-radius:4px;padding:1px 7px;margin-left:5px">${gwLabel}</span>` : ''}
        <span class="prov-url">${esc(p.base_url)}</span>
        <div class="prov-tags">
          ${p.is_embed ? `<span class="prov-tag embed">embed</span>` : ''}
          ${(distill_providers||[]).includes(p.name) ? `<span class="prov-tag" style="background:rgba(120,200,120,.15);color:#4a9e4a">A层·蒸馏</span>` : ''}
        </div>
      </div>
      <div class="prov-status" id="pstatus-${esc(p.name)}">
        <span class="prov-dot idle"></span>
      </div>
      <button type="button" class="btn btn-g" style="font-size:9px;padding:5px 10px"
        onclick="testProvider('${esc(p.name)}')">Test</button>
      <button type="button" class="btn btn-g" style="font-size:9px;padding:5px 10px"
        onclick="editProvider('${esc(p.name)}')">Edit</button>
      <button type="button" class="btn btn-d" style="font-size:9px;padding:5px 10px"
        onclick="deleteProvider('${esc(p.name)}')">✕</button>
    </div>`;
  }).join('');

  const configSection = `
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
      <div style="font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:8px;font-weight:700">Global Config</div>
      <div class="form-g" style="margin-bottom:8px">
        <label class="form-lbl">Default Chain <span style="opacity:.45">(comma-separated, in order)</span></label>
        <div style="display:flex;gap:8px">
          <input class="form-in" id="chainInput" value="${esc(default_chain)}" placeholder="nvidia,openrouter" style="flex:1">
          <button type="button" class="btn btn-g" style="font-size:9px;padding:5px 12px;white-space:nowrap" onclick="saveChain()">Save</button>
        </div>
      </div>
      <div class="form-g" style="margin-bottom:8px">
        <label class="form-lbl">A 层蒸馏模型 <span style="opacity:.45">(DISTILL_MODEL，后台蒸馏/自动任务)</span></label>
        <div style="display:flex;gap:8px">
          <input class="form-in" id="distillModelInput" value="${esc(distill_model||'')}" placeholder="google/gemma-4-31b-it" style="flex:1">
          <button type="button" class="btn btn-g" style="font-size:9px;padding:5px 12px;white-space:nowrap" onclick="saveDistillModel()">Save</button>
        </div>
        <div style="font-size:10px;color:var(--muted);margin-top:3px">
          当前 A 层 fallback 链：<b>${esc((distill_providers||[]).join(' → ') || '—')}</b>
        </div>
      </div>
    </div>`;

  const addForm = `
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
      <div style="font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:10px;font-weight:700" id="provFormTitle">Add Provider</div>
      <div class="form-g" style="margin-bottom:6px">
        <label class="form-lbl">Name <span style="opacity:.45">(lowercase, e.g. nvidia)</span></label>
        <input class="form-in" id="provName" placeholder="nvidia">
      </div>
      <div class="form-g" style="margin-bottom:6px">
        <label class="form-lbl">Base URL</label>
        <input class="form-in" id="provUrl" placeholder="https://integrate.api.nvidia.com/v1">
      </div>
      <div class="form-g" style="margin-bottom:6px">
        <label class="form-lbl">API Key</label>
        <input class="form-in" id="provKey" type="password" placeholder="sk-…">
      </div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <input type="checkbox" id="provEmbed" style="width:14px;height:14px">
        <label class="form-lbl" style="margin:0;cursor:pointer" for="provEmbed">Use for embeddings</label>
      </div>
      <div style="display:flex;gap:8px">
        <button type="button" class="btn btn-g" onclick="cancelProvEdit()" id="provCancelBtn" style="display:none">Cancel</button>
        <button type="button" class="btn btn-p" onclick="saveProvider()">Save Provider</button>
      </div>
    </div>`;

  document.getElementById('providersList').innerHTML =
    (providers.length ? provRows : `<div class="u-empty" style="padding:12px 0">No providers yet — add one below</div>`)
    + configSection + addForm;
}

function editProvider(name) {
  const p = _provData.providers.find(x => x.name === name);
  if (!p) return;
  _editingProvider = name;
  document.getElementById('provFormTitle').textContent = `Edit Provider — ${name}`;
  document.getElementById('provName').value  = p.name;
  document.getElementById('provName').disabled = true;
  document.getElementById('provUrl').value   = p.base_url;
  document.getElementById('provKey').value   = '';
  document.getElementById('provKey').placeholder = '(leave blank to keep current key)';
  document.getElementById('provEmbed').checked = p.is_embed;
  document.getElementById('provCancelBtn').style.display = '';
  document.getElementById('provName').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function cancelProvEdit() {
  _editingProvider = null;
  document.getElementById('provFormTitle').textContent = 'Add Provider';
  document.getElementById('provName').value = '';
  document.getElementById('provName').disabled = false;
  document.getElementById('provUrl').value  = '';
  document.getElementById('provKey').value  = '';
  document.getElementById('provKey').placeholder = 'sk-…';
  document.getElementById('provEmbed').checked = false;
  document.getElementById('provCancelBtn').style.display = 'none';
}

async function saveProvider() {
  const name    = document.getElementById('provName').value.trim().toLowerCase();
  const base    = document.getElementById('provUrl').value.trim();
  const api_key = document.getElementById('provKey').value.trim();
  const is_embed = document.getElementById('provEmbed').checked;

  if (!name || !base) { toast('Name and Base URL are required'); return; }
  if (!_editingProvider && !api_key) { toast('API Key is required for new provider'); return; }

  const body = { name, base_url: base, is_embed };
  if (api_key) body.api_key = api_key;
  else if (_editingProvider) {
    // keep existing key — fetch it from a temp test or send placeholder
    // Instead: re-send original (we don't have it client-side), so require key on edit too
    toast('Please enter the API Key (keys are not shown for security)'); return;
  }

  try {
    await api('/admin/api/providers', { method: 'POST', body });
    toast(`Provider "${name}" saved`);
    cancelProvEdit();
    await _loadProviders();
  } catch(e) { toast('Error: ' + e.message); }
}

async function deleteProvider(name) {
  if (!confirm(`Delete provider "${name}"?`)) return;
  try {
    await api(`/admin/api/providers/${enc(name)}`, { method: 'DELETE' });
    toast(`"${name}" deleted`);
    await _loadProviders();
  } catch(e) { toast('Error: ' + e.message); }
}

async function saveChain() {
  const chain = document.getElementById('chainInput').value.trim();
  try {
    await api('/admin/api/gateway-config', { method: 'POST', body: { default_chain: chain } });
    toast('Default chain saved');
    await _loadProviders();
  } catch(e) { toast('Error: ' + e.message); }
}

async function testProvider(name) {
  const statusEl = document.getElementById(`pstatus-${name}`);
  if (!statusEl) return;
  statusEl.innerHTML = `<span class="prov-dot testing"></span>`;
  try {
    const d = await api(`/admin/api/providers/${enc(name)}/test`, { method: 'POST', body: {} });
    if (d.ok) {
      statusEl.innerHTML = `<span class="prov-dot ok"></span><span style="font-size:9px;color:var(--muted);margin-left:3px">${d.latency_ms}ms</span>`;
    } else {
      statusEl.innerHTML = `<span class="prov-dot err" title="${esc(d.error||'')}"></span><span style="font-size:9px;color:var(--danger);margin-left:3px">${d.status||'err'}</span>`;
    }
  } catch(e) {
    statusEl.innerHTML = `<span class="prov-dot err"></span><span style="font-size:9px;color:var(--danger);margin-left:3px">fail</span>`;
  }
}

async function saveDistillModel() {
  const model = document.getElementById('distillModelInput').value.trim();
  if (!model) { toast('请输入模型名'); return; }
  try {
    await api('/admin/api/gateway-config', { method: 'POST', body: { distill_model: model } });
    toast('A 层蒸馏模型已保存，下次蒸馏生效');
    await _loadProviders();
  } catch(e) { toast('Error: ' + e.message); }
}

// ── Effective config display in agent settings modal ──────────────────────────
async function loadEnvChainForAgent(aid) {
  const el = document.getElementById('sEnvChain');
  if (!el) return;
  try {
    if (!_provData) _provData = await api('/admin/api/providers');
    const chain = _provData.default_chain || '(none)';
    const embed = _provData.embed_provider || '(none)';
    el.innerHTML =
      `Default chain: <strong>${esc(chain)}</strong><br>` +
      `Embed: <strong>${esc(embed)}</strong><br>` +
      `Override per-agent: set <em>API Chain</em> field above`;
  } catch {
    el.textContent = '—';
  }
}
