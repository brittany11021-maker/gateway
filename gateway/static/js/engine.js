/* engine.js – P3 State Engine: Config + Accumulators + Push Observability */

let _engSubTab   = 'config';
let _engAgentId  = '';
let _logSubTab   = 'pushlog';

// ── Shell ──────────────────────────────────────────────────────────────────
function loadEngineTab() {
  document.getElementById('area').innerHTML = '<div class="page-loading">Loading…</div>';
  document.getElementById('area').classList.remove('read-mode');
  _renderEngineShell();
  _switchEngSub(_engSubTab);
}

function _renderEngineShell() {
  document.getElementById('area').innerHTML = `
    <div class="d-tabs">
      <button class="d-tab" id="etab-config" onclick="_switchEngSub('config')">⚙️ Config</button>
      <button class="d-tab" id="etab-state"  onclick="_switchEngSub('state')">🧠 State</button>
      <button class="d-tab" id="etab-logs"   onclick="_switchEngSub('logs')">📊 Logs</button>
    </div>
    <div id="eng-sub"></div>`;
}

function _switchEngSub(tab) {
  _engSubTab = tab;
  ['config','state','logs'].forEach(t => {
    document.getElementById('etab-'+t)?.classList.toggle('active', t === tab);
  });
  const sub = document.getElementById('eng-sub');
  if (!sub) return;
  sub.innerHTML = '<div class="page-loading">Loading…</div>';
  if      (tab === 'config') _loadEngConfig();
  else if (tab === 'state')  _loadEngState();
  else if (tab === 'logs')   _loadEngLogs();
}

// ── Config ─────────────────────────────────────────────────────────────────
async function _loadEngConfig() {
  const sub = document.getElementById('eng-sub');
  try {
    const [routesResp, tzResp, morningResp, eveningResp, pushResp, newsResp, healthResp,
           musicResp, accumResp, sysInfoResp] = await Promise.all([
      apiFetch('/admin/api/config/api-routes').catch(()    => ({})),
      apiFetch('/admin/api/config/timezone').catch(()      => ({})),
      apiFetch('/admin/api/config/morning-push').catch(()  => ({})),
      apiFetch('/admin/api/config/evening-push').catch(()  => ({})),
      apiFetch('/admin/api/config/push-control').catch(()  => ({})),
      apiFetch('/admin/api/config/news').catch(()          => ({})),
      apiFetch('/admin/api/config/health').catch(()        => ({})),
      apiFetch('/admin/api/config/music').catch(()         => ({})),
      apiFetch('/admin/api/config/accumulator').catch(()   => ({})),
      apiFetch('/admin/api/config/system-info').catch(()   => ({})),
    ]);
    sub.innerHTML = _buildConfigPanel(
      routesResp, tzResp, morningResp, eveningResp, pushResp, newsResp, healthResp,
      musicResp, accumResp, sysInfoResp
    );
  } catch(e) {
    sub.innerHTML = `<div class="page-loading" style="color:var(--danger)">Error: ${e}</div>`;
  }
}

const _COMMON_TZ = [
  'Asia/Shanghai','Asia/Tokyo','Asia/Seoul','Asia/Kolkata',
  'Asia/Singapore','Asia/Hong_Kong','Europe/London','Europe/Paris',
  'America/New_York','America/Los_Angeles','UTC',
];

function _tzOpts(selected) {
  const all = [...new Set([..._COMMON_TZ, selected||''])].filter(Boolean);
  return all.map(t =>
    `<option value="${esc(t)}" ${t===(selected||'')?'selected':''}>${esc(t)}</option>`
  ).join('');
}

function _buildConfigPanel(routesResp, tzResp, morningResp, eveningResp, pushResp, newsResp, healthResp,
                           musicResp, accumResp, sysInfoResp) {
  // ── API Routes ───────────────────────────────────────────────────────────
  // GET /admin/api/config/api-routes → {routes: {proactive_push:{…}, analyzer:{…}}}
  const rMap = routesResp.routes || {};
  const routeNames   = ['proactive_push','analyzer'];
  const routeLabels  = { proactive_push:'推送线路 (proactive_push)', analyzer:'分析线路 (analyzer)' };

  const routeCards = routeNames.map(name => {
    const cfg      = rMap[name] || {};
    const fallback = Array.isArray(cfg.fallback_chain) ? cfg.fallback_chain.join(', ') : '';
    return `
      <div class="eng-route-card">
        <div class="eng-route-name">${routeLabels[name]}</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <div style="min-width:130px;flex:1">
            <label class="form-lbl">Provider</label>
            <input class="form-in" id="route-${name}-provider"
              value="${esc(cfg.provider||'')}" placeholder="nvidia-llm">
          </div>
          <div style="min-width:200px;flex:2">
            <label class="form-lbl">Model <span style="opacity:.5">(blank = provider default)</span></label>
            <input class="form-in" id="route-${name}-model"
              value="${esc(cfg.model||'')}" placeholder="e.g. meta/llama-3.3-70b-instruct">
          </div>
          <div style="min-width:180px;flex:2">
            <label class="form-lbl">Fallback chain <span style="opacity:.5">(comma-sep providers)</span></label>
            <input class="form-in" id="route-${name}-fallback"
              value="${esc(fallback)}" placeholder="openrouter,anthropic">
          </div>
        </div>
      </div>`;
  }).join('');

  // ── Timezone ─────────────────────────────────────────────────────────────
  // GET /admin/api/config/timezone → {timezone:{user:{default:"…"},character:{default:"…"}}}
  const tzData = tzResp.timezone || {};
  const userTzObj = tzData.user || {};
  const charTzObj = tzData.character || {};
  const userTz = typeof userTzObj === 'string' ? userTzObj : (userTzObj.default || 'Asia/Shanghai');
  const charTz = typeof charTzObj === 'string' ? charTzObj : (charTzObj.default || 'Asia/Shanghai');

  // ── Morning Push ─────────────────────────────────────────────────────────
  // GET /admin/api/config/morning-push → {config:{time_window:["08:15","08:30"],
  //   weather_normal_probability:0.2, weather_severe_probability:0.8,
  //   schedule_normal_probability:0.45, random_event_enabled:true}}
  const mCfg  = morningResp.config || {};
  const mWin  = Array.isArray(mCfg.time_window) ? mCfg.time_window : ['08:15','08:30'];
  const mWNP  = mCfg.weather_normal_probability  ?? 0.20;
  const mWSP  = mCfg.weather_severe_probability  ?? 0.80;
  const mSNP  = mCfg.schedule_normal_probability ?? 0.45;
  const mREE  = mCfg.random_event_enabled        !== false;
  const mEnabled = mCfg.enabled !== false;

  // ── Evening Push ─────────────────────────────────────────────────────────
  // GET /admin/api/config/evening-push → {config:{enabled:true,char_bedtime_window:["22:30","23:30"]}}
  const eCfg  = eveningResp.config || {};
  const eBed  = Array.isArray(eCfg.char_bedtime_window) ? eCfg.char_bedtime_window : ['22:30','23:30'];
  const eEnabled = eCfg.enabled !== false;

  // ── Push Control ─────────────────────────────────────────────────────────
  // GET /admin/api/config/push-control → {push_control:{max_daily_proactive_messages:3,
  //   quiet_hours:["01:00","07:30"], user_busy_override:true, …}}
  const pCfg  = pushResp.push_control || {};
  const pQH   = Array.isArray(pCfg.quiet_hours) ? pCfg.quiet_hours : ['01:00','07:30'];
  const pMax  = pCfg.max_daily_proactive_messages ?? 3;
  const pBusy = pCfg.user_busy_override || false;

  return `
  <div class="d-panel">

    <!-- ── API Routes ── -->
    <div class="eng-section">
      <div class="eng-section-head">🔀 API 线路配置
        <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:4px">对话线路在 Agent 设置中配置</span>
      </div>
      ${routeCards}
      <button class="btn btn-p" style="margin-top:10px" onclick="_saveApiRoutes()">保存线路配置</button>
    </div>

    <!-- ── Timezone ── -->
    <div class="eng-section">
      <div class="eng-section-head">🌐 时区设置</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <div style="flex:1;min-width:220px">
          <label class="form-lbl">用户时区</label>
          <select class="form-sel" id="tz-user">${_tzOpts(userTz)}</select>
          <div style="margin-top:4px;display:flex;gap:6px;align-items:center">
            <span style="font-size:10px;color:var(--muted);white-space:nowrap">或输入:</span>
            <input class="form-in" id="tz-user-custom" placeholder="Asia/…"
              style="flex:1;padding:3px 8px;font-size:10px">
          </div>
        </div>
        <div style="flex:1;min-width:220px">
          <label class="form-lbl">角色时区</label>
          <select class="form-sel" id="tz-char">${_tzOpts(charTz)}</select>
          <div style="margin-top:4px;display:flex;gap:6px;align-items:center">
            <span style="font-size:10px;color:var(--muted);white-space:nowrap">或输入:</span>
            <input class="form-in" id="tz-char-custom" placeholder="Asia/…"
              style="flex:1;padding:3px 8px;font-size:10px">
          </div>
        </div>
      </div>
      <button class="btn btn-p" style="margin-top:10px" onclick="_saveTimezone()">保存时区</button>
    </div>

    <!-- ── Morning Push ── -->
    <div class="eng-section">
      <div class="eng-section-head">🌅 早安推送</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
        <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px;margin-top:2px">
          <input type="checkbox" id="morning-enabled" ${mEnabled?'checked':''}
            style="accent-color:#8b5cf6;width:14px;height:14px">
          <span>启用早安推送</span>
        </label>
        <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px;margin-top:2px">
          <input type="checkbox" id="morning-random-event" ${mREE?'checked':''}
            style="accent-color:#8b5cf6;width:14px;height:14px">
          <span>随机事件模块</span>
        </label>
        <div>
          <label class="form-lbl">推送窗口（角色时区）</label>
          <div style="display:flex;gap:6px;align-items:center">
            <input class="form-in" id="morning-win-start" value="${esc(mWin[0]||'08:15')}"
              style="width:80px" placeholder="08:15">
            <span style="opacity:.5">–</span>
            <input class="form-in" id="morning-win-end" value="${esc(mWin[1]||'08:30')}"
              style="width:80px" placeholder="08:30">
          </div>
        </div>
        <div>
          <label class="form-lbl">天气概率 <span style="opacity:.5">普通 / 恶劣</span></label>
          <div style="display:flex;gap:6px;align-items:center">
            <input class="form-in" id="morning-weather-normal" type="number" min="0" max="1" step="0.05"
              value="${+mWNP}" style="width:70px">
            <span style="opacity:.5">/</span>
            <input class="form-in" id="morning-weather-severe" type="number" min="0" max="1" step="0.05"
              value="${+mWSP}" style="width:70px">
          </div>
        </div>
        <div>
          <label class="form-lbl">日程概率 <span style="opacity:.5">普通</span></label>
          <input class="form-in" id="morning-sched-normal" type="number" min="0" max="1" step="0.05"
            value="${+mSNP}" style="width:70px">
        </div>
      </div>
      <button class="btn btn-p" style="margin-top:10px" onclick="_saveMorningPush()">保存早安配置</button>
    </div>

    <!-- ── Evening Push ── -->
    <div class="eng-section">
      <div class="eng-section-head">🌙 晚安推送</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
        <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px">
          <input type="checkbox" id="evening-enabled" ${eEnabled?'checked':''}
            style="accent-color:#8b5cf6;width:14px;height:14px">
          <span>启用晚安推送</span>
        </label>
        <div>
          <label class="form-lbl">角色就寝窗口</label>
          <div style="display:flex;gap:6px;align-items:center">
            <input class="form-in" id="evening-bed-start" value="${esc(eBed[0]||'22:30')}"
              style="width:80px" placeholder="22:30">
            <span style="opacity:.5">–</span>
            <input class="form-in" id="evening-bed-end" value="${esc(eBed[1]||'23:30')}"
              style="width:80px" placeholder="23:30">
          </div>
        </div>
      </div>
      <button class="btn btn-p" style="margin-top:10px" onclick="_saveEveningPush()">保存晚安配置</button>
    </div>

    <!-- ── Push Control ── -->
    <div class="eng-section">
      <div class="eng-section-head">🚦 全局推送控制</div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
        <div>
          <label class="form-lbl">每日最大主动推送次数</label>
          <input class="form-in" id="push-max-daily" type="number" min="0" max="20"
            value="${+pMax}" style="width:70px">
        </div>
        <div>
          <label class="form-lbl">免打扰时间（用户时区）</label>
          <div style="display:flex;gap:6px;align-items:center">
            <input class="form-in" id="push-quiet-start" value="${esc(pQH[0]||'01:00')}"
              style="width:80px" placeholder="01:00">
            <span style="opacity:.5">–</span>
            <input class="form-in" id="push-quiet-end" value="${esc(pQH[1]||'07:30')}"
              style="width:80px" placeholder="07:30">
          </div>
        </div>
        <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px;margin-bottom:2px">
          <input type="checkbox" id="push-busy-override" ${pBusy?'checked':''}
            style="accent-color:#8b5cf6;width:14px;height:14px">
          <span>用户忙碌时暂停推送</span>
        </label>
      </div>
      <button class="btn btn-p" style="margin-top:10px" onclick="_savePushControl()">保存推送控制</button>
    </div>

    <!-- ── Push Channels ── -->
    ${_buildChannelsPanel(pCfg)}

    <!-- ── News Config ── -->
    ${_buildNewsPanel(newsResp)}

    <!-- ── Health Config ── -->
    ${_buildHealthPanel(healthResp)}

    <!-- ── Music Config ── -->
    ${_buildMusicPanel(musicResp)}

    <!-- ── Accumulator Thresholds ── -->
    ${_buildAccumulatorCfgPanel(accumResp)}

    <!-- ── System Info ── -->
    ${_buildSystemInfoPanel(sysInfoResp)}

  </div>`;
}

// ── Push Channels panel ────────────────────────────────────────────────────
const _PUSH_CATEGORIES = [
  { id:'morning_push',   label:'🌅 早安推送' },
  { id:'evening_push',   label:'🌙 晚安推送' },
  { id:'miss_you_trigger', label:'💭 思念触发' },
  { id:'low_mood_trigger', label:'😔 低落触发' },
  { id:'medication',     label:'💊 用药提醒' },
];

function _buildChannelsPanel(pCfg) {
  const chCfg   = (pCfg && pCfg.channels) || {};
  const defCh   = chCfg.default_channels   || ['telegram','bark'];
  const overrides = chCfg.category_overrides || {};

  const defTg = defCh.includes('telegram');
  const defBk = defCh.includes('bark');

  const overrideRows = _PUSH_CATEGORIES.map(cat => {
    const ov = overrides[cat.id];  // undefined = use default
    const hasTg = ov ? ov.includes('telegram') : defTg;
    const hasBk = ov ? ov.includes('bark')     : defBk;
    const isOverridden = !!ov;
    return `
      <div class="push-ch-row" id="pchrow-${cat.id}">
        <span class="push-ch-label">${cat.label}</span>
        <label class="push-ch-check">
          <input type="checkbox" id="pch-tg-${cat.id}" ${hasTg?'checked':''}
            onchange="_onChannelChange('${cat.id}')">
          <span>Telegram</span>
        </label>
        <label class="push-ch-check">
          <input type="checkbox" id="pch-bk-${cat.id}" ${hasBk?'checked':''}
            onchange="_onChannelChange('${cat.id}')">
          <span>Bark</span>
        </label>
        <span class="push-ch-tag ${isOverridden?'override':'default'}" id="pch-tag-${cat.id}">
          ${isOverridden ? '自定义' : '默认'}
        </span>
      </div>`;
  }).join('');

  return `
    <div class="eng-section">
      <div class="eng-section-head">📡 推送渠道分层
        <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:6px">默认同时发送 Telegram + Bark，可按类别覆盖</span>
      </div>
      <div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
        <span style="font-size:11px;font-weight:600;white-space:nowrap">默认渠道：</span>
        <label class="push-ch-check">
          <input type="checkbox" id="pch-default-tg" ${defTg?'checked':''}
            onchange="_onDefaultChannelChange()">
          <span>Telegram</span>
        </label>
        <label class="push-ch-check">
          <input type="checkbox" id="pch-default-bk" ${defBk?'checked':''}
            onchange="_onDefaultChannelChange()">
          <span>Bark</span>
        </label>
        <span style="font-size:10px;color:var(--muted)">（留空 = 两者都发）</span>
      </div>
      <div style="font-size:11px;font-weight:600;margin-bottom:6px">按类别覆盖：</div>
      <div class="push-ch-table">${overrideRows}</div>
      <div style="margin-top:8px;font-size:10px;color:var(--muted)">
        💊 medication 默认仅 Bark（iOS 药物提醒声音）
      </div>
      <button class="btn btn-p" style="margin-top:10px" onclick="_savePushControl()">保存渠道配置</button>
    </div>`;
}

// ── News panel (fully custom RSS) ─────────────────────────────────────────
let _newsFeeds = [];   // mutable working copy

function _buildNewsPanel(newsResp) {
  const cfg = (newsResp && newsResp.config) || {};
  const enabled    = cfg.enabled !== false;
  const maxItems   = cfg.max_items ?? 10;
  const injectProb = cfg.morning_inject_prob ?? 0.30;
  // Initialize working copy
  _newsFeeds = (cfg.feeds && cfg.feeds.length)
    ? cfg.feeds.map(f => ({ ...f }))
    : [
        { name:'虎嗅',       url:'https://rsshub.app/huxiu/article',               category:'china',      enabled:true  },
        { name:'联合早报',   url:'https://rsshub.app/zaobao/realtime/china',         category:'china',      enabled:true  },
        { name:'Dezeen',     url:'https://www.dezeen.com/feed/',                    category:'art_design', enabled:true  },
        { name:'Colossal',   url:'https://www.thisiscolossal.com/feed/',            category:'art_design', enabled:true  },
        { name:'36Kr',       url:'https://36kr.com/feed',                           category:'tech',       enabled:true  },
        { name:'少数派',     url:'https://sspai.com/feed',                          category:'tech',       enabled:false },
        { name:'小红书热门', url:'https://rsshub.app/xiaohongshu/explore',          category:'lifestyle',  enabled:false },
      ];

  const blockKws = Array.isArray(cfg.hard_block_keywords) ? cfg.hard_block_keywords : [];
  const blockText = blockKws.join('\n');

  return `
    <div class="eng-section">
      <div class="eng-section-head">📰 新闻推送配置
        <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:6px">自定义 RSS 源，过滤政治内容后注入晨推</span>
      </div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
        <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px">
          <input type="checkbox" id="news-enabled" ${enabled?'checked':''}
            style="accent-color:#8b5cf6;width:14px;height:14px">
          <span>启用新闻系统</span>
        </label>
        <div>
          <label class="form-lbl">每日最大条数</label>
          <input class="form-in" id="news-max-items" type="number" min="1" max="30"
            value="${+maxItems}" style="width:60px">
        </div>
        <div>
          <label class="form-lbl">晨推注入概率</label>
          <input class="form-in" id="news-inject-prob" type="number" min="0" max="1" step="0.05"
            value="${+injectProb}" style="width:70px">
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <div style="font-size:11px;font-weight:600">RSS 源：</div>
        <button class="btn btn-s btn-g" onclick="_newsAddFeed()">＋ 添加</button>
        <span style="font-size:10px;color:var(--muted)">支持 RSS 2.0 / Atom / RSSHub</span>
      </div>
      <div id="news-feeds-list">${_renderNewsFeedRows()}</div>
      <details style="margin-top:12px">
        <summary style="font-size:11px;font-weight:600;cursor:pointer;user-select:none">
          🚫 硬过滤关键词 <span style="font-weight:400;color:var(--muted)">(每行一个，命中则跳过该条新闻)</span>
        </summary>
        <textarea id="news-hard-block" class="form-ta"
          style="margin-top:6px;min-height:100px;font-size:10px;font-family:monospace;width:100%;box-sizing:border-box"
          placeholder="每行一个关键词，不区分大小写">${esc(blockText)}</textarea>
        <div style="font-size:10px;color:var(--muted);margin-top:3px">
          留空则使用系统默认过滤列表（政治/军事/党政类关键词）
        </div>
      </details>
      <button class="btn btn-p" style="margin-top:10px" onclick="_saveNewsCfg()">保存新闻配置</button>
    </div>`;
}

function _renderNewsFeedRows() {
  if (!_newsFeeds.length) return '<div style="font-size:11px;color:var(--muted);padding:8px 0">暂无 RSS 源，点击「添加」</div>';
  return _newsFeeds.map((f, i) => `
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;flex-wrap:wrap" id="nfrow-${i}">
      <label style="display:flex;align-items:center;gap:4px;cursor:pointer;flex-shrink:0">
        <input type="checkbox" onchange="_newsFeeds[${i}].enabled=this.checked" ${f.enabled!==false?'checked':''}
          style="accent-color:#8b5cf6;width:13px;height:13px">
      </label>
      <input class="form-in" value="${esc(f.name||'')}"
        oninput="_newsFeeds[${i}].name=this.value"
        style="width:90px;flex-shrink:0;font-size:10px" placeholder="名称">
      <input class="form-in" value="${esc(f.url||'')}"
        oninput="_newsFeeds[${i}].url=this.value"
        style="flex:1;min-width:180px;font-size:10px" placeholder="RSS URL">
      <input class="form-in" value="${esc(f.category||'general')}"
        oninput="_newsFeeds[${i}].category=this.value"
        style="width:80px;flex-shrink:0;font-size:10px" placeholder="分类">
      <button class="btn btn-s" style="background:var(--danger-soft);color:var(--danger);border:none;flex-shrink:0"
        onclick="_newsRemoveFeed(${i})">✕</button>
    </div>`).join('');
}

function _newsAddFeed() {
  _newsFeeds.push({ name:'', url:'', category:'general', enabled:true });
  const el = document.getElementById('news-feeds-list');
  if (el) el.innerHTML = _renderNewsFeedRows();
}

function _newsRemoveFeed(i) {
  _newsFeeds.splice(i, 1);
  const el = document.getElementById('news-feeds-list');
  if (el) el.innerHTML = _renderNewsFeedRows();
}

async function _saveNewsCfg() {
  const enabled    = document.getElementById('news-enabled')?.checked ?? true;
  const maxItems   = parseInt(document.getElementById('news-max-items')?.value) || 10;
  const injectProb = parseFloat(document.getElementById('news-inject-prob')?.value) || 0.30;
  const validFeeds = _newsFeeds.filter(f => f.url && f.url.trim());
  // Parse hard-block keywords: split by newlines, trim, drop empties
  const blockRaw = document.getElementById('news-hard-block')?.value || '';
  const blockKws = blockRaw.split('\n').map(s => s.trim()).filter(Boolean);
  const cfg = {
    enabled, max_items: maxItems, morning_inject_prob: injectProb,
    feeds: validFeeds,
    hard_block_keywords: blockKws.length ? blockKws : undefined,
  };
  try {
    await apiFetch('/admin/api/config/news', { method:'POST', body: JSON.stringify({ config: cfg }) });
    toast('新闻配置已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

// ── Health panel ────────────────────────────────────────────────────────────
function _buildHealthPanel(healthResp) {
  const cfg = (healthResp && healthResp.config) || {};
  const hr   = cfg.heart_rate   || {};
  const stp  = cfg.steps        || {};
  const slp  = cfg.sleep        || {};
  const men  = cfg.menstrual    || {};
  const enabled      = cfg.enabled !== false;
  const hrEnabled    = hr.enabled  !== false;
  const hrThreshold  = hr.resting_high_threshold ?? 100;
  const stepsEnabled = stp.enabled !== false;
  const stepsGoal    = stp.daily_goal ?? 8000;
  const sleepEnabled = slp.enabled !== false;
  const menses       = men.enabled || false;

  return `
    <div class="eng-section">
      <div class="eng-section-head">❤️ 健康监控配置
        <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:6px">需要配置 HEALTH_MCP_URL 环境变量</span>
      </div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start">
        <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px;margin-top:2px">
          <input type="checkbox" id="health-enabled" ${enabled?'checked':''}
            style="accent-color:#8b5cf6;width:14px;height:14px">
          <span>启用健康监控</span>
        </label>
      </div>
      <div style="margin-top:10px;display:flex;gap:12px;flex-wrap:wrap">
        <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:160px">
          <div style="font-size:11px;font-weight:600;margin-bottom:6px">💓 心率</div>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11px;margin-bottom:6px">
            <input type="checkbox" id="health-hr-enabled" ${hrEnabled?'checked':''}
              style="accent-color:#8b5cf6;width:13px;height:13px">
            <span>启用心率监控</span>
          </label>
          <label class="form-lbl">异常阈值（BPM）</label>
          <input class="form-in" id="health-hr-threshold" type="number" min="60" max="200"
            value="${+hrThreshold}" style="width:70px">
        </div>
        <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:160px">
          <div style="font-size:11px;font-weight:600;margin-bottom:6px">👟 步数</div>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11px;margin-bottom:6px">
            <input type="checkbox" id="health-steps-enabled" ${stepsEnabled?'checked':''}
              style="accent-color:#8b5cf6;width:13px;height:13px">
            <span>启用步数监控</span>
          </label>
          <label class="form-lbl">每日目标步数</label>
          <input class="form-in" id="health-steps-goal" type="number" min="1000" max="30000" step="500"
            value="${+stepsGoal}" style="width:80px">
        </div>
        <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:160px">
          <div style="font-size:11px;font-weight:600;margin-bottom:6px">😴 睡眠</div>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11px">
            <input type="checkbox" id="health-sleep-enabled" ${sleepEnabled?'checked':''}
              style="accent-color:#8b5cf6;width:13px;height:13px">
            <span>启用睡眠分析</span>
          </label>
        </div>
        <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:160px">
          <div style="font-size:11px;font-weight:600;margin-bottom:6px">🌸 生理期</div>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11px">
            <input type="checkbox" id="health-menses-enabled" ${menses?'checked':''}
              style="accent-color:#8b5cf6;width:13px;height:13px">
            <span>启用生理期追踪</span>
          </label>
        </div>
      </div>
      <button class="btn btn-p" style="margin-top:10px" onclick="_saveHealthCfg()">保存健康配置</button>
    </div>`;
}

async function _saveHealthCfg() {
  const cfg = {
    enabled: document.getElementById('health-enabled')?.checked ?? true,
    heart_rate: {
      enabled:                document.getElementById('health-hr-enabled')?.checked ?? true,
      resting_high_threshold: parseInt(document.getElementById('health-hr-threshold')?.value) || 100,
    },
    steps: {
      enabled:    document.getElementById('health-steps-enabled')?.checked ?? true,
      daily_goal: parseInt(document.getElementById('health-steps-goal')?.value) || 8000,
    },
    sleep: {
      enabled: document.getElementById('health-sleep-enabled')?.checked ?? true,
    },
    menstrual: {
      enabled: document.getElementById('health-menses-enabled')?.checked || false,
    },
  };
  try {
    await apiFetch('/admin/api/config/health', { method:'POST', body: JSON.stringify({ config: cfg }) });
    toast('健康配置已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

// ── Music config panel ─────────────────────────────────────────────────────
function _buildMusicPanel(musicResp) {
  const cfg     = (musicResp && musicResp.config) || {};
  const enabled = cfg.enabled !== false;
  const timeUtc = cfg.daily_time_utc   || '07:00';
  const prob    = cfg.daily_probability ?? 0.35;
  const cooldown= cfg.cooldown_hours    ?? 48;
  const kws     = Array.isArray(cfg.mood_low_keywords)
    ? cfg.mood_low_keywords.join(', ')
    : '治愈, 温暖, 陪伴, 轻柔';

  return `
    <div class="eng-section">
      <div class="eng-section-head">🎵 音乐推荐配置
        <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:6px">每日定时 + 心情低落触发</span>
      </div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
        <label style="display:flex;align-items:center;gap:7px;cursor:pointer;font-size:11px;align-self:flex-end;padding-bottom:2px">
          <input type="checkbox" id="music-enabled" ${enabled?'checked':''}
            style="accent-color:#8b5cf6;width:14px;height:14px">
          <span>启用音乐推荐</span>
        </label>
        <div>
          <label class="form-lbl">每日触发时间 <span style="opacity:.5">(UTC)</span></label>
          <input class="form-in" id="music-time-utc" value="${esc(timeUtc)}"
            style="width:80px" placeholder="07:00">
        </div>
        <div>
          <label class="form-lbl">每日触发概率</label>
          <input class="form-in" id="music-prob" type="number" min="0" max="1" step="0.05"
            value="${+prob}" style="width:70px">
        </div>
        <div>
          <label class="form-lbl">冷却时间 <span style="opacity:.5">(小时)</span></label>
          <input class="form-in" id="music-cooldown" type="number" min="1" max="168"
            value="${+cooldown}" style="width:70px">
        </div>
      </div>
      <div style="margin-top:8px">
        <label class="form-lbl">心情低落触发关键词 <span style="opacity:.5">(逗号分隔，出现在对话时触发推荐)</span></label>
        <input class="form-in" id="music-mood-kws" value="${esc(kws)}"
          style="width:100%;box-sizing:border-box" placeholder="治愈, 温暖, 陪伴, 轻柔">
      </div>
      <button class="btn btn-p" style="margin-top:10px" onclick="_saveMusicCfg()">保存音乐配置</button>
    </div>`;
}

async function _saveMusicCfg() {
  const enabled = document.getElementById('music-enabled')?.checked ?? true;
  const timeUtc = document.getElementById('music-time-utc')?.value.trim() || '07:00';
  const prob    = parseFloat(document.getElementById('music-prob')?.value) || 0.35;
  const cooldown= parseInt(document.getElementById('music-cooldown')?.value) || 48;
  const kwsRaw  = document.getElementById('music-mood-kws')?.value || '';
  const kws     = kwsRaw.split(',').map(s => s.trim()).filter(Boolean);
  const cfg = {
    enabled, daily_time_utc: timeUtc,
    daily_probability: prob, cooldown_hours: cooldown,
    mood_low_keywords: kws,
  };
  try {
    await apiFetch('/admin/api/config/music', { method:'POST', body: JSON.stringify({ config: cfg }) });
    toast('音乐配置已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

// ── Accumulator thresholds config panel ────────────────────────────────────
const _ACC_CFG_META = [
  { key:'miss_you', label:'💜 思念值', maxDef:10.0 },
  { key:'low_mood', label:'💙 低落值', maxDef:8.0  },
];

function _buildAccumulatorCfgPanel(accumResp) {
  const cfg = (accumResp && accumResp.config) || {};
  const rows = _ACC_CFG_META.map(a => {
    const c   = cfg[a.key] || {};
    const thr = c.threshold ?? a.maxDef;
    const rst = c.reset     ?? 0.0;
    return `
      <div style="border:1px solid var(--border);border-radius:8px;padding:10px 14px;min-width:180px;flex:1">
        <div style="font-size:11px;font-weight:600;margin-bottom:8px">${a.label}</div>
        <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
          <div>
            <label class="form-lbl">触发阈值</label>
            <input class="form-in" id="acc-thr-${a.key}" type="number" min="0" max="100" step="0.5"
              value="${+thr}" style="width:80px">
          </div>
          <div>
            <label class="form-lbl">触发后重置为</label>
            <input class="form-in" id="acc-rst-${a.key}" type="number" min="0" max="100" step="0.5"
              value="${+rst}" style="width:80px">
          </div>
        </div>
      </div>`;
  }).join('');

  return `
    <div class="eng-section">
      <div class="eng-section-head">💧 蓄水池阈值配置
        <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:6px">达到阈值时发送主动消息，消息后重置为指定值</span>
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">${rows}</div>
      <button class="btn btn-p" style="margin-top:10px" onclick="_saveAccumulatorCfg()">保存阈值配置</button>
    </div>`;
}

async function _saveAccumulatorCfg() {
  const cfg = {};
  for (const a of _ACC_CFG_META) {
    cfg[a.key] = {
      threshold: parseFloat(document.getElementById(`acc-thr-${a.key}`)?.value) || a.maxDef,
      reset:     parseFloat(document.getElementById(`acc-rst-${a.key}`)?.value) || 0.0,
    };
  }
  try {
    await apiFetch('/admin/api/config/accumulator', { method:'POST', body: JSON.stringify({ config: cfg }) });
    toast('阈值配置已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

// ── System info panel (read-only) ──────────────────────────────────────────
function _buildSystemInfoPanel(sysInfoResp) {
  const s = sysInfoResp || {};
  const rows = [
    { label:'蒸馏模型 (DISTILL_MODEL)',   val: s.distill_model  || '—' },
    { label:'向量 Provider (EMBED_PROVIDER)', val: s.embed_provider || '—' },
    { label:'RSSHub URL',                 val: s.rsshub_url     || '—' },
    { label:'网关公网 URL',               val: s.gateway_url    || '—' },
  ];
  const rowsHtml = rows.map(r => `
    <div style="display:flex;gap:8px;align-items:baseline;padding:4px 0;border-bottom:1px solid var(--ghost-bg)">
      <span style="font-size:10px;color:var(--muted);white-space:nowrap;min-width:220px">${esc(r.label)}</span>
      <code style="font-size:11px;color:var(--accent);word-break:break-all">${esc(r.val)}</code>
    </div>`).join('');
  return `
    <div class="eng-section">
      <div class="eng-section-head">ℹ️ 系统信息
        <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:6px">环境变量（只读，需在 .env 中修改后重启）</span>
      </div>
      ${rowsHtml}
    </div>`;
}

function _getChannels(tgId, bkId) {
  const tg = document.getElementById(tgId)?.checked;
  const bk = document.getElementById(bkId)?.checked;
  const channels = [];
  if (tg) channels.push('telegram');
  if (bk) channels.push('bark');
  return channels.length ? channels : ['telegram','bark'];  // never empty
}

function _onDefaultChannelChange() {
  // Refresh all "default" tag labels
  const defCh = _getChannels('pch-default-tg','pch-default-bk');
  _PUSH_CATEGORIES.forEach(cat => {
    const tag = document.getElementById(`pch-tag-${cat.id}`);
    if (tag && tag.textContent.trim() === '默认') {
      // Keep showing "默认" but visually acknowledge the default changed
    }
  });
}

function _onChannelChange(catId) {
  const defCh = _getChannels('pch-default-tg','pch-default-bk');
  const catCh = _getChannels(`pch-tg-${catId}`, `pch-bk-${catId}`);
  const same  = defCh.length === catCh.length && defCh.every((v,i) => v === catCh[i]);
  const tag   = document.getElementById(`pch-tag-${catId}`);
  if (tag) {
    tag.textContent = same ? '默认' : '自定义';
    tag.className = `push-ch-tag ${same?'default':'override'}`;
  }
}

// ── Config savers ──────────────────────────────────────────────────────────

async function _saveApiRoutes() {
  const routeNames = ['proactive_push','analyzer'];
  const routes = {};
  for (const name of routeNames) {
    const provider = document.getElementById(`route-${name}-provider`)?.value.trim() || '';
    const model    = document.getElementById(`route-${name}-model`)?.value.trim()    || '';
    const fbRaw    = document.getElementById(`route-${name}-fallback`)?.value.trim() || '';
    routes[name] = {
      provider,
      model,
      fallback_chain: fbRaw ? fbRaw.split(',').map(s => s.trim()).filter(Boolean) : [],
    };
  }
  try {
    // POST expects {routes: {...}}
    await apiFetch('/admin/api/config/api-routes', { method:'POST', body: JSON.stringify({ routes }) });
    toast('线路配置已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

async function _saveTimezone() {
  const userCustom = document.getElementById('tz-user-custom')?.value.trim();
  const charCustom = document.getElementById('tz-char-custom')?.value.trim();
  const userTz = userCustom || document.getElementById('tz-user')?.value || 'Asia/Shanghai';
  const charTz = charCustom || document.getElementById('tz-char')?.value || 'Asia/Shanghai';
  // Backend stores {user: {default: "…"}, character: {default: "…"}} under user_context.timezone
  const body = {
    timezone: {
      user:      { default: userTz },
      character: { default: charTz },
    },
  };
  try {
    await apiFetch('/admin/api/config/timezone', { method:'POST', body: JSON.stringify(body) });
    toast('时区已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

async function _saveMorningPush() {
  const cfg = {
    enabled:                    document.getElementById('morning-enabled')?.checked        ?? true,
    random_event_enabled:       document.getElementById('morning-random-event')?.checked   ?? true,
    time_window: [
      document.getElementById('morning-win-start')?.value.trim() || '08:15',
      document.getElementById('morning-win-end')?.value.trim()   || '08:30',
    ],
    weather_normal_probability:  parseFloat(document.getElementById('morning-weather-normal')?.value) || 0.2,
    weather_severe_probability:  parseFloat(document.getElementById('morning-weather-severe')?.value) || 0.8,
    schedule_normal_probability: parseFloat(document.getElementById('morning-sched-normal')?.value)   || 0.45,
  };
  try {
    // POST accepts body.get("config", body) — send flat (no wrapper needed)
    await apiFetch('/admin/api/config/morning-push', { method:'POST', body: JSON.stringify(cfg) });
    toast('早安配置已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

async function _saveEveningPush() {
  const cfg = {
    enabled:              document.getElementById('evening-enabled')?.checked ?? true,
    char_bedtime_window: [
      document.getElementById('evening-bed-start')?.value.trim() || '22:30',
      document.getElementById('evening-bed-end')?.value.trim()   || '23:30',
    ],
  };
  try {
    await apiFetch('/admin/api/config/evening-push', { method:'POST', body: JSON.stringify(cfg) });
    toast('晚安配置已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

async function _savePushControl() {
  // Collect channel overrides
  const defCh = _getChannels('pch-default-tg','pch-default-bk');
  const catOverrides = {};
  _PUSH_CATEGORIES.forEach(cat => {
    const catCh = _getChannels(`pch-tg-${cat.id}`, `pch-bk-${cat.id}`);
    // Only save as override if it differs from default
    const same = defCh.length === catCh.length && defCh.every((v,i) => v === catCh[i]);
    if (!same) catOverrides[cat.id] = catCh;
  });

  const ctrl = {
    max_daily_proactive_messages: parseInt(document.getElementById('push-max-daily')?.value) || 3,
    quiet_hours: [
      document.getElementById('push-quiet-start')?.value.trim() || '01:00',
      document.getElementById('push-quiet-end')?.value.trim()   || '07:30',
    ],
    user_busy_override: document.getElementById('push-busy-override')?.checked || false,
    channels: {
      default_channels:   defCh,
      category_overrides: catOverrides,
    },
  };
  try {
    await apiFetch('/admin/api/config/push-control', { method:'POST', body: JSON.stringify(ctrl) });
    toast('推送控制已保存 ✓');
  } catch(e) { toast('保存失败: '+e); }
}

// ── State ──────────────────────────────────────────────────────────────────
function _engCharAgents() {
  if (typeof allAgents === 'undefined') return [];
  if (typeof agentTypes === 'undefined' || !Object.keys(agentTypes).length) return allAgents;
  return allAgents.filter(a => agentTypes[a] === 'character');
}

async function _loadEngState() {
  const agents = _engCharAgents();
  if (!_engAgentId) _engAgentId = agents[0] || 'default';
  const agentOpts = agents.map(a =>
    `<option value="${esc(a)}" ${a===_engAgentId?'selected':''}>${esc(a)}</option>`).join('');
  const sub = document.getElementById('eng-sub');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">🧠 实时状态引擎
        <select class="daily-input" id="engStateAgent" style="min-width:120px;margin-left:12px"
          onchange="_engAgentId=this.value;_fetchEngState()">${agentOpts}</select>
        <button class="btn btn-s btn-g" style="margin-left:auto" onclick="_fetchEngState()">↻ 刷新</button>
      </div>
      <div id="engStateBody" style="margin-top:14px">
        <div class="page-loading" style="font-size:11px">Loading…</div>
      </div>
    </div>`;
  _fetchEngState();
}

async function _fetchEngState() {
  const aid  = document.getElementById('engStateAgent')?.value || _engAgentId || 'default';
  const body = document.getElementById('engStateBody');
  if (!body) return;
  body.innerHTML = '<div class="page-loading" style="font-size:11px">Loading…</div>';
  try {
    const resp = await apiFetch(`/admin/api/character-state/${encodeURIComponent(aid)}`);
    // Newer endpoint wraps in {state:…}; older returns flat — handle both
    const s = resp.state || resp;
    body.innerHTML = _buildEngStatePanel(s);
  } catch(e) {
    body.innerHTML = `<span style="color:var(--danger);font-size:11px">Error: ${e}</span>`;
  }
}

const _ACC_META = [
  { key:'miss_you',  label:'思念值', max:10, color:'#a78bfa', emoji:'💜', threshold:8 },
  { key:'low_mood',  label:'低落值', max:8,  color:'#60a5fa', emoji:'💙', threshold:6 },
  { key:'irritable', label:'烦躁值', max:6,  color:'#fb923c', emoji:'🔥', threshold:4.5 },
];

function _buildEngStatePanel(s) {
  const gaugesHtml = _ACC_META.map(acc => {
    const val   = parseFloat(s[acc.key] || 0);
    const pct   = Math.min(100, (val / acc.max) * 100).toFixed(1);
    const thPct = ((acc.threshold / acc.max) * 100).toFixed(1);
    const isHigh = val >= acc.threshold;
    return `
      <div class="eng-acc-card">
        <div class="eng-acc-head">
          <span class="eng-acc-emoji">${acc.emoji}</span>
          <span class="eng-acc-label">${acc.label}</span>
          <span class="eng-acc-val${isHigh?' eng-acc-val-high':''}">${val.toFixed(1)} / ${acc.max}</span>
        </div>
        <div class="eng-gauge">
          <div class="eng-gauge-fill" style="width:${pct}%;background:${acc.color}"></div>
          <div class="eng-gauge-threshold" style="left:${thPct}%"></div>
        </div>
        <div style="display:flex;gap:6px;align-items:center;margin-top:6px">
          <input type="number" class="form-in" id="acc-${acc.key}"
            value="${val.toFixed(1)}" min="0" max="${acc.max}" step="0.1"
            style="width:80px;padding:3px 8px;font-size:11px">
          <button class="btn btn-s btn-g" onclick="_setAccumulator('${acc.key}',${acc.max})">Set</button>
          <button class="btn btn-s btn-d" onclick="_resetAccumulator('${acc.key}')">Reset</button>
        </div>
      </div>`;
  }).join('');

  const busyOpts   = ['normal','light','busy','very_busy'].map(v =>
    `<option value="${v}" ${(s.busy_level||'normal')===v?'selected':''}>${v}</option>`).join('');
  const healthOpts = ['healthy','tired','sick','recovering'].map(v =>
    `<option value="${v}" ${(s.health_status||'healthy')===v?'selected':''}>${v}</option>`).join('');

  let itemsVal = '[]', promisesVal = '[]';
  try { itemsVal    = JSON.stringify(JSON.parse(s.items    || '[]'), null, 2); } catch(_) { itemsVal    = s.items    || '[]'; }
  try { promisesVal = JSON.stringify(JSON.parse(s.promises || '[]'), null, 2); } catch(_) { promisesVal = s.promises || '[]'; }

  const valence = s.mood_valence !== undefined ? `${s.mood_valence}` : '—';
  const energy  = s.mood_energy  !== undefined ? `${s.mood_energy}`  : '—';

  return `
    <div class="eng-acc-grid">${gaugesHtml}</div>

    <div class="eng-section" style="margin-top:16px">
      <div class="eng-section-head" style="margin-bottom:10px">📋 语义状态</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:10px">
        <div>
          <label class="form-lbl">忙碌状态</label>
          <select class="form-sel" id="engBusy" style="min-width:130px">${busyOpts}</select>
        </div>
        <div>
          <label class="form-lbl">健康状态</label>
          <select class="form-sel" id="engHealth" style="min-width:130px">${healthOpts}</select>
        </div>
        <div style="font-size:10px;color:var(--muted);line-height:1.8;padding-bottom:2px">
          情绪效价: <b>${esc(valence)}</b> &nbsp;|&nbsp; 活跃度: <b>${esc(energy)}</b>
          ${s.last_user_msg ? `<br>上次用户消息: ${esc(String(s.last_user_msg).slice(0,24))}` : ''}
        </div>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:220px">
          <label class="form-lbl">物品 <span style="opacity:.5">(JSON array)</span></label>
          <textarea class="form-ta" id="engItems"
            style="min-height:80px;font-family:monospace;font-size:11px">${esc(itemsVal)}</textarea>
        </div>
        <div style="flex:1;min-width:220px">
          <label class="form-lbl">未兑现承诺 <span style="opacity:.5">(JSON array)</span></label>
          <textarea class="form-ta" id="engPromises"
            style="min-height:80px;font-family:monospace;font-size:11px">${esc(promisesVal)}</textarea>
        </div>
      </div>
      <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
        <button class="btn btn-p" onclick="_saveEngState()">保存语义状态</button>
        <span id="engStateSaveMsg" style="font-size:10px;color:var(--muted)"></span>
      </div>
    </div>`;
}

async function _setAccumulator(key, max) {
  const aid  = document.getElementById('engStateAgent')?.value || _engAgentId || 'default';
  const val  = parseFloat(document.getElementById(`acc-${key}`)?.value);
  if (isNaN(val)) return toast('Invalid value');
  const clamped = Math.min(max, Math.max(0, val));
  try {
    await apiFetch(`/admin/api/character-state/${encodeURIComponent(aid)}`, {
      method: 'PATCH',
      body: JSON.stringify({ [key]: clamped }),
    });
    toast(`${key} = ${clamped} ✓`);
    _fetchEngState();
  } catch(e) { toast('Error: '+e); }
}

async function _resetAccumulator(key) {
  const aid = document.getElementById('engStateAgent')?.value || _engAgentId || 'default';
  try {
    await apiFetch(`/admin/api/character-state/${encodeURIComponent(aid)}`, {
      method: 'PATCH',
      body: JSON.stringify({ [key]: 0 }),
    });
    toast(`${key} reset ✓`);
    _fetchEngState();
  } catch(e) { toast('Error: '+e); }
}

async function _saveEngState() {
  const aid = document.getElementById('engStateAgent')?.value || _engAgentId || 'default';
  let items = '[]', promises = '[]';
  try { items    = JSON.stringify(JSON.parse(document.getElementById('engItems')?.value    || '[]')); } catch(_) {}
  try { promises = JSON.stringify(JSON.parse(document.getElementById('engPromises')?.value || '[]')); } catch(_) {}

  const body = {
    busy_level:    document.getElementById('engBusy')?.value   || 'normal',
    health_status: document.getElementById('engHealth')?.value || 'healthy',
    items,
    promises,
  };
  const msg = document.getElementById('engStateSaveMsg');
  try {
    await apiFetch(`/admin/api/character-state/${encodeURIComponent(aid)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
    if (msg) { msg.style.color='#22c55e'; msg.textContent='✓ 已保存'; }
    toast('语义状态已保存 ✓');
    setTimeout(_fetchEngState, 600);
  } catch(e) {
    if (msg) { msg.style.color='var(--danger)'; msg.textContent='✗ '+e; }
    toast('Error: '+e);
  }
}

// ── Logs ───────────────────────────────────────────────────────────────────
function _loadEngLogs() {
  const sub = document.getElementById('eng-sub');
  sub.innerHTML = `
    <div class="d-panel">
      <div class="d-panel-head">📊 推送日志 &amp; 链式事件</div>
      <div style="display:flex;gap:4px;margin-bottom:14px;border-bottom:1px solid var(--border)">
        <button class="d-tab${_logSubTab==='pushlog'?' active':''}" id="ltab-pushlog"
          onclick="_switchLogTab('pushlog')">📋 推送日志</button>
        <button class="d-tab${_logSubTab==='chains'?' active':''}" id="ltab-chains"
          onclick="_switchLogTab('chains')">⛓ 链式事件</button>
      </div>
      <div id="logBody"><div class="page-loading" style="font-size:11px">Loading…</div></div>
    </div>`;
  _switchLogTab(_logSubTab);
}

function _switchLogTab(tab) {
  _logSubTab = tab;
  ['pushlog','chains'].forEach(t => {
    document.getElementById('ltab-'+t)?.classList.toggle('active', t === tab);
  });
  const body = document.getElementById('logBody');
  if (!body) return;
  body.innerHTML = '<div class="page-loading" style="font-size:11px">Loading…</div>';
  if (tab === 'pushlog') _fetchPushLog();
  else                   _fetchChainEvents();
}

const _CAT_EMOJI = {
  morning_push:'🌅', evening_push:'🌙', miss_you:'💜',
  low_mood:'💙',     irritable:'🔥',    promise_remind:'📋',
  chain_event:'⛓',  screen_time:'📱',
};

async function _fetchPushLog() {
  const body = document.getElementById('logBody');
  if (!body) return;
  try {
    const data = await apiFetch('/admin/api/push-log');
    const logs = data.logs || [];
    if (!logs.length) {
      body.innerHTML = '<div class="daily-empty">暂无推送记录</div>';
      return;
    }
    body.innerHTML = `
      <div style="overflow-x:auto">
        <table class="eng-log-table">
          <thead><tr>
            <th>时间</th><th>类别</th><th>角色</th><th>触发</th><th>状态</th><th>模块</th><th>消息预览</th>
          </tr></thead>
          <tbody>
            ${logs.map(l => {
              const emoji     = _CAT_EMOJI[l.category] || '📤';
              const sentBadge = l.sent
                ? '<span class="eng-badge eng-badge-ok">✓ 已发</span>'
                : '<span class="eng-badge eng-badge-muted">✗ 跳过</span>';
              const dt = l.created_at
                ? new Date(l.created_at).toLocaleString('zh-CN',
                    {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
                : '—';
              const preview = (l.message||'').slice(0,42) + ((l.message||'').length>42?'…':'');
              return `<tr>
                <td class="eng-td-meta">${esc(dt)}</td>
                <td><span class="eng-badge">${emoji} ${esc(l.category||'—')}</span></td>
                <td class="eng-td-meta">${esc(l.agent_id||'—')}</td>
                <td class="eng-td-meta">${esc(l.trigger||'—')}</td>
                <td>${sentBadge}</td>
                <td class="eng-td-meta">${esc(l.modules||'—')}</td>
                <td class="eng-td-preview" title="${esc(l.message||'')}">${esc(preview)}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>`;
  } catch(e) {
    body.innerHTML = `<span style="color:var(--danger);font-size:11px">Error: ${e}</span>`;
  }
}

async function _fetchChainEvents() {
  const body = document.getElementById('logBody');
  if (!body) return;
  try {
    const data   = await apiFetch('/admin/api/chain-events');
    const events = data.events || [];
    if (!events.length) {
      body.innerHTML = '<div class="daily-empty">暂无待触发链式事件</div>';
      return;
    }
    const now = Date.now();
    body.innerHTML = events.map(e => {
      const fireAt = e.fire_at
        ? new Date(e.fire_at).toLocaleString('zh-CN',
            {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
        : '—';
      const fireMs = e.fire_at ? new Date(e.fire_at).getTime() : 0;
      const isPast = fireMs > 0 && fireMs < now;
      const badge  = e.fired
        ? '<span class="eng-badge eng-badge-ok">已触发</span>'
        : isPast
          ? '<span class="eng-badge eng-badge-warn">待触发</span>'
          : '<span class="eng-badge eng-badge-muted">等待中</span>';
      const content = e.event_content || e.content || '—';
      return `
        <div class="chain-evt-row">
          <div class="chain-evt-head">
            ${badge}
            <span class="eng-td-meta">角色: ${esc(e.agent_id||'—')}</span>
            <span class="eng-td-meta" style="margin-left:8px">触发: ${esc(fireAt)}</span>
            ${!e.fired
              ? `<button class="btn-icon" style="margin-left:auto;color:var(--danger)"
                  onclick="_cancelChainEvent('${esc(e.id)}')" title="取消">✕</button>`
              : ''}
          </div>
          <div class="chain-evt-content">${esc(content)}</div>
          ${e.mood_effect
              ? `<div class="eng-td-meta" style="margin-top:3px">情绪效果: ${esc(JSON.stringify(e.mood_effect))}</div>`
              : ''}
          ${e.accumulator_effect
              ? `<div class="eng-td-meta">累积器效果: ${esc(JSON.stringify(e.accumulator_effect))}</div>`
              : ''}
        </div>`;
    }).join('');
  } catch(e) {
    body.innerHTML = `<span style="color:var(--danger);font-size:11px">Error: ${e}</span>`;
  }
}

async function _cancelChainEvent(id) {
  if (!confirm('取消该链式事件？')) return;
  try {
    await apiFetch(`/admin/api/chain-events/${encodeURIComponent(id)}`, { method:'DELETE' });
    toast('已取消 ✓');
    _fetchChainEvents();
  } catch(e) { toast('Error: '+e); }
}
