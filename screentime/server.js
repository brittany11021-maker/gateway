/**
 * Screentime MCP — iPhone app usage tracker
 *
 * iPhone Shortcuts (HTTPS via nginx):
 *   GET /toggle?app=微信&battery=82&charging=0&model=iPhone15Pro&ios=18.3&wifi=Home&key=xxx
 *     → toggle app on/off + report device status (all in one call)
 *
 * Query endpoints:
 *   GET /today, /summary?days=7, /sessions/:app?days=1, /active
 *   GET /device/status
 *
 * MCP (both Streamable HTTP and legacy SSE):
 *   POST /mcp        → Streamable HTTP JSON-RPC (modern)
 *   GET  /mcp        → SSE (legacy) or Streamable HTTP notifications
 *   DELETE /mcp      → close Streamable HTTP session
 *   POST /mcp/message → legacy SSE messages
 */

import express from 'express';
import Database from 'better-sqlite3';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { SSEServerTransport } from '@modelcontextprotocol/sdk/server/sse.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import cron from 'node-cron';
import crypto from 'crypto';
import path from 'path';
import { fileURLToPath } from 'url';
import fs from 'fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DB_PATH      = process.env.DB_PATH || path.join(__dirname, 'data', 'screentime.db');
const API_KEY      = process.env.SCREENTIME_API_KEY || '';
const PORT         = parseInt(process.env.PORT || '3000');
const MCP_MSG_PATH = process.env.MCP_MSG_PATH || '/mcp/message';

// ─── Database ─────────────────────────────────────────────────────────────────
fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.exec(`
  CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    app          TEXT    NOT NULL,
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    duration_sec INTEGER
  );
  CREATE INDEX IF NOT EXISTS idx_app     ON sessions(app);
  CREATE INDEX IF NOT EXISTS idx_started ON sessions(started_at);

  CREATE TABLE IF NOT EXISTS device_status (
    id          INTEGER PRIMARY KEY,
    updated_at  INTEGER NOT NULL,
    battery_pct INTEGER,
    charging    INTEGER,
    model       TEXT,
    ios_version TEXT,
    wifi_ssid   TEXT
  );
`);

// ─── Toggle state ─────────────────────────────────────────────────────────────
const activeState = new Map();
for (const row of db.prepare('SELECT id, app, started_at FROM sessions WHERE ended_at IS NULL').all()) {
  activeState.set(row.app, { startedAt: row.started_at, rowId: row.id });
}
console.log(`[startup] Restored ${activeState.size} active sessions`);

// ─── Helpers ──────────────────────────────────────────────────────────────────
function fmt(secs) {
  if (!secs || secs < 0) return '0秒';
  if (secs < 60)   return `${secs}秒`;
  if (secs < 3600) return `${Math.floor(secs / 60)}分${secs % 60}秒`;
  return `${Math.floor(secs / 3600)}小时${Math.floor((secs % 3600) / 60)}分`;
}

function toggleApp(appName) {
  const now   = Math.floor(Date.now() / 1000);
  const state = activeState.get(appName);
  if (!state) {
    const { lastInsertRowid } = db.prepare(
      'INSERT INTO sessions (app, started_at) VALUES (?, ?)'
    ).run(appName, now);
    activeState.set(appName, { startedAt: now, rowId: lastInsertRowid });
    return { status: 'started', app: appName, started_at: now };
  } else {
    const duration = now - state.startedAt;
    db.prepare('UPDATE sessions SET ended_at=?, duration_sec=? WHERE id=?')
      .run(now, duration, state.rowId);
    activeState.delete(appName);
    return { status: 'ended', app: appName, duration_sec: duration, duration: fmt(duration) };
  }
}

// ─── Usage queries ────────────────────────────────────────────────────────────
function getTodayUsage() {
  const startOfDay = Math.floor(new Date().setHours(0, 0, 0, 0) / 1000);
  const now = Math.floor(Date.now() / 1000);
  const rows = db.prepare(`
    SELECT app, COUNT(*) as sessions, SUM(duration_sec) as total_sec
    FROM sessions WHERE started_at >= ? AND ended_at IS NOT NULL
    GROUP BY app ORDER BY total_sec DESC
  `).all(startOfDay);
  const active = [...activeState.entries()].map(([app, s]) => ({
    app, elapsed_sec: now - s.startedAt, elapsed: fmt(now - s.startedAt),
  }));
  const totalSec = rows.reduce((sum, r) => sum + (r.total_sec || 0), 0);
  return {
    date: new Date().toLocaleDateString('zh-CN'),
    total: fmt(totalSec),
    apps: rows.map(r => ({ ...r, duration: fmt(r.total_sec) })),
    active,
  };
}

function getUsageSummary(days = 7) {
  const since = Math.floor(Date.now() / 1000) - days * 86400;
  return db.prepare(`
    SELECT app, COUNT(*) as sessions,
           SUM(duration_sec) as total_sec,
           CAST(AVG(duration_sec) AS INTEGER) as avg_sec
    FROM sessions WHERE started_at >= ? AND ended_at IS NOT NULL
    GROUP BY app ORDER BY total_sec DESC
  `).all(since).map(r => ({ ...r, duration: fmt(r.total_sec), avg: fmt(r.avg_sec) }));
}

function getAppSessions(appName, days = 1) {
  const since = Math.floor(Date.now() / 1000) - days * 86400;
  return db.prepare(`
    SELECT id, app, started_at, ended_at, duration_sec,
           datetime(started_at, 'unixepoch', 'localtime') as started_local,
           datetime(ended_at,   'unixepoch', 'localtime') as ended_local
    FROM sessions WHERE app = ? AND started_at >= ?
    ORDER BY started_at DESC
  `).all(appName, since).map(r => ({ ...r, duration: fmt(r.duration_sec) }));
}

function getActiveApps() {
  const now = Math.floor(Date.now() / 1000);
  return [...activeState.entries()].map(([app, s]) => ({
    app, started_at: new Date(s.startedAt * 1000).toISOString(),
    elapsed_sec: now - s.startedAt, elapsed: fmt(now - s.startedAt),
  }));
}

// ─── Device status ────────────────────────────────────────────────────────────
const _upsertDevice = db.prepare(`
  INSERT OR REPLACE INTO device_status (id, updated_at, battery_pct, charging, model, ios_version, wifi_ssid)
  VALUES (1, ?, ?, ?, ?, ?, ?)
`);

function updateDevice(q) {
  const now = Math.floor(Date.now() / 1000);
  _upsertDevice.run(
    now,
    q.battery != null ? parseInt(q.battery) : null,
    q.charging != null ? (q.charging === '1' || q.charging === true || q.charging === 'true' ? 1 : 0) : null,
    q.model ?? null,
    q.ios   ?? null,
    q.wifi  ?? null,
  );
}

function getDeviceStatus() {
  const row = db.prepare('SELECT * FROM device_status WHERE id=1').get();
  if (!row) return { status: '暂无数据，iPhone快捷指令尚未上报' };
  const age = Math.floor(Date.now() / 1000) - row.updated_at;
  return {
    battery_pct: row.battery_pct,
    battery:     row.battery_pct != null ? `${row.battery_pct}%` : null,
    charging:    row.charging === 1,
    model:       row.model,
    ios_version: row.ios_version,
    wifi_ssid:   row.wifi_ssid,
    updated_at:  new Date(row.updated_at * 1000).toISOString(),
    data_age:    age < 60 ? `${age}秒前` : fmt(age) + '前',
  };
}

// ─── Cleanup ──────────────────────────────────────────────────────────────────
cron.schedule('0 3 * * *', () => {
  const cutoff = Math.floor(Date.now() / 1000) - 3 * 86400;
  const { changes } = db.prepare(
    'DELETE FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL'
  ).run(cutoff);
  if (changes > 0) console.log(`[cleanup] Deleted ${changes} sessions older than 3 days`);
});

// ─── Express ──────────────────────────────────────────────────────────────────
const app = express();

// CORS — MCP 客户端（Electron/浏览器）需要
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, x-api-key, Mcp-Session-Id');
  res.setHeader('Access-Control-Expose-Headers', 'Mcp-Session-Id');
  if (req.method === 'OPTIONS') return res.status(204).end();
  next();
});

// JSON body parser — skip /mcp/message (SSEServerTransport reads raw stream itself)
const jsonParser = express.json();
app.use((req, res, next) => {
  if (req.path === '/mcp/message') return next();
  jsonParser(req, res, next);
});

// Request logging for MCP debugging
app.use((req, res, next) => {
  if (req.path.startsWith('/mcp')) {
    console.log(`[mcp] ${req.method} ${req.path} sid=${req.query?.sessionId || req.headers['mcp-session-id'] || '-'}`);
  }
  next();
});

function auth(req, res, next) {
  if (!API_KEY) return next();
  const key = req.query.key || req.headers['x-api-key'];
  if (key !== API_KEY) return res.status(401).json({ error: 'Unauthorized' });
  next();
}

// ── Toggle + optional device reporting ────────────────────────────────────────
// GET /toggle?app=微信&battery=82&charging=0&model=iPhone15Pro&ios=18.3&wifi=Home&key=xxx
app.get('/toggle', auth, (req, res) => {
  const { app: appName, battery, charging, model, ios, wifi } = req.query;
  if (!appName) return res.status(400).json({ error: 'Missing ?app=' });

  // Side-effect: update device status if any device field is present
  if (battery !== undefined || model !== undefined) {
    updateDevice({ battery, charging, model, ios, wifi });
  }

  const r = toggleApp(appName);
  console.log(`[toggle] ${r.status}: ${appName}${r.duration ? ` (${r.duration})` : ''}`);
  res.json(r);
});

app.post('/toggle', auth, (req, res) => {
  const appName = req.body?.app || req.query?.app;
  if (!appName) return res.status(400).json({ error: 'Missing app' });
  const r = toggleApp(appName);
  res.json(r);
});

// ── Queries ───────────────────────────────────────────────────────────────────
app.get('/today',         auth, (_q, res) => res.json(getTodayUsage()));
app.get('/summary',       auth, (q,  res) => res.json(getUsageSummary(parseInt(q.query.days) || 7)));
app.get('/sessions/:app', auth, (q,  res) => res.json(getAppSessions(q.params.app, parseInt(q.query.days) || 1)));
app.get('/active',        auth, (_q, res) => res.json(getActiveApps()));

// ── Device (standalone update / read) ─────────────────────────────────────────
app.get('/device', auth, (req, res) => {
  const { battery, model } = req.query;
  if (battery !== undefined || model !== undefined) {
    updateDevice(req.query);
    return res.json({ ok: true });
  }
  res.json(getDeviceStatus());
});
app.post('/device',       auth, (req, res) => { updateDevice(req.body || {}); res.json({ ok: true }); });
app.get('/device/status', auth, (_q, res) => res.json(getDeviceStatus()));

app.get('/health', (_q, res) => res.json({ ok: true, active: activeState.size }));

// ─── MCP Tools ────────────────────────────────────────────────────────────────
const MCP_TOOLS = [
  {
    name: 'get_today_usage',
    description: '获取今天手机各App使用时长汇总，以及当前正在使用中的App',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'get_usage_summary',
    description: '获取最近N天各App使用时长统计（总时长、使用次数、平均时长）',
    inputSchema: {
      type: 'object',
      properties: { days: { type: 'number', description: '天数，默认7', default: 7 } },
    },
  },
  {
    name: 'get_app_sessions',
    description: '查看某个App的具体每次使用记录',
    inputSchema: {
      type: 'object',
      required: ['app'],
      properties: {
        app:  { type: 'string', description: 'App名称' },
        days: { type: 'number', description: '查询最近几天，默认1', default: 1 },
      },
    },
  },
  {
    name: 'get_active_apps',
    description: '查看现在正在使用中的App及已使用时长',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'get_device_status',
    description: '查看iPhone当前电量、充电状态、设备型号、WiFi网络等信息',
    inputSchema: { type: 'object', properties: {} },
  },
];

function makeMCPServer() {
  const server = new Server(
    { name: 'screentime', version: '1.0.0' },
    { capabilities: { tools: {} } }
  );
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: MCP_TOOLS }));
  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const { name, arguments: args = {} } = req.params;
    let result;
    switch (name) {
      case 'get_today_usage':   result = getTodayUsage(); break;
      case 'get_usage_summary': result = getUsageSummary(args.days); break;
      case 'get_app_sessions':
        if (!args.app) throw new Error('Missing required argument: app');
        result = getAppSessions(args.app, args.days);
        break;
      case 'get_active_apps':   result = getActiveApps(); break;
      case 'get_device_status': result = getDeviceStatus(); break;
      default: throw new Error(`Unknown tool: ${name}`);
    }
    return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
  });
  return server;
}

// ─── MCP Transport: Streamable HTTP (modern) + SSE (legacy) ───────────────────
const httpSessions = new Map();   // Streamable HTTP
const sseSessions  = new Map();   // Legacy SSE

// Streamable HTTP: POST /mcp → JSON-RPC request/response
app.post('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];

  if (sessionId) {
    const transport = httpSessions.get(sessionId);
    if (!transport) {
      return res.status(404).json({
        jsonrpc: '2.0',
        error: { code: -32000, message: 'Session not found. It may have expired.' },
        id: null,
      });
    }
    await transport.handleRequest(req, res, req.body);
    return;
  }

  // New session
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => crypto.randomUUID(),
  });
  transport.onclose = () => httpSessions.delete(transport.sessionId);

  const server = makeMCPServer();
  await server.connect(transport);
  httpSessions.set(transport.sessionId, transport);

  await transport.handleRequest(req, res, req.body);
});

// GET /mcp → Streamable HTTP notifications (with session) or Legacy SSE (without)
app.get('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];

  if (sessionId) {
    // Streamable HTTP: server-initiated notification stream
    const transport = httpSessions.get(sessionId);
    if (!transport) return res.status(404).send('Session not found');
    await transport.handleRequest(req, res);
    return;
  }

  // Legacy SSE transport (no session header)
  const transport = new SSEServerTransport(MCP_MSG_PATH, res);
  sseSessions.set(transport.sessionId, transport);
  res.on('close', () => sseSessions.delete(transport.sessionId));
  await makeMCPServer().connect(transport);
});

// Streamable HTTP: DELETE /mcp → close session
app.delete('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  if (!sessionId) return res.status(400).send('Missing Mcp-Session-Id');
  const transport = httpSessions.get(sessionId);
  if (!transport) return res.status(404).send('Session not found');
  await transport.handleRequest(req, res);
  httpSessions.delete(sessionId);
});

// Legacy SSE: POST /mcp/message?sessionId=xxx
app.post('/mcp/message', async (req, res) => {
  const sid = req.query.sessionId;
  const transport = sseSessions.get(sid);
  if (!transport) {
    console.log(`[mcp] POST /mcp/message 404: sid=${sid} known=[${[...sseSessions.keys()].join(',')}]`);
    return res.status(404).send('Session not found');
  }
  try {
    await transport.handlePostMessage(req, res);
  } catch (err) {
    console.error(`[mcp] handlePostMessage error:`, err.message);
    if (!res.headersSent) res.status(500).send(err.message);
  }
});

// ─── Start ────────────────────────────────────────────────────────────────────
app.listen(PORT, '0.0.0.0', () => {
  console.log(`[screentime] running on port ${PORT}`);
  console.log(`  toggle+device: GET /toggle?app=APP&battery=82&charging=0&model=iPhone&ios=18.3&wifi=Home&key=KEY`);
  console.log(`  mcp (modern):  POST /mcp`);
  console.log(`  mcp (legacy):  GET  /mcp`);
});
