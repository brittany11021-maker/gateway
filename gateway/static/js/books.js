// ── Books state ───────────────────────────────────────────────────────────────
let _books        = [];
let _bookFilter   = 'all';   // 'all' | 'reading' | 'want' | 'finished'
let _readBook     = null;    // current book object in reading view
let _readPage     = 1;
let _readAnns     = [];      // annotations for current book
let _readToc      = [];      // TOC for current book
let _readAgent    = '';      // currently reading agent
let _annPopup     = null;    // { text, x, y }
let _uploadDrag   = false;
let _tocOpen      = true;    // left TOC panel visibility
let _annOpen      = false;   // right annotations panel visibility

const STATUS_LABEL = { reading:'Reading', want:'Want to Read', finished:'Finished' };
const STATUS_ORDER = ['reading', 'want', 'finished'];

// ── Load books tab ────────────────────────────────────────────────────────────
async function loadBooksTab() {
  document.getElementById('area').classList.remove('read-mode');
  document.querySelector('.toolbar').style.display = '';
  setArea('<div class="u-loading" style="padding:40px 0">Loading…</div>');
  try {
    const d = await api('/api/books');
    _books = d.books || [];
    document.getElementById('n-books').textContent = _books.length;
    renderBooksGrid();
  } catch(e) {
    setArea(`<div class="u-empty" style="padding:40px 0">Error: ${e.message}</div>`);
  }
}

// ── Books grid ────────────────────────────────────────────────────────────────
function renderBooksGrid() {
  const filtered = _bookFilter === 'all'
    ? _books
    : _books.filter(b => b.status === _bookFilter);

  const pills = ['all','reading','want','finished'].map(f => `
    <button class="filter-pill${_bookFilter===f?' active':''}" onclick="_bookFilter='${f}';renderBooksGrid()">${
      f==='all' ? 'All' : STATUS_LABEL[f]
    } <span style="opacity:.55">${
      f==='all' ? _books.length : _books.filter(b=>b.status===f).length
    }</span></button>`).join('');

  if (!filtered.length) {
    setArea(`
      <div class="books-toolbar">
        <div class="books-filters">${pills}</div>
        <button class="btn btn-p" onclick="openUploadModal()">+ Upload</button>
      </div>
      <div class="page-empty" style="margin-top:40px">
        <div class="page-empty-ico">📚</div>
        <div class="page-empty-lbl">${_books.length ? 'No books in this filter' : 'No books yet — upload one'}</div>
      </div>`);
    return;
  }

  // Group by status in order
  const groups = _bookFilter === 'all'
    ? STATUS_ORDER.map(s => ({ status: s, items: _books.filter(b => b.status === s) })).filter(g => g.items.length)
    : [{ status: _bookFilter, items: filtered }];

  const groupsHtml = groups.map(g => `
    <div class="books-section-label">${STATUS_LABEL[g.status]}</div>
    <div class="books-grid">
      ${g.items.map(bookCard).join('')}
    </div>`).join('');

  setArea(`
    <div class="books-toolbar">
      <div class="books-filters">${pills}</div>
      <button class="btn btn-p" onclick="openUploadModal()">+ Upload</button>
    </div>
    ${groupsHtml}`);
}

function bookCard(b) {
  const cover = b.cover_url
    ? `<img src="${esc(b.cover_url)}" alt="${esc(b.title)}">`
    : `<div style="display:flex;align-items:center;justify-content:center;height:100%;font-size:36px;opacity:.3">📖</div>`;

  const progress = b.agents_progress || {};
  const agents = Object.keys(progress);
  const AGENT_COLORS = ['#6366f1','#10b981','#f97316','#e11d48','#0ea5e9','#a855f7'];
  const progBars = agents.length ? agents.map((aid, idx) => {
    const pg  = progress[aid] || 1;
    const pct = b.total_pages > 0 ? Math.round((pg / b.total_pages) * 100) : 0;
    const col = AGENT_COLORS[idx % AGENT_COLORS.length];
    return `<div class="prog-row">
      <span class="agent-dot" style="background:${col}"></span>
      <span class="prog-label">${esc(aid)}</span>
      <div class="prog-track"><div class="prog-fill" style="width:${pct}%;background:${col}"></div></div>
      <span class="prog-pct">${pct}%</span>
    </div>`;
  }).join('') : `<div class="prog-row"><span class="prog-label" style="opacity:.45;font-size:9px">尚无人在读</span></div>`;

  return `
    <div class="book-card" onclick="openReadView('${b.book_id}')">
      <div class="book-cover">${cover}</div>
      <div class="book-info">
        <div class="book-title">${esc(b.title)}</div>
        <div class="book-author">${esc(b.author || '—')}</div>
        <span class="badge-${b.status}">${STATUS_LABEL[b.status]}</span>
        <div class="book-progress">${progBars}</div>
      </div>
    </div>`;
}

// ── Reading view ──────────────────────────────────────────────────────────────
async function openReadView(bookId) {
  _readBook = _books.find(b => b.book_id === bookId);
  if (!_readBook) return;
  document.getElementById('area').classList.add('read-mode');
  document.querySelector('.toolbar').style.display = 'none';

  // Fetch full detail (includes toc + default_agent)
  try {
    const detail = await api(`/api/books/${bookId}`);
    _readToc = detail.toc || [];
    Object.assign(_readBook, detail);
  } catch(e) { _readToc = []; }

  // Set reading agent first so we can look up its specific saved page
  _readAgent = _readBook.default_agent || allAgents[0] || 'user';

  // Resume from current agent's last page; fallback to max across agents, then 1
  const progress = _readBook.agents_progress || {};
  const pickPage = (entry) => parseInt(entry?.page ?? entry) || 0;
  const myPage = pickPage(progress[_readAgent]);
  if (myPage >= 1) {
    _readPage = myPage;
  } else if (Object.keys(progress).length) {
    _readPage = Math.max(1, ...Object.values(progress).map(pickPage));
  } else {
    _readPage = 1;
  }
  if (!Number.isFinite(_readPage) || _readPage < 1) _readPage = 1;

  // Panel defaults: TOC open on wide, both closed on mobile
  _tocOpen = window.innerWidth > 900;
  _annOpen = false;

  const agentOpts = allAgents.map(a =>
    `<option value="${esc(a)}" ${_readAgent===a?'selected':''}>${esc(a)}</option>`
  ).join('');

  setArea(`
    <div class="read-wrap">
      <div class="read-topbar">
        <button class="btn btn-g read-topbar-btn" onclick="loadBooksTab()">←</button>
        <button class="btn btn-g read-topbar-btn" onclick="changeReadPage(-1)" title="上一章">◁</button>
        <button class="btn btn-g read-topbar-btn" onclick="changeReadPage(1)"  title="下一章">▷</button>
        <button class="read-topbar-bookmark" onclick="saveCurrentPage()" title="保存阅读位置">🔖</button>
        <div style="flex:1"></div>
        <select class="form-sel read-topbar-sel" id="rdAgent" title="阅读 Agent"
          onchange="_readAgent=this.value;saveDefaultAgent()">
          ${agentOpts}
        </select>
        <button class="btn btn-g read-topbar-btn" onclick="exportAnnotations('${_readBook.book_id}')">export</button>
      </div>
      <div class="read-body" id="readBody">
        <div class="read-toc-panel${_tocOpen?'':' panel-collapsed'}" id="tocPanel">
          <div class="read-toc-body">
            <div class="toc-list" id="tocList">${renderTocList()}</div>
          </div>
          <div class="read-toc-tab" onclick="togglePanel('toc')">contents</div>
        </div>
        <div class="read-text-col">
          <div class="read-text-panel" id="readText">
            <div class="u-loading">Loading…</div>
          </div>
          <div class="read-text-footer">
            <div class="read-text-footer-inner">
              <span>${esc(_readBook.title)}</span>
              <span><span id="rpNum">${_readPage}</span> / ${_readBook.total_pages}</span>
            </div>
          </div>
        </div>
        <div class="scroll-dots" id="scrollDots"></div>
        <div class="read-ann-panel${_annOpen?'':' panel-collapsed'}" id="annPanel">
          <div class="read-ann-tab" onclick="togglePanel('ann')">annotations</div>
          <div class="read-ann-body">
            <div id="annList"><div class="u-loading" style="padding:16px 0">Loading…</div></div>
          </div>
        </div>
      </div>
    </div>
    <div class="ann-popup" id="annPopup" style="display:none">
      <button class="ann-popup-btn" onclick="openNoteOverlay()">+ 批注</button>
      <button class="ann-popup-btn" onclick="addBookmark()">🔖 书签</button>
      <button class="ann-popup-btn" onclick="openBookChat()">💬 问</button>
    </div>
    <div class="note-overlay" id="noteOverlay" style="display:none" onclick="if(event.target===this)closeNoteOverlay()">
      <div class="note-box" onclick="event.stopPropagation()">
        <div class="note-quote" id="noteQuote"></div>
        <textarea class="note-ta" id="noteTxt" placeholder="备注（选填）…"></textarea>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
          <button class="btn btn-g" onclick="closeNoteOverlay()">取消</button>
          <button class="btn btn-p" onclick="submitAnnotation()">保存</button>
        </div>
      </div>
    </div>
    <div class="note-overlay" id="bookChatOverlay" style="display:none" onclick="if(event.target===this)closeBookChat()">
      <div class="note-box" onclick="event.stopPropagation()" style="max-width:520px;width:92vw">
        <div class="note-quote" id="bookChatQuote"></div>
        <div id="bookChatMsgs" style="max-height:300px;overflow-y:auto;margin:10px 0;display:flex;flex-direction:column;gap:8px"></div>
        <div style="display:flex;gap:6px;align-items:flex-end">
          <textarea class="note-ta" id="bookChatInput" placeholder="问 agent…" style="flex:1;min-height:44px" onkeydown="if((event.metaKey||event.ctrlKey)&&event.key==='Enter'){event.preventDefault();sendBookChat()}"></textarea>
          <button class="btn btn-p" onclick="sendBookChat()" style="height:44px;padding:0 14px">发送</button>
        </div>
        <div style="display:flex;justify-content:flex-end;margin-top:8px">
          <button class="btn btn-g" onclick="closeBookChat()">关闭</button>
        </div>
      </div>
    </div>`);

  await Promise.all([loadReadPage(_readPage), loadAnnotations()]);
  // Persist current page so the next open resumes here
  api(`/api/books/${_readBook.book_id}/progress`, {
    method:'POST', body:{ agent_id: _readAgent, page: _readPage }
  }).catch(() => {});
}

function renderTocList() {
  if (!_readToc.length) return '<div class="u-empty" style="font-size:10px;padding:12px">暂无目录</div>';
  // Build a set of chapter pages that contain at least one bookmark
  const bookmarkPages = new Set(
    _readAnns.filter(a => a.color === 'bookmark').map(a => a.page)
  );
  // A chapter "contains" a bookmark if any bookmark page falls between this
  // chapter's start page and the next chapter's start page.
  const chapterRanges = _readToc.map((ch, i) => ({
    page: ch.page,
    end:  i + 1 < _readToc.length ? _readToc[i + 1].page : Infinity,
  }));
  return _readToc.map((ch, i) => {
    const depth   = ch.depth || 0;
    const indent  = depth * 14;
    const active  = ch.page === _readPage;
    const range   = chapterRanges[i];
    const hasBm   = [...bookmarkPages].some(p => p >= range.page && p < range.end);
    return `<button class="toc-item${active?' toc-active':''}"
      style="padding-left:${10 + indent}px"
      onclick="jumpToTocPage(${ch.page})" title="第${ch.page}页">
      <span class="toc-title">${esc(ch.title)}${hasBm ? ' <span class="toc-bookmark">🔖</span>' : ''}</span>
      <span class="toc-page">p.${ch.page}</span>
    </button>`;
  }).join('');
}

function jumpToTocPage(page) {
  hideAnnPopup();
  loadReadPage(page);
  api(`/api/books/${_readBook.book_id}/progress`, {
    method:'POST', body:{ agent_id: _readAgent, page }
  }).catch(() => {});
}

function togglePanel(which) {
  if (which === 'toc') {
    _tocOpen = !_tocOpen;
    document.getElementById('tocPanel')?.classList.toggle('panel-collapsed', !_tocOpen);
  } else {
    _annOpen = !_annOpen;
    document.getElementById('annPanel')?.classList.toggle('panel-collapsed', !_annOpen);
  }
}

function saveCurrentPage() {
  api(`/api/books/${_readBook.book_id}/progress`, {
    method:'POST', body:{ agent_id: _readAgent, page: _readPage }
  }).then(() => toast('📍 位置已保存')).catch(() => {});
}

async function addBookmark() {
  if (!_annPopup) return;
  const text = _annPopup.text;
  hideAnnPopup();
  try {
    await api(`/api/books/${_readBook.book_id}/annotations`, {
      method:'POST',
      body:{ agent_id: _readAgent, selected_text: text, comment: '', page: _readPage, color: 'bookmark' }
    });
    toast('🔖 书签已添加');
    loadAnnotations();
  } catch(e) { toast('Error: '+e.message); }
}

async function saveDefaultAgent() {
  try {
    await api(`/api/books/${_readBook.book_id}`, {
      method: 'PUT', body: { default_agent: _readAgent }
    });
  } catch(e) { /* silent */ }
}


// ── Mobile swipe & tap navigation ────────────────────────────────────────────
function _initMobileNav() {
  const text = document.getElementById('readText');
  if (!text || text._mobileNavInited) return;
  text._mobileNavInited = true;

  let _tx = 0, _ty = 0;
  text.addEventListener('touchstart', e => {
    _tx = e.changedTouches[0].clientX;
    _ty = e.changedTouches[0].clientY;
  }, { passive: true });

  text.addEventListener('touchend', e => {
    // If user selected text, show annotation popup instead of navigating
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) {
      const txt = sel.toString().trim();
      if (txt) { onTextSelectTouch(e); return; }
    }
    const dx = e.changedTouches[0].clientX - _tx;
    const dy = e.changedTouches[0].clientY - _ty;
    if (Math.abs(dx) < 40 && Math.abs(dy) < 40) {
      const x = e.changedTouches[0].clientX;
      const w = window.innerWidth;
      if (x < w * 0.3) changeReadPage(-1);
      else if (x > w * 0.7) changeReadPage(1);
    } else if (Math.abs(dx) > Math.abs(dy) * 1.5 && Math.abs(dx) > 50) {
      changeReadPage(dx < 0 ? 1 : -1);
    }
  }, { passive: true });
}

async function loadReadPage(page) {
  _readPage = page;
  const rpNum = document.getElementById('rpNum');
  if (rpNum) rpNum.textContent = page;
  const panel = document.getElementById('readText');
  if (!panel) return;
  panel.innerHTML = '<div class="u-loading">Loading…</div>';
  try {
    const d = await api(`/api/books/${_readBook.book_id}/page/${page}`);
    const raw = d.content || '';
    // New books use \n\n paragraph boundaries; old books only have \n
    const paras = raw.includes('\n\n')
      ? raw.split(/\n\n+/).map(p => p.trim()).filter(Boolean)
      : raw.split(/\n/).map(p => p.trim()).filter(Boolean);
    const html  = paras.map(p => `<p>${esc(p)}</p>`).join('');
    panel.innerHTML = `<div class="read-page-content" onmouseup="onTextSelect(event)">${html}</div>`;
    _initMobileNav();
    panel.scrollTop = 0;
    applyHighlights();
    buildScrollDots();
    // Refresh TOC active highlight
    const tocList = document.getElementById('tocList');
    if (tocList) tocList.innerHTML = renderTocList();
    // Auto-open ann panel if an annotation exists on this page
    if (_readAnns.some(a => a.page === page) && !_annOpen) {
      // don't auto-open, just highlight the button
    }
  } catch(e) {
    panel.innerHTML = `<div class="u-empty">Error: ${e.message}</div>`;
  }
}

function changeReadPage(delta) {
  const next = _readPage + delta;
  if (!_readBook || next < 1 || next > _readBook.total_pages) return;
  hideAnnPopup();
  loadReadPage(next);
  // Update agent progress in DB (fire and forget)
  api(`/api/books/${_readBook.book_id}/progress`, {
    method:'POST', body:{ agent_id: _readAgent, page: next }
  }).catch(() => {});
}

// ── Annotation popup on text selection ───────────────────────────────────────
function onTextSelect(e) {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) { hideAnnPopup(); return; }
  const text = sel.toString().trim();
  if (!text) { hideAnnPopup(); return; }
  _annPopup = { text };
  const popup = document.getElementById('annPopup');
  if (!popup) return;
  const rect = e.target.getBoundingClientRect();
  const selRect = sel.getRangeAt(0).getBoundingClientRect();
  popup.style.left = (selRect.left + selRect.width / 2 - 40) + 'px';
  popup.style.top  = (selRect.top + window.scrollY - 42) + 'px';
  popup.style.display = '';
}

function onTextSelectTouch(e) {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) { hideAnnPopup(); return; }
  const text = sel.toString().trim();
  if (!text) { hideAnnPopup(); return; }
  _annPopup = { text };
  const popup = document.getElementById('annPopup');
  if (!popup) return;
  const touch = e.changedTouches[0];
  popup.style.left = Math.max(0, touch.clientX - 60) + 'px';
  popup.style.top  = Math.max(0, touch.clientY + window.scrollY - 52) + 'px';
  popup.style.display = '';
}

function hideAnnPopup() {
  const popup = document.getElementById('annPopup');
  if (popup) popup.style.display = 'none';
  _annPopup = null;
}

function openNoteOverlay() {
  if (!_annPopup) return;
  document.getElementById('noteQuote').textContent = `「${_annPopup.text.slice(0, 160)}${_annPopup.text.length > 160 ? '…' : ''}」`;
  document.getElementById('noteTxt').value = '';
  document.getElementById('noteOverlay').style.display = '';
  setTimeout(() => document.getElementById('noteTxt').focus(), 50);
  hideAnnPopup();
}

function closeNoteOverlay() {
  document.getElementById('noteOverlay').style.display = 'none';
  _annPopup = null;
}

async function submitAnnotation() {
  const quote   = (document.getElementById('noteQuote')?.textContent || '').replace(/^「|」$/g,'').trim();
  const comment = document.getElementById('noteTxt')?.value.trim() || '';
  if (!quote) return;
  try {
    await api(`/api/books/${_readBook.book_id}/annotations`, {
      method:'POST',
      body:{ agent_id: _readAgent, selected_text: quote, comment, page: _readPage }
    });
    toast('批注已保存');
    closeNoteOverlay();
    loadAnnotations();
  } catch(e) { toast('Error: '+e.message); }
}

// ── Annotations sidebar ───────────────────────────────────────────────────────
async function loadAnnotations() {
  try {
    const d = await api(`/api/books/${_readBook.book_id}/annotations`);
    _readAnns = d.annotations || [];
    renderAnnotations();
    applyHighlights();
    // Refresh TOC so bookmark icons stay in sync
    const tocList = document.getElementById('tocList');
    if (tocList) tocList.innerHTML = renderTocList();
  } catch(e) { console.error(e); }
}

function renderAnnotations() {
  const list = document.getElementById('annList');
  if (!list) return;
  if (!_readAnns.length) {
    list.innerHTML = '<div class="u-empty" style="font-size:10px;padding:8px 0">No annotations yet.<br>Select text to add one.</div>';
    return;
  }
  list.innerHTML = _readAnns.map(a => {
    const hasCmt   = !!a.comment;
    const isBookmk = a.color === 'bookmark';
    const cardCls  = hasCmt ? 'ann-card ann-bubble' : 'ann-card ann-plain';
    const tag      = isBookmk ? '<span class="ann-bookmark" title="书签">🔖</span>' : '';
    return `
    <div class="${cardCls}">
      ${tag}<div class="ann-quote">${esc(a.selected_text.slice(0,100))}${a.selected_text.length>100?'…':''}</div>
      ${hasCmt ? `<div class="ann-comment">${esc(a.comment)}</div>` : ''}
      <div class="ann-meta">
        <span style="font-weight:600;font-size:10px">${esc(a.agent_id)}</span>
        <span style="color:var(--muted);font-size:10px">· p.${a.page} · ${fmtIsoDate(a.created_at)}</span>
        <button class="ann-copy" onclick="navigator.clipboard.writeText(${JSON.stringify(a.selected_text+(a.comment?'\n\n'+a.comment:''))});toast('Copied')" title="Copy">⎘</button>
        <button class="ann-copy" onclick="deleteAnnotation('${a.annotation_id}')" title="Delete" style="color:var(--danger)">✕</button>
      </div>
    </div>`;
  }).join('');
}

async function deleteAnnotation(id) {
  if (!confirm('Delete this annotation?')) return;
  try {
    await api(`/api/books/${_readBook.book_id}/annotations/${id}`, { method:'DELETE' });
    _readAnns = _readAnns.filter(a => a.annotation_id !== id);
    renderAnnotations();
    toast('Deleted');
  } catch(e) { toast('Error: '+e.message); }
}

// ── Book status update ────────────────────────────────────────────────────────
async function updateBookStatus() {
  const status = document.getElementById('rdStatus').value;
  try {
    await api(`/api/books/${_readBook.book_id}`, { method:'PUT', body:{ status } });
    _readBook.status = status;
    toast('Status updated');
  } catch(e) { toast('Error: '+e.message); }
}

// ── Delete book ───────────────────────────────────────────────────────────────
async function confirmDeleteBook(bookId) {
  const book = _books.find(b => b.book_id === bookId);
  if (!book) return;
  if (!confirm(`Delete "${book.title}"?\nThis will remove the book, all pages, annotations and search index.`)) return;
  try {
    await api(`/api/books/${bookId}`, { method:'DELETE' });
    toast('Book deleted');
    _books = _books.filter(b => b.book_id !== bookId);
    document.getElementById('n-books').textContent = _books.length;
    renderBooksGrid();
  } catch(e) { toast('Error: '+e.message); }
}

// ── Export annotations ────────────────────────────────────────────────────────
async function exportAnnotations(bookId) {
  const book = _books.find(b => b.book_id === bookId);
  if (!book) return;
  try {
    const d = await api(`/api/books/${bookId}/annotations`);
    const anns = d.annotations || [];
    if (!anns.length) { toast('No annotations to export'); return; }
    let md = `# ${book.title}\n\n`;
    for (const a of anns) {
      md += `> ${a.selected_text}\n`;
      if (a.comment) md += `\n${a.comment}\n`;
      md += `\n— ${a.agent_id} · p.${a.page}\n\n---\n\n`;
    }
    const blob = new Blob([md], { type:'text/markdown' });
    const url  = URL.createObjectURL(blob);
    Object.assign(document.createElement('a'),
      { href: url, download: `${book.title.replace(/[^a-z0-9]/gi,'_')}_annotations.md` }).click();
    URL.revokeObjectURL(url);
    toast('Exported annotations');
  } catch(e) { toast('Error: '+e.message); }
}

// ── Highlight annotations in text ────────────────────────────────────────────
function applyHighlights() {
  const container = document.querySelector('.read-page-content');
  if (!container || !_readAnns.length) return;
  const pageAnns = _readAnns.filter(a => a.page === _readPage);
  if (!pageAnns.length) return;
  // Longest first so shorter matches don't split longer ones
  pageAnns.sort((a, b) => b.selected_text.length - a.selected_text.length);
  let html = container.innerHTML;
  for (const a of pageAnns) {
    const sel = a.selected_text.trim();
    if (!sel) continue;
    const escHtml = esc(sel);
    const escRegex = escHtml.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const cls = a.comment ? 'mark-quoted' : '';
    const repl = `<mark class="${cls}" data-aid="${a.annotation_id}">${escHtml}</mark>`;
    try {
      html = html.replace(new RegExp(escRegex, 'g'), repl);
    } catch(e) { /* skip malformed regex */ }
  }
  container.innerHTML = html;
}

// ── Scroll dots ───────────────────────────────────────────────────────────────
function buildScrollDots() {
  const dots = document.getElementById('scrollDots');
  const panel = document.getElementById('readText');
  if (!dots || !panel) return;
  const totalDots = Math.max(8, Math.min(20, Math.ceil(panel.scrollHeight / Math.max(panel.clientHeight, 1)) * 4));
  dots.innerHTML = Array.from({length: totalDots}, (_, i) =>
    `<div class="dot" data-i="${i}" onclick="jumpScrollDot(${i}, ${totalDots})"></div>`
  ).join('');
  updateScrollDots();
  panel.onscroll = updateScrollDots;
}

function updateScrollDots() {
  const dots = document.getElementById('scrollDots');
  const panel = document.getElementById('readText');
  if (!dots || !panel) return;
  const all = dots.querySelectorAll('.dot');
  if (!all.length) return;
  const max = panel.scrollHeight - panel.clientHeight;
  const ratio = max > 0 ? panel.scrollTop / max : 0;
  const activeIdx = Math.min(all.length - 1, Math.floor(ratio * all.length));
  all.forEach((d, i) => d.classList.toggle('active', i === activeIdx));
}

function jumpScrollDot(i, total) {
  const panel = document.getElementById('readText');
  if (!panel) return;
  const max = panel.scrollHeight - panel.clientHeight;
  panel.scrollTo({ top: max * (i / Math.max(total - 1, 1)), behavior: 'smooth' });
}

// ── Upload modal ──────────────────────────────────────────────────────────────
function openUploadModal() {
  document.getElementById('uploadTitle').value  = '';
  document.getElementById('uploadAuthor').value = '';
  document.getElementById('uploadStatus').value = 'want';
  document.getElementById('uploadFile').value   = '';
  document.getElementById('uploadFileName').textContent = 'No file selected';
  document.getElementById('uploadProgress').style.display = 'none';
  // Populate agent dropdown
  const sel = document.getElementById('uploadAgent');
  if (sel) {
    sel.innerHTML = '<option value="">— 不指定 —</option>' +
      allAgents.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
    // Pre-select first agent if any
    if (allAgents.length) sel.value = allAgents[0];
  }
  document.getElementById('bookUploadOv').classList.add('open');
}

function closeUploadModal() {
  document.getElementById('bookUploadOv').classList.remove('open');
}

function onUploadFileChange(input) {
  const file = input.files[0];
  if (!file) return;
  document.getElementById('uploadFileName').textContent = file.name;
  // Pre-fill title from filename (strip extension)
  if (!document.getElementById('uploadTitle').value) {
    document.getElementById('uploadTitle').value = file.name.replace(/\.[^.]+$/, '');
  }
}

function onUploadDrop(e) {
  e.preventDefault();
  document.getElementById('uploadDropZone').classList.remove('drag-over');
  const file = e.dataTransfer?.files?.[0];
  if (!file) return;
  const input = document.getElementById('uploadFile');
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  onUploadFileChange(input);
}

async function doUpload() {
  const file = document.getElementById('uploadFile').files[0];
  if (!file) { toast('Select a file first'); return; }
  if (_books.length >= 4) { toast('Max 4 books allowed'); return; }
  const title         = document.getElementById('uploadTitle').value.trim() || file.name.replace(/\.[^.]+$/,'');
  const author        = document.getElementById('uploadAuthor').value.trim();
  const status        = document.getElementById('uploadStatus').value;
  const default_agent = document.getElementById('uploadAgent')?.value || '';

  const prog = document.getElementById('uploadProgress');
  prog.style.display = '';
  prog.textContent   = 'Uploading…';

  try {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('title', title);
    fd.append('author', author);
    fd.append('status', status);
    fd.append('default_agent', default_agent);
    const resp = await fetch('/api/books/upload', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${S.key}` },
      body: fd,
    });
    if (!resp.ok) { const t = await resp.text(); throw new Error(t); }
    const d = await resp.json();
    toast(`"${d.title}" uploaded (${d.total_pages} pages)`);
    closeUploadModal();
    await loadBooksTab();
  } catch(e) {
    prog.textContent = '✕ ' + e.message;
    toast('Upload failed: ' + e.message);
  }
}

// ── Book chat (划线问 agent) ──────────────────────────────────────────────────
let _bookChatHistory = [];
let _bookChatQuote   = '';

function openBookChat() {
  if (!_annPopup) return;
  _bookChatQuote   = _annPopup.text;
  _bookChatHistory = [];
  hideAnnPopup();
  const overlay = document.getElementById('bookChatOverlay');
  if (!overlay) return;
  document.getElementById('bookChatQuote').textContent =
    '\u300c' + _bookChatQuote.slice(0, 120) + (_bookChatQuote.length > 120 ? '\u2026' : '') + '\u300d';
  document.getElementById('bookChatMsgs').innerHTML = '';
  const inp = document.getElementById('bookChatInput');
  if (inp) { inp.placeholder = '\u95ee ' + _readAgent + '\u2026'; inp.value = ''; }
  overlay.style.display = '';
  setTimeout(() => inp && inp.focus(), 50);
}

function closeBookChat() {
  const el = document.getElementById('bookChatOverlay');
  if (el) el.style.display = 'none';
  _bookChatHistory = [];
  _bookChatQuote   = '';
}

function _appendBookChatMsg(role, text) {
  const box = document.getElementById('bookChatMsgs');
  if (!box) return;
  const d = document.createElement('div');
  d.style.cssText = role === 'user'
    ? 'align-self:flex-end;background:var(--accent,#6366f1);color:#fff;padding:6px 10px;border-radius:10px 10px 2px 10px;font-size:13px;max-width:85%;white-space:pre-wrap'
    : 'align-self:flex-start;background:var(--surface2,#2a2a2a);padding:6px 10px;border-radius:10px 10px 10px 2px;font-size:13px;max-width:85%;white-space:pre-wrap';
  d.textContent = text;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

async function sendBookChat() {
  const inp = document.getElementById('bookChatInput');
  const msg = inp ? inp.value.trim() : '';
  if (!msg) return;
  inp.value = '';
  _appendBookChatMsg('user', msg);

  // First turn: embed context into user message, preserving agent system prompt
  let userContent = msg;
  if (_bookChatHistory.length === 0) {
    const title = (_readBook && _readBook.title) || '';
    const pageEl = document.getElementById('readText');
    const pageText = pageEl ? (pageEl.innerText || pageEl.textContent || '').trim() : '';
    userContent = '\u300a' + title + '\u300b \u7b2c' + _readPage + '\u9875\n'
      + '\u5212\u7ebf\uff1a\u300c' + _bookChatQuote + '\u300d\n\n'
      + (pageText ? '\u672c\u9875\u5185\u5bb9\uff1a\n' + pageText.slice(0, 2000) + (pageText.length > 2000 ? '\n\u2026' : '') + '\n\n' : '')
      + msg;
  }
  _bookChatHistory.push({ role: 'user', content: userContent });

  const thinking = document.createElement('div');
  thinking.style.cssText = 'align-self:flex-start;opacity:.45;font-size:12px;padding:4px 8px';
  thinking.textContent = '\u2026';
  document.getElementById('bookChatMsgs').appendChild(thinking);
  document.getElementById('bookChatMsgs').scrollTop = 9999;

  try {
    const resp = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + S.key,
        'X-Agent-ID': _readAgent,
      },
      body: JSON.stringify({ messages: _bookChatHistory, stream: false }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data  = await resp.json();
    const reply = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '(\u65e0\u56de\u590d)';
    thinking.remove();
    _appendBookChatMsg('assistant', reply);
    _bookChatHistory.push({ role: 'assistant', content: reply });
  } catch(e) {
    thinking.remove();
    _appendBookChatMsg('assistant', '\u9519\u8bef\uff1a' + e.message);
  }
}
