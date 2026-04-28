// ── Backup & Restore state ────────────────────────────────────────────────────
let _importData = null;
let _autoPillOn = false;
let _histFile   = null;

// ── Open/close ────────────────────────────────────────────────────────────────
async function openBackupModal() {
  _importData = null;
  document.getElementById('importFile').value = '';
  document.getElementById('importFileName').textContent = 'No file selected';
  document.getElementById('importPreview').style.display = 'none';
  document.getElementById('importResult').style.display  = 'none';
  document.getElementById('importResult').className = 'bk-result';

  _histFile = null;
  document.getElementById('histFile').value = '';
  document.getElementById('histFileName').textContent = 'No file selected';
  document.getElementById('histPreview').style.display = 'none';
  document.getElementById('histResult').style.display  = 'none';
  document.getElementById('histResult').className = 'bk-result';
  const sel = document.getElementById('histAgentSel');
  sel.innerHTML = allAgents.length
    ? allAgents.map(a => `<option value="${a}">${a}</option>`).join('')
    : '<option value="default">default</option>';

  try {
    const d = await api('/admin/api/backup/settings');
    _autoPillOn = d.enabled;
    document.getElementById('autoPill').classList.toggle('on', _autoPillOn);
    document.getElementById('autoInterval').value = d.interval_days || 7;
    const meta = [];
    if (d.last_backup_at) meta.push(`Last backup: ${d.last_backup_at.slice(0,16).replace('T',' ')}`);
    meta.push(`Files on server: ${d.backup_count}`);
    document.getElementById('autoMeta').textContent = meta.join(' · ');
  } catch {}

  document.getElementById('backupOv').classList.add('open');
}

function closeBackupOv() { document.getElementById('backupOv').classList.remove('open'); }

// ── Export ────────────────────────────────────────────────────────────────────
async function doExport() {
  toast('Preparing export…');
  try {
    const r = await fetch('/admin/api/export', {
      headers: { 'Authorization': `Bearer ${S.key}` }
    });
    if (!r.ok) throw new Error(r.status);
    const blob = await r.blob();
    const cd   = r.headers.get('Content-Disposition') || '';
    const m    = cd.match(/filename="([^"]+)"/);
    const name = m ? m[1] : 'memory_backup.json';
    const url  = URL.createObjectURL(blob);
    Object.assign(document.createElement('a'),
      { href: url, download: name }).click();
    URL.revokeObjectURL(url);
    toast('Downloaded ' + name);
  } catch(e) { toast('Export failed: ' + e.message); }
}

// ── Import preview ────────────────────────────────────────────────────────────
function previewImport(input) {
  const file = input.files[0];
  if (!file) return;
  document.getElementById('importFileName').textContent = file.name;
  document.getElementById('importPreview').style.display = 'none';
  document.getElementById('importResult').style.display  = 'none';
  const reader = new FileReader();
  reader.onload = e => {
    try {
      const data = JSON.parse(e.target.result);
      if (!data.agents && !data.users) { toast('Invalid backup file'); return; }
      _importData = data;
      const agentMap = data.agents || data.users;
      let users = 0, mems = 0, convs = 0;
      for (const ud of Object.values(agentMap)) {
        users++;
        for (const tier of ['profile','project','recent'])
          mems  += (ud.memories?.[tier] || []).length;
        convs += (ud.conversations || []).length;
      }
      document.getElementById('pvUsers').textContent = users;
      document.getElementById('pvMems').textContent  = mems;
      document.getElementById('pvConvs').textContent = convs;
      const mode = document.getElementById('importMode').value;
      document.getElementById('importWarn').textContent =
        mode === 'overwrite' ? '⚠ Overwrite will delete ALL current data' : '';
      document.getElementById('importPreview').style.display = '';
    } catch(e) { toast('Invalid JSON: ' + e.message); }
  };
  reader.readAsText(file);
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('importMode').addEventListener('change', () => {
    if (!_importData) return;
    const mode = document.getElementById('importMode').value;
    document.getElementById('importWarn').textContent =
      mode === 'overwrite' ? '⚠ Overwrite will delete ALL current data' : '';
  });
});

async function confirmImport() {
  if (!_importData) return;
  const mode = document.getElementById('importMode').value;
  if (mode === 'overwrite' && !confirm('This will DELETE ALL current data and replace it.\nAre you sure?')) return;
  const btn = document.getElementById('importBtn');
  btn.disabled = true; btn.textContent = 'Importing…';
  const res = document.getElementById('importResult');
  res.style.display = 'none';
  try {
    const d = await api('/admin/api/import', { method:'POST', body:{ mode, data:_importData } });
    res.className = 'bk-result ok';
    res.innerHTML = `✓ Done<br>
      Users: ${d.imported_users} &nbsp;·&nbsp;
      Memories: ${d.imported_memories} &nbsp;·&nbsp;
      Conversations: ${d.imported_conversations} &nbsp;·&nbsp;
      Skipped: ${d.skipped}`;
    res.style.display = '';
    toast('Import complete');
    loadGlobalStats(); loadAgents();
  } catch(e) {
    res.className = 'bk-result err';
    res.textContent = '✕ Error: ' + e.message;
    res.style.display = '';
  } finally {
    btn.disabled = false; btn.textContent = 'Import now';
  }
}

// ── History import ────────────────────────────────────────────────────────────
function previewHistImport(input) {
  const file = input.files[0];
  if (!file) return;
  _histFile = file;
  document.getElementById('histFileName').textContent = file.name;
  document.getElementById('histPreview').style.display = 'none';
  document.getElementById('histResult').style.display  = 'none';
  const reader = new FileReader();
  reader.onload = e => {
    try {
      const data = JSON.parse(e.target.result);
      let count = 0;
      if (Array.isArray(data) && data.length && data[0].chat_messages !== undefined) {
        count = data.length;
      } else if (data.agents || data.users) {
        const agents = data.agents || data.users;
        for (const aid of Object.keys(agents))
          count += (agents[aid].conversations || []).length;
      } else {
        toast('Unknown format — expected Claude.ai or gateway export'); return;
      }
      document.getElementById('histConvCount').textContent = count;
      document.getElementById('histPreview').style.display = '';
    } catch(err) { toast('Invalid JSON: ' + err.message); }
  };
  reader.readAsText(file);
}

async function confirmHistImport() {
  if (!_histFile) return;
  const agent_id = document.getElementById('histAgentSel').value;
  if (!agent_id) { toast('Select a target agent first'); return; }
  const btn = document.getElementById('histImportBtn');
  const count = document.getElementById('histConvCount').textContent;
  btn.disabled = true;
  btn.textContent = `Importing ${count} conversations…`;
  const res = document.getElementById('histResult');
  res.style.display = 'none';
  try {
    const fd = new FormData();
    fd.append('agent_id', agent_id);
    fd.append('file', _histFile);
    const resp = await fetch('/admin/api/import/conversations', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${S.key}` },
      body: fd,
    });
    if (!resp.ok) { const t = await resp.text(); throw new Error(t); }
    const d = await resp.json();
    res.className = 'bk-result ok';
    res.innerHTML = `✓ Done &nbsp;·&nbsp; Imported: <strong>${d.imported}</strong> &nbsp;·&nbsp; Memories extracted: <strong>${d.memories_created}</strong> &nbsp;·&nbsp; Skipped: ${d.skipped}`;
    res.style.display = '';
    toast('Import complete');
    loadGlobalStats(); loadAgents();
  } catch(err) {
    res.className = 'bk-result err';
    res.textContent = '✕ ' + err.message;
    res.style.display = '';
  } finally {
    btn.disabled = false; btn.textContent = 'Import & Extract';
  }
}

// ── Auto backup ───────────────────────────────────────────────────────────────
function toggleAutoPill() {
  _autoPillOn = !_autoPillOn;
  document.getElementById('autoPill').classList.toggle('on', _autoPillOn);
}

async function saveAutoBackup() {
  const interval = parseInt(document.getElementById('autoInterval').value) || 7;
  try {
    await api('/admin/api/backup/settings', {
      method:'POST', body:{ enabled: String(_autoPillOn), interval_days: String(interval) }
    });
    toast('Auto-backup settings saved');
  } catch(e) { toast('Error: '+e.message); }
}

async function triggerBackup() {
  toast('Creating backup…');
  try {
    const d = await api('/admin/api/backup/trigger', { method:'POST', body:{} });
    toast('Saved: ' + d.filename);
    document.getElementById('autoMeta').textContent =
      `Last backup: now · Files on server: ${d.backup_count}`;
  } catch(e) { toast('Error: '+e.message); }
}
