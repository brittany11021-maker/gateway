/* worldbook.js — Worldbook admin: books + entries, full CRUD */

// ── State ──────────────────────────────────────────────────────────────────
let _wbAgentId    = '';
let _wbBooks      = [];
let _wbOpenBookId = null;
let _wbEntries    = [];
let _wbEditEntry  = null;  // null = new, obj = editing

async function loadWorldbookTab() {
  const area = document.getElementById('area');
  area.innerHTML = '<div class="page-loading">Loading worldbook…</div>';
  area.classList.remove('read-mode');
  _renderWbShell();
  await _fetchBooks();
}

function _renderWbShell() {
  const agents = (typeof allAgents !== 'undefined' ? allAgents : []);
  const agentOpts = ['<option value="">— Global (all agents) —</option>',
    ...agents.map(a => `<option value="${_e(a)}">${_e(a)}</option>`)].join('');
  document.getElementById('area').innerHTML = `
    <div class="wb-layout">
      <!-- Left: book list -->
      <div class="wb-sidebar" id="wbSidebar">
        <div class="wb-sidebar-head">
          <span style="font-weight:700;font-size:13px">📚 Books</span>
          <button class="btn btn-s btn-accent" onclick="_openNewBookModal()">+ Book</button>
        </div>
        <div class="wb-filter-row">
          <select class="form-sel" id="wbAgentFilter" onchange="_onAgentFilter(this.value)" style="font-size:11px">
            ${agentOpts}
          </select>
        </div>
        <div id="wbBookList" class="wb-book-list"></div>
      </div>
      <!-- Right: entry list / editor -->
      <div class="wb-main" id="wbMain">
        <div class="wb-main-empty">← Select a book to manage entries</div>
      </div>
    </div>

    <!-- Book modal -->
    <div class="ov" id="wbBookOv" onclick="if(event.target===this)_closeBookModal()">
      <div class="modal" onclick="event.stopPropagation()" style="width:420px">
        <div class="modal-title" id="wbBookModalTitle">New Book</div>
        <div class="form-g">
          <label class="form-lbl">Name</label>
          <input class="form-in" id="wbBName" placeholder="e.g. Chiaki's World">
        </div>
        <div class="form-g">
          <label class="form-lbl">简介</label>
          <textarea class="form-ta" id="wbBDesc" style="min-height:60px" placeholder="What this book is for…"></textarea>
        </div>
        <div class="form-g">
          <label class="form-lbl">Associate with Agent <span style="opacity:.45;font-weight:400">(blank = global)</span></label>
          <select class="form-sel" id="wbBAgent">${agentOpts}</select>
        </div>
        <div class="form-g" style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="wbBEnabled" checked style="width:14px;height:14px">
          <label style="font-size:12px;cursor:pointer" for="wbBEnabled">Enabled</label>
        </div>
        <div class="modal-acts">
          <button class="btn btn-g" onclick="_closeBookModal()">Cancel</button>
          <button class="btn btn-p" onclick="_saveBook()">Save</button>
        </div>
        <input type="hidden" id="wbBId">
      </div>
    </div>

    <!-- Entry editor panel (slide-over) -->
    <div class="ov" id="wbEntryOv" onclick="if(event.target===this)_closeEntryEditor()">
      <div class="modal wb-entry-modal" onclick="event.stopPropagation()">
        <div class="modal-title" id="wbEntryTitle">New Entry</div>
        <div style="display:flex;gap:12px">
          <div style="flex:2">
            <div class="form-g">
              <label class="form-lbl">Entry Name</label>
              <input class="form-in" id="weEName" placeholder="e.g. Chiaki's background">
            </div>
            <div class="form-g">
              <label class="form-lbl">Content <span style="opacity:.45;font-weight:400">(injected into context)</span></label>
              <textarea class="form-ta" id="weEContent" style="min-height:160px;font-size:12px;font-family:inherit"
                placeholder="The lore, persona, or world info to inject…"></textarea>
            </div>
          </div>
          <div style="flex:1;min-width:200px">
            <div class="form-g">
              <label class="form-lbl">Enabled</label>
              <label style="display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer;margin-top:4px">
                <input type="checkbox" id="weEEnabled" checked style="width:14px;height:14px"> Active
              </label>
            </div>
            <div class="form-g">
              <label class="form-lbl">Activation</label>
              <label style="display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer;margin-top:4px">
                <input type="checkbox" id="weEConstant" checked onchange="_onConstantChange()" style="width:14px;height:14px">
                Always on (常驻)
              </label>
            </div>
            <div id="weTriggerGroup" style="display:none">
              <div class="form-g">
                <label class="form-lbl">Trigger Mode</label>
                <select class="form-sel" id="weETrigger" onchange="_onTriggerModeChange()" style="font-size:11px">
                  <option value="keyword">🔑 Keyword match</option>
                  <option value="regex">🔍 Regex</option>
                  <option value="vector">🧠 Vector search</option>
                </select>
              </div>
              <div id="weTriggerKeywords" class="form-g">
                <label class="form-lbl">Keywords <span style="opacity:.45;font-weight:400">comma-separated</span></label>
                <input class="form-in" id="weEKeywords" placeholder="e.g. 学校, 朋友, school" style="font-size:11px">
              </div>
              <div id="weTriggerRegex" class="form-g" style="display:none">
                <label class="form-lbl">Regex Pattern</label>
                <input class="form-in" id="weERegex" placeholder="e.g. (学校|school)" style="font-size:11px">
              </div>
              <div class="form-g">
                <label class="form-lbl">Scan Depth <span style="opacity:.45;font-weight:400">(messages)</span></label>
                <input class="form-in" id="weEScanDepth" type="number" min="1" max="20" value="3" style="width:70px;font-size:11px">
              </div>
            </div>
            <div class="form-g">
              <label class="form-lbl">Injection Position</label>
              <select class="form-sel" id="weEPosition" style="font-size:11px">
                <option value="after_system">After system prompt</option>
                <option value="before_system">Before system prompt</option>
              </select>
            </div>
            <div class="form-g">
              <label class="form-lbl">Injected as Role</label>
              <select class="form-sel" id="weERole" style="font-size:11px">
                <option value="system">system</option>
                <option value="user">user</option>
                <option value="assistant">assistant</option>
              </select>
            </div>
            <div class="form-g">
              <label class="form-lbl">Priority <span style="opacity:.45;font-weight:400">1(high)…99(low)</span></label>
              <input class="form-in" id="weEPriority" type="number" min="1" max="99" value="10" style="width:70px;font-size:11px">
            </div>
            <div class="form-g">
              <label class="form-lbl">Agent Override <span style="opacity:.45;font-weight:400">(blank=book's agent)</span></label>
              <input class="form-in" id="weEAgent" placeholder="agent_id" style="font-size:11px">
            </div>
          </div>
        </div>
        <div class="modal-acts spread">
          <button class="btn btn-d" id="weDelBtn" onclick="_deleteEntry()" style="display:none">Delete</button>
          <div style="display:flex;gap:10px">
            <button class="btn btn-g" onclick="_closeEntryEditor()">Cancel</button>
            <button class="btn btn-p" onclick="_saveEntry()">Save</button>
          </div>
        </div>
        <input type="hidden" id="weEId">
        <input type="hidden" id="weEBookId">
      </div>
    </div>`;
}

// ── Agent filter ──────────────────────────────────────────────────────────
function _onAgentFilter(val) {
  _wbAgentId = val;
  _fetchBooks();
}

// ── Books ─────────────────────────────────────────────────────────────────
async function _fetchBooks() {
  const list = document.getElementById('wbBookList');
  if (!list) return;
  try {
    const data = await apiFetch('/admin/api/worldbook/books?agent_id=' + encodeURIComponent(_wbAgentId));
    _wbBooks = data.books || [];
    _renderBookList();
    if (_wbOpenBookId) _loadEntries(_wbOpenBookId);
  } catch(e) { list.innerHTML = `<div style="color:var(--danger);font-size:11px;padding:10px">${e}</div>`; }
}

function _renderBookList() {
  const list = document.getElementById('wbBookList');
  if (!list) return;
  if (!_wbBooks.length) {
    list.innerHTML = '<div class="wb-empty">No books yet. Create one!</div>'; return;
  }
  list.innerHTML = _wbBooks.map(b => `
    <div class="wb-book-item ${b.id===_wbOpenBookId?'active':''}" onclick="_openBook('${_e(b.id)}')">
      <div class="wb-book-row">
        <span class="wb-book-dot" style="background:${b.enabled?'var(--ok)':'var(--muted)'}"></span>
        <span class="wb-book-name">${_e(b.name)}</span>
        ${b.agent_id ? `<span class="wb-book-agent">${_e(b.agent_id)}</span>` : '<span class="wb-book-global">global</span>'}
      </div>
      ${b.description ? `<div class="wb-book-desc">${_e(b.description.substring(0,60))}${b.description.length>60?'…':''}</div>` : ''}
    </div>`).join('');
}

function _openBook(bookId) {
  _wbOpenBookId = bookId;
  _renderBookList();
  _loadEntries(bookId);
}

async function _loadEntries(bookId) {
  const main = document.getElementById('wbMain');
  if (!main) return;
  const book = _wbBooks.find(b => b.id === bookId);
  main.innerHTML = `
    <div class="wb-entries-head">
      <div style="display:flex;align-items:center;gap:10px;flex:1">
        <span class="wb-entries-title">${_e(book?.name||'Book')}</span>
        ${book?.agent_id ? `<span class="wb-book-agent">${_e(book.agent_id)}</span>` : '<span class="wb-book-global">global</span>'}
        <span class="wb-toggle-chip ${book?.enabled?'on':'off'}" onclick="_toggleBook('${_e(bookId)}')">${book?.enabled?'ON':'OFF'}</span>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-s btn-g" onclick="_openEditBookModal('${_e(bookId)}')">✎ Edit book</button>
        <button class="btn btn-s btn-accent" onclick="_openNewEntryEditor('${_e(bookId)}')">+ Entry</button>
      </div>
    </div>
    ${book?.description ? `<div class="wb-entries-desc">${_e(book.description)}</div>` : ''}
    <div id="wbEntryList" class="wb-entry-list"><div class="page-loading" style="font-size:11px">Loading…</div></div>`;
  try {
    const data = await apiFetch('/admin/api/worldbook/entries?book_id=' + encodeURIComponent(bookId));
    _wbEntries = data.entries || [];
    _renderEntryList();
  } catch(e) {
    document.getElementById('wbEntryList').innerHTML =
      `<div style="color:var(--danger);font-size:11px">${e}</div>`;
  }
}

function _renderEntryList() {
  const el = document.getElementById('wbEntryList');
  if (!el) return;
  if (!_wbEntries.length) {
    el.innerHTML = '<div class="wb-empty">No entries. Add one with the + button.</div>'; return;
  }
  el.innerHTML = _wbEntries.map(e => {
    const triggerBadge = e.constant
      ? '<span class="wb-badge constant">常驻</span>'
      : `<span class="wb-badge trigger">${e.trigger_mode}</span>`;
    const posBadge = `<span class="wb-badge pos">${e.position==='before_system'?'↑sys':'sys↓'}</span>`;
    const roleBadge = `<span class="wb-badge role">${e.role}</span>`;
    const priColor  = e.priority <= 3 ? 'var(--danger)' : e.priority >= 50 ? 'var(--muted)' : 'var(--text)';
    return `
    <div class="wb-entry-row ${e.enabled?'':'disabled'}" onclick="_openEditEntryEditor(${JSON.stringify(JSON.stringify(e))})">
      <div class="wb-entry-top">
        <span class="wb-entry-dot" style="background:${e.enabled?'var(--ok)':'var(--muted)'}"></span>
        <span class="wb-entry-name">${_e(e.name||'(unnamed)')}</span>
        ${triggerBadge} ${posBadge} ${roleBadge}
        <span style="margin-left:auto;font-size:10px;font-weight:700;color:${priColor}">P${e.priority}</span>
        <button class="btn-icon" onclick="event.stopPropagation();_quickToggleEntry('${_e(e.id)}')"
          title="${e.enabled?'Disable':'Enable'}" style="margin-left:6px">${e.enabled?'●':'○'}</button>
      </div>
      <div class="wb-entry-preview">${_e((e.content||'').substring(0,120))}${(e.content||'').length>120?'…':''}</div>
    </div>`;
  }).join('');
}

async function _quickToggleEntry(id) {
  try {
    const r = await apiFetch(`/admin/api/worldbook/entries/${id}/toggle`, {method:'PATCH', body:'{}'});
    const idx = _wbEntries.findIndex(e => e.id === id);
    if (idx >= 0) { _wbEntries[idx].enabled = r.enabled; _renderEntryList(); }
  } catch(e) { toast('Error: '+e); }
}

async function _toggleBook(id) {
  try {
    const r = await apiFetch(`/admin/api/worldbook/books/${id}/toggle`, {method:'PATCH', body:'{}'});
    const idx = _wbBooks.findIndex(b => b.id === id);
    if (idx >= 0) { _wbBooks[idx].enabled = r.enabled; _renderBookList(); _loadEntries(id); }
  } catch(e) { toast('Error: '+e); }
}

// ── Book modal ────────────────────────────────────────────────────────────
function _openNewBookModal() {
  document.getElementById('wbBookModalTitle').textContent = 'New Book';
  document.getElementById('wbBId').value = '';
  document.getElementById('wbBName').value = '';
  document.getElementById('wbBDesc').value = '';
  document.getElementById('wbBEnabled').checked = true;
  document.getElementById('wbBAgent').value = _wbAgentId;
  document.getElementById('wbBookOv').classList.add('open');
}
function _openEditBookModal(id) {
  const b = _wbBooks.find(x => x.id === id);
  if (!b) return;
  document.getElementById('wbBookModalTitle').textContent = 'Edit Book';
  document.getElementById('wbBId').value = b.id;
  document.getElementById('wbBName').value = b.name;
  document.getElementById('wbBDesc').value = b.description||'';
  document.getElementById('wbBEnabled').checked = b.enabled;
  document.getElementById('wbBAgent').value = b.agent_id||'';
  document.getElementById('wbBookOv').classList.add('open');
}
function _closeBookModal() { document.getElementById('wbBookOv').classList.remove('open'); }

async function _saveBook() {
  const id   = document.getElementById('wbBId').value;
  const body = {
    name:        document.getElementById('wbBName').value.trim(),
    description: document.getElementById('wbBDesc').value.trim(),
    enabled:     document.getElementById('wbBEnabled').checked,
    agent_id:    document.getElementById('wbBAgent').value,
  };
  if (!body.name) return toast('Name required');
  try {
    if (id) {
      await apiFetch(`/admin/api/worldbook/books/${id}`, {method:'PUT', body:JSON.stringify(body)});
    } else {
      await apiFetch('/admin/api/worldbook/books', {method:'POST', body:JSON.stringify(body)});
    }
    _closeBookModal(); await _fetchBooks();
    toast(id ? 'Book updated' : 'Book created');
  } catch(e) { toast('Error: '+e); }
}

// ── Entry editor ──────────────────────────────────────────────────────────
function _onConstantChange() {
  const constant = document.getElementById('weEConstant').checked;
  document.getElementById('weTriggerGroup').style.display = constant ? 'none' : '';
}
function _onTriggerModeChange() {
  const mode = document.getElementById('weETrigger').value;
  document.getElementById('weTriggerKeywords').style.display = mode === 'regex' ? 'none' : '';
  document.getElementById('weTriggerRegex').style.display    = mode === 'regex' ? '' : 'none';
}

function _fillEntryEditor(entry, bookId) {
  document.getElementById('weEId').value       = entry?.id || '';
  document.getElementById('weEBookId').value   = bookId || entry?.book_id || _wbOpenBookId || '';
  document.getElementById('weEName').value     = entry?.name || '';
  document.getElementById('weEContent').value  = entry?.content || '';
  document.getElementById('weEEnabled').checked = entry ? entry.enabled : true;
  document.getElementById('weEConstant').checked = entry ? entry.constant : true;
  document.getElementById('weETrigger').value  = entry?.trigger_mode || 'keyword';
  const kws = (entry?.keywords || []).join(', ');
  document.getElementById('weEKeywords').value = kws;
  document.getElementById('weERegex').value    = entry?.regex || '';
  document.getElementById('weEScanDepth').value = entry?.scan_depth ?? 3;
  document.getElementById('weEPosition').value = entry?.position || 'after_system';
  document.getElementById('weERole').value     = entry?.role || 'system';
  document.getElementById('weEPriority').value = entry?.priority ?? 10;
  document.getElementById('weEAgent').value    = entry?.agent_id || '';
  document.getElementById('weDelBtn').style.display = entry ? '' : 'none';
  _onConstantChange();
  _onTriggerModeChange();
}

function _openNewEntryEditor(bookId) {
  _wbEditEntry = null;
  document.getElementById('wbEntryTitle').textContent = 'New Entry';
  _fillEntryEditor(null, bookId);
  document.getElementById('wbEntryOv').classList.add('open');
}

function _openEditEntryEditor(entryJson) {
  const entry = JSON.parse(entryJson);
  _wbEditEntry = entry;
  document.getElementById('wbEntryTitle').textContent = 'Edit Entry';
  _fillEntryEditor(entry, entry.book_id);
  document.getElementById('wbEntryOv').classList.add('open');
}
function _closeEntryEditor() { document.getElementById('wbEntryOv').classList.remove('open'); }

async function _saveEntry() {
  const id     = document.getElementById('weEId').value;
  const bookId = document.getElementById('weEBookId').value;
  const kwRaw  = document.getElementById('weEKeywords').value;
  const kws    = kwRaw.split(',').map(s=>s.trim()).filter(Boolean);
  const body   = {
    book_id:      bookId || null,
    agent_id:     document.getElementById('weEAgent').value.trim(),
    name:         document.getElementById('weEName').value.trim(),
    enabled:      document.getElementById('weEEnabled').checked,
    content:      document.getElementById('weEContent').value,
    constant:     document.getElementById('weEConstant').checked,
    trigger_mode: document.getElementById('weETrigger').value,
    keywords:     kws,
    regex:        document.getElementById('weERegex').value.trim(),
    scan_depth:   parseInt(document.getElementById('weEScanDepth').value)||3,
    position:     document.getElementById('weEPosition').value,
    role:         document.getElementById('weERole').value,
    priority:     parseInt(document.getElementById('weEPriority').value)||10,
  };
  try {
    if (id) {
      await apiFetch(`/admin/api/worldbook/entries/${id}`, {method:'PUT', body:JSON.stringify(body)});
    } else {
      await apiFetch('/admin/api/worldbook/entries', {method:'POST', body:JSON.stringify(body)});
    }
    _closeEntryEditor();
    if (_wbOpenBookId) await _loadEntries(_wbOpenBookId);
    toast(id ? 'Entry updated' : 'Entry created');
  } catch(e) { toast('Error: '+e); }
}

async function _deleteEntry() {
  const id = document.getElementById('weEId').value;
  if (!id || !confirm('Delete this entry?')) return;
  try {
    await apiFetch(`/admin/api/worldbook/entries/${id}`, {method:'DELETE'});
    _closeEntryEditor();
    if (_wbOpenBookId) await _loadEntries(_wbOpenBookId);
    toast('Entry deleted');
  } catch(e) { toast('Error: '+e); }
}

function _e(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
