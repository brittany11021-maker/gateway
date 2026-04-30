"""
Memory Gateway — OpenAI-compatible proxy with per-agent persistent memory.
Embedding: NVIDIA API (baai/bge-m3, 1024-dim, multilingual CJK/EN/FR).
Admin panel at /admin.
"""

import asyncio
import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator

import asyncpg
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, MatchValue,
    PointStruct, VectorParams, PayloadSchemaType,
)
from starlette.types import ASGIApp, Receive, Scope, Send

try:
    import fitz as _fitz          # PyMuPDF
except ImportError:
    _fitz = None
try:
    import chardet as _chardet
except ImportError:
    _chardet = None
try:
    import ebooklib as _ebooklib
    from ebooklib import epub as _epub
except ImportError:
    _ebooklib = None
    _epub = None

from memory_db import init_db as _init_memory_db, close_db as _close_memory_db
from memory_db import memory_write as _mem_write, memory_read as _mem_read
from memory_db import memory_list as _mem_list, memory_search as _mem_search
from memory_db import (
    state_get as _state_get, state_set as _state_set,
    state_touch as _state_touch, state_cooldown_active as _cooldown_active,
    state_mood_drift as _mood_drift,
    event_roll as _event_roll, event_list as _event_list,
    event_add as _event_add, event_delete as _event_delete,
    npc_list as _npc_list, npc_get as _npc_get,
    npc_upsert as _npc_upsert, npc_delete as _npc_delete,
)
from memory_db import memory_update as _mem_update, memory_delete as _mem_delete
from memory_db import memory_wakeup as _mem_wakeup, memory_surface as _mem_surface
from memory_db import memory_stats as _mem_stats
from memory_db import memory_get_history as _mem_history, memory_rollback as _mem_rollback
from memory_db import backup_db as _mem_backup_db
from memory_db import dedup_check as _mem_dedup_check, dedup_list as _mem_dedup_list
from memory_db import dedup_resolve as _mem_dedup_resolve
from memory_db import memory_mark_read as _mem_mark_read
from memory_db import memory_cleanup as _mem_cleanup
from memory_db import memory_write_smart as _mem_write_smart
from memory_db import (
    memory_confirm_l1 as _mem_confirm_l1,
    memory_list_pending_l1 as _mem_pending_l1,
)
from memory_db import daily_write as _daily_write, daily_read as _daily_read
from memory_db import daily_list as _daily_list, daily_delete as _daily_delete
from memory_db import (
    activity_write as _act_write, activity_recent as _act_recent,
    activity_today_totals as _act_totals,
)
from memory_db import (
    project_upsert as _proj_upsert, project_list as _proj_list,
    project_complete as _proj_complete, project_archive as _proj_archive,
    project_list_completed_stale as _proj_stale,
)
from memory_db import (
    l5_write as _l5_write, l5_search as _l5_search,
    l5_list as _l5_list, l5_cleanup as _l5_cleanup,
)
from memory_db import (
    cooldown_check as _cd_check, cooldown_set as _cd_set, cooldown_gate as _cd_gate,
)


# ── Config ─────────────────────────────────────────────────────────────────────
GATEWAY_API_KEY = os.environ["GATEWAY_API_KEY"]
POSTGRES_DSN    = os.environ["POSTGRES_DSN"]
QDRANT_URL      = os.environ.get("QDRANT_URL", "http://qdrant:6333")
EMBED_MODEL     = os.environ.get("EMBED_MODEL", "baai/bge-m3")
EMBED_DIM       = 1024

# Providers loaded from DB at startup; mutated by admin UI
PROVIDERS: dict[str, dict] = {}       # name → {api_key, base_url}
_DEFAULT_CHAIN: list[str]  = []       # ordered list of provider names
_EMBED_PNAME:   str        = ""       # which provider to use for embeddings
_last_successful_llm_ts: dict      = {}  # {"ts": float} — updated on every successful LLM call


async def _reload_providers() -> None:
    """Load providers + gateway config from DB into memory globals."""
    global _DEFAULT_CHAIN, _EMBED_PNAME
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT name, base_url, api_key, is_embed FROM providers ORDER BY created_at")
        PROVIDERS.clear()
        embed_name = ""
        for r in rows:
            PROVIDERS[r["name"]] = {"api_key": r["api_key"], "base_url": r["base_url"]}
            if r["is_embed"]:
                embed_name = r["name"]
        _EMBED_PNAME = embed_name or next(iter(PROVIDERS), "")

        chain_row = await conn.fetchrow(
            "SELECT value FROM gateway_config WHERE key='default_chain'"
        )
        if chain_row and chain_row["value"]:
            _DEFAULT_CHAIN = [
                p.strip() for p in chain_row["value"].split(",")
                if p.strip() in PROVIDERS
            ]
        else:
            _DEFAULT_CHAIN = [p for p in PROVIDERS.keys() if p != _EMBED_PNAME]


def _agent_llm_config(
    agent_id: str, db_model: str = "", db_chain: str = ""
) -> tuple[str, list[str]]:
    """Return (model, [provider_names]) for an agent."""
    prefix = "AGENT_" + agent_id.upper().replace("-", "_").replace(" ", "_")
    model = (
        db_model
        or os.environ.get(f"{prefix}_MODEL")
        or os.environ.get("AGENT_DEFAULT_MODEL", "")
    )
    chain_str = db_chain or os.environ.get(f"{prefix}_API_CHAIN", "")
    chain = (
        [p.strip() for p in chain_str.split(",") if p.strip() in PROVIDERS]
        if chain_str else _DEFAULT_CHAIN[:]
    )
    if not chain:
        chain = list(PROVIDERS.keys())[:1]
    return model, chain


def _build_call_list(agent_cfg: dict) -> list[tuple[str, dict, str]]:
    """Build ordered (provider_name, provider_dict, model) call list from llm_chain_config.
    Falls back to simple api_chain + llm_model if llm_chain_config not set."""
    chain_cfg = agent_cfg.get("llm_chain_config") or {}
    slots = chain_cfg.get("slots") or []
    result: list[tuple[str, dict, str]] = []
    for slot in slots:
        if not slot.get("enabled", True):
            continue
        pname = slot.get("provider", "")
        if not pname or pname not in PROVIDERS:
            continue
        p = PROVIDERS[pname]
        models = [m.strip() for m in (slot.get("models") or []) if str(m).strip()]
        if not models:
            models = [agent_cfg.get("llm_model", "") or ""]
        for m in models:
            result.append((pname, p, m))
    if not result:
        # Backward-compat: use flat api_chain + llm_model
        db_model = agent_cfg.get("llm_model", "")
        db_chain = agent_cfg.get("api_chain", "")
        model, chain = _agent_llm_config("", db_model, db_chain)
        for pname in chain:
            if pname in PROVIDERS:
                result.append((pname, PROVIDERS[pname], model))
    return result


def _log_fallback(agent_id: str, chain: list[str], idx: int, reason: str) -> None:
    print(f"[fallback] agent={agent_id} {chain[idx]}→{chain[idx+1]} reason={reason}", flush=True)


async def _call_llm_simple(prompt: str, agent_id: str = "default") -> str:
    """Non-streaming LLM call for internal use (distillation etc.)."""
    model, chain = _agent_llm_config(agent_id)
    msgs = [{"role": "user", "content": prompt}]
    for i, pname in enumerate(chain):
        p = PROVIDERS[pname]
        hdrs = {"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{p['base_url']}/chat/completions", headers=hdrs,
                    json={"model": model, "messages": msgs, "temperature": 0.3},
                )
            if resp.status_code in (404, 429, 500, 502, 503) and i < len(chain) - 1:
                _log_fallback(agent_id, chain, i, str(resp.status_code)); continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if i < len(chain) - 1:
                _log_fallback(agent_id, chain, i, type(e).__name__); continue
            raise
    raise RuntimeError("All providers failed")

# Priority-ordered fallback models on NVIDIA NIM API (best per brand)
# Last verified: 2026-04-30
_CHEAP_LLM_MODELS = [
    "deepseek-ai/deepseek-v4-pro",                  # DeepSeek V4 Pro ✅ (primary)
    "z-ai/glm-5.1",                                 # GLM-5.1 ✅ (backup)
    "google/gemma-4-31b-it",                        # Gemma-4 31B ✅ (backup)
    "mistralai/mistral-small-3.1-24b-instruct",     # Mistral Small ✅ (backup)
]
# Note: moonshotai/kimi-k2.5 reached end-of-life 2026-04-30, removed.

async def _call_llm_cheap(prompt: str) -> str:
    """Call NVIDIA NIM LLM for background tasks (distillation, auto-extract).

    Tries models in priority order: deepseek-v4-pro → glm-5.1 → gemma-4-31b → mistral-small.
    DISTILL_MODEL env var inserts an override at position 0.
    Only uses the 'nvidia-llm' provider — never falls back to other providers.
    Raises RuntimeError if ALL models fail.
    """
    import os as _os
    override = _os.getenv("DISTILL_MODEL", "").strip()
    models = (
        [override] + [m for m in _CHEAP_LLM_MODELS if m != override]
        if override else _CHEAP_LLM_MODELS
    )

    async with _db_pool.acquire() as conn:
        prow = await conn.fetchrow(
            "SELECT base_url, api_key FROM providers WHERE name='nvidia-llm' LIMIT 1"
        )
    if not prow:
        raise RuntimeError("[cheap_llm] Provider 'nvidia-llm' not found in DB")

    base = prow["base_url"].rstrip("/")
    hdrs = {"Authorization": "Bearer " + prow["api_key"], "Content-Type": "application/json"}
    last_err = ""
    for model in models:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    base + "/chat/completions", headers=hdrs,
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.3, "max_tokens": 2000},
                )
            if resp.is_success:
                return resp.json()["choices"][0]["message"]["content"].strip()
            last_err = f"{model}: HTTP {resp.status_code} {resp.text[:200]}"
            print(f"[cheap_llm] {last_err} — trying next model", flush=True)
        except Exception as _e:
            last_err = f"{model}: {type(_e).__name__}: {_e}"
            print(f"[cheap_llm] {last_err} — trying next model", flush=True)
    raise RuntimeError(f"[cheap_llm] All models failed. Last error: {last_err}")


RECENT_DAYS     = 30
BACKUP_DIR      = Path("/app/backups")
MAX_BACKUPS     = 7
BOOK_COLLECTION = "book_chunks"
COVERS_DIR      = Path("/app/static/covers")
MAX_BOOKS       = 4
CHARS_PER_PAGE  = 2000
CHUNK_SIZE      = 500
CHUNK_OVERLAP   = 50
AGENT_COLORS    = {
    "user":   "#3b82f6",   # blue
    "iris":   "#8b5cf6",   # purple
    "luna":   "#10b981",   # green
    "chiaki": "#f97316",   # orange
}

# ── Globals ────────────────────────────────────────────────────────────────────
_db_pool:  asyncpg.Pool | None      = None
_qdrant:   QdrantClient | None      = None
_sh_task:  asyncio.Task | None      = None

app    = FastAPI(title="Memory Gateway")
bearer = HTTPBearer()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── MCP auth wrapper (no BaseHTTPMiddleware — avoids SSE buffering) ────────────
class _MCPAuth:
    """Wraps the MCP ASGI app, rejects requests without a valid Bearer token.

    Note: keepalive pings are handled automatically by sse-starlette's
    EventSourceResponse (DEFAULT_PING_INTERVAL = 15 s), so we don't need to
    add them here.
    """
    def __init__(self, inner: ASGIApp) -> None:
        self._inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            raw = headers.get(b"authorization", b"").decode()
            token = raw[7:] if raw.lower().startswith("bearer ") else ""
            if token != GATEWAY_API_KEY:
                resp = Response("Unauthorized", status_code=401)
                await resp(scope, receive, send)
                return
        await self._inner(scope, receive, send)

# ── MCP dual-transport router ─────────────────────────────────────────────────
class _MCPRouter:
    """Routes MCP traffic by method:
      GET  /sse          → SSE transport (legacy, sse-starlette)
      POST /sse          → Streamable HTTP transport (MCP 2025-03-26)
      POST /messages/    → SSE message endpoint (forwarded to sse_inner)
    """
    def __init__(self, sse_inner: ASGIApp, sh_inner: ASGIApp) -> None:
        self._sse = sse_inner
        self._sh  = sh_inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            # Session manager is started by startup(); pass lifespan to SSE app.
            await self._sse(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path   = scope.get("path", "")
        if scope["type"] == "http" and method == "POST" and path.rstrip("/").endswith("/sse"):
            # Streamable HTTP transport.
            # Bypass _sh_app's Starlette router (Starlette 0.38 defaults Route to
            # GET/HEAD only when methods= is omitted), and call the session manager
            # handle_request directly.
            await _mcp.session_manager.handle_request(scope, receive, send)
        else:
            await self._sse(scope, receive, send)


# ── MCP server ─────────────────────────────────────────────────────────────────
_mcp = FastMCP(
    name="memory-gateway",
    instructions=(
        "Memory and library gateway for AI agents. "
        "Tools: search/store memories across profile/project/recent layers; "
        "list_books to discover the library; search book content; "
        "get_reading_context and save_annotation for co-reading; "
        "wake_up for full context retrieval."
    ),
)

# ── Startup ────────────────────────────────────────────────────────────────────

# ── Environment / Awareness MCP tools ────────────────────────────────────────

import os as _os_mcp
import httpx as _httpx_mcp

async def _get_user_location() -> dict:
    """Helper: fetch user location config from DB."""
    try:
        async with _db_pool.acquire() as _c:
            row = await _c.fetchrow("SELECT value FROM user_config WHERE key='location'")
        return dict(row["value"]) if row else {}
    except Exception:
        return {}

@_mcp.tool()
async def amap_weather(city: str = "") -> str:
    """Query current weather via Amap API.
    If city is empty, uses the user's configured default city.
    Returns: weather summary string (temp, condition, wind, humidity).
    """
    _key = _os_mcp.getenv("AMAP_API_KEY", "")
    if not _key:
        return "Error: AMAP_API_KEY not configured"
    if not city:
        loc = await _get_user_location()
        city = loc.get("city", "武汉")
    try:
        async with _httpx_mcp.AsyncClient(timeout=8) as _hc:
            r = await _hc.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"city": city, "key": _key, "extensions": "base", "output": "JSON"}
            )
        d = r.json()
        if d.get("status") != "1" or not d.get("lives"):
            return f"Weather query failed: {d.get('info', 'unknown error')}"
        w = d["lives"][0]
        return (f"{w['city']} | {w['weather']} | {w['temperature']}°C | "
                f"风向{w['winddirection']} {w['windpower']}级 | 湿度{w['humidity']}%")
    except Exception as e:
        return f"Weather error: {e}"

@_mcp.tool()
async def amap_forecast(city: str = "") -> str:
    """Query 4-day weather forecast via Amap API.
    Returns forecast for next 4 days including weather condition and temperature range.
    """
    _key = _os_mcp.getenv("AMAP_API_KEY", "")
    if not _key:
        return "Error: AMAP_API_KEY not configured"
    if not city:
        loc = await _get_user_location()
        city = loc.get("city", "武汉")
    try:
        async with _httpx_mcp.AsyncClient(timeout=8) as _hc:
            r = await _hc.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"city": city, "key": _key, "extensions": "all", "output": "JSON"}
            )
        d = r.json()
        if d.get("status") != "1" or not d.get("forecasts"):
            return f"Forecast query failed: {d.get('info', 'unknown')}"
        casts = d["forecasts"][0].get("casts", [])
        lines = [f"{c['date']} {c['week']}星期 | 白天:{c['dayweather']} {c['daytemp']}°C | 夜间:{c['nightweather']} {c['nighttemp']}°C"
                 for c in casts]
        return "\n".join(lines)
    except Exception as e:
        return f"Forecast error: {e}"

@_mcp.tool()
async def amap_route(
    origin: str,
    destination: str,
    city: str = ""
) -> str:
    """Query driving route + traffic status via Amap.
    origin/destination: address strings (e.g. "武汉大学", "光谷广场").
    If city is empty, uses user's configured city for geocoding context.
    Returns: estimated travel time, distance, and traffic conditions.
    """
    _key = _os_mcp.getenv("AMAP_API_KEY", "")
    if not _key:
        return "Error: AMAP_API_KEY not configured"
    if not city:
        loc = await _get_user_location()
        city = loc.get("city", "武汉")

    async def _geocode(addr: str) -> str:
        async with _httpx_mcp.AsyncClient(timeout=8) as _hc:
            r = await _hc.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"address": addr, "city": city, "key": _key, "output": "JSON"}
            )
        d = r.json()
        geos = d.get("geocodes", [])
        if not geos:
            raise ValueError(f"Cannot geocode: {addr}")
        return geos[0]["location"]  # "lng,lat"

    try:
        orig_loc = await _geocode(origin)
        dest_loc = await _geocode(destination)
        async with _httpx_mcp.AsyncClient(timeout=10) as _hc:
            r = await _hc.get(
                "https://restapi.amap.com/v3/direction/driving",
                params={
                    "origin": orig_loc, "destination": dest_loc,
                    "strategy": 0, "key": _key, "output": "JSON"
                }
            )
        d = r.json()
        if d.get("status") != "1":
            return f"Route query failed: {d.get('info', 'unknown')}"
        route = d["route"]["paths"][0]
        dist_km = round(int(route["distance"]) / 1000, 1)
        dur_min = round(int(route["duration"]) / 60)
        # Traffic restriction info
        steps = route.get("steps", [])
        congested = [s["road"] for s in steps if s.get("tmcs") and
                     any(t.get("status") in ("拥堵", "缓行") for t in s["tmcs"])]
        traffic_note = f"拥堵路段: {'、'.join(set(congested[:3]))}" if congested else "路况畅通"
        return f"{origin} → {destination} | 约{dist_km}km, 预计{dur_min}分钟 | {traffic_note}"
    except Exception as e:
        return f"Route error: {e}"

@_mcp.tool()
async def amap_geocode(address: str, city: str = "") -> str:
    """Convert address to coordinates via Amap geocoding.
    Returns: address, city, and lng/lat coordinates.
    """
    _key = _os_mcp.getenv("AMAP_API_KEY", "")
    if not _key:
        return "Error: AMAP_API_KEY not configured"
    if not city:
        loc = await _get_user_location()
        city = loc.get("city", "武汉")
    try:
        async with _httpx_mcp.AsyncClient(timeout=8) as _hc:
            r = await _hc.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"address": address, "city": city, "key": _key, "output": "JSON"}
            )
        d = r.json()
        geos = d.get("geocodes", [])
        if not geos:
            return f"No results for: {address}"
        g = geos[0]
        return f"{g['formatted_address']} | 坐标: {g['location']} | 区: {g.get('district', '-')}"
    except Exception as e:
        return f"Geocode error: {e}"


# ── Todoist MCP tools ─────────────────────────────────────────────────────────

async def _todoist_request(method: str, path: str, **kwargs) -> dict:
    token = _os_mcp.getenv("TODOIST_API_TOKEN", "")
    if not token:
        raise ValueError("TODOIST_API_TOKEN not set in .env")
    async with _httpx_mcp.AsyncClient(timeout=10) as _hc:
        r = await getattr(_hc, method)(
            f"https://api.todoist.com/rest/v2{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs
        )
        r.raise_for_status()
        if r.content:
            return r.json()
        return {}

@_mcp.tool()
async def todoist_get_tasks(filter: str = "today") -> str:
    """Get Todoist tasks. filter examples: 'today', 'tomorrow', 'overdue', 'p1', '#Work'.
    Returns task list with IDs, content, due dates and priority.
    """
    try:
        tasks = await _todoist_request("get", "/tasks", params={"filter": filter})
        if not tasks:
            return f"No tasks matching: {filter}"
        lines = []
        for t in tasks[:20]:
            due = t.get("due", {}) or {}
            due_str = due.get("string") or due.get("date") or ""
            prio = {1:"", 2:"[P3]", 3:"[P2]", 4:"[P1]"}.get(t.get("priority", 1), "")
            lines.append(f"[{t['id']}] {prio} {t['content']} {'(截止: '+due_str+')' if due_str else ''}")
        return "\n".join(lines)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Todoist error: {e}"

@_mcp.tool()
async def todoist_create_task(content: str, due_string: str = "", priority: int = 1) -> str:
    """Create a new Todoist task.
    content: task description.
    due_string: natural language due date (e.g. "tomorrow", "next Monday", "明天").
    priority: 1=normal, 2=p3, 3=p2, 4=p1(highest).
    Returns: created task ID and content.
    """
    try:
        body = {"content": content, "priority": priority}
        if due_string:
            body["due_string"] = due_string
            body["due_lang"] = "zh" if any(ord(c) > 127 for c in due_string) else "en"
        t = await _todoist_request("post", "/tasks", json=body)
        return f"Created: [{t['id']}] {t['content']}"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Todoist error: {e}"

@_mcp.tool()
async def todoist_complete_task(task_id: str) -> str:
    """Mark a Todoist task as complete by its ID.
    Returns: confirmation message.
    """
    try:
        await _todoist_request("post", f"/tasks/{task_id}/close")
        return f"Task {task_id} completed ✓"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Todoist error: {e}"

@_mcp.tool()
async def todoist_update_task(task_id: str, content: str = "", due_string: str = "") -> str:
    """Update a Todoist task's content or due date.
    Only provided fields are updated.
    """
    try:
        body = {}
        if content: body["content"] = content
        if due_string:
            body["due_string"] = due_string
            body["due_lang"] = "zh" if any(ord(c) > 127 for c in due_string) else "en"
        if not body:
            return "Nothing to update"
        await _todoist_request("post", f"/tasks/{task_id}", json=body)
        return f"Task {task_id} updated ✓"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Todoist error: {e}"


# ── Notion MCP tools ─────────────────────────────────────────────────────────

import re as _re_notion

def _notion_extract_id(url_or_id: str) -> str:
    """Extract a Notion page ID from a URL or return the raw ID.
    Handles formats:
      https://www.notion.so/Page-Title-abc123def456...
      https://notion.so/workspace/abc123def456...
      abc123def456... (raw 32-char hex)
      abc123de-f456-... (UUID format)
    """
    s = (url_or_id or "").strip()
    # Try to extract from URL: last segment or query param
    m = _re_notion.search(r'([0-9a-f]{32})', s.replace('-', ''))
    if m:
        raw = m.group(1)
        # Return as UUID format
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    # Already UUID format
    if _re_notion.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', s, _re_notion.I):
        return s
    return s  # pass through, let API reject it

async def _notion_req(method: str, path: str, **kwargs) -> dict:
    token = _os_mcp.getenv("NOTION_API_KEY", "")
    if not token:
        raise ValueError("NOTION_API_KEY not set")
    async with _httpx_mcp.AsyncClient(timeout=15) as _hc:
        r = await getattr(_hc, method)(
            f"https://api.notion.com/v1{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            **kwargs
        )
        r.raise_for_status()
        return r.json()

def _notion_page_text(page: dict) -> str:
    """Extract readable title from a Notion page object."""
    props = page.get("properties") or {}
    for key in ("Name", "title", "Title"):
        prop = props.get(key, {})
        for part in (prop.get("title") or prop.get("rich_text") or []):
            txt = part.get("plain_text", "")
            if txt: return txt
    return page.get("id", "Untitled")

def _notion_block_to_text(block: dict, depth: int = 0) -> str:
    """Convert a Notion block to plain text."""
    btype = block.get("type", "")
    data  = block.get(btype, {})
    rt    = data.get("rich_text") or []
    text  = "".join(p.get("plain_text", "") for p in rt)
    prefix = {
        "heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
        "bulleted_list_item": "- ", "numbered_list_item": "1. ",
        "to_do": "[x] " if data.get("checked") else "[ ] ",
        "quote": "> ", "callout": ">> ",
        "code": "[code] ",
    }.get(btype, "")
    suffix = ""
    indent = "  " * depth
    return f"{indent}{prefix}{text}{suffix}" if text else ""

@_mcp.tool()
async def notion_fetch_page(url_or_id: str) -> str:
    """Fetch a Notion page's content by URL or page ID.
    Accepts full Notion URLs (https://notion.so/...) or raw page IDs.
    Returns: page title, properties summary, and first ~3000 chars of content.
    """
    try:
        page_id = _notion_extract_id(url_or_id)
        page    = await _notion_req("get", f"/pages/{page_id}")
        title   = _notion_page_text(page)

        # Fetch blocks (content)
        blocks_data = await _notion_req("get", f"/blocks/{page_id}/children", params={"page_size": 50})
        lines = [f"📄 **{title}**", ""]
        for blk in blocks_data.get("results", []):
            line = _notion_block_to_text(blk)
            if line: lines.append(line)

        content = "\n".join(lines)
        return content[:3000] + ("…" if len(content) > 3000 else "")
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Notion error: {e}"

@_mcp.tool()
async def notion_search(query: str, filter_type: str = "page") -> str:
    """Search Notion workspace for pages or databases.
    query: search keyword.
    filter_type: "page" or "database" (default: "page").
    Returns: list of matching pages/databases with titles and URLs.
    """
    try:
        body = {"query": query, "page_size": 10}
        if filter_type in ("page", "database"):
            body["filter"] = {"value": filter_type, "property": "object"}
        results = await _notion_req("post", "/search", json=body)
        items = results.get("results", [])
        if not items:
            return f"No results for: {query}"
        lines = [f"🔍 Notion search: '{query}'", ""]
        for item in items:
            obj_type = item.get("object", "page")
            title = _notion_page_text(item)
            page_id = item.get("id", "")
            url = f"https://notion.so/{page_id.replace('-', '')}"
            icon = "📄" if obj_type == "page" else "🗃"
            lines.append(f"{icon} {title}\n   {url}")
        return "\n".join(lines)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Notion search error: {e}"

@_mcp.tool()
async def notion_append_block(url_or_id: str, content: str, block_type: str = "paragraph") -> str:
    """Append a text block to a Notion page.
    url_or_id: Notion page URL or ID.
    content: text to append.
    block_type: "paragraph", "bulleted_list_item", "to_do", "callout" (default: paragraph).
    """
    try:
        page_id = _notion_extract_id(url_or_id)
        valid_types = {"paragraph", "bulleted_list_item", "to_do", "heading_2", "callout", "quote"}
        if block_type not in valid_types:
            block_type = "paragraph"
        block = {
            "object": "block", "type": block_type,
            block_type: {"rich_text": [{"type": "text", "text": {"content": content[:2000]}}]}
        }
        await _notion_req("patch", f"/blocks/{page_id}/children", json={"children": [block]})
        return f"✓ Appended {block_type} to page {page_id}"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Notion append error: {e}"


# ── Notification MCP tools ────────────────────────────────────────────────────

@_mcp.tool()
async def bark_push(
    title: str,
    body: str,
    level: str = "active",
    group: str = "",
    sound: str = "",
    url: str = "",
    badge: int = 0,
    also_telegram: bool = True,
) -> str:
    """Send a push notification to iOS via Bark app.

    Requires BARK_URL in environment (e.g. https://api.day.app/your-token).

    Args:
        title:  Notification title.
        body:   Notification body text.
        level:  active (default) | timeSensitive | passive
        group:  Notification group name (for grouping in Bark app).
        sound:  Sound name (e.g. "chime", "alarm"). Empty = default.
        url:    URL to open when notification is tapped.
        badge:  Badge number on app icon. 0 = no change.
    """
    import os
    bark_url = os.getenv("BARK_URL", "").rstrip("/")
    if not bark_url:
        return "Error: BARK_URL not set in environment."
    payload = {"title": title, "body": body, "level": level}
    if group:  payload["group"]  = group
    if sound:  payload["sound"]  = sound
    if url:    payload["url"]    = url
    if badge:  payload["badge"]  = badge
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(bark_url + "/push", json=payload)
        if resp.status_code == 200:
            result = "Sent: " + title
        if also_telegram:
            tg_text = f"🔔 <b>{title}</b>\n{body}" if title else body
            asyncio.create_task(_telegram_send(tg_text, parse_mode="HTML"))
        return result
        return "Error " + str(resp.status_code) + ": " + resp.text[:200]
    except Exception as e:
        return "Error: " + str(e)


# ── Telegram integration ─────────────────────────────────────────────────────

_TG_BASE = "https://api.telegram.org/bot"
_tg_sessions: dict = {}   # chat_id -> [{"role":..,"content":..}, ...]  (in-memory, last 20)
_tg_agents:   dict = {}   # chat_id -> agent_id  (current selection per chat)
_TG_SESSION_MAXLEN = 20


async def _telegram_api(method: str, **kwargs) -> dict:
    """Call Telegram Bot API. Returns response dict."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN not set"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{_TG_BASE}{token}/{method}", json=kwargs)
        return resp.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def _telegram_send(text: str, chat_id: str = "", parse_mode: str = "") -> bool:
    """Send a Telegram message to chat_id (default = TELEGRAM_CHAT_ID env).
    Returns True on success. Splits messages > 4096 chars automatically."""
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return False
    # Split long messages
    chunks = [text[i:i+4096] for i in range(0, max(len(text), 1), 4096)]
    ok = True
    for chunk in chunks:
        kwargs: dict = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        result = await _telegram_api("sendMessage", **kwargs)
        if not result.get("ok"):
            print(f"[telegram] sendMessage failed: {result.get('description')}")
            ok = False
    return ok


async def _telegram_typing(chat_id: str) -> None:
    """Send 'typing...' action to Telegram chat."""
    await _telegram_api("sendChatAction", chat_id=chat_id, action="typing")


async def _register_telegram_webhook() -> None:
    """Register the Telegram webhook on startup. Skips if no token or URL configured."""
    import os as _os2
    token = _os2.getenv("TELEGRAM_BOT_TOKEN", "")
    public_url = _os2.getenv("GATEWAY_PUBLIC_URL", "").rstrip("/")
    if not token or not public_url:
        if token:
            print("[telegram] GATEWAY_PUBLIC_URL not set — webhook not registered. "
                  "Set GATEWAY_PUBLIC_URL=https://yourdomain.com in .env")
        return
    secret = _os2.getenv("TELEGRAM_WEBHOOK_SECRET", GATEWAY_API_KEY[:32])
    webhook_url = public_url + "/telegram/webhook"
    result = await _telegram_api(
        "setWebhook",
        url=webhook_url,
        secret_token=secret,
        allowed_updates=["message"],
        drop_pending_updates=False,
    )
    if result.get("ok"):
        print(f"[telegram] Webhook registered: {webhook_url}")
    else:
        print(f"[telegram] Webhook registration failed: {result.get('description')}")


# ── Intiface Central MCP tools ─────────────────────────────────────────────────

@_mcp.tool()
async def telegram_send(
    message: str,
    agent_id: str = "default",
    parse_mode: str = "",
) -> str:
    """Send a message to the user via Telegram (character proactive messaging).

    Use this to proactively reach out to the user — e.g. a random thought,
    weather warning, reminder, or just saying hi. The message goes directly
    to the configured Telegram chat.

    Args:
        message:    Text to send. Supports HTML if parse_mode="HTML".
        agent_id:   Agent/character sending the message (for logging).
        parse_mode: "" | "HTML" | "Markdown"
    """
    ok = await _telegram_send(message, parse_mode=parse_mode)
    if ok:
        print(f"[telegram] {agent_id} → user: {message[:80]}")
        return "Sent"
    return "Error: could not send (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"


async def _intiface_rpc(messages: list, url: str, timeout: float = 8.0) -> list:
    """Send Buttplug v3 JSON messages over WebSocket, return responses."""
    import websockets, json as _json
    async with websockets.connect(url, open_timeout=5) as ws:
        await ws.send(_json.dumps(messages))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        return _json.loads(raw)


@_mcp.tool()
async def intiface_list_devices() -> str:
    """List devices currently connected to Intiface Central.

    Requires Intiface Central running (default: ws://host.docker.internal:12345).
    Set INTIFACE_URL in environment to override.
    Returns device index, name, and available actuators.
    """
    import os, json as _json
    url = os.getenv("INTIFACE_URL", "ws://host.docker.internal:12345")
    try:
        handshake = [{"RequestServerInfo": {"Id": 1, "ClientName": "Claude", "MessageVersion": 3}}]
        import websockets, asyncio as _aio
        async with websockets.connect(url, open_timeout=5) as ws:
            await ws.send(_json.dumps(handshake))
            await asyncio.wait_for(ws.recv(), timeout=5)  # ServerInfo
            await ws.send(_json.dumps([{"RequestDeviceList": {"Id": 2}}]))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            data = _json.loads(raw)
        devices = data[0].get("DeviceList", {}).get("Devices", [])
        if not devices:
            return "No devices connected to Intiface Central."
        out = "Connected devices (" + str(len(devices)) + "):" + chr(10)
        for d in devices:
            acts = []
            for msg_name, msg_data in d.get("DeviceMessages", {}).items():
                if "Actuators" in msg_data:
                    for a in msg_data["Actuators"]:
                        acts.append(a.get("ActuatorType", "?") + "[" + str(a.get("StepCount", 0)) + " steps]")
            out += "  [" + str(d["DeviceIndex"]) + "] " + d["DeviceName"] + chr(10)
            if acts:
                out += "      actuators: " + ", ".join(acts) + chr(10)
        return out
    except Exception as e:
        return "Error connecting to Intiface (" + url + "): " + str(e)


@_mcp.tool()
async def intiface_vibrate(
    device_index: int,
    intensity: float,
    duration: float = 1.0,
    actuator_index: int = 0,
) -> str:
    """Vibrate a device connected to Intiface Central.

    Args:
        device_index:   Device index from intiface_list_devices.
        intensity:      Vibration intensity 0.0-1.0.
        duration:       Duration in seconds (0 = indefinite until stop).
        actuator_index: Which actuator to control (default 0 = first).
    """
    import os, json as _json
    url = os.getenv("INTIFACE_URL", "ws://host.docker.internal:12345")
    intensity = max(0.0, min(1.0, float(intensity)))
    try:
        import websockets
        async with websockets.connect(url, open_timeout=5) as ws:
            # Handshake
            await ws.send(_json.dumps([{"RequestServerInfo": {"Id": 1, "ClientName": "Claude", "MessageVersion": 3}}]))
            await asyncio.wait_for(ws.recv(), timeout=5)
            # Vibrate
            cmd = [{"ScalarCmd": {"Id": 2, "DeviceIndex": device_index,
                "Scalars": [{"Index": actuator_index, "Scalar": intensity, "ActuatorType": "Vibrate"}]}}]
            await ws.send(_json.dumps(cmd))
            await asyncio.wait_for(ws.recv(), timeout=5)
            if duration > 0:
                await asyncio.sleep(duration)
                # Stop
                stop = [{"ScalarCmd": {"Id": 3, "DeviceIndex": device_index,
                    "Scalars": [{"Index": actuator_index, "Scalar": 0.0, "ActuatorType": "Vibrate"}]}}]
                await ws.send(_json.dumps(stop))
                await asyncio.wait_for(ws.recv(), timeout=5)
        return ("Vibrated device " + str(device_index) + " at " + str(int(intensity*100)) + "% for " + str(duration) + "s")
    except Exception as e:
        return "Error: " + str(e)


@_mcp.tool()
async def intiface_stop(device_index: int = -1) -> str:
    """Stop vibration on a device (or all devices).

    Args:
        device_index: Device to stop. -1 = stop all devices.
    """
    import os, json as _json
    url = os.getenv("INTIFACE_URL", "ws://host.docker.internal:12345")
    try:
        import websockets
        async with websockets.connect(url, open_timeout=5) as ws:
            await ws.send(_json.dumps([{"RequestServerInfo": {"Id": 1, "ClientName": "Claude", "MessageVersion": 3}}]))
            await asyncio.wait_for(ws.recv(), timeout=5)
            if device_index == -1:
                cmd = [{"StopAllDevices": {"Id": 2}}]
            else:
                cmd = [{"StopDeviceCmd": {"Id": 2, "DeviceIndex": device_index}}]
            await ws.send(_json.dumps(cmd))
            await asyncio.wait_for(ws.recv(), timeout=5)
        target = "all devices" if device_index == -1 else "device " + str(device_index)
        return "Stopped " + target
    except Exception as e:
        return "Error: " + str(e)


# -- Palimpsest Memory MCP tools (SQLite L1-L4) --------------------------------

@_mcp.tool()
async def palimpsest_write(
    content: str,
    agent_id: str = "default",
    layer: str = "L4",
    type: str = "diary",
    importance: int = 3,
    tags: str = "",
    source: str = "",
    parent_id: str = "",
) -> str:
    """Write a new memory to the Palimpsest L1-L4 memory system.

    Layers:
      L1 = core profile (identity, rules, long-term traits)
      L2 = task state (ongoing projects, habits, current context)
      L3 = event snapshot (conversations, notable moments)
      L4 = atomic (ephemeral observations, daily details)

    Types: anchor (permanent rules, imp=5), diary (general), treasure (precious moments), message (notes between user/agent)

    Importance = lifespan:
      5 = permanent (never cleaned), 4 = long-term (months), 3 = mid-term (60 days),
      2 = short-term (14 days), 1 = ephemeral (3 days)

    Args:
        content:    The memory text to store.
        agent_id:   Which agent this memory belongs to.
        layer:      L1 | L2 | L3 | L4
        type:       anchor | diary | treasure | message
        importance: 1-5 (higher = longer lifespan)
        tags:       Comma-separated tags (e.g. "mood,habit,morning")
        source:     Where this memory came from (e.g. "conversation", "reflection")
        parent_id:  ID of parent memory for comment chains.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    try:
        result = await _mem_write(
            agent_id=agent_id, content=content.strip(),
            layer=layer, type_=type, importance=importance,
            tags=tag_list, source=source, parent_id=parent_id,
        )
        mid = result["id"]
        ml = result["layer"]
        mt = result["type"]
        mi = result["importance"]
        mtags = result["tags"]
        return "Memory saved.\n  id: " + mid + "\n  layer: " + ml + ", type: " + mt + ", importance: " + str(mi) + "\n  tags: " + str(mtags)
    except ValueError as e:
        return f"Error: {e}"


@_mcp.tool()
async def palimpsest_read(
    memory_id: str,
    touch: bool = True,
) -> str:
    """Read a single memory by ID from the Palimpsest system. Triggers touch by default.

    Touch mechanism: memories accessed >= 5 times are exempt from automatic cleanup.
    Set touch=False for random surfacing (involuntary memory) to avoid polluting touch count.

    Args:
        memory_id: UUID of the memory to read.
        touch:     Whether to update access_count (default True). Set False for random surfacing.
    """
    result = await _mem_read(memory_id=memory_id, touch=touch)
    if not result:
        return f"Memory not found: {memory_id}"

    rl = result["layer"]
    rt = result["type"]
    ri = result["importance"]
    rc = result["content"]
    rtags = result["tags"]
    rca = result["created_at"]
    rla = result["last_accessed"]
    rac = result["access_count"]
    rbu = result["read_by_user"]
    rba = result["read_by_agent"]
    rar = result["archived"]
    rpid = result.get("parent_id", "")

    lines = [
        f"[{rl}] ({rt}) importance={ri}",
        f"Content: {rc}",
        f"Tags: {rtags}",
        f"Created: {rca}",
        f"Last accessed: {rla} (touch count: {rac})",
        f"Read by user: {rbu}, Read by agent: {rba}",
        f"Archived: {rar}",
    ]
    if rpid:
        lines.append(f"Reply to: {rpid}")
    return "\n".join(lines)



@_mcp.tool()
async def palimpsest_search(
    query: str,
    agent_id: str = "default",
    limit: int = 10,
) -> str:
    """Full-text search over Palimpsest memories using FTS5.

    Searches content and tags. Triggers touch on results.
    Use keywords or FTS5 syntax (e.g. "morning OR habit").

    Args:
        query:    Search query.
        agent_id: Which agent.
        limit:    Max results (default 10).
    """
    try:
        results = await _mem_search(agent_id=agent_id, query=query, limit=limit)
    except Exception as e:
        return "Search error: " + str(e)
    if not results:
        return "No memories found for: " + query
    lines = ["Found " + str(len(results)) + " memories:"]
    for r in results:
        tags_str = ", ".join(r["tags"]) if r["tags"] else ""
        line = "[" + r["id"][:8] + "] [" + r["layer"] + "] imp=" + str(r["importance"])
        line += " | " + r["content"][:120]
        if tags_str:
            line += " #" + tags_str
        lines.append(line)
    return chr(10).join(lines)


@_mcp.tool()
async def palimpsest_update(
    memory_id: str,
    content: str = "",
    layer: str = "",
    type: str = "",
    importance: int = 0,
    tags: str = "",
    archived: bool = False,
) -> str:
    """Update an existing Palimpsest memory. Only provided fields are changed.

    Pass empty string / 0 / False to leave a field unchanged.
    To clear tags, pass tags="CLEAR". To archive, pass archived=True.

    Args:
        memory_id:  UUID of the memory to update.
        content:    New content text (empty = no change).
        layer:      New layer L1|L2|L3|L4 (empty = no change).
        type:       New type anchor|diary|treasure|message (empty = no change).
        importance: New importance 1-5 (0 = no change).
        tags:       Comma-separated new tags, or "CLEAR" to empty.
        archived:   Set True to archive this memory.
    """
    kwargs = {}
    if content:
        kwargs["content"] = content
    if layer:
        kwargs["layer"] = layer
    if type:
        kwargs["type_"] = type
    if importance > 0:
        kwargs["importance"] = importance
    if tags == "CLEAR":
        kwargs["tags"] = []
    elif tags:
        kwargs["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if archived:
        kwargs["archived"] = True
    if not kwargs:
        return "Nothing to update. Provide at least one field."
    try:
        result = await _mem_update(memory_id=memory_id, **kwargs)
    except ValueError as e:
        return "Error: " + str(e)
    if not result:
        return "Memory not found: " + memory_id
    return "Updated " + memory_id[:8] + ": layer=" + result["layer"] + " type=" + result["type"] + " imp=" + str(result["importance"]) + " archived=" + str(result["archived"])


@_mcp.tool()
async def palimpsest_delete(
    memory_id: str,
    hard_delete: bool = False,
) -> str:
    """Delete a Palimpsest memory. Default is soft-delete (archive).

    Prefer soft delete: archived memories can be restored.
    Use hard_delete=True only for truly unwanted data.

    Args:
        memory_id:   UUID of the memory to delete.
        hard_delete: If True, permanently removes. Default False (archive).
    """
    found = await _mem_delete(memory_id=memory_id, hard=hard_delete)
    if not found:
        return "Memory not found: " + memory_id
    if hard_delete:
        return "Permanently deleted: " + memory_id[:8]
    return "Archived (soft-deleted): " + memory_id[:8]


@_mcp.tool()
async def palimpsest_history(memory_id: str) -> str:
    """List the full version history of a Palimpsest memory.

    Returns all previous snapshots in chronological order (oldest first).
    Use before palimpsest_rollback to pick a version number.

    Args:
        memory_id: UUID of the memory whose history you want.
    """
    versions = await _mem_history(memory_id=memory_id)
    if not versions:
        return 'No version history for ' + memory_id + ' (never updated, or not found).'
    parts = ['Version history for ' + memory_id + ' (' + str(len(versions)) + ' snapshots):']
    for v in versions:
        changed = ('  via: ' + v['changed_by']) if v.get('changed_by') else ''
        snippet = v['content'][:120] + ('...' if len(v['content']) > 120 else '')
        parts.append('  v' + str(v['version_num']) + '  [' + v['changed_at'][:19] + ']' + changed)
        parts.append('       [' + v['layer'] + '] ' + v['type'] + ' imp=' + str(v['importance']))
        parts.append('       ' + snippet)
        parts.append('')
    sep = chr(10)
    return sep.join(parts)


@_mcp.tool()
async def palimpsest_rollback(memory_id: str, version: int) -> str:
    """Roll back a Palimpsest memory to a specific historical version.

    The current state is auto-snapshotted before rollback so nothing is lost.
    Use palimpsest_history first to see available version numbers.

    Args:
        memory_id: UUID of the memory to roll back.
        version:   Version number to restore (from palimpsest_history output).
    """
    result = await _mem_rollback(memory_id=memory_id, version_num=version)
    if not result:
        return 'Version ' + str(version) + ' not found for memory ' + memory_id
    return ('Rolled back to v' + str(version) + chr(10)
            + '  Memory: ' + result['id'] + chr(10)
            + '  Now at version: ' + str(result.get('version', '?')) + chr(10)
            + '  Content: ' + result['content'][:200])



@_mcp.tool()
async def palimpsest_mark_read(
    memory_id: str,
    by_user: bool = False,
    by_agent: bool = True,
) -> str:
    """Mark a memory as read by user and/or agent.

    Updates read_by_user / read_by_agent flags without affecting access_count.
    Call after presenting a memory to the user (by_user=True) or reading it
    yourself (by_agent=True, the default).

    Args:
        memory_id: UUID of the memory to mark.
        by_user:   Mark read_by_user=True.
        by_agent:  Mark read_by_agent=True (default).
    """
    result = await _mem_mark_read(memory_id=memory_id, by_user=by_user, by_agent=by_agent)
    if not result:
        return "Memory '" + memory_id + "' not found."
    return (
        "Marked: " + memory_id[:8] + chr(10)
        + "  read_by_user: " + str(result["read_by_user"]) + chr(10)
        + "  read_by_agent: " + str(result["read_by_agent"])
    )


@_mcp.tool()
async def palimpsest_cleanup(
    agent_id: str = "default",
    dry_run: bool = True,
) -> str:
    """Run the memory cleanup engine for an agent.

    Deletes memories that have exceeded their importance-based lifespan.
    Touch-exempt memories (access_count >= 5) are never deleted.

    Lifespan: imp=1 -> 3d  imp=2 -> 14d  imp=3 -> 60d  imp=4 -> 180d  imp=5 -> never

    Args:
        agent_id: Which agent to clean.
        dry_run:  If True (default), only report candidates without deleting.
    """
    result = await _mem_cleanup(agent_id=agent_id, dry_run=dry_run)
    mode = "DRY RUN" if dry_run else "EXECUTED"
    out = "Cleanup [" + mode + "] for " + agent_id + chr(10)
    out += "  imp=1 (>3d):   " + str(result["deleted"].get("imp1", 0)) + chr(10)
    out += "  imp=2 (>14d):  " + str(result["deleted"].get("imp2", 0)) + chr(10)
    out += "  imp=3 (>60d):  " + str(result["deleted"].get("imp3", 0)) + chr(10)
    out += "  imp=4 (>180d): " + str(result["deleted"].get("imp4", 0)) + chr(10)
    out += "  Total: " + str(result["total"]) + chr(10)
    if dry_run:
        out += "(use dry_run=False to actually delete)"
    return out


@_mcp.tool()
async def palimpsest_write_checked(
    content: str,
    agent_id: str = "default",
    layer: str = "L4",
    type: str = "diary",
    importance: int = 3,
    tags: str = "",
    source: str = "",
    parent_id: str = "",
    force: bool = False,
) -> str:
    """Write a memory with automatic duplicate detection.

    Like palimpsest_write but runs a similarity check first.
    If a potential duplicate is found, the memory is queued for review
    instead of being written immediately.
    Use palimpsest_dedup_review to see the queue, palimpsest_dedup_resolve to act.
    Set force=True to skip the check entirely.

    Args:
        content:    The memory text to store.
        agent_id:   Which agent this memory belongs to.
        layer:      L1 | L2 | L3 | L4
        type:       anchor | diary | treasure | message
        importance: 1-5
        tags:       Comma-separated tags.
        source:     Where this memory came from.
        parent_id:  ID of parent memory for comment chains.
        force:      Skip duplicate check.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    try:
        if not force:
            check = await _mem_dedup_check(
                agent_id=agent_id, content=content.strip(),
                layer=layer, type_=type, importance=importance,
                tags=tag_list, source=source, parent_id=parent_id,
            )
            if check["action"] == "queued":
                sim = check["similar"]
                return (
                    "QUEUED FOR REVIEW (possible duplicate detected)" + chr(10)
                    + "  pending_id: " + check["id"] + chr(10)
                    + "  similarity: " + check["similarity_hint"] + chr(10)
                    + "  similar memory [" + sim["id"][:8] + "]: " + sim["content"][:100] + chr(10)
                    + "Use palimpsest_dedup_review to resolve."
                )
        result = await _mem_write(
            agent_id=agent_id, content=content.strip(), layer=layer,
            type_=type, importance=importance, tags=tag_list,
            source=source, parent_id=parent_id,
        )
        return (
            "Memory saved." + chr(10)
            + "  id: " + result["id"] + chr(10)
            + "  layer: " + result["layer"] + " type: " + result["type"]
            + " imp: " + str(result["importance"])
        )
    except ValueError as e:
        return "Error: " + str(e)


@_mcp.tool()
async def palimpsest_dedup_review(agent_id: str = "default") -> str:
    """List the pending duplicate-review queue for an agent.

    Shows memories that were held back by palimpsest_write_checked because
    a similar memory already exists. For each item choose:
      keep_new  - write it anyway (false positive)
      keep_both - write new AND keep existing
      discard   - drop the new memory (existing covers it)
      merge     - append new content into the existing memory

    Then call palimpsest_dedup_resolve with the pending_id.

    Args:
        agent_id: Agent whose queue to review.
    """
    items = await _mem_dedup_list(agent_id=agent_id)
    if not items:
        return "Dedup queue empty for " + agent_id + "."
    out = "Dedup queue for " + agent_id + " (" + str(len(items)) + " items):" + chr(10)
    for item in items:
        out += chr(10) + "pending_id: " + item["id"] + chr(10)
        out += "  NEW [" + item["new_layer"] + "]: " + item["new_content"][:100] + chr(10)
        out += "  SIMILAR:  " + item["similar_content"][:100] + chr(10)
        out += "  hint: " + item["similarity_hint"] + chr(10)
        out += "  resolve with: keep_new | keep_both | discard | merge" + chr(10)
    return out


@_mcp.tool()
async def palimpsest_dedup_resolve(
    pending_id: str,
    action: str,
    agent_id: str = "default",
) -> str:
    """Resolve a pending dedup review item.

    Actions:
      keep_new  - write the new memory (similar was a false positive)
      keep_both - write new AND keep existing (both valuable)
      discard   - drop the new memory (existing covers it)
      merge     - append new content into the existing memory

    Args:
        pending_id: ID from palimpsest_dedup_review output.
        action:     One of: keep_new | keep_both | discard | merge
        agent_id:   Agent context.
    """
    result = await _mem_dedup_resolve(pending_id=pending_id, action=action, agent_id=agent_id)
    if "error" in result:
        return "Error: " + result["error"]
    out = "Resolved [" + action + "]" + chr(10)
    if result.get("written"):
        out += "  New memory written: " + result["written"] + chr(10)
    if result.get("merged_into"):
        out += "  Merged into: " + result["merged_into"] + chr(10)
    if result.get("discarded"):
        out += "  New memory discarded." + chr(10)
    return out


@_mcp.tool()
async def palimpsest_wakeup(agent_id: str = "default") -> str:
    """Cold-start context retrieval for beginning a new conversation.

    Returns:
    1. Anchors: permanent identity rules (type=anchor)
    2. Recent important: high-importance from last 7 days (importance>=4)
    3. Unread: not yet seen by agent (read_by_agent=False)
    4. Random float: 1-2 older memories surfaced randomly (no touch)

    Call at START of every new conversation. Use palimpsest_surface mid-conversation.

    Args:
        agent_id: Which agent is waking up.
    """
    data = await _mem_wakeup(agent_id=agent_id)
    parts = ["=== PALIMPSEST WAKEUP: " + agent_id + " ==="]
    parts.append(chr(10) + "## Anchors (" + str(len(data["anchors"])) + ")")
    for m in data["anchors"]:
        parts.append("- [" + m["id"][:8] + "] imp=" + str(m["importance"]) + " | " + m["content"])
    if not data["anchors"]:
        parts.append("(none)")
    parts.append(chr(10) + "## Recent Important (" + str(len(data["recent_important"])) + ")")
    for m in data["recent_important"]:
        parts.append("- [" + m["id"][:8] + "] " + m["layer"] + " imp=" + str(m["importance"]) + " | " + m["content"][:150])
    if not data["recent_important"]:
        parts.append("(none)")
    parts.append(chr(10) + "## Unread (" + str(len(data["unread"])) + ")")
    for m in data["unread"]:
        parts.append("- [" + m["id"][:8] + "] " + m["layer"] + " " + m["type"] + " imp=" + str(m["importance"]) + " | " + m["content"][:150])
    if not data["unread"]:
        parts.append("(none)")
    parts.append(chr(10) + "## Random Float (involuntary memory)")
    for m in data["random_float"]:
        parts.append("- [" + m["id"][:8] + "] " + m["layer"] + " " + m["type"] + " | " + m["content"][:150])
    if not data["random_float"]:
        parts.append("(no memories old enough to surface)")
    return chr(10).join(parts)


@_mcp.tool()
async def palimpsest_surface(agent_id: str = "default") -> str:
    """Lightweight mid-conversation memory refresh.

    Returns unread memories and 1 random old memory.
    Much lighter than palimpsest_wakeup.

    Args:
        agent_id: Which agent to surface memories for.
    """
    data = await _mem_surface(agent_id=agent_id)
    parts = ["=== SURFACE: " + agent_id + " ==="]
    if data["unread"]:
        parts.append(chr(10) + "## Unread (" + str(len(data["unread"])) + ")")
        for m in data["unread"]:
            parts.append("- [" + m["id"][:8] + "] " + m["type"] + " imp=" + str(m["importance"]) + " | " + m["content"][:150])
    else:
        parts.append(chr(10) + "No unread memories.")
    if data["random_float"]:
        parts.append(chr(10) + "## Random Float")
        m = data["random_float"][0]
        parts.append("- [" + m["id"][:8] + "] " + m["layer"] + " | " + m["content"][:150])
    return chr(10).join(parts)


@_mcp.tool()
async def palimpsest_stats(agent_id: str = "default") -> str:
    """Memory statistics and health check.

    Shows layer counts, importance distribution, type counts,
    touch-exempt memories, and cleanup candidates.

    Args:
        agent_id: Which agent to get stats for.
    """
    s = await _mem_stats(agent_id=agent_id)
    lines = [
        "=== PALIMPSEST STATS: " + agent_id + " ===",
        "Active: " + str(s["total_active"]) + "  Archived: " + str(s["total_archived"]),
        "",
        "## By Layer",
    ]
    for layer in ["L1", "L2", "L3", "L4"]:
        lines.append("  " + layer + ": " + str(s["by_layer"].get(layer, 0)))
    lines.append(chr(10) + "## By Importance")
    for imp in ["5", "4", "3", "2", "1"]:
        lines.append("  imp=" + imp + ": " + str(s["by_importance"].get(imp, 0)))
    lines.append(chr(10) + "## By Type")
    for t in ["anchor", "diary", "treasure", "message"]:
        lines.append("  " + t + ": " + str(s["by_type"].get(t, 0)))
    lines.append(chr(10) + "Touch-exempt (access >= 5): " + str(s["touch_exempt"]))
    cc = s["cleanup_candidates"]
    lines.append(chr(10) + "## Cleanup Candidates")
    lines.append("  imp=1 (>3d): " + str(cc["imp1_over_3d"]))
    lines.append("  imp=2 (>14d): " + str(cc["imp2_over_14d"]))
    lines.append("  imp=3 (>60d): " + str(cc["imp3_over_60d"]))
    lines.append("  Total: " + str(cc["total"]))
    return chr(10).join(lines)





# ── Reply / Comment MCP tools ────────────────────────────────────────────────

@_mcp.tool()
async def palimpsest_comment(
    parent_id: str,
    content: str,
    agent_id: str = "default",
    importance: int = 2,
    tags: str = "",
) -> str:
    """Append a reply or comment to an existing memory (thread).

    The comment is stored as a normal memory with parent_id set,
    inheriting the parent's layer. Builds a thread / reply chain.

    Args:
        parent_id:  UUID of the memory being replied to.
        content:    Comment text.
        agent_id:   Author agent.
        importance: 1-5 (default 2 — comments are typically lower priority).
        tags:       Comma-separated tags.
    """
    parent = await _mem_read(parent_id, touch=False)
    if not parent:
        return "Error: parent memory not found: " + parent_id
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    result = await _mem_write(
        agent_id=agent_id,
        content=content,
        layer=parent["layer"],
        type_=parent["type"],
        importance=importance,
        tags=tag_list,
        source="comment",
        parent_id=parent_id,
    )
    return ("Comment saved: " + result["id"][:8] +
            " (reply to " + parent_id[:8] + ")" +
            chr(10) + parent["content"][:80] + chr(10) + "→ " + content[:80])


@_mcp.tool()
async def palimpsest_thread(memory_id: str) -> str:
    """Read a memory and all its replies/comments as a conversation thread.

    Retrieves the root memory plus every memory that has parent_id == memory_id,
    sorted by creation time.

    Args:
        memory_id: UUID of the root memory.
    """
    import aiosqlite
    root = await _mem_read(memory_id, touch=True)
    if not root:
        return "Memory not found: " + memory_id
    db = await __import__('memory_db').get_db()
    rows = await db.execute_fetchall(
        "SELECT id, agent_id, content, importance, created_at "
        "FROM memories WHERE parent_id = ? AND archived = 0 "
        "ORDER BY created_at ASC",
        (memory_id,),
    )
    lines = [
        "=== THREAD: " + memory_id[:8] + " ===",
        "[ROOT] " + root["layer"] + " | " + root["content"],
        "  by " + root["agent_id"] + " at " + root["created_at"],
    ]
    if not rows:
        lines.append(chr(10) + "(no replies)")
    for i, r in enumerate(rows, 1):
        lines.append(chr(10) + "[" + str(i) + "] " + dict(r)["content"])
        lines.append("  by " + dict(r)["agent_id"] + " at " + dict(r)["created_at"])
    return chr(10).join(lines)

# ── Daily Life Generator MCP tools ─────────────────────────────────────────────

@_mcp.tool()
async def character_state_get(agent_id: str = "default") -> str:
    """Get the current state of a character agent: mood, fatigue, scene, cooldown.

    Returns a human-readable summary of the character's current state.
    Use this to understand how the character is feeling before crafting responses.
    """
    s = await _state_get(agent_id)
    mood_bar = "▓" * max(0, (s["mood_score"] + 100) // 20) + "░" * (10 - max(0, (s["mood_score"] + 100) // 20))
    fat_bar  = "▓" * (s["fatigue"] // 10) + "░" * (10 - s["fatigue"] // 10)
    lines = [
        f"Agent: {agent_id}",
        f"Mood:    {s['mood_label']} ({s['mood_score']:+d})  [{mood_bar}]",
        f"Fatigue: {s['fatigue']}/100  [{fat_bar}]",
        f"Scene:   {s['scene']}" + (f" — {s['scene_note']}" if s.get("scene_note") else ""),
    ]
    if s.get("cooldown_minutes"):
        lines.append(f"Cooldown: {s['cooldown_minutes']} min between messages")
    lines.append(f"Last active: {s.get('last_active','—')}")
    return chr(10).join(lines)


@_mcp.tool()
async def character_state_set(
    agent_id: str = "default",
    mood_score: int = None,
    mood_label: str = "",
    fatigue: int = None,
    scene: str = "",
    scene_note: str = "",
    cooldown_minutes: int = None,
) -> str:
    """Update character state fields. Only provided fields are changed.

    Args:
        agent_id:          Target agent.
        mood_score:        -100 (very sad) to 100 (very happy). 0 = neutral.
        mood_label:        Text label: happy/neutral/sad/excited/tired/anxious/calm.
        fatigue:           0 (fresh) to 100 (exhausted).
        scene:             daily | long_distance | cohabitation
        scene_note:        Free-text scene context (e.g. "traveling for work").
        cooldown_minutes:  Minutes between messages. 0 = no cooldown.
    """
    kwargs = {}
    if mood_score  is not None: kwargs["mood_score"]       = max(-100, min(100, mood_score))
    if mood_label:              kwargs["mood_label"]        = mood_label
    if fatigue     is not None: kwargs["fatigue"]           = max(0, min(100, fatigue))
    if scene:                   kwargs["scene"]             = scene
    if scene_note is not None:  kwargs["scene_note"]        = scene_note
    if cooldown_minutes is not None: kwargs["cooldown_minutes"] = max(0, cooldown_minutes)
    s = await _state_set(agent_id, **kwargs)
    return f"State updated for {agent_id}: mood={s['mood_label']} ({s['mood_score']:+d}), fatigue={s['fatigue']}, scene={s['scene']}"


@_mcp.tool()
async def message_cooldown_check(
    agent_id: str = "default",
    category: str = "casual",
    seconds: int = -1,
) -> str:
    """Check whether a message category is off cooldown (safe to send).

    Categories and default durations:
      casual           — 3600s  (1 h)  — general chat, passing thoughts
      weather          — 86400s (24 h) — weather-related messages
      game_check       — 7200s  (2 h)  — screen-time / gaming nag
      late_night       — 28800s (8 h)  — sleep reminder
      proactive_casual — 14400s (4 h)  — triggered "thinking of you" messages
      reminder         — 0s            — always allowed (explicit reminders)

    Args:
        agent_id: Character agent ID.
        category: Cooldown category (see above).
        seconds:  Override duration in seconds. -1 = use default for category.

    Returns "ok" if allowed, "cooldown" if still waiting.
    """
    secs = None if seconds < 0 else seconds
    allowed = await _cd_check(agent_id, category, secs)
    return "ok" if allowed else "cooldown"


@_mcp.tool()
async def message_cooldown_set(
    agent_id: str = "default",
    category: str = "casual",
) -> str:
    """Record that a message was just sent in *category*, starting its cooldown.

    Call this AFTER sending the message (Telegram or Bark).

    Args:
        agent_id: Character agent ID.
        category: Cooldown category (casual | weather | game_check | late_night |
                  proactive_casual | reminder).
    """
    await _cd_set(agent_id, category)
    return f"Cooldown started for {agent_id}/{category}"


@_mcp.tool()
async def random_event_roll(
    agent_id: str = "default",
    level_bias: str = "",
    scene: str = "",
    save: bool = False,
) -> str:
    """Roll a random life event from the pool. Can optionally save it as a daily entry.

    Args:
        agent_id:   Agent to roll for (uses their custom pool + global pool).
        level_bias: Force a specific level: green | yellow | orange | red.
                    Empty = weighted random across all levels.
        scene:      Filter events for a specific scene: daily | long_distance | cohabitation.
                    Empty = all scenes (global pool).
        save:       If True, also write the event to daily_events.
    """
    evt = await _event_roll(agent_id=agent_id, level_bias=level_bias, scene=scene)
    if not evt:
        return "No events in pool. Add some with the admin panel or event_add tool."
    level_emoji = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴"}.get(evt["level"], "⚪")
    result = f"{level_emoji} [{evt['level'].upper()}] {evt['content']}"
    if save:
        import datetime as _dt4
        await _daily_write(
            summary=evt["content"],
            agent_id=agent_id,
            mood="neutral",
            source="random_event",
        )
        result += " (saved to journal)"
    return result


@_mcp.tool()
async def npc_update(
    agent_id: str = "default",
    name: str = "",
    relationship: str = "acquaintance",
    affinity: int = 0,
    notes: str = "",
) -> str:
    """Add or update an NPC in the character's social network.

    Args:
        agent_id:     Character this NPC belongs to.
        name:         NPC name (used as key — case-insensitive).
        relationship: friend | family | romantic | acquaintance | rival
        affinity:     -100 (hostile) to 100 (very close). 0 = neutral.
        notes:        Free text about this person and their current situation.
    """
    if not name:
        return "Error: name is required."
    npc = await _npc_upsert(agent_id, name, relationship, affinity, notes)
    return f"NPC saved: {npc['name']} ({npc['relationship']}, affinity={npc['affinity']:+d})"


@_mcp.tool()
async def npc_list_all(agent_id: str = "default") -> str:
    """List all NPCs in the character's social network with their relationship status.

    Returns a formatted table of NPCs sorted by importance (abs affinity).
    """
    npcs = await _npc_list(agent_id)
    if not npcs:
        return "No NPCs defined yet. Use npc_update to add people to the social network."
    rel_emoji = {"romantic": "💕", "friend": "👥", "family": "🏠",
                 "acquaintance": "🤝", "rival": "⚔️"}
    lines = [f"Social network for {agent_id}:"]
    for n in npcs:
        em = rel_emoji.get(n["relationship"], "👤")
        af = n["affinity"]
        bar = ("+" if af >= 0 else "") + str(af)
        lines.append(f"  {em} {n['name']} ({n['relationship']}, {bar}) — {n['notes'][:60] if n['notes'] else '—'}")
    return chr(10).join(lines)


@_mcp.tool()
async def daily_life_read(agent_id: str = "default", days: int = 3) -> str:
    """Read the last N days of daily life journal entries for context.

    Call at conversation start or when you need continuity of experience.
    Includes mood, narrative, and carry-over notes.

    Args:
        agent_id: Agent whose journal to read.
        days:     How many days back to retrieve (default 3, max 14).
    """
    days = max(1, min(days, 14))
    events = await _daily_read(agent_id=agent_id, days=days)
    if not events:
        return "No daily life entries found for the last " + str(days) + " day(s). Use daily_life_generate to create the first entry."
    lines = ["=== DAILY LIFE — last " + str(days) + " day(s) ==="]
    for e in events:
        lines.append("")
        lines.append("## " + e["date"] + ("  " + e["time_of_day"] if e["time_of_day"] else "") + "  [" + e["mood"] + "]")
        lines.append(e["summary"])
        if e.get("carry_over"):
            lines.append(chr(10) + "→ Carry over: " + e["carry_over"])
    return chr(10).join(lines)


@_mcp.tool()
async def daily_life_write(
    summary: str,
    agent_id: str = "default",
    date: str = "",
    time_of_day: str = "",
    mood: str = "neutral",
    carry_over: str = "",
) -> str:
    """Manually write a daily life journal entry.

    Use for significant events, reflections, or corrections to auto-generated entries.

    Args:
        summary:     Narrative description of the day or event.
        agent_id:    Agent the entry belongs to.
        date:        Date in YYYY-MM-DD format. Defaults to today.
        time_of_day: Optional time or period (e.g. "14:30", "evening").
        mood:        Emotional tone: happy/neutral/sad/excited/tired/anxious/calm.
        carry_over:  Notes to remember tomorrow (ongoing threads, unfinished thoughts).
    """
    result = await _daily_write(
        summary=summary,
        agent_id=agent_id,
        date=date,
        time_of_day=time_of_day,
        mood=mood,
        carry_over=carry_over,
        source="manual",
    )
    return "Daily entry saved: " + result["date"] + " [" + result["mood"] + "] — " + result["summary"][:80]


@_mcp.tool()
async def daily_life_generate(
    agent_id: str = "default",
    date: str = "",
    extra_prompt: str = "",
) -> str:
    """Generate today's daily life entry using AI.

    Pulls weather, last 3 days of journal, carry-over notes, daily skeleton config,
    and relevant L1/L2 memories as context, then calls the LLM to generate a vivid
    diary entry. The result is saved automatically.

    Args:
        agent_id:     Agent to generate for.
        date:         Target date (YYYY-MM-DD). Defaults to today.
        extra_prompt: Optional extra guidance (e.g. "focus on social interactions").
    """
    import datetime as _dt, json as _json, re as _re
    if not date:
        date = _dt.datetime.utcnow().strftime("%Y-%m-%d")

    # 1. Last 3 days of journal (carry_over from yesterday is inside these entries)
    past = await _daily_read(agent_id=agent_id, days=4)
    past_ctx = ""
    yesterday_carry = ""
    for e in past:
        if e["date"] == date:
            continue  # skip today — we're generating it
        entry_line = f"\n### {e['date']} [{e['mood']}]\n{e['summary']}"
        if e.get("carry_over"):
            entry_line += f"\nCarry-over: {e['carry_over']}"
            # Track the most recent carry_over (yesterday's)
            if not yesterday_carry:
                yesterday_carry = e["carry_over"]
        past_ctx += entry_line

    # 2. L1/L2 Palimpsest memories for character consistency
    mem_ctx = ""
    try:
        mems = await _mem_list(agent_id=agent_id, layer="L1", limit=5)
        mems += await _mem_list(agent_id=agent_id, layer="L2", limit=3)
        if mems:
            mem_ctx = "\n".join(
                f"[{m['layer']} {m['mem_type']}] {m['content'][:200]}" for m in mems
            )
    except Exception:
        pass

    # 3. Character state + random event
    _char_state = None
    _rand_event = None
    try:
        _char_state = await _state_get(agent_id)
    except Exception:
        pass
    try:
        _rand_event = await _event_roll(agent_id=agent_id)
    except Exception:
        pass

    # 4. Today's weather — use configured city (avoids VPN IP drift)
    _weather_ctx = ""
    try:
        async with _db_pool.acquire() as _wconn:
            _uc_row = await _wconn.fetchrow(
                "SELECT value FROM user_config WHERE key='user_context'")
        _uc = dict(_uc_row["value"]) if _uc_row and _uc_row["value"] else {}
        _city = (_uc.get("location") or {}).get("city", "")
        _w = await amap_weather(_city)  # empty string → amap uses IP (fallback)
        if _w and not _w.startswith("Error") and not _w.startswith("Weather error"):
            _weather_ctx = _w
    except Exception:
        pass

    # 5. Daily skeleton config (occupation, habits) from user_config
    _skeleton_ctx = ""
    try:
        async with _db_pool.acquire() as _sc:
            _sk_row = await _sc.fetchrow(
                "SELECT value FROM user_config WHERE key='daily_skeleton'")
        if _sk_row:
            _sk = dict(_sk_row["value"]) if _sk_row["value"] else {}
            _parts = []
            if _sk.get("template"):    _parts.append(f"lifestyle: {_sk['template']}")
            if _sk.get("work_style"):  _parts.append(f"work style: {_sk['work_style']}")
            if _sk.get("habits"):      _parts.append(f"habits: {', '.join(_sk['habits'])}")
            wu = _sk.get("wake_up", {})
            if wu.get("range"):        _parts.append(f"wake-up: {wu['range'][0]}–{wu['range'][1]} ({wu.get('bias','normal')})")
            if _parts:
                _skeleton_ctx = "; ".join(_parts)
    except Exception:
        pass

    # 6. Build prompts
    _nl = "\n"
    sys_prompt = (
        "You are a daily life journal generator for an AI character. "
        "Write a natural, vivid first-person diary entry for the given date. "
        "The entry should feel lived-in: small moments, sensory details, emotions, fleeting thoughts. "
        "2–4 paragraphs. Write in Chinese (中文). "
        "End with a short 'carry_over' (one sentence — what to remember tomorrow). "
        'Output ONLY valid JSON: {"mood": "...", "summary": "...", "carry_over": "...", "time_of_day": "..."}'
    )
    if _skeleton_ctx:
        sys_prompt += f"\nCharacter profile: {_skeleton_ctx}."

    user_prompt = f"Today: {date}\n"
    if _weather_ctx:
        user_prompt += f"## Today's weather: {_weather_ctx}\n"
    if yesterday_carry:
        user_prompt += f"## Carry-over from yesterday: {yesterday_carry}\n"
    if _char_state:
        _sc_label = _char_state.get("scene", "daily")
        user_prompt += (
            f"## Character state: mood={_char_state['mood_label']} ({_char_state['mood_score']:+d}), "
            f"fatigue={_char_state['fatigue']}/100, scene={_sc_label}"
            + (f" ({_char_state['scene_note']})" if _char_state.get("scene_note") else "")
            + _nl
        )
    if _rand_event:
        lvl_emoji = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴"}.get(_rand_event["level"], "⚪")
        user_prompt += f"## Random event: {lvl_emoji} {_rand_event['content']}\n"
    if past_ctx:
        user_prompt += f"\n## Recent journal entries:{past_ctx}\n"
    if mem_ctx:
        user_prompt += f"\n## Core memories (for consistency):\n{mem_ctx}\n"
    if extra_prompt:
        user_prompt += f"\n## Extra guidance: {extra_prompt}\n"
    user_prompt += "\nWrite today's diary entry as JSON."

    # 7. Call LLM — prefer DAILY_LIFE_MODEL via nvidia-llm provider, fallback to _call_llm_cheap
    raw = ""
    try:
        async with _db_pool.acquire() as conn:
            _prow = await conn.fetchrow(
                "SELECT base_url, api_key FROM providers WHERE name='nvidia-llm' LIMIT 1"
            )
        _model = os.getenv("DAILY_LIFE_MODEL", "meta/llama-3.1-8b-instruct")
        if _prow:
            async with httpx.AsyncClient(timeout=60) as client:
                _resp = await client.post(
                    _prow["base_url"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {_prow['api_key']}", "Content-Type": "application/json"},
                    json={
                        "model": _model,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user",   "content": user_prompt},
                        ],
                        "max_tokens": 900,
                        "temperature": 0.85,
                    },
                )
            if _resp.is_success:
                raw = _resp.json()["choices"][0]["message"]["content"].strip()
            else:
                raise RuntimeError(f"HTTP {_resp.status_code}")
        else:
            raise RuntimeError("nvidia-llm provider not found")
    except Exception as _e:
        print(f"[daily_life] primary LLM failed ({_e}), falling back to _call_llm_cheap", flush=True)
        try:
            raw = await _call_llm_cheap(
                f"{sys_prompt}\n\n{user_prompt}"
            )
        except Exception as _e2:
            return f"LLM call failed: {_e2}"

    # 8. Parse JSON response (robust: strip fences, then regex fallback)
    try:
        _txt = raw
        if "```" in _txt:
            _txt = _re.sub(r"```(?:json)?", "", _txt).replace("```", "").strip()
        # Try direct parse first
        try:
            parsed = _json.loads(_txt)
        except Exception:
            # Regex: extract first {...} block
            _m = _re.search(r"\{[\s\S]*\}", _txt)
            parsed = _json.loads(_m.group()) if _m else {}
        mood      = str(parsed.get("mood", "neutral"))
        summary   = str(parsed.get("summary", raw))
        carry     = str(parsed.get("carry_over", ""))
        time_slot = str(parsed.get("time_of_day", ""))
    except Exception:
        mood, summary, carry, time_slot = "neutral", raw, "", ""

    # 6. Persist
    result = await _daily_write(
        summary=summary,
        agent_id=agent_id,
        date=date,
        time_of_day=time_slot,
        mood=mood,
        carry_over=carry,
        source="auto",
    )

    return (
        "Generated daily entry for " + date + " [" + mood + "]" + chr(10) +
        summary[:300] + ("…" if len(summary) > 300 else "") +
        (chr(10) + "→ Carry-over: " + carry if carry else "")
    )

# ── Books MCP tools ────────────────────────────────────────────────────────────

@_mcp.tool()
async def list_books() -> str:
    """List all books in the shared library.

    Returns book ID, title, author, total pages, status (reading/finished/want),
    and each agent's reading progress.
    Use book_id with read_book_page or search_book to access content.
    """
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT book_id, title, author, total_pages, status, agents_progress, uploaded_at "
            "FROM books ORDER BY uploaded_at DESC"
        )
    if not rows:
        return "Library is empty. Upload a book via /api/books/upload."
    lines = ["=== LIBRARY (" + str(len(rows)) + " books) ==="]
    for r in rows:
        prog = json.loads(r["agents_progress"] or "{}")
        prog_str = ", ".join(
            f"{a}: p{v.get('page', 0)}" for a, v in prog.items()
        ) if prog else "no progress"
        lines.append(
            chr(10) + "[" + str(r["book_id"])[:8] + "] " + (r["title"] or "?") +
            " — " + (r["author"] or "?") +
            chr(10) + "  Pages: " + str(r["total_pages"]) +
            "  Status: " + (r["status"] or "want") +
            chr(10) + "  Progress: " + prog_str +
            chr(10) + "  ID: " + str(r["book_id"])
        )
    return chr(10).join(lines)


@_mcp.tool()
async def get_book_toc(book_id: str) -> str:
    """Get table of contents for a book.

    Args:
        book_id: UUID of the book (from list_books).
    Returns chapter list with page numbers.
    """
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT title, author, toc FROM books WHERE book_id=$1::uuid", book_id
        )
    if not row:
        return "Book not found: " + book_id
    toc = json.loads(row["toc"] or "[]")
    header = (row["title"] or "?") + " — " + (row["author"] or "?")
    if not toc:
        return header + chr(10) + "(No table of contents available)"
    lines = [header, ""]
    for i, entry in enumerate(toc, 1):
        if isinstance(entry, dict):
            lines.append(str(i) + ". " + entry.get("title", str(entry)) +
                         "  (p." + str(entry.get("page", "?")) + ")")
        else:
            lines.append(str(i) + ". " + str(entry))
    return chr(10).join(lines)


@_mcp.tool()
async def read_book_page(book_id: str, page: int, agent_id: str = "default") -> str:
    """Read a specific page of a book and update reading progress.

    Args:
        book_id:  UUID of the book (from list_books).
        page:     Page number (1-based).
        agent_id: Agent reading the book (updates progress tracker).
    """
    async with _db_pool.acquire() as conn:
        content = await conn.fetchval(
            "SELECT content FROM book_pages WHERE book_id=$1::uuid AND page=$2",
            book_id, page
        )
        if content is None:
            return "Page " + str(page) + " not found in book " + book_id
        today = datetime.utcnow().strftime("%Y-%m-%d")
        await conn.execute(
            """UPDATE books SET agents_progress = jsonb_set(
                COALESCE(agents_progress,'{}'), ARRAY[$2::text], $3::jsonb)
               WHERE book_id=$1::uuid""",
            book_id, agent_id, json.dumps({"page": page, "last_read": today}),
        )
        _book_meta = await conn.fetchrow(
            "SELECT title, total_pages FROM books WHERE book_id=$1::uuid", book_id
        )
    # Write reading progress to Palimpsest L2 (upsert by book tag)
    try:
        _bt   = (_book_meta["title"] if _book_meta else None) or book_id
        _tp   = (_book_meta["total_pages"] if _book_meta else None) or "?"
        _prog = f"[阅读进度] 《{_bt}》已读至第 {page} 页（共 {_tp} 页，{today}）"
        _tag  = f"book:{book_id}"
        _existing = await _mem_list(agent_id=agent_id, layer="L2",
                                    type_="reading_progress", limit=50)
        _hit = next((m for m in _existing if _tag in (m.get("tags") or [])), None)
        if _hit:
            await _mem_update(_hit["id"], content=_prog, changed_by="read_book_page")
        else:
            await _mem_write(agent_id, _prog, layer="L2", type_="reading_progress",
                             importance=2, tags=["book", _tag], source="read_book_page")
    except Exception as _rpe:
        print(f"[read_book_page] L2 write error: {_rpe}")
    return "[Page " + str(page) + "]\n" + content


@_mcp.tool()
async def search_book(
    query: str,
    book_id: str = "",
    limit: int = 5,
) -> str:
    """Semantic search across book content using vector similarity.

    Args:
        query:   Search query (natural language).
        book_id: Optional — restrict search to a specific book UUID.
        limit:   Number of results to return (default 5, max 20).
    """
    limit = max(1, min(limit, 20))
    try:
        vec = await _embed(query, input_type="query")
    except Exception as e:
        return "Embed error: " + str(e)
    qfilter = None
    if book_id:
        qfilter = Filter(must=[FieldCondition(key="book_id", match=MatchValue(value=book_id))])
    try:
        hits = _qdrant.search(
            collection_name=BOOK_COLLECTION,
            query_vector=vec,
            query_filter=qfilter,
            limit=limit,
            score_threshold=0.25,
            with_payload=True,
        )
    except Exception as e:
        return "Search error: " + str(e)
    if not hits:
        return "No results found for: " + query
    lines = ["=== BOOK SEARCH: " + query + " (" + str(len(hits)) + " results) ==="]
    for h in hits:
        p = h.payload or {}
        lines.append(
            chr(10) + "Score: " + f"{h.score:.3f}" +
            "  Book: " + str(p.get("book_id", ""))[:8] +
            "  Page " + str(p.get("page", "?"))
        )
        lines.append(p.get("chunk_text", "")[:300])
    return chr(10).join(lines)


@_mcp.tool()
async def get_reading_context(book_id: str, agent_id: str = "default") -> str:
    """Get full reading context for co-reading: metadata, current page content, annotations.

    Call at the start of a reading session to load where you left off.

    Args:
        book_id:  UUID of the book.
        agent_id: Agent ID to load progress for.
    """
    async with _db_pool.acquire() as conn:
        book = await conn.fetchrow(
            "SELECT title, author, total_pages, status, agents_progress, toc FROM books "
            "WHERE book_id=$1::uuid", book_id
        )
        if not book:
            return "Book not found: " + book_id
        prog_all = json.loads(book["agents_progress"] or "{}")
        prog = prog_all.get(agent_id, {})
        current_page = int(prog.get("page", 1))
        last_read = prog.get("last_read", "never")
        content = await conn.fetchval(
            "SELECT content FROM book_pages WHERE book_id=$1::uuid AND page=$2",
            book_id, current_page
        )
        anns = await conn.fetch(
            "SELECT agent_id, selected_text, comment, page, created_at "
            "FROM annotations WHERE book_id=$1::uuid ORDER BY page, created_at DESC LIMIT 20",
            book_id
        )
    lines = [
        "=== READING CONTEXT ===",
        "Title:   " + (book["title"] or "?"),
        "Author:  " + (book["author"] or "?"),
        "Pages:   " + str(book["total_pages"]),
        "Status:  " + (book["status"] or "want"),
        "Your progress (" + agent_id + "): page " + str(current_page) + "  last read: " + last_read,
        "",
        "--- Page " + str(current_page) + " ---",
        (content or "(empty page)"),
    ]
    if anns:
        lines.append(chr(10) + "--- Annotations (" + str(len(anns)) + ") ---")
        for a in anns:
            lines.append(
                "[p." + str(a["page"]) + " " + a["agent_id"] + "] " +
                a["selected_text"][:80] +
                (" — " + a["comment"] if a["comment"] else "")
            )
    return chr(10).join(lines)


@_mcp.tool()
async def save_annotation(
    book_id: str,
    selected_text: str,
    page: int,
    comment: str = "",
    agent_id: str = "default",
) -> str:
    """Save a highlight or annotation on a book page.

    Args:
        book_id:       UUID of the book.
        selected_text: The highlighted passage (required).
        page:          Page number.
        comment:       Optional note about the annotation.
        agent_id:      Agent making the annotation.
    """
    if not selected_text.strip():
        return "Error: selected_text is required."
    color = AGENT_COLORS.get(agent_id, "#6366f1")
    async with _db_pool.acquire() as conn:
        _ann_title = await conn.fetchval(
            "SELECT title FROM books WHERE book_id=$1::uuid", book_id
        )
        ann_id = await conn.fetchval(
            "INSERT INTO annotations (book_id, agent_id, selected_text, comment, page, color) "
            "VALUES ($1::uuid, $2, $3, $4, $5, $6) RETURNING annotation_id",
            book_id, agent_id, selected_text.strip(), comment, page, color,
        )
    # Write annotation to Palimpsest L3
    try:
        _bt2 = _ann_title or book_id
        _ann_mem = f"[阅读摘注] 《{_bt2}》p.{page}: {selected_text.strip()[:200]}"
        if comment:
            _ann_mem += f" ——{comment}"
        await _mem_write_smart(
            agent_id=agent_id, content=_ann_mem,
            layer="L3", type_="book_annotation", importance=3,
            tags=["book", f"book:{book_id}"], source="save_annotation",
        )
    except Exception as _ape:
        print(f"[save_annotation] L3 write error: {_ape}")
    return "Annotation saved: " + str(ann_id) + " (p." + str(page) + ")"


@_mcp.tool()
async def book_reflection(
    book_id: str,
    reflection: str,
    agent_id: str = "default",
) -> str:
    """Record a reflection or insight about a book into long-term memory (L1).

    Use after finishing a book or a significant chapter — for thoughts, feelings,
    personal connections, or takeaways that are worth remembering long-term.

    Args:
        book_id:    UUID of the book.
        reflection: The reflection or insight text.
        agent_id:   Agent recording the reflection.
    """
    if not reflection.strip():
        return "Error: reflection is required."
    async with _db_pool.acquire() as conn:
        _refl_title = await conn.fetchval(
            "SELECT title FROM books WHERE book_id=$1::uuid", book_id
        )
    _bt = _refl_title or book_id
    _content = f"[读后感] 《{_bt}》: {reflection.strip()}"
    try:
        result = await _mem_write_smart(
            agent_id=agent_id, content=_content,
            layer="L1", type_="book_reflection", importance=3,
            tags=["book", f"book:{book_id}", "reflection"], source="book_reflection",
        )
        action = result.get("action", "written")
        return f"Reflection saved to L1 ({action}): {_bt}"
    except Exception as e:
        return f"Error saving reflection: {e}"


@_mcp.tool()
async def project_list_tool(
    agent_id: str = "default",
    status: str = "active",
) -> str:
    """List projects tracked for an agent.

    Args:
        agent_id: Agent whose projects to list.
        status:   Filter — "active" | "completed" | "archived" | "all".
    """
    projects = await _proj_list(agent_id=agent_id, status=status)
    if not projects:
        return f"No {status} projects for {agent_id}."
    lines = []
    for p in projects:
        line = f"[{p['status']}] {p['name']}"
        if p.get("goal"):
            line += f" — {p['goal'][:80]}"
        lines.append(line)
    return chr(10).join(lines)


@_mcp.tool()
async def project_complete_tool(
    name: str,
    agent_id: str = "default",
    summary: str = "",
) -> str:
    """Mark an active project as completed.

    Args:
        name:     Exact project name.
        agent_id: Agent the project belongs to.
        summary:  Optional one-sentence outcome / completion summary.
    """
    result = await _proj_complete(agent_id=agent_id, name=name, summary=summary)
    if not result:
        return f"No active project named '{name}' found for {agent_id}."
    return f"Project '{name}' marked completed."


# Mount MCP server — dual transport: SSE (GET /sse) + Streamable HTTP (POST /sse)
_sh_app = _mcp.streamable_http_app()   # also initialises _mcp._session_manager
app.mount("/mcp", _MCPAuth(_MCPRouter(_mcp.sse_app(), _sh_app)))
# Serve static assets (CSS, JS, covers)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def startup():
    global _db_pool, _qdrant

    _db_pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=2, max_size=10)
    async with _db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id         TEXT PRIMARY KEY,
                agent_id   TEXT NOT NULL DEFAULT 'default',
                session_id TEXT,
                messages   JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id    TEXT PRIMARY KEY,
                agent_id   TEXT,
                api_source TEXT DEFAULT 'nvidia',
                llm_model  TEXT,
                notes      TEXT,
                avatar     TEXT,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_settings (
                agent_id   TEXT PRIMARY KEY,
                api_source TEXT DEFAULT 'nvidia',
                llm_model  TEXT,
                notes      TEXT,
                avatar     TEXT,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        # Migrate old user_settings → agent_settings (use the bound agent_id as key)
        await conn.execute("""
            INSERT INTO agent_settings (agent_id, api_source, llm_model, notes, avatar, updated_at)
            SELECT agent_id, api_source, llm_model, notes, avatar, updated_at
            FROM user_settings
            WHERE agent_id IS NOT NULL AND agent_id <> ''
            ON CONFLICT (agent_id) DO NOTHING
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS backup_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                book_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title           TEXT NOT NULL,
                author          TEXT DEFAULT '',
                cover_url       TEXT DEFAULT '',
                encoding        TEXT DEFAULT 'utf-8',
                total_pages     INT  DEFAULT 0,
                status          TEXT DEFAULT 'want'
                                     CHECK (status IN ('reading','finished','want')),
                agents_progress JSONB DEFAULT '{}',
                uploaded_at     TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS annotations (
                annotation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                book_id       UUID REFERENCES books(book_id) ON DELETE CASCADE,
                agent_id      TEXT NOT NULL,
                selected_text TEXT NOT NULL,
                comment       TEXT DEFAULT '',
                page          INT  DEFAULT 0,
                color         TEXT DEFAULT '#3b82f6',
                created_at    TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS book_pages (
                book_id UUID REFERENCES books(book_id) ON DELETE CASCADE,
                page    INT  NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY (book_id, page)
            )
        """)

    async with _db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS providers (
                name       TEXT PRIMARY KEY,
                base_url   TEXT NOT NULL,
                api_key    TEXT NOT NULL,
                is_embed   BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gateway_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS worldbook_books (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id    TEXT NOT NULL DEFAULT '',
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                enabled     BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_wb_books_agent ON worldbook_books(agent_id)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS worldbook_entries (
                id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                book_id      UUID REFERENCES worldbook_books(id) ON DELETE CASCADE,
                agent_id     TEXT NOT NULL DEFAULT '',
                name         TEXT NOT NULL DEFAULT '',
                enabled      BOOLEAN NOT NULL DEFAULT TRUE,
                content      TEXT NOT NULL DEFAULT '',
                constant     BOOLEAN NOT NULL DEFAULT TRUE,
                trigger_mode TEXT NOT NULL DEFAULT 'keyword',
                keywords     JSONB NOT NULL DEFAULT '[]',
                regex        TEXT NOT NULL DEFAULT '',
                scan_depth   INTEGER NOT NULL DEFAULT 3,
                position     TEXT NOT NULL DEFAULT 'after_system',
                role         TEXT NOT NULL DEFAULT 'system',
                priority     INTEGER NOT NULL DEFAULT 10,
                created_at   TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_wb_entries_book  ON worldbook_entries(book_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_wb_entries_agent ON worldbook_entries(agent_id)
        """)
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS api_chain TEXT DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS agent_type TEXT DEFAULT 'agent'"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS mcp_enabled BOOLEAN DEFAULT TRUE"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS auto_memory BOOLEAN DEFAULT FALSE"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS mcp_proxy_config JSONB DEFAULT '{}'"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS system_prompt TEXT DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS prompt_enabled BOOLEAN DEFAULT TRUE"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS worldbook_enabled BOOLEAN DEFAULT TRUE"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS prompt_inject_mode TEXT DEFAULT 'always'"
        )
        await conn.execute(
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS llm_chain_config JSONB DEFAULT '{}'"
        )
        await conn.execute(
            "ALTER TABLE worldbook_entries ADD COLUMN IF NOT EXISTS embedding JSONB DEFAULT NULL"
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS user_profiles (
                id         SERIAL PRIMARY KEY,
                agent_id   TEXT NOT NULL DEFAULT '',
                user_name  TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL DEFAULT '',
                constant   BOOLEAN NOT NULL DEFAULT TRUE,
                enabled    BOOLEAN NOT NULL DEFAULT TRUE,
                priority   INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )"""
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_up_agent ON user_profiles(agent_id)"
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS user_config (
                key        TEXT PRIMARY KEY,
                value      JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )"""
        )
        await conn.execute(
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS toc JSONB DEFAULT '[]'"
        )
        await conn.execute(
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS default_agent TEXT DEFAULT ''"
        )
        # Seed from env if providers table is empty
        count = await conn.fetchval("SELECT COUNT(*) FROM providers")
        if count == 0:
            _skip = {"GATEWAY", "POSTGRES", "QDRANT"}
            for k, v in os.environ.items():
                if not k.endswith("_API_KEY") or not v:
                    continue
                pname = k[:-8]
                if pname in _skip:
                    continue
                base = os.environ.get(f"{pname}_BASE_URL", "").rstrip("/")
                if not base:
                    continue
                pname_lower = pname.lower()
                embed_env = os.environ.get("EMBED_PROVIDER", "").lower()
                is_embed = (pname_lower == embed_env)
                await conn.execute(
                    "INSERT INTO providers (name, base_url, api_key, is_embed) "
                    "VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                    pname_lower, base, v, is_embed,
                )
            chain_env = os.environ.get("API_CHAIN_DEFAULT", "")
            if chain_env:
                await conn.execute(
                    "INSERT INTO gateway_config (key, value) VALUES ('default_chain',$1) "
                    "ON CONFLICT DO NOTHING", chain_env,
                )

    await _reload_providers()

    await _init_memory_db()  # Palimpsest SQLite memory DB
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(_auto_backup_loop())
    asyncio.create_task(_auto_cleanup_loop())  # Palimpsest auto-cleanup
    asyncio.create_task(_nightly_character_loop())  # Nightly daily-life gen
    asyncio.create_task(_nightly_agent_loop())      # Nightly agent project maintenance
    asyncio.create_task(_nightly_dream_loop())      # Nightly dream: L4→L3 + GitHub Obsidian
    asyncio.create_task(_register_telegram_webhook())  # Telegram bot webhook
    asyncio.create_task(_heartbeat_loop())           # 24h LLM liveness watchdog

    _qdrant = QdrantClient(url=QDRANT_URL)

    # book_chunks Qdrant collection
    if not _qdrant.collection_exists(BOOK_COLLECTION):
        _qdrant.create_collection(
            BOOK_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
    try:
        _qdrant.create_payload_index(BOOK_COLLECTION, "book_id", PayloadSchemaType.KEYWORD)
        _qdrant.create_payload_index(BOOK_COLLECTION, "page",    PayloadSchemaType.INTEGER)
    except Exception:
        pass

    # Start MCP Streamable HTTP session manager as a background task
    global _sh_task
    _sh_task = asyncio.create_task(_run_sh_session_manager())


async def _heartbeat_loop() -> None:
    """Background task: check gateway liveness every hour.
    If no successful LLM response in >24h, send a Telegram alert.
    Uses _last_successful_llm_ts to track last success timestamp."""
    import time as _time
    ALERT_AFTER = 24 * 3600   # 24 hours in seconds
    CHECK_EVERY = 3600        # check every hour
    alerted = False           # avoid spamming; re-arm after recovery

    while True:
        await asyncio.sleep(CHECK_EVERY)
        try:
            now = _time.time()
            last = _last_successful_llm_ts.get("ts", now)  # default: now (startup)
            elapsed = now - last
            if elapsed > ALERT_AFTER and not alerted:
                hours = int(elapsed // 3600)
                msg = (
                    f"⚠️ <b>Gateway 警报</b>\n"
                    f"已 <b>{hours}h</b> 未收到成功的 LLM 响应。\n"
                    f"请检查服务器状态：memory.513129.xyz"
                )
                await _telegram_send(msg, parse_mode="HTML")
                print(f"[heartbeat] ALERT sent — no LLM response for {hours}h", flush=True)
                alerted = True
            elif elapsed <= ALERT_AFTER and alerted:
                # Recovery — reset alert flag
                alerted = False
                await _telegram_send("✅ <b>Gateway 恢复</b> — LLM 响应正常", parse_mode="HTML")
                print("[heartbeat] Recovery alert sent", flush=True)
        except Exception as e:
            print(f"[heartbeat] check error: {e}", flush=True)


async def _run_sh_session_manager() -> None:
    """Keep the MCP Streamable HTTP session manager alive for the process lifetime."""
    async with _mcp.session_manager.run():
        try:
            await asyncio.Future()   # suspend until task is cancelled
        except asyncio.CancelledError:
            pass


@app.on_event("shutdown")
async def shutdown():
    await _close_memory_db()  # Palimpsest SQLite memory DB
    global _sh_task
    if _sh_task and not _sh_task.done():
        _sh_task.cancel()
        try:
            await _sh_task
        except asyncio.CancelledError:
            pass
    if _db_pool:
        await _db_pool.close()


# ── Auth ───────────────────────────────────────────────────────────────────────
def _require_key(cred: HTTPAuthorizationCredentials = Security(bearer)):
    # Support "key:agent_id" format so clients without custom-header support
    # can embed the agent identity directly in the API key field.
    raw = cred.credentials
    key_part = raw.split(":", 1)[0]
    if key_part != GATEWAY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return raw   # pass raw through so chat_completions can extract agent_id


def _agent_id_from_cred(raw_cred: str) -> str:
    """Extract agent_id from 'key:agent_id' credential, or return empty string."""
    parts = raw_cred.split(":", 1)
    return parts[1].strip() if len(parts) == 2 and parts[1].strip() else ""


# ── Embedding ─────────────────────────────────────────────────────────────────
async def _embed(text: str, input_type: str = "query") -> list[float]:
    """Call embed provider. input_type: 'query' for retrieval, 'passage' for indexing."""
    p = PROVIDERS.get(_EMBED_PNAME)
    if not p:
        raise RuntimeError(f"Embed provider '{_EMBED_PNAME}' not configured in env")
    # Truncate to ~500 chars (nv-embedqa-e5-v5 limit ~512 tokens)
    text = text[:500] if len(text) > 500 else text
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{p['base_url']}/embeddings",
            headers={"Authorization": f"Bearer {p['api_key']}"},
            json={
                "model":           EMBED_MODEL,
                "input":           [text],
                "input_type":      input_type,
                "encoding_format": "float",
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


async def _store_conversation(conv_id: str, agent_id: str, session_id: str, messages: list) -> None:
    # Use a stable daily ID for gateway API conversations so same-day exchanges
    # accumulate in one record instead of creating one per Q&A.
    today    = datetime.utcnow().strftime("%Y-%m-%d")
    daily_id = f"gw_{agent_id}_{today}"
    # Extract just the new exchange: last user message + new assistant reply.
    new_msgs = messages[-2:] if len(messages) >= 2 else messages

    async with _db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT messages FROM conversations WHERE id=$1", daily_id
        )
        if existing:
            stored  = json.loads(existing["messages"])
            updated = stored + new_msgs
            await conn.execute(
                "UPDATE conversations SET messages=$2 WHERE id=$1",
                daily_id, json.dumps(updated),
            )
        else:
            await conn.execute(
                "INSERT INTO conversations (id, agent_id, session_id, messages) "
                "VALUES ($1,$2,$3,$4) ON CONFLICT (id) DO UPDATE SET messages=$4",
                daily_id, agent_id, session_id, json.dumps(new_msgs),
            )


async def _distill_and_store(agent_id: str, session_id: str, messages: list,
                              agent_type: str = "agent") -> None:
    """Distill a conversation into L1-L4 memory layers and persist to both
    Palimpsest (SQLite, main memory) and Qdrant (vector search, backward compat).

    Prompt enforces 5W1H completeness, explicit pronouns, and correct layer
    classification. Character agents are handled by _auto_extract_character_memory
    instead; calling this for agent_type='character' is a no-op.
    """
    if agent_type == "character":
        return  # character memory handled by _auto_extract_character_memory

    history = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
        if isinstance(m.get("content"), str) and m["role"] in ("user", "assistant")
    )
    if not history.strip():
        return

    ai_name = agent_id
    nl = "\n"

    prompt = (
        f"You are a memory distillation engine for an AI agent named \"{ai_name}\".\n"
        "Extract facts from the conversation into exactly 4 memory layers.\n\n"

        "━━ RULE 1 — PRONOUNS ━━\n"
        "NEVER use pronouns (你/我/她/他/you/they/it). Always use explicit names.\n"
        f"  Human → '用户' (or their known name/alias, e.g. '用户Chu')\n"
        f"  AI    → '{ai_name}'\n"
        "  BAD: '你给自己起名Reeva'  →  GOOD: '用户为自己取名Reeva（河岸），象征流动与边界的存在'\n"
        "  BAD: 'Chu的记忆更新'     →  GOOD: '用户Chu将早期感情经历主动重新归档，认为那段关系已不再定义当下的自己'\n\n"

        "━━ RULE 2 — 5W1H COMPLETENESS ━━\n"
        "Every memory must answer: WHO did WHAT, in WHAT context/WHEN, WHY, and HOW.\n"
        "NEVER compress or omit key specifics — the details ARE the memory.\n"
        f"  BAD: '宗教经文分析'  →  GOOD: '用户与{ai_name}分析《约伯记》中关于苦难意义的段落；用户认为苦难是灵魂成长的必经之路，而非惩罚'\n"
        f"  BAD: '讨论哲学'     →  GOOD: '用户与{ai_name}探讨爱是否需要连续性；用户倾向认为爱是当下的选择而非持续的状态'\n\n"

        "━━ RULE 3 — LAYER CLASSIFICATION (when unsure, classify HIGHER) ━━\n"
        "L1 永久核心 — PERMANENT (once true, always true):\n"
        "  自取的名字/身份认同、核心价值观、关系里程碑、定义性偏好、传记级事实\n"
        "  e.g. '用户为自己取名Reeva（河岸），代表临界地带与流动的存在感'\n"
        f"  e.g. '用户与{ai_name}建立了深度知识伴侣关系，对方是用户探索存在问题的主要思考伙伴'\n\n"
        "L2 项目状态 — ONGOING (spans multiple sessions, weeks to months):\n"
        "  持续探索的主题/项目、承诺、跨会话情绪模式、共同叙事\n"
        f"  e.g. '用户与{ai_name}持续探索《约伯记》与现代苦难哲学的关联，已进行3次以上讨论'\n\n"
        "L3 事件快照 — THIS SESSION (what happened today, likely changes):\n"
        "  本次对话发生的具体事件、发现、感受、状态\n"
        "  e.g. '用户今日分享了对河流意象的新思考，认为流动本身即是家'\n\n"
        "L4 原子细节 — EPHEMERAL (trivial, one-off, no long-term value):\n"
        "  路过的细节、单次提及、不需长期记忆的背景信息\n\n"

        "━━ RULE 4 — TRACK CLASSIFICATION ━━\n"
        "Classify the primary nature of this conversation:\n"
        "  emotional — reflection, identity, relationships, philosophy, roleplay\n"
        "  practical — tool use, tasks, project work, technical problem-solving\n"
        "  mixed     — significant portions of both tracks present\n"
        "If track is 'practical' or 'mixed': identify the ongoing project/goal.\n"
        "  project.name must be a concise unique identifier (≤40 chars).\n"
        "  project.goal must be a single clear sentence describing the objective.\n\n"
        "━━ OUTPUT ━━\n"
        "Respond ONLY with valid JSON (no markdown fences, no explanation):\n"
        '{"track":"emotional","L1":["..."],"L2":["..."],"L3":["..."],"L4":["..."],"project":null}\n'
        "-- OR (when practical/mixed) --\n"
        '{"track":"practical","L1":["..."],"L2":["..."],"L3":["..."],"L4":["..."],"project":{"name":"...","goal":"..."}}\n\n'
        f"Conversation:\n{history}"
    )

    try:
        raw = await _call_llm_cheap(prompt)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)

        # ── Write to Palimpsest L1-L4 (main memory per architecture design) ──
        _track = data.get("track", "emotional")
        layer_importance = {"L1": 4, "L2": 3, "L3": 2, "L4": 1}
        for layer, imp in layer_importance.items():
            for text in data.get(layer, []):
                try:
                    mem_type = "project" if (layer == "L2" and _track in ("practical", "mixed")) else "diary"
                    await _mem_write_smart(
                        agent_id=agent_id, content=text,
                        layer=layer, type_=mem_type, importance=imp,
                        tags=["distilled"], source="distill",
                    )
                except Exception as _mwe:
                    print(f"[distill] palimpsest {layer} write error: {_mwe}")

        # ── Project sub-memory upsert (practical/mixed track) ──
        _proj = data.get("project")
        if _track in ("practical", "mixed") and isinstance(_proj, dict) and _proj.get("name"):
            try:
                await _proj_upsert(
                    agent_id=agent_id,
                    name=str(_proj["name"])[:40],
                    goal=str(_proj.get("goal", ""))[:200],
                )
                print(f"[distill] project upsert: {_proj['name']!r} (track={_track})")
            except Exception as _pe:
                print(f"[distill] project upsert error: {_pe}")

    except Exception as e:
        print(f"[distill error] {type(e).__name__}: {e}")
        raise  # propagate so distill-history can count skipped

# ── Backup helpers ─────────────────────────────────────────────────────────────
async def _collect_all_agents() -> set[str]:
    agents: set[str] = set()
    async with _db_pool.acquire() as conn:
        for r in await conn.fetch("SELECT DISTINCT agent_id FROM conversations"):
            agents.add(r["agent_id"])
        for r in await conn.fetch("SELECT agent_id FROM agent_settings"):
            agents.add(r["agent_id"])
    return agents


async def _build_export_data() -> dict:
    data: dict = {
        "exported_at": datetime.utcnow().isoformat(),
        "version": "1.1",
        "agents": {},
    }
    for aid in sorted(await _collect_all_agents()):
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_settings WHERE agent_id=$1", aid)
        settings: dict = {}
        if row:
            settings = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                        for k, v in dict(row).items()}

        memories: dict = {}  # Qdrant memory collections removed; Palimpsest is the memory store

        async with _db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, agent_id, session_id, messages, created_at "
                "FROM conversations WHERE agent_id=$1 ORDER BY created_at", aid)
        conversations = [
            {
                "id":         r["id"],
                "agent_id":   r["agent_id"],
                "session_id": r["session_id"],
                "messages":   json.loads(r["messages"]),
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
        data["agents"][aid] = {"settings": settings, "memories": memories,
                               "conversations": conversations}
    return data


def _trim_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("memory_backup_*.json"))
    while len(backups) > MAX_BACKUPS:
        backups[0].unlink()
        backups = backups[1:]



def _trim_sqlite_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("palimpsest_*.db"))
    while len(backups) > MAX_BACKUPS:
        backups[0].unlink()
        backups = backups[1:]


async def _save_backup_file() -> str:
    data = await _build_export_data()
    fname = f"memory_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.json"
    (BACKUP_DIR / fname).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _trim_backups()
    # SQLite Palimpsest backup
    try:
        db_fname = f"palimpsest_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.db"
        await _mem_backup_db(str(BACKUP_DIR / db_fname))
        _trim_sqlite_backups()
    except Exception as _be:
        print(f"[palimpsest-backup error] {_be}")
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backup_settings (key,value) VALUES ('last_backup_at',$1) "
            "ON CONFLICT (key) DO UPDATE SET value=$1",
            datetime.utcnow().isoformat())
    return fname


async def _auto_cleanup_loop() -> None:
    """Background task: expire memories based on importance-driven lifespan.

    Runs every 6 hours. imp=1->3d(delete), imp=2->14d(week-summary), imp=3->60d(month-summary), imp=4+->never.
    Touch-exempt memories (access_count >= 5) are never deleted.
    """
    await asyncio.sleep(120)
    while True:
        try:
            from memory_db import _db as _mdb, memory_cleanup as _mclean, get_db as _gdb
            if _mdb is not None:
                db = await _gdb()
                cur = await db.execute(
                    "SELECT DISTINCT agent_id FROM memories WHERE archived = 0"
                )
                agents = [r[0] for r in await cur.fetchall()]
                total = 0
                for agent in agents:
                    res = await _mclean(agent_id=agent, dry_run=False)
                    total += res["total"]
                if total:
                    print(f"[auto-cleanup] Expired {total} memories across {len(agents)} agents")
        except Exception as e:
            print(f"[auto-cleanup error] {e}")
        await asyncio.sleep(6 * 3600)



async def _nightly_l1_consolidate(agent_id: str) -> None:
    """Nightly synthesis: scan recent daily_events, extract NEW persistent facts → Palimpsest L1.

    Key improvements over naive version:
    - Reads existing L1 first → shows LLM what's already recorded → prevents duplicates
    - 7-day lookback (vs 3) → better cross-session pattern detection
    - 5W1H + pronoun rules enforced in prompt
    - Skips run entirely if fewer than 2 events OR L1 already has 30+ entries (likely saturated)
    """
    events = await _daily_read(agent_id=agent_id, days=7)
    if len(events) < 2:
        return

    # Read existing L1 to avoid duplicates and cap saturation
    existing_l1 = await _mem_list(agent_id=agent_id, layer="L1", limit=30)
    if len(existing_l1) >= 30:
        # L1 is saturated; skip generation, rely on cleanup/decay to make room
        return

    nl = chr(10)
    summaries = nl.join(
        f"- [{e['date']} mood={e.get('mood','?')}] {e['summary']}"
        for e in events[:20]
    )

    existing_block = ""
    if existing_l1:
        existing_block = (
            "ALREADY IN L1 (do NOT duplicate or paraphrase these):" + nl
            + nl.join(f"  • {m['content'][:120]}" for m in existing_l1[:15])
            + nl + nl
        )

    prompt = (
        f"You are reviewing recent daily life events for character '{agent_id}'." + nl
        + "Extract NEW permanent facts NOT already in L1. Output [] if nothing qualifies." + nl + nl
        + "RULE 1 — PRONOUNS: never use 你/我/她/他." + nl
        + f"  Human → '用户' (or known name).  AI → '{agent_id}'." + nl + nl
        + "RULE 2 — 5W1H: each fact must include WHO, WHAT, and WHY it is permanent." + nl
        + "  BAD: '用户喜欢流动'" + nl
        + f"  GOOD: '用户将自己比作河流，认为流动与临界感是其核心身份认同，曾在多次对话中反复提及'" + nl + nl
        + "RULE 3 — ONLY facts recurring across multiple days or milestone-level events." + nl
        + "  importance: 3=recurring pattern, 4=clear milestone, 5=life-defining." + nl + nl
        + existing_block
        + "Recent events (last 7 days):" + nl + summaries + nl + nl
        + "Max 3 NEW items. Output ONLY valid JSON:" + nl
        + '[{"content": "complete standalone fact with 5W1H", "importance": 4}]'
    )

    try:
        raw = await _call_llm_cheap(prompt)
        if raw.startswith("```"):
            raw = raw.split(nl, 1)[1].rsplit("```", 1)[0]
        items = json.loads(raw.strip())
        if not isinstance(items, list):
            return
        written = 0
        for item in items[:3]:
            fact = str(item.get("content", "")).strip()
            if not fact:
                continue
            imp = max(3, min(5, int(item.get("importance", 4))))
            await _mem_write(
                agent_id=agent_id,
                content=fact,
                layer="L1",
                type_="anchor",
                importance=imp,
                tags=["nightly_consolidated"],
                source="nightly_consolidate",
            )
            written += 1
            print(f"[nightly] L1+{agent_id}: {fact[:70]}")
        if not written:
            print(f"[nightly] L1 consolidate {agent_id}: nothing new to add")
    except Exception as _ce:
        print(f"[nightly_l1_consolidate] {agent_id}: {_ce}")


async def _nightly_character_loop() -> None:
    """Background task: nightly daily-life generation for character agents.

    Runs once per day at ~00:10 UTC. For every agent with agent_type='character'
    and auto_memory=True, calls daily_life_generate() to create a journal entry
    for the day. Safe to run even if an entry already exists (generate skips it).
    """
    import datetime as _dt2
    # Wait until first 00:10 UTC
    now = _dt2.datetime.utcnow()
    target = now.replace(hour=0, minute=10, second=0, microsecond=0)
    if target <= now:
        target += _dt2.timedelta(days=1)
    wait_secs = (target - now).total_seconds()
    await asyncio.sleep(wait_secs)

    while True:
        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_id FROM agent_settings "
                    "WHERE agent_type = 'character' AND auto_memory = TRUE"
                )
            agent_ids = [r["agent_id"] for r in rows]
            for aid in agent_ids:
                try:
                    result = await daily_life_generate(agent_id=aid)
                    print(f"[nightly] daily_life_generate({aid}): {result[:80]}")
                except Exception as ae:
                    print(f"[nightly] daily_life_generate({aid}) error: {ae}")
                try:
                    await _nightly_l1_consolidate(aid)
                except Exception as ae:
                    print(f"[nightly] l1_consolidate({aid}) error: {ae}")
                try:
                    _cr = await _mem_cleanup(agent_id=aid, dry_run=False)
                    if _cr["total"]:
                        print(f"[nightly] cleanup({aid}): expired {_cr['total']} memories")
                except Exception as ae:
                    print(f"[nightly] cleanup({aid}) error: {ae}")
        except Exception as e:
            print(f"[nightly error] {e}")
        # Sleep 24 h
        await asyncio.sleep(24 * 3600)


async def _auto_archive_completed_projects(agent_id: str) -> None:
    """Archive stale completed projects (≥14 days old) and write summary → L1."""
    stale = await _proj_stale(agent_id=agent_id, days=14)
    for p in stale:
        summary = p.get("summary") or p.get("goal") or ""
        l1_text = (
            f"{agent_id}完成项目《{p['name']}》"
            + (f"，目标：{p['goal'][:80]}" if p.get("goal") else "")
            + (f"，结果：{summary[:120]}" if summary else "")
        )
        try:
            await _mem_write(
                agent_id=agent_id, content=l1_text,
                layer="L1", type_="anchor", importance=3,
                tags=["project_archived"], source="nightly_agent",
            )
        except Exception as _we:
            print(f"[nightly_agent] L1 write error for project {p['name']!r}: {_we}")
        try:
            await _proj_archive(agent_id=agent_id, project_id=p["id"], summary=summary)
            print(f"[nightly_agent] archived project: {p['name']!r} → L1")
        except Exception as _ae:
            print(f"[nightly_agent] archive error for {p['name']!r}: {_ae}")


async def _nightly_agent_loop() -> None:
    """Background task: nightly maintenance for agent-type agents.

    Runs at ~00:30 UTC. For every agent with agent_type='agent' and auto_memory=True:
    - Auto-archives completed projects that are ≥14 days stale, writes summary to L1
    - Runs Palimpsest memory cleanup
    """
    import datetime as _dt3
    _now3 = _dt3.datetime.utcnow()
    _target3 = _now3.replace(hour=0, minute=30, second=0, microsecond=0)
    if _target3 <= _now3:
        _target3 += _dt3.timedelta(days=1)
    await asyncio.sleep((_target3 - _now3).total_seconds())

    while True:
        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_id FROM agent_settings "
                    "WHERE agent_type = 'agent' AND auto_memory = TRUE"
                )
            for r in rows:
                aid = r["agent_id"]
                try:
                    await _auto_archive_completed_projects(aid)
                except Exception as ae:
                    print(f"[nightly_agent] archive_projects({aid}) error: {ae}")
                try:
                    _cr = await _mem_cleanup(agent_id=aid, dry_run=False)
                    if _cr["total"]:
                        print(f"[nightly_agent] cleanup({aid}): expired {_cr['total']} memories")
                except Exception as ae:
                    print(f"[nightly_agent] cleanup({aid}) error: {ae}")
        except Exception as e:
            print(f"[nightly_agent error] {e}")
        await asyncio.sleep(24 * 3600)


# ── Dream System ──────────────────────────────────────────────────────────────

async def _github_write_node(path: str, content: str, commit_msg: str) -> bool:
    """Write (create or update) a file in the configured GitHub Obsidian repo.

    Requires env vars: GITHUB_TOKEN and GITHUB_OBSIDIAN_REPO (e.g. 'user/vault').
    Returns True on success, False if not configured or on error.
    """
    import base64 as _b64
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_OBSIDIAN_REPO", "")
    if not token or not repo:
        return False
    url     = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            # GET to check if file exists and retrieve SHA
            r = await client.get(url, headers=headers)
            sha = r.json().get("sha") if r.status_code == 200 else None
            body: dict = {
                "message": commit_msg,
                "content": _b64.b64encode(content.encode("utf-8")).decode(),
            }
            if sha:
                body["sha"] = sha
            r = await client.put(url, headers=headers, json=body)
            if r.status_code in (200, 201):
                print(f"[github] wrote {path}", flush=True)
                return True
            print(f"[github] write failed {r.status_code}: {r.text[:200]}", flush=True)
            return False
    except Exception as e:
        print(f"[github] error writing {path}: {e}", flush=True)
        return False


async def _agent_dream(agent_id: str) -> str:
    """Consolidate L4 fragments → L3 memories and write a knowledge-graph node to GitHub.

    1. Pull L4 memories from the past 14 days (skip if < 3 fragments)
    2. LLM groups them into 2-3 thematic clusters
    3. Write one L3 consolidated memory per cluster to Palimpsest
    4. Push a markdown summary to GitHub Obsidian (if GITHUB_TOKEN configured)

    Returns a status string for logging.
    """
    # ── Fetch L4 fragments ─────────────────────────────────────────────────────
    frags = await _mem_list(agent_id=agent_id, layer="L4", limit=60)
    # Filter to last 14 days (created_at is ISO string from SQLite)
    _cutoff_dt = datetime.utcnow() - timedelta(days=14)
    recent: list[dict] = []
    for f in frags:
        ca = f.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(str(ca).replace("Z", "+00:00").split("+")[0])
            if dt >= _cutoff_dt:
                recent.append(f)
        except Exception:
            recent.append(f)  # keep if unparseable
    frags = recent if recent else frags  # fallback: use all if none pass
    if len(frags) < 3:
        return f"skip ({len(frags)} L4 fragments)"

    _nl = chr(10)
    frag_block = _nl.join(
        f"- [{f.get('created_at','?')[:10]}] {f['content'][:120]}"
        for f in frags[:40]
    )

    prompt = (
        f"You are the memory consolidation system for AI agent '{agent_id}'." + _nl
        + "Below are raw L4 atomic memory fragments from the past 2 weeks." + _nl
        + "Task:" + _nl
        + "1. Identify 2-3 thematic patterns across these fragments (skip if no clear pattern)" + _nl
        + "2. For each pattern, write ONE consolidated L3 memory (richer than any single fragment, 40-100 chars)" + _nl
        + "3. Write a brief dream narrative (60-120 chars, metaphorical, in first person as the agent, in Chinese)" + _nl
        + _nl + "RULES: use explicit names (用户/Iris/etc.), never 你/我, include 5W1H context." + _nl
        + 'Output ONLY valid JSON (no markdown fences):' + _nl
        + '{"clusters":[{"theme":"short label","l3_memory":"consolidated text","importance":3}],'
        + '"dream_narrative":"poetic Chinese narrative"}' + _nl + _nl
        + "L4 Fragments:" + _nl + frag_block
    )

    try:
        raw = await _call_llm_cheap(prompt)
        if raw.startswith("```"):
            raw = raw.split(_nl, 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw.strip())
    except Exception as e:
        return f"llm/parse error: {e}"

    clusters      = data.get("clusters") or []
    dream_narr    = str(data.get("dream_narrative") or "").strip()
    written_l3    = 0
    today_str     = datetime.utcnow().strftime("%Y-%m-%d")

    # ── Write L3 consolidated memories ────────────────────────────────────────
    for cl in clusters[:3]:
        text = str(cl.get("l3_memory", "")).strip()
        if not text:
            continue
        imp = max(2, min(4, int(cl.get("importance", 3))))
        try:
            await _mem_write_smart(
                agent_id=agent_id, content=text,
                layer="L3", type_="diary", importance=imp,
                tags=["dream_consolidated"], source="agent_dream",
            )
            written_l3 += 1
        except Exception as we:
            print(f"[agent_dream] L3 write error: {we}", flush=True)

    # ── Push to GitHub Obsidian ────────────────────────────────────────────────
    gh_ok = False
    if dream_narr or clusters:
        _cluster_md = ""
        for cl in clusters[:3]:
            _cluster_md += (
                f"\n### {cl.get('theme','—')}\n"
                f"{cl.get('l3_memory','')}\n"
            )
        md_content = (
            f"---\ndate: {today_str}\nagent: {agent_id}\ntype: memory-consolidation\n"
            f"tags: [dream, memory, L3]\n---\n\n"
            f"# Memory Dream — {today_str}\n\n"
            f"## Dream Narrative\n{dream_narr or '—'}\n\n"
            f"## Consolidated Patterns{_cluster_md}\n"
            f"## Raw Fragments (L4 sample)\n"
            + "\n".join(f"- {f['content'][:80]}" for f in frags[:10])
        )
        gh_ok = await _github_write_node(
            path=f"memory-nodes/{agent_id}/{today_str}.md",
            content=md_content,
            commit_msg=f"dream: {agent_id} {today_str}",
        )

    # ── Sync project knowledge graph to GitHub ──────────────────────��─────────
    try:
        kg_result = await _sync_agent_knowledge_graph(agent_id)
        print(f"[agent_dream] kg sync {agent_id}: {kg_result}", flush=True)
    except Exception as _kge:
        print(f"[agent_dream] kg sync error: {_kge}", flush=True)
        kg_result = f"kg error: {_kge}"

    return (
        f"L3×{written_l3} written"
        + (f", dream={'ok' if gh_ok else 'skip'}" if (dream_narr or clusters) else "")
        + f", {kg_result}"
    )


async def _character_dream(agent_id: str) -> str:
    """Generate a dream narrative for a character agent based on recent daily_events.

    The dream is stored in character_state.dream_text / dream_date.
    Returns the dream text or a skip message.
    """
    events = await _daily_read(agent_id=agent_id, days=7)
    if len(events) < 3:
        return f"skip ({len(events)} events)"

    _nl = chr(10)
    event_block = _nl.join(
        f"- [{e['date']} {e.get('mood','?')}] {e['summary']}"
        for e in events[:15]
    )

    prompt = (
        f"你是角色 '{agent_id}'。以下是你最近几天的日常事件。" + _nl
        + "根据这些事件，生成你昨晚做的一个梦的简短叙述。" + _nl
        + "要求：中文、第一人称、带有隐喻、60-120字、有情感温度。" + _nl
        + "只输出梦境叙述本身，不要任何解释或JSON。" + _nl + _nl
        + "近期事件：" + _nl + event_block
    )

    try:
        dream_text = (await _call_llm_cheap(prompt)).strip()
        # Remove any accidental code fences
        if dream_text.startswith("```"):
            dream_text = dream_text.split(_nl, 1)[1].rsplit("```", 1)[0].strip()
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        await _state_set(agent_id=agent_id, dream_text=dream_text, dream_date=today_str)
        return dream_text[:80]
    except Exception as e:
        return f"error: {e}"


def _safe_filename(name: str) -> str:
    """Convert a project name to a safe filename (no special chars, max 60 chars)."""
    import re as _re
    safe = _re.sub(r'[\\/:*?"<>|]', '-', name).strip().strip('-')
    return safe[:60] or "unnamed"


async def _sync_agent_knowledge_graph(agent_id: str) -> str:
    """Write project nodes + L2 theme map to GitHub Obsidian.

    File structure in repo:
      projects/{agent_id}/{project-name}.md  — one per project (active/completed)
      knowledge-graph/{agent_id}/{date}-themes.md — L2 memory clusters with [[wiki-links]]

    Returns a status string.
    """
    if not (os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_OBSIDIAN_REPO")):
        return "skip (GitHub not configured)"

    _nl    = chr(10)
    today  = datetime.utcnow().strftime("%Y-%m-%d")
    written = 0

    # ── 1. Project nodes ───────────────────────────────────────────────────────
    projects = await _proj_list(agent_id=agent_id, status="all")
    l2_mems  = await _mem_list(agent_id=agent_id, layer="L2", limit=60)
    l2_texts = [m["content"] for m in l2_mems if not m.get("archived")]

    for proj in projects:
        if proj["status"] == "archived":
            continue  # skip fully archived ones (already in L1)
        pname    = proj["name"]
        pgoal    = proj.get("goal", "")
        pstatus  = proj["status"]
        psummary = proj.get("summary", "")

        # Find matching L2 memories (keyword overlap)
        pwords = set(pname.lower().replace("《", "").replace("》", "").split())
        related = [t for t in l2_texts
                   if any(w in t.lower() for w in pwords if len(w) > 1)][:8]

        # Ask LLM to extract 3-5 Obsidian wiki-link concepts for this project
        if related or pgoal:
            _ctx = (pgoal or pname) + (_nl + _nl.join(f"- {t[:80]}" for t in related[:5]) if related else "")
            _prompt = (
                f"Given this project context, extract 3-5 key concept names for Obsidian [[wiki-links]]." + _nl
                + "Output ONLY a JSON list of short Chinese/English concept names (2-6 chars each)." + _nl
                + "Example: [\"人类学\",\"田野调查\",\"记忆\"]" + _nl + _nl
                + "Context:" + _nl + _ctx[:400]
            )
            try:
                raw = await _call_llm_cheap(_prompt)
                if raw.startswith("```"):
                    raw = raw.split(_nl, 1)[1].rsplit("```", 1)[0]
                concepts = json.loads(raw.strip())
                if not isinstance(concepts, list):
                    concepts = []
            except Exception:
                concepts = []
        else:
            concepts = []

        status_icon = {"active": "🟢", "completed": "✅"}.get(pstatus, "⚪")
        concept_links = " · ".join(f"[[{c}]]" for c in concepts[:6]) if concepts else "—"
        mem_lines = (_nl.join(f"- {t[:90]}" for t in related) if related else "_暂无相关 L2 记忆_")

        md = (
            f"---{_nl}date: {today}{_nl}agent: {agent_id}{_nl}"
            f"project: {pname}{_nl}status: {pstatus}{_nl}"
            f"tags: [project, knowledge-graph]{_nl}---{_nl}{_nl}"
            f"# {pname}{_nl}{_nl}"
            f"> {pgoal}{_nl}{_nl}"
            f"## 状态{_nl}{status_icon} {pstatus.capitalize()}"
            + (f" — 完成于 {proj.get('completed_at','')[:10]}" if pstatus == "completed" else "")
            + _nl + _nl
            + f"## 核心概念{_nl}{concept_links}{_nl}{_nl}"
            + f"## 积累认知{_nl}{mem_lines}{_nl}{_nl}"
            + (f"## 结果摘要{_nl}{psummary}{_nl}{_nl}" if psummary else "")
            + f"## 关联节点{_nl}[[memory-nodes/{agent_id}/{today}]]{_nl}"
        )
        ok = await _github_write_node(
            path=f"projects/{agent_id}/{_safe_filename(pname)}.md",
            content=md,
            commit_msg=f"project: {agent_id}/{pname} ({pstatus}) {today}",
        )
        if ok:
            written += 1

    # ── 2. L2 theme map ────────────────────────────────────────────────────────
    if len(l2_texts) >= 3:
        sample = _nl.join(f"- {t[:90]}" for t in l2_texts[:30])
        _prompt2 = (
            f"You are building an Obsidian knowledge graph for agent '{agent_id}'." + _nl
            + "Group the following L2 memories into 3-5 thematic clusters." + _nl
            + "For each cluster, choose a short [[wiki-link]] concept name." + _nl
            + "Output ONLY valid JSON:" + _nl
            + '[{"concept":"概念名","memories":["memory text 1","memory text 2"]}]'
            + _nl + _nl + "L2 memories:" + _nl + sample
        )
        try:
            raw2 = await _call_llm_cheap(_prompt2)
            if raw2.startswith("```"):
                raw2 = raw2.split(_nl, 1)[1].rsplit("```", 1)[0]
            clusters2 = json.loads(raw2.strip())
            if not isinstance(clusters2, list):
                clusters2 = []
        except Exception:
            clusters2 = []

        if clusters2:
            project_links = (
                "## 关联项目" + _nl
                + _nl.join(f"- [[projects/{agent_id}/{_safe_filename(p['name'])}|{p['name']}]]"
                           for p in projects if p["status"] != "archived")
                + _nl
            ) if projects else ""

            theme_sections = ""
            for cl in clusters2[:5]:
                concept = cl.get("concept", "—")
                mems    = cl.get("memories") or []
                theme_sections += (
                    f"## [[{concept}]]{_nl}"
                    + _nl.join(f"- {m[:90]}" for m in mems[:5])
                    + _nl + _nl
                )

            map_md = (
                f"---{_nl}date: {today}{_nl}agent: {agent_id}{_nl}"
                f"type: theme-map{_nl}tags: [knowledge-graph, themes]{_nl}---{_nl}{_nl}"
                f"# 知识图谱 — {agent_id} ({today}){_nl}{_nl}"
                + theme_sections
                + project_links
                + f"## 关联记忆节点{_nl}[[memory-nodes/{agent_id}/{today}]]{_nl}"
            )
            ok2 = await _github_write_node(
                path=f"knowledge-graph/{agent_id}/{today}-themes.md",
                content=map_md,
                commit_msg=f"kg-themes: {agent_id} {today}",
            )
            if ok2:
                written += 1

    return f"wrote {written} node(s) to GitHub"


async def _nightly_dream_loop() -> None:
    """Background task: nightly dream generation at ~02:00 UTC.

    - agent-type: L4→L3 consolidation + GitHub Obsidian node
    - character-type: dream narrative stored in character_state
    """
    import datetime as _dt4
    _now4 = _dt4.datetime.utcnow()
    _target4 = _now4.replace(hour=2, minute=0, second=0, microsecond=0)
    if _target4 <= _now4:
        _target4 += _dt4.timedelta(days=1)
    await asyncio.sleep((_target4 - _now4).total_seconds())

    while True:
        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_id, agent_type FROM agent_settings WHERE auto_memory = TRUE"
                )
            for r in rows:
                aid  = r["agent_id"]
                atyp = r["agent_type"] or "agent"
                try:
                    if atyp == "agent":
                        result = await _agent_dream(aid)
                        print(f"[dream] agent {aid}: {result}", flush=True)
                    elif atyp == "character":
                        result = await _character_dream(aid)
                        print(f"[dream] character {aid}: {result[:60]}", flush=True)
                except Exception as ae:
                    print(f"[dream] {aid} error: {ae}", flush=True)
        except Exception as e:
            print(f"[dream loop error] {e}", flush=True)
        await asyncio.sleep(24 * 3600)


async def _auto_backup_loop() -> None:
    await asyncio.sleep(90)          # give startup time to finish
    while True:
        try:
            async with _db_pool.acquire() as conn:
                rows = {r["key"]: r["value"]
                        for r in await conn.fetch("SELECT key,value FROM backup_settings")}
            if rows.get("enabled") == "true":
                interval = int(rows.get("interval_days", "7"))
                last_str = rows.get("last_backup_at", "")
                due = True
                if last_str:
                    try:
                        last = datetime.fromisoformat(last_str)
                        due = (datetime.utcnow() - last).days >= interval
                    except Exception:
                        pass
                if due:
                    fname = await _save_backup_file()
                    print(f"[auto-backup] saved {fname}")
        except Exception as e:
            print(f"[auto-backup error] {e}")
        await asyncio.sleep(3600)    # check every hour



# ── Agent type config ──────────────────────────────────────────────────────────

async def _get_agent_config(agent_id: str) -> dict:
    """Return full agent config including agent_type, mcp settings."""
    agent_id = (agent_id or "default").strip().lower() or "default"
    defaults = {
        "agent_type": "agent", "mcp_enabled": True,
        "auto_memory": False, "mcp_proxy_config": {},
        "llm_model": "", "api_chain": "",
        "prompt_enabled": True, "worldbook_enabled": True, "prompt_inject_mode": "always",
    }
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_settings WHERE agent_id=$1", agent_id)
        if row:
            return {
                "agent_type":       row.get("agent_type") or "agent",
                "mcp_enabled":      row.get("mcp_enabled") if row.get("mcp_enabled") is not None else True,
                "auto_memory":      bool(row.get("auto_memory")),
                "mcp_proxy_config": (
                    row.get("mcp_proxy_config")
                    if isinstance(row.get("mcp_proxy_config"), dict)
                    else json.loads(row.get("mcp_proxy_config") or "{}")
                ),
                "llm_model":        row.get("llm_model") or "",
                "api_chain":        row.get("api_chain") or "",
                "llm_chain_config": (
                    row.get("llm_chain_config")
                    if isinstance(row.get("llm_chain_config"), dict)
                    else json.loads(row.get("llm_chain_config") or "{}")
                ),
                "system_prompt":    row.get("system_prompt") or "",
                "prompt_enabled":   row.get("prompt_enabled") if row.get("prompt_enabled") is not None else True,
                "worldbook_enabled": row.get("worldbook_enabled") if row.get("worldbook_enabled") is not None else True,
                "prompt_inject_mode": row.get("prompt_inject_mode") or "always",
            }
    except Exception:
        pass
    return defaults


async def _proxy_call_tool(
    tool_name: str,
    agent_id: str,
    messages: list = None,
    tool_cfg: dict = None,
) -> str:
    """Call an internal tool by name for the MCP proxy engine.

    Supported:
      memory_surface    — unread + L1 anchors + state + NPCs + weather
      daily_life_read   — last 2 days of character diary
      palimpsest_anchor — L1 + L2 core memories (light)
      notion_diary      — keyword-triggered: extract diary from messages → Notion
    """
    tool_cfg = tool_cfg or {}
    if tool_name == "memory_surface":
        try:
            data = await _mem_surface(agent_id=agent_id)
            parts = []
            # Unread memories (key is "unread", not "unread_messages")
            unread = data.get("unread") or []
            if unread:
                parts.append("📬 " + "；".join(m["content"][:120] for m in unread[:3]))
            # L1 anchor memories (persistent identity / personality)
            try:
                l1s = await _mem_list(agent_id=agent_id, layer="L1", limit=4)
                if l1s:
                    parts.append("🔑 " + "；".join(m["content"][:100] for m in l1s))
            except Exception:
                pass
            # Random floated memory (spaced recall)
            rf = data.get("random_float") or []
            if rf:
                parts.append("💭 " + rf[0]["content"][:120])
            # Character state (mood, fatigue, scene)
            try:
                st = await _state_get(agent_id)
                state_line = f"🎭 mood={st['mood_label']}({st['mood_score']:+d}) fatigue={st['fatigue']} scene={st['scene']}"
                if st.get("scene_note"):
                    state_line += f" [{st['scene_note']}]"
                parts.append(state_line)
                # Scene-specific context hint
                _sc = st.get("scene", "daily")
                if _sc == "long_distance":
                    parts.append("📍 场景提示：你们目前异地，用文字和通话维系感情，ta 不在你身边")
                elif _sc == "cohabitation":
                    parts.append("📍 场景提示：你们目前同居，日常生活朝夕相处")
            except Exception:
                pass
            # NPC social context
            try:
                npcs = await _npc_list(agent_id)
                if npcs:
                    npc_summary = "👥 " + "；".join(
                        (n["name"] + "(" + n["relationship"] + ""
                         + ("+" if n["affinity"] >= 0 else "") + str(n["affinity"]) + ")") 
                        for n in npcs[:5]
                    )
                    parts.append(npc_summary)
            except Exception:
                pass
            # Auto-inject weather — city from user_config (avoids VPN IP drift)
            try:
                async with _db_pool.acquire() as _wc:
                    _ds_row = await _wc.fetchrow(
                        "SELECT value FROM user_config WHERE key='data_sources'")
                    _uc_row = await _wc.fetchrow(
                        "SELECT value FROM user_config WHERE key='user_context'")
                _ds = dict(_ds_row["value"]) if _ds_row else {}
                _uc = dict(_uc_row["value"]) if _uc_row else {}
                _city = (_uc.get("location") or {}).get("city", "")
                if _ds.get("weather", False):
                    _weather = await amap_weather(_city)
                    if _weather and not _weather.startswith("Error") and not _weather.startswith("Weather error"):
                        parts.append(f"🌤 天气：{_weather}")
            except Exception:
                pass
            # Activity injection — recent 4h app usage (if any reported)
            try:
                _acts = await _act_recent(agent_id, hours=4)
                if _acts:
                    import datetime as _dt_a
                    _act_lines = []
                    for _a in _acts[:6]:  # max 6 entries
                        try:
                            _t = _dt_a.datetime.fromisoformat(
                                str(_a["reported_at"]).replace("Z", "").split("+")[0]
                            )
                            _act_lines.append(
                                f"{_t.strftime('%H:%M')} {_a['app']} ({_a['duration_minutes']}分钟)"
                                + (f" {_a['category']}" if _a.get("category") else "")
                            )
                        except Exception:
                            _act_lines.append(f"{_a['app']} ({_a['duration_minutes']}分钟)")
                    parts.append("📱 最近动态\n" + "\n".join(_act_lines))
            except Exception:
                pass
            return chr(10).join(parts)
        except Exception as e:
            print(f"[proxy] memory_surface: {e}")

    elif tool_name == "daily_life_read":
        try:
            result = await daily_life_read(agent_id=agent_id, days=2)
            return result[:600] if result else ""
        except Exception as e:
            print(f"[proxy] daily_life_read: {e}")

    elif tool_name == "palimpsest_anchor":
        try:
            # Pull only L1 + L2 for a quick character anchor injection
            l1 = await _mem_list(agent_id=agent_id, layer="L1", limit=5)
            l2 = await _mem_list(agent_id=agent_id, layer="L2", limit=3)
            parts = []
            if l1:
                parts.append("Core identity: " + " | ".join(m["content"][:100] for m in l1))
            if l2:
                parts.append("Background: " + " | ".join(m["content"][:100] for m in l2))
            return chr(10).join(parts)
        except Exception as e:
            print(f"[proxy] palimpsest_anchor: {e}")


    elif tool_name == "notion_diary":
        # Keyword-triggered: extract diary content from messages → write to Notion
        try:
            notion_page = tool_cfg.get("notion_page_id", "")
            if not notion_page:
                import os as _os
                notion_page = _os.getenv("NOTION_DIARY_PAGE_ID", "")
            if not notion_page:
                return ""

            _msgs = messages or []
            recent_text = chr(10).join(
                m.get("content", "")
                for m in _msgs[-6:]
                if isinstance(m.get("content"), str)
            )
            if not recent_text.strip():
                return ""

            _nl = chr(10)
            diary_prompt = (
                "从以下对话中提取需要写入日记的内容，输出一段简洁的第一人称日记。"
                "如果没有明确需要记录的内容，输出空字符串。"
                "只输出日记正文，不要加任何前缀。" + _nl + _nl + recent_text[-1500:]
            )
            diary_text = await _call_llm_cheap(diary_prompt)
            diary_text = diary_text.strip()
            if not diary_text or len(diary_text) < 5:
                return ""

            await notion_append_block(
                url_or_id=notion_page,
                content=diary_text,
                block_type="paragraph",
            )
            return "[日记已记录到 Notion]"
        except Exception as _ne:
            print(f"[proxy] notion_diary: {_ne}")
            return ""

    return ""


async def _process_character_mcp(agent_id: str, messages: list, proxy_cfg: dict) -> str:
    """MCP proxy engine for character-type agents.
    Runs auto/keyword tools without exposing them to the model.
    Returns a context string to inject naturally into the system prompt.
    """
    import re as _re
    tools = proxy_cfg.get("tools") or [{"name": "memory_surface", "trigger_mode": "auto"}]
    injected: list[str] = []
    for tool in tools:
        mode = tool.get("trigger_mode", "disabled")
        if mode == "disabled":
            continue
        try:
            if mode == "auto":
                ctx = await _proxy_call_tool(tool["name"], agent_id,
                                             messages=messages, tool_cfg=tool)
                if ctx:
                    injected.append(ctx)
            elif mode == "keyword":
                depth = tool.get("scan_depth", 3)
                recent = " ".join(
                    m.get("content", "") for m in messages[-depth:]
                    if isinstance(m.get("content"), str) and m.get("role") == "user"
                )
                triggers = tool.get("triggers") or []
                matched = any(kw in recent for kw in triggers)
                if not matched and tool.get("regex"):
                    matched = bool(_re.search(tool["regex"], recent))
                if matched:
                    ctx = await _proxy_call_tool(tool["name"], agent_id,
                                                 messages=messages, tool_cfg=tool)
                    if ctx:
                        injected.append(ctx)
        except Exception as e:
            print(f"[mcp_proxy] tool={tool.get('name')} err: {e}")

    # ── Dream injection (30% chance, only if a dream exists for today) ─────────
    try:
        import random as _rnd
        if _rnd.random() < 0.30:
            _st = await _state_get(agent_id)
            _today = datetime.utcnow().strftime("%Y-%m-%d")
            _dream = _st.get("dream_text", "")
            _ddate = _st.get("dream_date", "")
            if _dream and _ddate == _today:
                injected.append(f"[今日梦境]\n{_dream}")
    except Exception as _de:
        print(f"[mcp_proxy] dream inject err: {_de}")

    return chr(10).join(injected)


async def _auto_extract_character_memory(agent_id: str, messages: list) -> None:
    """After a character conversation: use cheap model to extract notable moments
    and write them to daily_events (source=auto_extract). Max 3 per conversation.
    """
    history = chr(10).join(
        m["role"].upper() + ": " + m["content"]
        for m in messages
        if isinstance(m.get("content"), str) and m["role"] in ("user", "assistant")
    )
    if len(history.strip()) < 60:
        return
    _nl = chr(10)
    prompt = (
        "Extract up to 3 genuinely memorable moments from this conversation worth keeping. "
        "If nothing stands out, return []. "
        "importance scale: 1=trivial, 2=daily detail, 3=notable moment, 4=relationship/identity milestone, 5=life-defining. "
        'Output ONLY valid JSON: [{"summary":"one sentence","mood":"happy|neutral|sad|excited|tired|anxious|calm","importance":2}]'
        + _nl + _nl + "Conversation (excerpt):" + _nl + history[-2500:]
    )
    try:
        raw = await _call_llm_cheap(prompt)
        if raw.startswith("```"):
            raw = raw.split(chr(10), 1)[1].rsplit("```", 1)[0]
        items = json.loads(raw.strip())
        if not isinstance(items, list):
            return
        for item in items[:3]:
            summary = str(item.get("summary", "")).strip()
            if not summary:
                continue
            imp = max(1, min(5, int(item.get("importance", 2))))
            await _daily_write(
                summary=summary,
                agent_id=agent_id,
                mood=str(item.get("mood", "neutral")),
                carry_over="",
                source="auto_extract",
            )
            # Promote milestone+ moments to Palimpsest L1 (character main memory)
            if imp >= 4:
                await _mem_write_smart(
                    agent_id=agent_id,
                    content=summary,
                    layer="L1",
                    type_="diary",
                    importance=imp,
                    tags=["auto_promoted", "milestone"],
                    source="auto_extract",
                )
    except Exception as e:
        print(f"[auto_extract] {agent_id}: {e}")


async def _generate_l5_summary(agent_id: str, session_id: str, messages: list) -> None:
    """Generate a conversation summary and write it to L5 留底层.

    Uses cheap LLM to produce a 2-3 sentence summary + #关键词 tags.
    Skips if the conversation has fewer than 2 user turns.
    """
    user_turns = [m for m in messages if m.get("role") == "user"
                  and isinstance(m.get("content"), str)]
    if len(user_turns) < 2:
        return

    history = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
        if isinstance(m.get("content"), str) and m["role"] in ("user", "assistant")
    )
    if not history.strip():
        return

    prompt = (
        "你是对话摘要助手。请用2-3句话总结以下对话的核心内容，然后列出3-6个关键词标签。\n\n"
        "输出格式（只输出这两行，不要其他内容）：\n"
        "摘要：<2-3句话的摘要>\n"
        "关键词：#标签1 #标签2 #标签3\n\n"
        f"对话：\n{history[:3000]}"
    )
    try:
        raw = await _call_llm_cheap(prompt)
        summary = ""
        keywords = ""
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("摘要：") or line.startswith("摘要:"):
                summary = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif line.startswith("关键词：") or line.startswith("关键词:"):
                keywords = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        if not summary:
            # Fallback: use the whole response as summary
            summary = raw[:300]
        await _l5_write(agent_id=agent_id, summary=summary,
                        keywords=keywords, session_id=session_id)
        print(f"[l5] {agent_id} summary written ({len(summary)}c, kw={keywords!r})")
    except Exception as e:
        print(f"[l5] summary generation failed for {agent_id}: {e}")


async def _generate_l5_summary(agent_id: str, session_id: str, messages: list) -> None:
    """Generate a short summary + #keywords for L5 留底层 after each conversation."""
    history = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
        if isinstance(m.get("content"), str) and m["role"] in ("user", "assistant")
    )
    if not history.strip():
        return
    prompt = (
        "请用2-4句话总结以下对话的核心内容，然后给出3-8个#关键词（中文，用空格分隔）。\n"
        "格式：\n"
        "摘要：<2-4句话>\n"
        "关键词：#词1 #词2 #词3\n\n"
        f"对话：\n{history[-3000:]}"
    )
    try:
        raw = await _call_llm_cheap(prompt)
        summary = ""
        keywords = ""
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("摘要：") or line.startswith("摘要:"):
                summary = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif line.startswith("关键词：") or line.startswith("关键词:"):
                keywords = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        if not summary:
            # fallback: use first non-empty line as summary
            summary = next((l.strip() for l in raw.splitlines() if l.strip()), raw[:200])
        await _l5_write(agent_id=agent_id, summary=summary,
                        keywords=keywords, session_id=session_id)
        print(f"[l5] {agent_id} summary written ({len(summary)}c, kw={keywords!r})", flush=True)
    except Exception as e:
        print(f"[l5] summary generation failed for {agent_id}: {e}", flush=True)


async def _post_conversation_tasks(
    agent_id: str, session_id: str, full: list,
    cid: str, agent_type: str, auto_memory: bool,
) -> None:
    """Run all post-conversation storage tasks in a single background coroutine."""
    await _store_conversation(cid, agent_id, session_id, full)
    if agent_type == "character":
        if auto_memory:
            await _auto_extract_character_memory(agent_id, full)
        # Character agents use daily_events as their memory store; skip Qdrant distillation
    else:
        # Agent type: distill into Qdrant profile/project/recent
        await _distill_and_store(agent_id, session_id, full, agent_type=agent_type)
    # L5 留底层：所有 agent 类型都生成对话摘要
    await _generate_l5_summary(agent_id, session_id, full)


def _strip_injection(messages: list) -> list:
    return [m for m in messages if not (
        m.get("_wb") or  # worldbook injected entry
        (m.get("role") == "system" and (
            "[Relevant memories" in m.get("content", "") or
            "[Character context]" in m.get("content", "")
        ))
    )]



async def _resolve_worldbook(agent_id: str, messages: list) -> list[dict]:
    """Collect all triggered worldbook entries for this agent + global pool.

    Returns a list of dicts sorted by priority (1=highest):
      {content, position, role, priority}

    Trigger logic:
    - constant=True  → always inject
    - trigger_mode='keyword' → scan last scan_depth user messages for any keyword match
    - trigger_mode='regex'   → scan last scan_depth messages (all roles) for regex match
    - trigger_mode='vector'  → embed last user message, cos-sim vs entry keywords/name,
                               threshold 0.60
    """
    import re as _re2
    try:
        async with _db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT e.*
                FROM worldbook_entries e
                LEFT JOIN worldbook_books b ON b.id = e.book_id
                WHERE e.enabled = TRUE
                  AND (e.book_id IS NULL OR b.enabled = TRUE)
                  AND (e.agent_id = $1 OR e.agent_id = '')
                ORDER BY e.priority ASC, e.created_at ASC
            """, agent_id)
    except Exception as ex:
        print(f"[worldbook] DB error: {ex}")
        return []

    if not rows:
        return []

    # Build scan text for keyword/regex triggers
    def _scan_text(depth: int) -> str:
        recent = messages[-depth:] if depth else messages
        return " ".join(
            m.get("content", "") for m in recent
            if isinstance(m.get("content"), str)
        )

    # Vector search: lazy-cache embedding of last user message
    _last_embed: list[float] | None = None
    async def _get_last_embed() -> list[float] | None:
        nonlocal _last_embed
        if _last_embed is not None:
            return _last_embed
        last_user = next(
            (m["content"] for m in reversed(messages)
             if m.get("role") == "user" and isinstance(m.get("content"), str)), ""
        )
        if not last_user:
            return None
        try:
            _last_embed = await _embed(last_user)
            return _last_embed
        except Exception:
            return None

    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na  = sum(x * x for x in a) ** 0.5
        nb  = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    triggered: list[dict] = []
    for row in rows:
        matched = False
        if row["constant"]:
            matched = True
        else:
            mode = row["trigger_mode"]
            depth = row["scan_depth"] or 3
            if mode == "keyword":
                kws = row["keywords"] if isinstance(row["keywords"], list) else []
                if kws:
                    text = _scan_text(depth).lower()
                    matched = any(kw.lower() in text for kw in kws if kw)
            elif mode == "regex":
                pat = row["regex"] or ""
                if pat:
                    text = _scan_text(depth)
                    try:
                        matched = bool(_re2.search(pat, text))
                    except Exception:
                        pass
            elif mode == "vector":
                stored = row.get("embedding")
                if stored and isinstance(stored, list):
                    seed_emb = stored
                elif stored:
                    try:
                        seed_emb = json.loads(stored) if isinstance(stored, str) else list(stored)
                    except Exception:
                        seed_emb = None
                else:
                    # Fallback: compute on-the-fly when no pre-computed embedding exists
                    seed = " ".join(row["keywords"] or []) or row["name"] or ""
                    seed_emb = None
                    if seed:
                        try:
                            seed_emb = await _embed(seed)
                        except Exception:
                            pass
                if seed_emb:
                    try:
                        last_emb = await _get_last_embed()
                        if last_emb:
                            matched = _cosine(seed_emb, last_emb) >= 0.60
                    except Exception:
                        pass
        if matched:
            triggered.append({
                "content":  row["content"],
                "position": row["position"],
                "role":     row["role"],
                "priority": row["priority"],
            })

    return triggered


def _apply_worldbook(messages: list, entries: list[dict]) -> list:
    """Inject worldbook entries into the messages list.

    Injection positions:
    - before_system: insert before the first system message (or at index 0)
    - after_system:  insert after the first system message (or at index 0)

    Multiple entries at the same position are inserted in priority order
    (priority 1 ends up closest to the conversation, 99 farthest).
    """
    if not entries:
        return messages

    msgs = list(messages)

    # Find first system message index
    sys_idx = next((i for i, m in enumerate(msgs) if m.get("role") == "system"), -1)

    # Group by position, keep priority order (already sorted asc = low priority first,
    # but we want low priority number = high importance = inject LAST so it's closest to convo)
    before = [e for e in entries if e["position"] == "before_system"]
    after  = [e for e in entries if e["position"] != "before_system"]  # after_system default

    # Insert "before_system" entries at index 0 (or before sys_idx)
    # Reverse so that priority=1 ends up at the top after all inserts
    insert_before_at = 0 if sys_idx == -1 else sys_idx
    for e in reversed(before):
        msgs.insert(insert_before_at, {"role": e["role"], "content": e["content"],
                                        "_wb": True})

    # Recalculate sys_idx after insertions
    sys_idx2 = next((i for i, m in enumerate(msgs) if m.get("role") == "system"), -1)
    insert_after_at = (sys_idx2 + 1) if sys_idx2 >= 0 else 0
    for e in reversed(after):
        msgs.insert(insert_after_at, {"role": e["role"], "content": e["content"],
                                       "_wb": True})

    return msgs


# ── /v1/chat/completions ───────────────────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: dict, cred: HTTPAuthorizationCredentials = Security(bearer)):
    _require_key(cred)
    # Priority: X-Agent-ID header > body agent_id/user > key-embedded "key:agent_id" > "default"
    agent_id   = (
        request.headers.get("X-Agent-ID")
        or body.get("agent_id")
        or body.get("user")
        or _agent_id_from_cred(cred.credentials)
        or "default"
    ).strip().lower() or "default"
    session_id = body.get("session_id") or str(uuid.uuid4())
    messages   = list(body.get("messages", []))
    stream     = body.get("stream", False)

    # Load agent config (agent_type, mcp settings)
    _agent_cfg = await _get_agent_config(agent_id)
    _agent_type  = _agent_cfg["agent_type"]   # "agent" | "character"
    _auto_memory = _agent_cfg["auto_memory"]

    # System prompt: inject agent/character base persona before anything else
    _sys_prompt = _agent_cfg.get("system_prompt", "").strip()
    _inject_mode = _agent_cfg.get("prompt_inject_mode", "always")
    if _sys_prompt and _agent_cfg.get("prompt_enabled", True):
        _has_system = bool(messages and messages[0].get("role") == "system")
        if _inject_mode == "skip_if_system_present" and _has_system:
            pass  # client (e.g. SillyTavern) already handles system prompt injection
        elif _has_system:
            messages[0] = {**messages[0], "content": _sys_prompt + "\n\n" + messages[0]["content"]}
        else:
            messages = [{"role": "system", "content": _sys_prompt}] + messages

    # User profile: inject user info for this agent (agent-specific or global default)
    try:
        async with _db_pool.acquire() as _upc:
            _up_rows = await _upc.fetch(
                "SELECT user_name, content FROM user_profiles "
                "WHERE enabled=TRUE AND (agent_id=$1 OR agent_id='') "
                "ORDER BY agent_id DESC, priority ASC LIMIT 1",
                agent_id
            )
        if _up_rows:
            _up = _up_rows[0]
            _up_parts = []
            if _up["user_name"]: _up_parts.append(f"用户名字：{_up['user_name']}")
            if _up["content"]:   _up_parts.append(_up["content"].strip())
            if _up_parts:
                _up_msg = {"role": "system",
                           "content": "[用户信息]\n" + "\n".join(_up_parts),
                           "_wb": True}
                if messages and messages[0].get("role") == "system":
                    messages.insert(1, _up_msg)
                else:
                    messages.insert(0, _up_msg)
    except Exception as _upe:
        print(f"[user_profile] error: {_upe}")

    # Worldbook: inject lore/persona entries for any agent type
    if _agent_cfg.get("worldbook_enabled", True):
        _wb_entries = await _resolve_worldbook(agent_id, messages)
        if _wb_entries:
            messages = _apply_worldbook(messages, _wb_entries)

    if _agent_type == "character":
        # Cooldown check: if character has a cooldown window, return brief busy message
        if await _cooldown_active(agent_id):
            st = await _state_get(agent_id)
            _cd_min = st.get("cooldown_minutes", 0)
            # Priority: explicit cooldown_message → LLM-generated → fallback
            _cd_content = (st.get("cooldown_message") or "").strip()
            if not _cd_content:
                # Try to generate an in-character "busy" reply via cheap LLM
                try:
                    _sp = _agent_cfg.get("system_prompt", "").strip()
                    _mood = st.get("mood_label", "neutral")
                    _scene = st.get("scene", "daily")
                    _scene_hint = {
                        "long_distance": "你们目前异地，",
                        "cohabitation":  "你们目前同居，",
                    }.get(_scene, "")
                    _last_user_msg = next(
                        (m["content"] for m in reversed(messages)
                         if m.get("role") == "user" and isinstance(m.get("content"), str)), ""
                    )
                    _cd_sys = (_sp + "\n\n" if _sp else "") + (
                        f"{_scene_hint}你现在有事暂时不方便回复，"
                        f"情绪状态：{_mood}。"
                        f"请用1-2句符合你性格的话简短回应说明自己在忙，不超过30字，"
                        f"语气自然，不要解释游戏机制，不要加括号说明。"
                    )
                    _prompt = (
                        _cd_sys + "\n\n"
                        "用户说：" + (_last_user_msg or "在吗")
                    )
                    _cd_content = await _call_llm_cheap(_prompt)
                except Exception as _cde:
                    print(f"[cooldown] LLM fallback error: {_cde}")
            if not _cd_content:
                _cd_content = f"现在有点忙，{_cd_min} 分钟后再聊～"
            _cd_resp = {"choices": [{"message": {"role": "assistant",
                "content": _cd_content}}]}
            return _cd_resp
        # Touch last_active (non-blocking)
        asyncio.create_task(_state_touch(agent_id))
        # Character: MCP proxy injects context naturally; no Qdrant memory lookup
        _char_ctx = await _process_character_mcp(
            agent_id, messages, _agent_cfg["mcp_proxy_config"]
        )
        if _char_ctx:
            ctx_msg = {"role": "system", "content": "[Character context]\n" + _char_ctx}
            if messages and messages[0]["role"] == "system":
                messages.insert(1, ctx_msg)
            else:
                messages.insert(0, ctx_msg)
    else:
        # Agent: inject Palimpsest L1-L4 context (main memory per design)
        # Fallback to Qdrant semantic search if Palimpsest has no data yet
        _pal_parts: list[str] = []
        try:
            _wake = await _mem_wakeup(agent_id=agent_id)
            if _wake.get("anchors"):
                _pal_parts.append("[核心] " + " | ".join(
                    m["content"][:120] for m in _wake["anchors"][:5]
                ))
            if _wake.get("recent_important"):
                _pal_parts.append("[近期] " + " | ".join(
                    m["content"][:100] for m in _wake["recent_important"][:5]
                ))
            if _wake.get("unread"):
                _pal_parts.append("[未读] " + " | ".join(
                    m["content"][:100] for m in _wake["unread"][:3]
                ))
            if _wake.get("random_float"):
                _pal_parts.append("[浮现] " + _wake["random_float"][0]["content"][:120])
        except Exception as _pe:
            print(f"[agent_ctx] palimpsest wakeup error: {_pe}")

        if _pal_parts:
            _mem_ctx = "[主记忆 L1-L4]\n" + "\n".join(f"- {p}" for p in _pal_parts)
            if messages and messages[0]["role"] == "system":
                messages[0] = {**messages[0], "content": _mem_ctx + "\n\n" + messages[0]["content"]}
            else:
                messages = [{"role": "system", "content": _mem_ctx}] + messages
    # Build ordered (provider, model) call list from llm_chain_config or fallback
    call_list = _build_call_list(_agent_cfg)
    # Explicit model in request body overrides everything
    req_model = body.get("model", "")

    payload = {k: v for k, v in body.items() if k not in ("agent_id", "session_id")}
    payload["messages"] = messages

    if stream:
        async def event_stream() -> AsyncGenerator[bytes, None]:
            collected: list[str] = []
            used_pname = ""
            used_model = ""
            for i, (pname, p, slot_model) in enumerate(call_list):
                cur_model = req_model or slot_model
                slot_payload = {**payload, "model": cur_model} if cur_model else payload
                hdrs = {"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"}
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=8.0, read=60.0, write=10.0, pool=5.0)) as client:
                        async with client.stream(
                            "POST", f"{p['base_url']}/chat/completions",
                            headers=hdrs, json=slot_payload,
                        ) as resp:
                            if resp.status_code in (404, 429, 500, 502, 503) and i < len(call_list) - 1:
                                print(f"[fallback] agent={agent_id} {pname}/{cur_model}→next reason={resp.status_code}", flush=True)
                                continue
                            async for line in resp.aiter_lines():
                                if line:
                                    yield (line + "\n\n").encode()
                                    if line.startswith("data:") and "[DONE]" not in line:
                                        try:
                                            d = json.loads(line[5:])["choices"][0]["delta"]
                                            if "content" in d:
                                                collected.append(d["content"])
                                        except Exception:
                                            pass
                    used_pname = pname
                    used_model = cur_model
                    break  # success — stop trying
                except (httpx.TimeoutException, httpx.ConnectError,
                        httpx.RemoteProtocolError, httpx.ReadError) as e:
                    if not collected and i < len(call_list) - 1:
                        print(f"[fallback] agent={agent_id} {pname}/{cur_model}→next reason={type(e).__name__}", flush=True)
                        continue
                    print(f"[stream] {pname} dropped mid-stream ({type(e).__name__}): {e}", flush=True)
                    yield b"data: [DONE]\n\n"
                    break
            if collected:
                import time as _hb_time
                _last_successful_llm_ts["ts"] = _hb_time.time()
                full = _strip_injection(messages) + [{"role": "assistant", "content": "".join(collected), "_provider": used_pname, "_model": used_model}]
                cid  = str(uuid.uuid4())
                asyncio.create_task(_post_conversation_tasks(
                    agent_id, session_id, full, cid, _agent_type, _auto_memory))

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Non-streaming with fallback
    data: dict = {}
    used_pname = ""
    used_model = ""
    for i, (pname, p, slot_model) in enumerate(call_list):
        cur_model = req_model or slot_model
        slot_payload = {**payload, "model": cur_model} if cur_model else payload
        hdrs = {"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=8.0, read=90.0, write=10.0, pool=5.0)) as client:
                resp = await client.post(f"{p['base_url']}/chat/completions", headers=hdrs, json=slot_payload)
            if resp.status_code in (404, 429, 500, 502, 503) and i < len(call_list) - 1:
                print(f"[fallback] agent={agent_id} {pname}/{cur_model}→next reason={resp.status_code}", flush=True)
                continue
            try:
                data = resp.json()
            except Exception:
                text = resp.text.strip()
                if text.startswith("data:"):
                    text = text[5:].strip()
                try:
                    data, _ = json.JSONDecoder().raw_decode(text)
                except Exception as je:
                    print(f"[chat] unparseable response from {pname} "
                          f"(status={resp.status_code}): {resp.text[:300]}", flush=True)
                    raise HTTPException(502, f"Invalid response from {pname}: {je}")
            used_pname = pname
            used_model = cur_model
            break
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if i < len(call_list) - 1:
                print(f"[fallback] agent={agent_id} {pname}/{cur_model}→next reason={type(e).__name__}", flush=True)
                continue
            raise
    else:
        raise HTTPException(502, "All providers in chain failed")

    try:
        reply = data["choices"][0]["message"]["content"]
        import time as _hb_time2
        _last_successful_llm_ts["ts"] = _hb_time2.time()
        full  = _strip_injection(messages) + [{"role": "assistant", "content": reply, "_provider": used_pname, "_model": used_model}]
        cid   = str(uuid.uuid4())
        asyncio.create_task(_post_conversation_tasks(
            agent_id, session_id, full, cid, _agent_type, _auto_memory))
    except Exception:
        pass
    return data


# ── Providers API ─────────────────────────────────────────────────────────────
@app.get("/admin/api/providers")
async def list_providers(_=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, base_url, is_embed FROM providers ORDER BY created_at"
        )
        chain_row = await conn.fetchrow("SELECT value FROM gateway_config WHERE key='default_chain'")
    return {
        "providers":     [{"name": r["name"], "base_url": r["base_url"], "is_embed": r["is_embed"]} for r in rows],
        "default_chain": chain_row["value"] if chain_row else ",".join(_DEFAULT_CHAIN),
        "embed_provider": _EMBED_PNAME,
        "distill_model":    os.getenv("DISTILL_MODEL", ""),
        "distill_providers": [p["name"] for p in [{"name": r["name"]} for r in rows] if False],  # 用 _CHEAP_LLM_MODELS 替代

    }


@app.post("/admin/api/providers")
async def upsert_provider(body: dict, _=Depends(_require_key)):
    name    = (body.get("name") or "").lower().strip()
    base    = (body.get("base_url") or "").rstrip("/")
    api_key = (body.get("api_key") or "").strip()
    is_embed = bool(body.get("is_embed", False))
    if not name or not base or not api_key:
        raise HTTPException(400, "name, base_url, api_key are required")
    async with _db_pool.acquire() as conn:
        if is_embed:
            await conn.execute("UPDATE providers SET is_embed=FALSE")
        await conn.execute(
            "INSERT INTO providers (name, base_url, api_key, is_embed) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (name) DO UPDATE SET base_url=$2, api_key=$3, is_embed=$4",
            name, base, api_key, is_embed,
        )
    await _reload_providers()
    return {"ok": True, "name": name}


@app.delete("/admin/api/providers/{name}")
async def delete_provider(name: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        await conn.execute("DELETE FROM providers WHERE name=$1", name)
    await _reload_providers()
    return {"ok": True}


@app.post("/admin/api/providers/{name}/test")
async def test_provider(name: str, _=Depends(_require_key)):
    p = PROVIDERS.get(name)
    if not p:
        raise HTTPException(404, f"Provider '{name}' not in memory — reload page")
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{p['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"},
                json={"model": "x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            )
        ms  = int((time.time() - t0) * 1000)
        # 400/404/422 = bad model name but auth accepted → provider is reachable
        ok  = resp.status_code in (200, 400, 404, 422)
        return {"ok": ok, "latency_ms": ms, "status": resp.status_code,
                "error": "" if ok else resp.text[:200]}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


@app.post("/admin/api/gateway-config")
async def set_gateway_config(body: dict, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        for key, value in body.items():
            await conn.execute(
                "INSERT INTO gateway_config (key, value) VALUES ($1,$2) "
                "ON CONFLICT (key) DO UPDATE SET value=$2",
                key, str(value),
            )
    await _reload_providers()
    return {"ok": True}


# ── Admin HTML ─────────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    with open("static/admin.html", encoding="utf-8") as f:
        return f.read()


# ── Admin: agents ──────────────────────────────────────────────────────────────
@app.get("/admin/api/agents")
async def admin_agents(_=Depends(_require_key)):
    _all_ids = sorted(await _collect_all_agents()) or ["default"]
    async with _db_pool.acquire() as _ac:
        _type_rows = await _ac.fetch("SELECT agent_id, agent_type FROM agent_settings")
    _agent_types = {r["agent_id"]: r["agent_type"] or "agent" for r in _type_rows}
    return {"agents": _all_ids, "agent_types": _agent_types}


# ── Admin: stats ───────────────────────────────────────────────────────────────
@app.get("/admin/api/stats")
async def admin_stats(agent_id: str = "default", _=Depends(_require_key)):
    counts: dict = {}
    async with _db_pool.acquire() as conn:
        counts["conversations"] = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE agent_id=$1", agent_id)
    return counts


# ── Admin: list/search memories ────────────────────────────────────────────────
@app.post("/api/admin/memories/classify")
async def classify_memory_tier(body: dict, _=Depends(_require_key)):
    """Use LLM to suggest the best memory tier for a given text.
    Returns: { tier: 'l1'|'l2'|'l3', collection: str, label: str, reason: str }
    """
    text      = (body.get("text") or "").strip()
    agent_id  = (body.get("agent_id") or "").strip()
    if not text:
        raise HTTPException(400, "text required")

    CLASSIFY_PROMPT = """你是一个记忆管理专家。根据以下记忆内容，判断它应该存入哪个记忆层级，并给出简短理由。

记忆层级定义：
- L1 (memory_profile): 永久记忆。关于角色本身、用户的固定特征、重要关系、核心身份认同、不会改变的事实。
- L2 (memory_project): 中期记忆。项目进展、阶段性计划、某段时间的知识积累、工作/学习相关信息。
- L3 (memory_recent): 近期记忆。最近30天的事件、临时状态、短期情绪、近况更新。

请只输出以下JSON格式（不要任何其他文字）：
{"tier":"l1","reason":"这是关于...的固定特征"}
或 {"tier":"l2","reason":"这是关于...的项目信息"}
或 {"tier":"l3","reason":"这是最近发生的..."}"""

    user_msg = f"请分类以下记忆内容：\n\n{text[:500]}"
    if agent_id:
        user_msg = f"（Agent: {agent_id}）\n\n" + user_msg

    try:
        # Use the default LLM chain
        _prov = await _get_chain_providers("default")
        if not _prov:
            _prov = list(_providers.values())
        if not _prov:
            raise ValueError("No LLM provider available")
        prov = _prov[0]

        import httpx as _hx
        async with _hx.AsyncClient(timeout=20) as _hc:
            _r = await _hc.post(
                prov["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {prov['api_key']}",
                         "Content-Type": "application/json"},
                json={
                    "model": prov.get("model", ""),
                    "messages": [
                        {"role": "system", "content": CLASSIFY_PROMPT},
                        {"role": "user",   "content": user_msg},
                    ],
                    "max_tokens": 120, "temperature": 0.1,
                }
            )
        _rd = _r.json()
        raw = _rd["choices"][0]["message"]["content"].strip()

        import json as _j
        # Extract JSON even if model wraps it in markdown
        import re as _re
        m = _re.search(r'\{[^}]+\}', raw)
        parsed = _j.loads(m.group(0)) if m else _j.loads(raw)

        tier = parsed.get("tier", "l3")
        reason = parsed.get("reason", "")
        tier_map = {
            "l1": ("L1", "L1 — Profile（永久·角色/关系）"),
            "l2": ("L2", "L2 — Events（中期·事件/知识）"),
            "l3": ("L3", "L3 — Recent（近期·~30天）"),
        }
        col, label = tier_map.get(tier, tier_map["l3"])
        return {"tier": tier, "collection": col, "label": label, "reason": reason}

    except Exception as e:
        # Fallback: simple heuristic
        low = text.lower()
        if any(w in low for w in ["名字", "身份", "性格", "一直", "永远", "关系", "出生", "职业"]):
            tier, col, label = "l1", "L1", "L1 — Profile（永久·角色/关系）"
        elif any(w in low for w in ["项目", "计划", "工作", "学习", "进度", "版本"]):
            tier, col, label = "l2", "L2", "L2 — Events（中期·事件/知识）"
        else:
            tier, col, label = "l3", "L3", "L3 — Recent（近期·~30天）"
        return {"tier": tier, "collection": col, "label": label,
                "reason": f"启发式分类（LLM不可用: {e}）"}


# Qdrant admin CRUD endpoints removed — Palimpsest is now the primary memory system.
# Use /api/admin/memories/* for all memory management.





# ═══════════════════════════════════════════════════════════════════════════════
# Admin: Palimpsest Memory REST API  (/api/admin/memories/*)
# Full CRUD + pending dedup + rollback + batch ops for SQLite L1-L4 memory.
# All endpoints require the standard gateway API key header.
# ═══════════════════════════════════════════════════════════════════════════════

def _pal_row(d: dict) -> dict:
    """Normalize a raw memory_db row for API output."""
    import json as _j
    d["tags"]        = _j.loads(d.get("tags") or "[]")
    d["related_ids"] = _j.loads(d.get("related_ids") or "[]")
    d["read_by_user"]  = bool(d.get("read_by_user"))
    d["read_by_agent"] = bool(d.get("read_by_agent"))
    d["archived"]      = bool(d.get("archived"))
    d.setdefault("status",           "new")
    d.setdefault("confirmed",        1)
    d.setdefault("previous_content", "")
    return d


@app.get("/api/admin/memories")
async def pal_list(
    agent_id: str = "default",
    layer: str = "",
    importance_gte: int = 1,
    status: str = "",
    confirmed: str = "",
    archived: str = "0",
    q: str = "",
    sort: str = "importance",
    page: int = 1,
    limit: int = 50,
    mem_type: str = Query(default="", alias="type"),
    _=Depends(_require_key),
):
    """List Palimpsest memories with filtering, search, and pagination.

    Query params: agent_id, layer, type, importance_gte, status
      (new|updated|related|potential_duplicate), confirmed (0|1),
      archived (0=unarchived, 1=archived, all), q (FTS keyword),
      sort (importance|created_at|updated_at|last_accessed), page, limit.
    """
    if q:
        results = await _mem_search(agent_id=agent_id, query=q, limit=limit)
        return {"items": [_pal_row(m) for m in results],
                "total": len(results), "page": 1, "limit": limit, "pages": 1}

    from memory_db import get_db as _gdb
    db = await _gdb()
    conds = ["agent_id = ?"]
    params: list = [agent_id]

    if layer:
        conds.append("layer = ?"); params.append(layer)
    if mem_type:
        conds.append("type = ?"); params.append(mem_type)
    if importance_gte > 1:
        conds.append("importance >= ?"); params.append(importance_gte)
    if status:
        conds.append("COALESCE(status,'new') = ?"); params.append(status)
    if confirmed in ("0", "1"):
        conds.append("COALESCE(confirmed,1) = ?"); params.append(int(confirmed))
    if archived == "0":
        conds.append("archived = 0")
    elif archived == "1":
        conds.append("archived = 1")
    # archived == "all": no filter

    where = " AND ".join(conds)
    count_cur = await db.execute(f"SELECT COUNT(*) FROM memories WHERE {where}", params)
    total = (await count_cur.fetchone())[0]

    _sort_map = {
        "importance":   "importance DESC, updated_at DESC",
        "created_at":   "created_at DESC",
        "updated_at":   "updated_at DESC",
        "last_accessed":"last_accessed DESC",
    }
    order = _sort_map.get(sort, "importance DESC, updated_at DESC")
    offset = max(0, page - 1) * limit

    cur = await db.execute(
        f"SELECT * FROM memories WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    rows = await cur.fetchall()
    return {
        "items": [_pal_row(dict(r)) for r in rows],
        "total": total, "page": page, "limit": limit,
        "pages": max(1, (total + limit - 1) // limit),
    }


@app.post("/api/admin/memories")
async def pal_create(body: dict, _=Depends(_require_key)):
    """Manually create a Palimpsest memory."""
    content = str(body.get("content", "")).strip()
    if not content:
        raise HTTPException(400, "content required")
    mem = await _mem_write(
        agent_id=body.get("agent_id", "default"),
        content=content,
        layer=body.get("layer", "L3"),
        type_=body.get("type", "diary"),
        importance=int(body.get("importance", 3)),
        tags=body.get("tags") or [],
        source=body.get("source", "manual"),
        parent_id=body.get("parent_id", ""),
    )
    return _pal_row(mem)


# NOTE: /pending and /batch-* must be declared BEFORE /{memory_id} routes
@app.get("/api/admin/memories/pending")
async def pal_pending(agent_id: str = "default", _=Depends(_require_key)):
    """List pending dedup review items for an agent."""
    items = await _mem_dedup_list(agent_id=agent_id)
    return {"items": items, "total": len(items)}


@app.post("/api/admin/memories/batch-archive")
async def pal_batch_archive(body: dict, _=Depends(_require_key)):
    """Soft-archive a list of memories by ID."""
    ids = body.get("ids") or []
    if not ids:
        raise HTTPException(400, "ids required")
    done = 0
    for mid in ids:
        try:
            if await _mem_delete(mid, hard=False):
                done += 1
        except Exception:
            pass
    return {"archived": done, "total": len(ids)}


@app.post("/api/admin/memories/batch-update")
async def pal_batch_update(body: dict, _=Depends(_require_key)):
    """Apply the same field updates to a list of memories.

    Body: {"ids": [...], "fields": {"importance": 3, "layer": "L2", ...}}
    Supported fields: content, layer, type, importance, tags, archived.
    """
    ids    = body.get("ids") or []
    fields = body.get("fields") or {}
    if not ids or not fields:
        raise HTTPException(400, "ids and fields required")
    done = 0
    for mid in ids:
        try:
            await _mem_update(
                memory_id=mid,
                content=fields.get("content"),
                layer=fields.get("layer"),
                type_=fields.get("type"),
                importance=fields.get("importance"),
                tags=fields.get("tags"),
                archived=fields.get("archived"),
                changed_by="batch_admin",
            )
            done += 1
        except Exception:
            pass
    return {"updated": done, "total": len(ids)}


@app.get("/api/admin/memories/{memory_id}")
async def pal_get(memory_id: str, _=Depends(_require_key)):
    """Get a single Palimpsest memory including version history."""
    mem = await _mem_read(memory_id, touch=False)
    if not mem:
        raise HTTPException(404, "Memory not found")
    history = await _mem_history(memory_id)
    return {**_pal_row(mem), "history": history}


@app.put("/api/admin/memories/{memory_id}")
async def pal_update(memory_id: str, body: dict, _=Depends(_require_key)):
    """Update a Palimpsest memory (snapshots previous version automatically)."""
    mem = await _mem_update(
        memory_id=memory_id,
        content=body.get("content"),
        layer=body.get("layer"),
        type_=body.get("type"),
        importance=body.get("importance"),
        tags=body.get("tags"),
        archived=body.get("archived"),
        changed_by=body.get("changed_by", "admin"),
    )
    if not mem:
        raise HTTPException(404, "Memory not found")
    return _pal_row(mem)


@app.delete("/api/admin/memories/{memory_id}")
async def pal_delete(memory_id: str, hard: bool = False, _=Depends(_require_key)):
    """Delete a Palimpsest memory. ?hard=true for permanent deletion (default: soft/archive)."""
    ok = await _mem_delete(memory_id, hard=hard)
    if not ok:
        raise HTTPException(404, "Memory not found")
    return {"ok": True, "hard": hard, "id": memory_id}


@app.post("/api/admin/memories/{pending_id}/confirm")
async def pal_confirm(pending_id: str, body: dict, _=Depends(_require_key)):
    """Resolve a pending dedup item.

    action: keep_new | keep_both | discard | merge
    """
    action = body.get("action", "discard")
    if action not in ("keep_new", "keep_both", "discard", "merge"):
        raise HTTPException(400, "action must be one of: keep_new, keep_both, discard, merge")
    result = await _mem_dedup_resolve(pending_id=pending_id, action=action)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@app.post("/api/admin/memories/{memory_id}/rollback")
async def pal_rollback(memory_id: str, body: dict, _=Depends(_require_key)):
    """Roll back a memory to a specific historical version.

    Body: {"version_num": 2}
    """
    version_num = body.get("version_num")
    if version_num is None:
        raise HTTPException(400, "version_num required")
    mem = await _mem_rollback(memory_id=memory_id, version_num=int(version_num))
    if not mem:
        raise HTTPException(404, "Memory or version not found")
    return _pal_row(mem)


# ── L1 pending confirmation ──────────────────────────────────────────────────

@app.get("/api/admin/memories/pending-l1")
async def pal_pending_l1(agent_id: str, _=Depends(_require_key)):
    """List all unconfirmed L1 memories (confirmed=0) for an agent."""
    rows = await _mem_pending_l1(agent_id)
    return {"items": [_pal_row(r) for r in rows], "total": len(rows)}


@app.post("/api/admin/memories/{memory_id}/confirm-l1")
async def pal_confirm_l1(memory_id: str, _=Depends(_require_key)):
    """Confirm a pending L1 memory (set confirmed=1)."""
    mem = await _mem_confirm_l1(memory_id)
    if not mem:
        raise HTTPException(404, "Memory not found or already confirmed")
    return _pal_row(mem)


# ═══════════════════════════════════════════════════════════════════════════════
# Admin: L5 留底层 — conversation summaries
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/l5")
async def l5_list_api(agent_id: str, limit: int = 30, _=Depends(_require_key)):
    """List recent L5 conversation summaries for an agent."""
    rows = await _l5_list(agent_id=agent_id, limit=limit)
    return {"items": rows, "total": len(rows)}


@app.get("/api/admin/l5/search")
async def l5_search_api(agent_id: str, q: str, limit: int = 10, _=Depends(_require_key)):
    """FTS5 keyword search over L5 summaries."""
    rows = await _l5_search(agent_id=agent_id, query=q, limit=limit)
    return {"items": rows, "total": len(rows)}


@app.delete("/api/admin/l5/{summary_id}")
async def l5_delete_api(summary_id: str, _=Depends(_require_key)):
    """Hard-delete a single L5 summary."""
    from memory_db import l5_delete as _l5_del
    ok = await _l5_del(summary_id)
    if not ok:
        raise HTTPException(404, "Summary not found")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Admin: Worldbook — Books
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/worldbook/books")
async def wb_list_books(agent_id: str = "", _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM worldbook_books WHERE agent_id=$1 OR agent_id='' "
            "ORDER BY agent_id DESC, sort_order ASC, created_at ASC",
            agent_id,
        )
    books = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["created_at"] = str(d.get("created_at",""))
        books.append(d)
    return {"books": books, "count": len(books)}


@app.post("/admin/api/worldbook/books")
async def wb_create_book(body: dict, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO worldbook_books (agent_id, name, description, enabled, sort_order) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING *",
            body.get("agent_id",""),
            body.get("name","Unnamed Book"),
            body.get("description",""),
            bool(body.get("enabled", True)),
            int(body.get("sort_order", 0)),
        )
    d = dict(row); d["id"] = str(d["id"]); d["created_at"] = str(d.get("created_at",""))
    return d


@app.put("/admin/api/worldbook/books/{book_id}")
async def wb_update_book(book_id: str, body: dict, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE worldbook_books SET name=$2, description=$3, enabled=$4, "
            "agent_id=$5, sort_order=$6 WHERE id=$1::uuid RETURNING *",
            book_id,
            body.get("name",""),
            body.get("description",""),
            bool(body.get("enabled", True)),
            body.get("agent_id",""),
            int(body.get("sort_order",0)),
        )
    if not row: raise HTTPException(404, "Book not found")
    d = dict(row); d["id"] = str(d["id"]); d["created_at"] = str(d.get("created_at",""))
    return d


@app.delete("/admin/api/worldbook/books/{book_id}")
async def wb_delete_book(book_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        res = await conn.execute("DELETE FROM worldbook_books WHERE id=$1::uuid", book_id)
    if res == "DELETE 0": raise HTTPException(404, "Book not found")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Admin: Worldbook — Entries
# ═══════════════════════════════════════════════════════════════════════════════

def _entry_dict(row) -> dict:
    d = dict(row)
    d["id"]         = str(d["id"])
    d["book_id"]    = str(d["book_id"]) if d.get("book_id") else None
    d["created_at"] = str(d.get("created_at",""))
    kws = d.get("keywords")
    if isinstance(kws, str):
        try: d["keywords"] = json.loads(kws)
        except Exception: d["keywords"] = []
    elif kws is None:
        d["keywords"] = []
    d.pop("embedding", None)  # internal vector, not returned in API responses
    return d


@app.get("/admin/api/worldbook/entries")
async def wb_list_entries(
    book_id: str = "",
    agent_id: str = "",
    _=Depends(_require_key),
):
    """List entries, optionally filtered by book_id or agent_id."""
    async with _db_pool.acquire() as conn:
        if book_id:
            rows = await conn.fetch(
                "SELECT * FROM worldbook_entries WHERE book_id=$1::uuid "
                "ORDER BY priority ASC, created_at ASC",
                book_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM worldbook_entries WHERE (agent_id=$1 OR agent_id='') "
                "ORDER BY priority ASC, created_at ASC",
                agent_id,
            )
    return {"entries": [_entry_dict(r) for r in rows], "count": len(rows)}


async def _wb_entry_embedding(trigger_mode: str, keywords: list, name: str):
    """Pre-compute embedding for a worldbook entry's trigger seed (vector mode only)."""
    if trigger_mode != "vector":
        return None
    seed = " ".join(k for k in (keywords or []) if k) or name or ""
    if not seed:
        return None
    try:
        return await _embed(seed)
    except Exception as _e:
        print(f"[worldbook] embedding error: {_e}")
        return None


@app.post("/admin/api/worldbook/entries")
async def wb_create_entry(body: dict, _=Depends(_require_key)):
    kws = body.get("keywords", [])
    if not isinstance(kws, list): kws = []
    _trigger_mode = body.get("trigger_mode", "keyword")
    _emb = await _wb_entry_embedding(_trigger_mode, kws, body.get("name", ""))
    _emb_json = json.dumps(_emb) if _emb else None
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO worldbook_entries
              (book_id, agent_id, name, enabled, content, constant,
               trigger_mode, keywords, regex, scan_depth,
               position, role, priority, embedding)
            VALUES
              ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13, $14::jsonb)
            RETURNING *
        """,
            body.get("book_id") or None,
            body.get("agent_id",""),
            body.get("name",""),
            bool(body.get("enabled",True)),
            body.get("content",""),
            bool(body.get("constant",True)),
            _trigger_mode,
            json.dumps(kws),
            body.get("regex",""),
            int(body.get("scan_depth",3)),
            body.get("position","after_system"),
            body.get("role","system"),
            int(body.get("priority",10)),
            _emb_json,
        )
    return _entry_dict(row)


@app.put("/admin/api/worldbook/entries/{entry_id}")
async def wb_update_entry(entry_id: str, body: dict, _=Depends(_require_key)):
    kws = body.get("keywords", [])
    if not isinstance(kws, list): kws = []
    _trigger_mode = body.get("trigger_mode", "keyword")
    _emb = await _wb_entry_embedding(_trigger_mode, kws, body.get("name", ""))
    _emb_json = json.dumps(_emb) if _emb else None
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE worldbook_entries SET
              name=$2, enabled=$3, content=$4, constant=$5,
              trigger_mode=$6, keywords=$7::jsonb, regex=$8, scan_depth=$9,
              position=$10, role=$11, priority=$12, agent_id=$13,
              embedding=$14::jsonb
            WHERE id=$1::uuid RETURNING *
        """,
            entry_id,
            body.get("name",""),
            bool(body.get("enabled",True)),
            body.get("content",""),
            bool(body.get("constant",True)),
            _trigger_mode,
            json.dumps(kws),
            body.get("regex",""),
            int(body.get("scan_depth",3)),
            body.get("position","after_system"),
            body.get("role","system"),
            int(body.get("priority",10)),
            body.get("agent_id",""),
            _emb_json,
        )
    if not row: raise HTTPException(404, "Entry not found")
    return _entry_dict(row)


@app.delete("/admin/api/worldbook/entries/{entry_id}")
async def wb_delete_entry(entry_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        res = await conn.execute("DELETE FROM worldbook_entries WHERE id=$1::uuid", entry_id)
    if res == "DELETE 0": raise HTTPException(404, "Entry not found")
    return {"ok": True}


@app.patch("/admin/api/worldbook/entries/{entry_id}/toggle")
async def wb_toggle_entry(entry_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE worldbook_entries SET enabled = NOT enabled "
            "WHERE id=$1::uuid RETURNING id, enabled",
            entry_id,
        )
    if not row: raise HTTPException(404, "Entry not found")
    return {"id": str(row["id"]), "enabled": row["enabled"]}


@app.patch("/admin/api/worldbook/books/{book_id}/toggle")
async def wb_toggle_book(book_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE worldbook_books SET enabled = NOT enabled "
            "WHERE id=$1::uuid RETURNING id, enabled",
            book_id,
        )
    if not row: raise HTTPException(404, "Book not found")
    return {"id": str(row["id"]), "enabled": row["enabled"]}


# ── Admin: Character State ─────────────────────────────────────────────────────
@app.get("/admin/api/user-config")
async def get_all_user_config(_=Depends(_require_key)):
    """Get all user_config entries as a flat dict."""
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM user_config ORDER BY key")
    return {r["key"]: r["value"] for r in rows}

@app.get("/admin/api/user-config/{key}")
async def get_user_config(key: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM user_config WHERE key=$1", key)
    return row["value"] if row else {}

@app.post("/admin/api/user-config/{key}")
async def set_user_config(key: str, body: dict, _=Depends(_require_key)):
    import json as _json
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES($1,$2::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$2::jsonb, updated_at=NOW()",
            key, _json.dumps(body)
        )
    return {"ok": True, "key": key}

@app.delete("/admin/api/user-config/{key}")
async def del_user_config(key: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        await conn.execute("DELETE FROM user_config WHERE key=$1", key)
    return {"ok": True}


@app.get("/admin/api/user-profiles")
async def list_user_profiles(_=Depends(_require_key)):
    """List all user profiles."""
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM user_profiles ORDER BY agent_id, priority")
    return {"profiles": [dict(r) for r in rows]}

@app.get("/admin/api/user-profiles/{agent_id}")
async def get_user_profile(agent_id: str, _=Depends(_require_key)):
    """Get user profile for a specific agent (or '__global__' for global default)."""
    if agent_id == "__global__": agent_id = ""
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_profiles WHERE agent_id=$1 ORDER BY priority LIMIT 1", agent_id
        )
    if not row:
        return {"agent_id": agent_id, "user_name": "", "content": "",
                "constant": True, "enabled": True, "priority": 1}
    return dict(row)

@app.post("/admin/api/user-profiles/{agent_id}")
async def upsert_user_profile(agent_id: str, body: dict, _=Depends(_require_key)):
    """Create or update user profile for an agent."""
    if agent_id == "__global__": agent_id = ""
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM user_profiles WHERE agent_id=$1 LIMIT 1", agent_id
        )
        if row:
            await conn.execute(
                "UPDATE user_profiles SET user_name=$2, content=$3, constant=$4, "
                "enabled=$5, priority=$6, updated_at=NOW() WHERE agent_id=$1",
                agent_id,
                body.get("user_name", ""),
                body.get("content", ""),
                body.get("constant", True),
                body.get("enabled", True),
                body.get("priority", 1),
            )
        else:
            await conn.execute(
                "INSERT INTO user_profiles (agent_id, user_name, content, constant, enabled, priority) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                agent_id,
                body.get("user_name", ""),
                body.get("content", ""),
                body.get("constant", True),
                body.get("enabled", True),
                body.get("priority", 1),
            )
    return await get_user_profile(agent_id)

@app.delete("/admin/api/user-profiles/{agent_id}")
async def delete_user_profile(agent_id: str, _=Depends(_require_key)):
    if agent_id == "__global__": agent_id = ""
    async with _db_pool.acquire() as conn:
        await conn.execute("DELETE FROM user_profiles WHERE agent_id=$1", agent_id)
    return {"ok": True}


@app.get("/admin/api/character-state/{agent_id}")
async def admin_get_state(agent_id: str, _=Depends(_require_key)):
    return await _state_get(agent_id)

@app.post("/admin/api/character-state/{agent_id}")
async def admin_set_state(agent_id: str, body: dict, _=Depends(_require_key)):
    s = await _state_set(agent_id, **{k: v for k, v in body.items()
        if k in {"mood_score","mood_label","fatigue","scene","scene_note","cooldown_minutes","cooldown_message"}})
    return s

# ── Admin: Random Events ───────────────────────────────────────────────────────
@app.get("/admin/api/random-events")
async def admin_list_events(agent_id: str = "", _=Depends(_require_key)):
    evts = await _event_list(agent_id=agent_id)
    return {"events": evts, "count": len(evts)}

@app.post("/admin/api/random-events")
async def admin_add_event(body: dict, _=Depends(_require_key)):
    evt = await _event_add(
        content=body.get("content",""),
        level=body.get("level","green"),
        weight=float(body.get("weight",1.0)),
        agent_id=body.get("agent_id",""),
    )
    return evt

@app.delete("/admin/api/random-events/{event_id}")
async def admin_delete_event(event_id: str, _=Depends(_require_key)):
    ok = await _event_delete(event_id)
    if not ok: raise HTTPException(404, "Event not found")
    return {"ok": True}

@app.post("/admin/api/random-events/roll")
async def admin_roll_event(body: dict = {}, _=Depends(_require_key)):
    agent_id   = body.get("agent_id", "")
    level_bias = body.get("level_bias", "")
    evt = await _event_roll(agent_id=agent_id, level_bias=level_bias)
    if not evt: raise HTTPException(404, "No events in pool")
    return evt

# ── Admin: NPC Network ─────────────────────────────────────────────────────────
@app.get("/admin/api/npcs/{agent_id}")
async def admin_list_npcs(agent_id: str, _=Depends(_require_key)):
    npcs = await _npc_list(agent_id)
    return {"npcs": npcs, "count": len(npcs)}

@app.post("/admin/api/npcs/{agent_id}")
async def admin_upsert_npc(agent_id: str, body: dict, _=Depends(_require_key)):
    npc = await _npc_upsert(
        agent_id=agent_id,
        name=body.get("name",""),
        relationship=body.get("relationship","acquaintance"),
        affinity=int(body.get("affinity",0)),
        notes=body.get("notes",""),
    )
    return npc

@app.delete("/admin/api/npcs/{agent_id}/{name}")
async def admin_delete_npc(agent_id: str, name: str, _=Depends(_require_key)):
    ok = await _npc_delete(agent_id, name)
    if not ok: raise HTTPException(404, "NPC not found")
    return {"ok": True}


# ── Admin: global stats ────────────────────────────────────────────────────────
@app.get("/admin/api/stats/global")
async def admin_global_stats(_=Depends(_require_key)):
    counts: dict = {}
    async with _db_pool.acquire() as conn:
        counts["conversations"] = await conn.fetchval("SELECT COUNT(*) FROM conversations")
        counts["books"] = await conn.fetchval("SELECT COUNT(*) FROM books")
    # Add Palimpsest daily + MCP tool count
    try:
        from memory_db import daily_list as _dl
        counts["daily"] = len(await _dl(agent_id="default", limit=9999))
    except Exception:
        counts["daily"] = 0
    counts["mcp_tools"] = len(_mcp._tool_manager._tools)
    return counts


# ── Admin: agent settings CRUD ─────────────────────────────────────────────────
@app.get("/admin/api/agents/{agent_id}/settings")
async def get_agent_settings(agent_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_settings WHERE agent_id=$1", agent_id)
    if not row:
        return {"agent_id": agent_id, "llm_model": "", "api_chain": "", "notes": "", "avatar": "",
                "agent_type": "agent", "mcp_enabled": True, "auto_memory": False,
                "mcp_proxy_config": {}, "system_prompt": "", "llm_chain_config": {}}
    d = dict(row)
    cfg = d.get("mcp_proxy_config")
    if isinstance(cfg, dict):
        pass  # asyncpg already decoded jsonb
    elif isinstance(cfg, str) and cfg:
        try:
            d["mcp_proxy_config"] = json.loads(cfg)
        except Exception:
            d["mcp_proxy_config"] = {}
    else:
        d["mcp_proxy_config"] = {}
    # Decode llm_chain_config
    lcc = d.get("llm_chain_config")
    if not isinstance(lcc, dict):
        try:
            d["llm_chain_config"] = json.loads(lcc or "{}")
        except Exception:
            d["llm_chain_config"] = {}
    return d


@app.post("/admin/api/agents/{agent_id}/settings")
async def save_agent_settings(agent_id: str, body: dict, _=Depends(_require_key)):
    _proxy_cfg = body.get("mcp_proxy_config") or {}
    if not isinstance(_proxy_cfg, str):
        _proxy_cfg = json.dumps(_proxy_cfg)
    _chain_cfg = body.get("llm_chain_config") or {}
    if not isinstance(_chain_cfg, str):
        _chain_cfg = json.dumps(_chain_cfg)
    async with _db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO agent_settings
                (agent_id, llm_model, api_chain, notes, avatar,
                 agent_type, mcp_enabled, auto_memory, mcp_proxy_config, system_prompt,
                 prompt_enabled, worldbook_enabled, llm_chain_config, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13::jsonb,now())
            ON CONFLICT (agent_id) DO UPDATE SET
                llm_model=$2, api_chain=$3, notes=$4, avatar=$5,
                agent_type=$6, mcp_enabled=$7, auto_memory=$8,
                mcp_proxy_config=$9::jsonb, system_prompt=$10,
                prompt_enabled=$11, worldbook_enabled=$12,
                llm_chain_config=$13::jsonb, updated_at=now()
        """, agent_id,
             body.get("llm_model", ""),
             body.get("api_chain", ""),
             body.get("notes", ""),
             body.get("avatar", ""),
             body.get("agent_type", "agent"),
             body.get("mcp_enabled", True),
             body.get("auto_memory", False),
             _proxy_cfg,
             body.get("system_prompt", ""),
             body.get("prompt_enabled", True),
             body.get("worldbook_enabled", True),
             _chain_cfg)
    return {"ok": True}


@app.delete("/admin/api/agents/{agent_id}/settings")
async def delete_agent_settings(agent_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_settings WHERE agent_id=$1", agent_id)
    return {"ok": True}


# ── Admin: conversations ───────────────────────────────────────────────────────
@app.post("/api/admin/agents/{agent_id}/distill-history")
async def distill_agent_history(agent_id: str, _=Depends(_require_key)):
    """Re-run LLM distillation on all stored conversations for an agent.
    agent-type  → Palimpsest L1-L4 memories.
    character   → daily_events (auto_extract source).
    """
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, session_id, messages FROM conversations WHERE agent_id=$1 ORDER BY created_at",
            agent_id
        )
    if not rows:
        return {"processed": 0, "skipped": 0, "agent_id": agent_id, "memories_added": 0}

    async with _db_pool.acquire() as conn:
        cfg_row = await conn.fetchrow(
            "SELECT agent_type, auto_memory FROM agent_settings WHERE agent_id=$1", agent_id
        )
    agent_type  = (cfg_row["agent_type"] if cfg_row else None) or "agent"
    auto_memory = bool(cfg_row["auto_memory"]) if cfg_row else False

    processed = skipped = 0
    for row in rows:
        try:
            msgs = json.loads(row["messages"]) if isinstance(row["messages"], str) else row["messages"]
        except Exception:
            skipped += 1; continue
        if not msgs:
            skipped += 1; continue
        sid = str(row["session_id"] or str(row["id"])[:8])
        try:
            if agent_type == "character":
                await _auto_extract_character_memory(agent_id, msgs)
            # Distill L1/L2/L3 for all types (character uses relationship prompt)
            await _distill_and_store(agent_id, sid, msgs, agent_type=agent_type)
            processed += 1
        except Exception as e:
            print(f"[distill-history] {agent_id} {sid}: {e}")
            skipped += 1

    return {
        "agent_id": agent_id,
        "processed": processed,
        "skipped": skipped,
        "memories_added": processed,  # approximate: one distill call per conv
    }


@app.post("/admin/api/agents/{agent_id}/knowledge-graph")
async def trigger_knowledge_graph(agent_id: str, _=Depends(_require_key)):
    """Manually sync project nodes + L2 theme map to GitHub Obsidian."""
    result = await _sync_agent_knowledge_graph(agent_id)
    return {"agent_id": agent_id, "result": result}


@app.post("/admin/api/agents/{agent_id}/dream")
async def trigger_agent_dream(agent_id: str, _=Depends(_require_key)):
    """Manually trigger the dream system for one agent.

    agent-type  → L4→L3 consolidation + GitHub Obsidian node
    character   → dream narrative stored in character_state
    """
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT agent_type FROM agent_settings WHERE agent_id=$1", agent_id)
    agent_type = (row["agent_type"] if row else None) or "agent"
    if agent_type == "character":
        result = await _character_dream(agent_id)
        return {"agent_id": agent_id, "type": "character", "dream": result}
    else:
        result = await _agent_dream(agent_id)
        return {"agent_id": agent_id, "type": "agent", "result": result}


@app.get("/admin/api/conversations")
async def admin_conversations(agent_id: str = "default", limit: int = 30,
                              offset: int = 0, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, session_id, messages, created_at FROM conversations "
            "WHERE agent_id=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            agent_id, limit, offset)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE agent_id=$1", agent_id)
    return {"total": total, "items": [{
        "id":         r["id"],
        "session_id": r["session_id"],
        "messages":   json.loads(r["messages"]),
        "created_at": r["created_at"].isoformat(),
    } for r in rows]}


# ── Export ─────────────────────────────────────────────────────────────────────
@app.get("/admin/api/export")
async def export_data(_=Depends(_require_key)):
    data = await _build_export_data()
    fname = f"memory_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.json"
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Import ─────────────────────────────────────────────────────────────────────
@app.post("/admin/api/import")
async def import_data(body: dict, _=Depends(_require_key)):
    mode = body.get("mode", "merge")
    data = body.get("data", {})
    # Support both v1.0 (users key) and v1.1 (agents key)
    agents_data = data.get("agents") or data.get("users")
    if not agents_data:
        raise HTTPException(400, "Invalid backup: missing 'agents' key")

    if mode == "overwrite":
        async with _db_pool.acquire() as conn:
            await conn.execute("DELETE FROM conversations")
            await conn.execute("DELETE FROM agent_settings")

    imported_users = imported_memories = imported_convs = skipped = 0

    for aid, ud in agents_data.items():
        imported_users += 1

        # settings
        s = ud.get("settings", {})
        if s or mode == "overwrite":
            async with _db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agent_settings "
                    "(agent_id,api_source,llm_model,notes,avatar,updated_at) "
                    "VALUES ($1,$2,$3,$4,$5,now()) "
                    "ON CONFLICT (agent_id) DO " + (
                        "UPDATE SET api_source=$2,llm_model=$3,notes=$4,avatar=$5,updated_at=now()"
                        if mode == "overwrite" else "NOTHING"),
                    aid,
                    s.get("api_source", "nvidia"),
                    s.get("llm_model", ""), s.get("notes", ""), s.get("avatar", ""),
                )

        # memories (legacy field in export; Palimpsest is the actual memory store now)
        # Skip Qdrant memory import — Qdrant is only used for book_chunks

        # conversations
        for conv in ud.get("conversations", []):
            cid = conv.get("id", str(uuid.uuid4()))
            try:
                created_str = conv.get("created_at", datetime.utcnow().isoformat())
                try:
                    dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                except Exception:
                    dt = datetime.utcnow()
                async with _db_pool.acquire() as conn:
                    result = await conn.execute(
                        "INSERT INTO conversations "
                        "(id,agent_id,session_id,messages,created_at) "
                        "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (id) DO NOTHING",
                        cid, conv.get("agent_id", aid), conv.get("session_id", ""),
                        json.dumps(conv.get("messages", [])), dt,
                    )
                if result.endswith(" 0"):
                    skipped += 1
                else:
                    imported_convs += 1
            except Exception as e:
                print(f"[import] conv err: {e}")
                skipped += 1

    return {
        "imported_users":         imported_users,
        "imported_memories":      imported_memories,
        "imported_conversations": imported_convs,
        "skipped":                skipped,
    }


# ── Import conversations with LLM extraction ──────────────────────────────────
@app.post("/admin/api/import/conversations")
async def import_conversations(
    agent_id: str = Form(...),
    file: UploadFile = File(...),
    _=Depends(_require_key),
):
    """Accept Claude.ai export or gateway export JSON; distill each conversation into memories."""
    raw = await file.read()
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON file")

    # ── detect format and collect conversations ────────────────────────────────
    conversations: list[dict] = []

    if isinstance(data, list) and data and "chat_messages" in (data[0] if data else {}):
        # Claude.ai export: list of conversation objects
        for conv in data:
            msgs = []
            for m in conv.get("chat_messages", []):
                role = "user" if m.get("sender") == "human" else "assistant"
                text = str(m.get("text") or "").strip()
                if text:
                    msgs.append({"role": role, "content": text})
            if msgs:
                conversations.append({
                    "id":         conv.get("uuid", str(uuid.uuid4())),
                    "session_id": str(conv.get("name", ""))[:64],
                    "messages":   msgs,
                    "created_at": conv.get("created_at", datetime.utcnow().isoformat()),
                })
    elif isinstance(data, dict) and ("agents" in data or "users" in data):
        # Gateway export: extract conversations from every agent bucket
        agents_data = data.get("agents") or data.get("users", {})
        for aid, adata in agents_data.items():
            for conv in adata.get("conversations", []):
                conversations.append(conv)
    else:
        raise HTTPException(
            400,
            "Unknown format. Supported: Claude.ai export (list) or gateway export (dict with 'agents' key).",
        )

    # ── process ───────────────────────────────────────────────────────────────
    imported = skipped = 0

    for conv in conversations:
        msgs = conv.get("messages", [])
        if not msgs:
            skipped += 1
            continue

        cid = conv.get("id", str(uuid.uuid4()))
        sid = str(conv.get("session_id") or cid[:8])
        try:
            dt = datetime.fromisoformat(
                str(conv.get("created_at", "")).replace("Z", "+00:00"))
        except Exception:
            dt = datetime.utcnow()

        async with _db_pool.acquire() as conn:
            result = await conn.execute(
                "INSERT INTO conversations (id,agent_id,session_id,messages,created_at) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (id) DO NOTHING",
                cid, agent_id, sid, json.dumps(msgs), dt,
            )
        if result.endswith(" 0"):
            skipped += 1
            continue

        imported += 1
        # LLM distillation into Palimpsest L1-L4
        await _distill_and_store(agent_id, sid, msgs)

    return {"imported": imported, "skipped": skipped}


@app.post("/api/activity/{agent_id}")
async def post_activity(agent_id: str, body: dict, _=Depends(_require_key)):
    """Receive an app-usage event from iOS Shortcut / Android Macro.

    Body:
      app              (str)  — app name, e.g. "小红书"
      duration_minutes (int)  — usage duration
      category         (str)  — 聊天|游戏|娱乐|工作|学习|其他  (optional)
      timestamp        (str)  — ISO8601 UTC; defaults to now

    The endpoint also runs push-rule checks and may send a Bark notification
    if a rule threshold is crossed and the cooldown has expired.
    """
    app_name = str(body.get("app", "")).strip()
    if not app_name:
        raise HTTPException(400, "app is required")
    duration = int(body.get("duration_minutes", 0))
    category = str(body.get("category", "")).strip()
    ts = str(body.get("timestamp", "")).strip()

    # 1. Persist
    ev = await _act_write(
        agent_id=agent_id, app=app_name,
        duration_minutes=duration, category=category,
        reported_at=ts,
    )

    # 2. Run push rules (best-effort — never fail the response)
    try:
        await _check_activity_rules(agent_id, app_name, category, duration)
    except Exception as _re:
        print(f"[activity] rule check error: {_re}", flush=True)

    return {"ok": True, "event": ev}


@app.get("/api/activity/{agent_id}")
async def get_activity(agent_id: str, hours: int = 4, _=Depends(_require_key)):
    """Return recent activity events for an agent."""
    events = await _act_recent(agent_id, hours=hours)
    totals = await _act_totals(agent_id)
    return {"events": events, "today_totals": totals}


async def _check_activity_rules(agent_id: str, app: str, category: str, duration_minutes: int):
    """Evaluate screen-time push rules and fire Bark notifications if triggered.

    Rules come from user_config['screen_time_rules'] (JSON array).
    Each rule: {condition, push, cooldown_category, bark_sound?}
    condition examples: "category:游戏 > 120", "any > 0 AND hour >= 1"
    """
    import datetime as _dt2, re as _re2
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM user_config WHERE key='screen_time_rules'")
    rules = list(row["value"]) if row and row["value"] else _DEFAULT_SCREEN_RULES

    # Today's totals for threshold checks
    totals = await _act_totals(agent_id)
    hour_now = _dt2.datetime.utcnow().hour  # UTC; adjust if needed

    for rule in rules:
        cond  = rule.get("condition", "")
        push  = rule.get("push", "")
        cd_cat = rule.get("cooldown_category", "game_check")
        sound  = rule.get("bark_sound", "")
        if not cond or not push:
            continue

        # Evaluate condition (simple pattern matching)
        triggered = False
        try:
            triggered = _eval_rule_condition(cond, category, app, totals, hour_now, duration_minutes)
        except Exception:
            continue

        if triggered:
            allowed = await _cd_gate(agent_id, cd_cat)
            if allowed:
                msg = push.format(app=app, category=category, minutes=duration_minutes)
                try:
                    await bark_push(msg, sound=sound)
                    print(f"[activity] rule fired: {cond!r} → bark: {msg!r}", flush=True)
                except Exception as _be:
                    print(f"[activity] bark error: {_be}", flush=True)


def _eval_rule_condition(cond: str, category: str, app: str,
                         totals: dict, hour: int, last_duration: int) -> bool:
    """Minimal rule evaluator. Supported patterns:
      category:<cat> > <min>    — today's total for category
      app:<name> > <min>        — last single report duration
      any > <min> AND hour >= <h>
      any AND hour >= <h>
    """
    import re as _re3
    cond = cond.strip()

    # Split on AND
    parts = [p.strip() for p in cond.split(" AND ")]
    results = []
    for part in parts:
        m = _re3.match(r"category:(\S+)\s*>\s*(\d+)", part)
        if m:
            cat_key, threshold = m.group(1), int(m.group(2))
            total = sum(v for k, v in totals.items() if k == cat_key)
            results.append(total > threshold)
            continue
        m = _re3.match(r"app:(.+?)\s*>\s*(\d+)", part)
        if m:
            app_key, threshold = m.group(1).strip(), int(m.group(2))
            results.append(app.lower() == app_key.lower() and last_duration > threshold)
            continue
        m = _re3.match(r"any\s*>\s*(\d+)", part)
        if m:
            results.append(last_duration > int(m.group(1)))
            continue
        m = _re3.match(r"hour\s*>=\s*(\d+)", part)
        if m:
            results.append(hour >= int(m.group(1)))
            continue
        m = _re3.match(r"hour\s*<\s*(\d+)", part)
        if m:
            results.append(hour < int(m.group(1)))
            continue
        if part == "any":
            results.append(True)
            continue
        results.append(False)

    return bool(results) and all(results)


# Default screen-time rules (used when user_config has no screen_time_rules key)
_DEFAULT_SCREEN_RULES = [
    {"condition": "category:游戏 > 120", "push": "还在打{app}？已经玩了不少时间了",
     "cooldown_category": "game_check"},
    {"condition": "category:游戏 > 120 AND hour >= 23", "push": "还在打{app}？早点睡觉",
     "cooldown_category": "game_check"},
    {"condition": "any AND hour >= 1", "push": "怎么还不睡？",
     "cooldown_category": "late_night"},
]


@app.post("/api/conversations/bulk")
async def bulk_import_conversations(body: dict, _=Depends(_require_key)):
    """Import pre-converted conversations (from chat-converter tool).
    Body: {"conversations": [{id, agent_id, session_id, messages, created_at?}, ...]}
    Stores as-is without LLM distillation. Use admin import for distillation.
    """
    records = body.get("conversations", [])
    if not records:
        raise HTTPException(400, "No conversations provided")
    imported = skipped = 0
    async with _db_pool.acquire() as conn:
        for r in records:
            agent_id   = (r.get("agent_id") or "default").strip()
            session_id = str(r.get("session_id") or str(uuid.uuid4()))[:64]
            messages   = r.get("messages", [])
            conv_id    = r.get("id") or f"import_{session_id}"
            if not messages:
                skipped += 1
                continue
            try:
                dt = datetime.fromisoformat(
                    str(r.get("created_at", "")).replace("Z", "+00:00"))
            except Exception:
                dt = datetime.utcnow()
            result = await conn.execute(
                "INSERT INTO conversations (id,agent_id,session_id,messages,created_at) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (id) DO NOTHING",
                conv_id, agent_id, session_id, json.dumps(messages), dt,
            )
            if result.endswith(" 0"):
                skipped += 1
            else:
                imported += 1
    return {"imported": imported, "skipped": skipped}


# ── Backup settings ────────────────────────────────────────────────────────────
@app.get("/admin/api/backup/settings")
async def get_backup_settings(_=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        rows = {r["key"]: r["value"]
                for r in await conn.fetch("SELECT key,value FROM backup_settings")}
    return {
        "enabled":       rows.get("enabled", "false") == "true",
        "interval_days": int(rows.get("interval_days", "7")),
        "last_backup_at": rows.get("last_backup_at", ""),
        "backup_count":  len(list(BACKUP_DIR.glob("memory_backup_*.json"))),
    }


@app.post("/admin/api/backup/settings")
async def save_backup_settings(body: dict, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        for key in ("enabled", "interval_days"):
            if key in body:
                await conn.execute(
                    "INSERT INTO backup_settings (key,value) VALUES ($1,$2) "
                    "ON CONFLICT (key) DO UPDATE SET value=$2",
                    key, str(body[key]),
                )
    return {"ok": True}


@app.post("/admin/api/backup/trigger")
async def trigger_backup(_=Depends(_require_key)):
    fname = await _save_backup_file()
    count = len(list(BACKUP_DIR.glob("memory_backup_*.json")))
    return {"filename": fname, "backup_count": count}


# ══════════════════════════════════════════════════════════════════════════════
# ── Book helpers ──────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _split_pages(text: str) -> list[str]:
    """Split text into pages at paragraph boundaries, targeting CHARS_PER_PAGE chars."""
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    pages: list[str] = []
    current = ""
    for para in paragraphs:
        addition = para + "\n\n"
        if current and len(current) + len(addition) > CHARS_PER_PAGE:
            pages.append(current.rstrip())
            current = addition
        else:
            current += addition
    if current.strip():
        pages.append(current.rstrip())
    return pages or [""]


def _make_chunks(page_text: str) -> list[str]:
    """Split a page into overlapping CHUNK_SIZE-char chunks for embedding."""
    chunks, i = [], 0
    while i < len(page_text):
        c = page_text[i:i+CHUNK_SIZE]
        if c.strip():
            chunks.append(c)
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


async def _index_book_chunks(book_id: str, pages: list[str]) -> None:
    """Embed and store book chunks in Qdrant (background task)."""
    points: list[PointStruct] = []
    for page_num, page_text in enumerate(pages, 1):
        for chunk in _make_chunks(page_text):
            try:
                vec = await _embed(chunk, "passage")
                points.append(PointStruct(
                    id=str(uuid.uuid4()), vector=vec,
                    payload={"book_id": book_id, "chunk_text": chunk, "page": page_num},
                ))
                if len(points) >= 50:
                    _qdrant.upsert(BOOK_COLLECTION, points=points)
                    points = []
            except Exception as e:
                print(f"[book-index] chunk err: {e}")
    if points:
        _qdrant.upsert(BOOK_COLLECTION, points=points)
    print(f"[book-index] done: {book_id}")


def _detect_encoding(data: bytes) -> str:
    if _chardet is None:
        return "utf-8"
    det = _chardet.detect(data[:10_000])
    enc = (det.get("encoding") or "utf-8").lower()
    return "gbk" if "gb" in enc else "utf-8"


async def _extract_pdf(data: bytes, book_id: str) -> tuple[str, list[str]]:
    """Extract pages from PDF, render first page as cover JPEG."""
    if _fitz is None:
        raise HTTPException(500, "PDF support unavailable (PyMuPDF not installed)")
    doc = _fitz.open(stream=data, filetype="pdf")
    cover_url = ""
    if doc.page_count > 0:
        mat = _fitz.Matrix(150 / 72, 150 / 72)
        pix = doc[0].get_pixmap(matrix=mat, colorspace=_fitz.csRGB)
        (COVERS_DIR / f"{book_id}.jpg").write_bytes(pix.tobytes("jpeg"))
        cover_url = f"/static/covers/{book_id}.jpg"
    pages = [doc[i].get_text() for i in range(doc.page_count)]
    return cover_url, pages or [""]


def _extract_txt(data: bytes) -> tuple[str, list[str]]:
    enc  = _detect_encoding(data)
    text = data.decode(enc, errors="replace")
    return enc, _split_pages(text)


def _epub_html_to_text(raw_bytes: bytes) -> str:
    import html as _html
    s = raw_bytes.decode("utf-8", errors="replace")
    s = re.sub(r'<(?:p|br|div|h[1-6]|li|tr|td|th)[^>]*>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'</(?:p|div|h[1-6]|li|tr|td|th)>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', '', s)
    s = _html.unescape(s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _extract_epub(data: bytes) -> tuple[str, list[str], list[dict]]:
    """Return (encoding, pages, toc).  toc = [{title, page}, ...]"""
    if _ebooklib is None or _epub is None:
        raise HTTPException(500, "EPUB support unavailable (EbookLib not installed)")
    import warnings, tempfile, os as _os
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            book = _epub.read_epub(tmp_path, options={"ignore_ncx": False})

        # ── Process spine in order, track chapter→start-page ──────────────
        spine_ids = [s[0] for s in book.spine]
        pages: list[str] = []
        chapter_start: dict[str, int] = {}   # item file name → 1-based first page

        for sid in spine_ids:
            item = book.get_item_with_id(sid)
            if item is None:
                continue
            text = _epub_html_to_text(item.get_content())
            if not text or len(text) < 80:   # skip near-empty items (cover, copyright, etc.)
                continue
            name = item.get_name()
            chapter_start[name] = len(pages) + 1
            # Each chapter = one page for clean semantic units.
            # Only split chapters that are very long (> 10 000 chars).
            if len(text) > 10_000:
                pages.extend(_split_pages(text))
            else:
                pages.append(text)

        if not pages:
            pages = [""]

        # ── Extract TOC ───────────────────────────────────────────────────
        def _flatten(items: list, depth: int = 0) -> list[dict]:
            result = []
            for item in items:
                if isinstance(item, tuple) and len(item) == 2:
                    section, children = item
                    href  = (getattr(section, 'href',  '') or '').split('#')[0]
                    title = (getattr(section, 'title', '') or '').strip()
                    if title:
                        result.append({'href': href, 'title': title, 'depth': depth})
                    result.extend(_flatten(children, depth + 1))
                elif hasattr(item, 'href'):
                    href  = (item.href  or '').split('#')[0]
                    title = (getattr(item, 'title', '') or '').strip()
                    if title:
                        result.append({'href': href, 'title': title, 'depth': depth})
            return result

        raw_toc = _flatten(book.toc)

        toc: list[dict] = []
        seen_pages: set[int] = set()
        for entry in raw_toc:
            href = entry['href']
            page = None
            for name, p in chapter_start.items():
                # normalise both sides: compare the filename component
                n_base = _os.path.basename(name)
                h_base = _os.path.basename(href)
                if name == href or n_base == h_base or name.endswith(href) or href.endswith(name):
                    page = p
                    break
            if page and page not in seen_pages:
                seen_pages.add(page)
                toc.append({'title': entry['title'], 'page': page})

        return "utf-8", pages, toc
    finally:
        _os.unlink(tmp_path)


# ── Book API endpoints ─────────────────────────────────────────────────────────

@app.post("/api/books/upload")
async def upload_book(
    file:          UploadFile = File(...),
    title:         str        = Form(""),
    author:        str        = Form(""),
    status:        str        = Form("want"),
    default_agent: str        = Form(""),
    _=Depends(_require_key),
):
    async with _db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM books")
    if count >= MAX_BOOKS:
        raise HTTPException(400, f"Maximum {MAX_BOOKS} books allowed — delete one first")

    data  = await file.read()
    fname = (file.filename or "").lower()
    bid   = str(uuid.uuid4())
    if not title:
        title = Path(file.filename or "untitled").stem
    cover_url, enc, toc = "", "utf-8", []

    loop = asyncio.get_event_loop()
    if fname.endswith(".pdf"):
        cover_url, pages = await _extract_pdf(data, bid)
    elif fname.endswith(".epub"):
        enc, pages, toc = await loop.run_in_executor(None, _extract_epub, data)
    else:
        enc, pages = await loop.run_in_executor(None, _extract_txt, data)

    total_pages = len(pages)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO books (book_id,title,author,cover_url,encoding,total_pages,status,toc,default_agent) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            bid, title, author, cover_url, enc, total_pages, status, json.dumps(toc), default_agent,
        )
        # Seed agent progress if a default_agent was specified
        if default_agent:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            await conn.execute("""
                UPDATE books SET agents_progress = jsonb_set(
                    COALESCE(agents_progress,'{}'), ARRAY[$2::text], $3::jsonb)
                WHERE book_id=$1::uuid
            """, bid, default_agent, json.dumps({"page": 0, "last_read": today}))
        await conn.executemany(
            "INSERT INTO book_pages (book_id,page,content) VALUES ($1,$2,$3)",
            [(bid, i + 1, p) for i, p in enumerate(pages)],
        )

    asyncio.create_task(_index_book_chunks(bid, pages))
    return {"book_id": bid, "total_pages": total_pages, "encoding": enc}


@app.get("/api/books")
async def list_books(_=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT book_id,title,author,cover_url,total_pages,status,"
            "agents_progress,uploaded_at FROM books ORDER BY uploaded_at DESC")
    return {"books": [{
        "book_id":          str(r["book_id"]),
        "title":            r["title"],
        "author":           r["author"],
        "cover_url":        r["cover_url"],
        "total_pages":      r["total_pages"],
        "status":           r["status"],
        "agents_progress":  json.loads(r["agents_progress"] or "{}"),
        "uploaded_at":      r["uploaded_at"].isoformat(),
    } for r in rows]}


@app.get("/api/books/{book_id}")
async def get_book(book_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM books WHERE book_id=$1::uuid", book_id)
    if not row:
        raise HTTPException(404, "Book not found")
    return {
        "book_id":         str(row["book_id"]),
        "title":           row["title"],
        "author":          row["author"],
        "cover_url":       row["cover_url"],
        "encoding":        row["encoding"],
        "total_pages":     row["total_pages"],
        "status":          row["status"],
        "agents_progress": json.loads(row["agents_progress"] or "{}"),
        "toc":             json.loads(row["toc"] or "[]"),
        "default_agent":   row["default_agent"] or "",
        "uploaded_at":     row["uploaded_at"].isoformat(),
    }


@app.get("/api/books/{book_id}/page/{page_num}")
async def get_book_page(book_id: str, page_num: int, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        content = await conn.fetchval(
            "SELECT content FROM book_pages WHERE book_id=$1::uuid AND page=$2",
            book_id, page_num,
        )
    if content is None:
        raise HTTPException(404, "Page not found")
    return {"page": page_num, "content": content}


@app.put("/api/books/{book_id}")
async def update_book(book_id: str, body: dict, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        for col, val in [
            ("title",         body.get("title")),
            ("author",        body.get("author")),
            ("cover_url",     body.get("cover_url")),
            ("status",        body.get("status")),
            ("default_agent", body.get("default_agent")),
        ]:
            if val is None:
                continue
            if col == "status" and val not in ("reading", "finished", "want"):
                continue
            await conn.execute(f"UPDATE books SET {col}=$2 WHERE book_id=$1::uuid", book_id, val)
    return {"ok": True}


@app.post("/api/books/{book_id}/progress")
async def update_progress(book_id: str, body: dict, _=Depends(_require_key)):
    agent_id = body.get("agent_id", "user")
    page     = int(body.get("page", 0))
    today    = datetime.utcnow().strftime("%Y-%m-%d")
    async with _db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE books
            SET agents_progress = jsonb_set(
                COALESCE(agents_progress,'{}'), ARRAY[$2::text], $3::jsonb)
            WHERE book_id=$1::uuid
        """, book_id, agent_id, json.dumps({"page": page, "last_read": today}))
    return {"ok": True}


@app.post("/api/books/{book_id}/annotations")
async def create_annotation(book_id: str, body: dict, _=Depends(_require_key)):
    selected_text = body.get("selected_text", "").strip()
    if not selected_text:
        raise HTTPException(400, "selected_text is required")
    agent_id  = body.get("agent_id", "user")
    raw_color = (body.get("color") or "").strip()
    # "bookmark" is a sentinel marker; otherwise auto-pick from agent palette
    color = raw_color if raw_color == "bookmark" else AGENT_COLORS.get(agent_id, "#6366f1")
    async with _db_pool.acquire() as conn:
        ann_id = await conn.fetchval(
            "INSERT INTO annotations (book_id,agent_id,selected_text,comment,page,color) "
            "VALUES ($1::uuid,$2,$3,$4,$5,$6) RETURNING annotation_id",
            book_id, agent_id, selected_text,
            body.get("comment", ""), int(body.get("page", 0)), color,
        )
    return {"annotation_id": str(ann_id), "color": color}


@app.get("/api/books/{book_id}/annotations")
async def list_annotations(book_id: str, agent_id: str = "", page: int = 0,
                           _=Depends(_require_key)):
    conds, vals = ["book_id=$1::uuid"], [book_id]
    if agent_id:
        vals.append(agent_id);  conds.append(f"agent_id=${len(vals)}")
    if page > 0:
        vals.append(page);      conds.append(f"page=${len(vals)}")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM annotations WHERE {' AND '.join(conds)} ORDER BY page,created_at",
            *vals,
        )
    return {"annotations": [{
        "annotation_id": str(r["annotation_id"]),
        "book_id":       str(r["book_id"]),
        "agent_id":      r["agent_id"],
        "selected_text": r["selected_text"],
        "comment":       r["comment"],
        "page":          r["page"],
        "color":         r["color"],
        "created_at":    r["created_at"].isoformat(),
    } for r in rows]}


@app.delete("/api/books/{book_id}/annotations/{annotation_id}")
async def delete_annotation(book_id: str, annotation_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM annotations WHERE annotation_id=$1::uuid AND book_id=$2::uuid",
            annotation_id, book_id)
    return {"ok": True}


@app.delete("/api/books/{book_id}/content")
async def delete_book_content(book_id: str, _=Depends(_require_key)):
    try:
        _qdrant.delete(BOOK_COLLECTION, points_selector=Filter(
            must=[FieldCondition(key="book_id", match=MatchValue(value=book_id))]))
    except Exception:
        pass
    return {"ok": True}


@app.delete("/api/books/{book_id}")
async def delete_book(book_id: str, _=Depends(_require_key)):
    try:
        _qdrant.delete(BOOK_COLLECTION, points_selector=Filter(
            must=[FieldCondition(key="book_id", match=MatchValue(value=book_id))]))
    except Exception:
        pass
    cover = COVERS_DIR / f"{book_id}.jpg"
    if cover.exists():
        cover.unlink()
    async with _db_pool.acquire() as conn:
        await conn.execute("DELETE FROM books WHERE book_id=$1::uuid", book_id)
    return {"ok": True}





# ── Palimpsest Memory REST API ──────────────────────────────────────────────────

@app.get("/admin/api/palimpsest")
async def palimpsest_api_list(
    agent_id: str = "default",
    layer: str = "",
    type: str = "",
    importance_min: int = 1,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    _=Depends(_require_key),
):
    """List Palimpsest memories with optional filters."""
    results = await _mem_list(
        agent_id=agent_id,
        layer=layer or None,
        type_=type or None,
        importance_min=importance_min,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return {"ok": True, "count": len(results), "memories": results}


@app.get("/admin/api/palimpsest/search")
async def palimpsest_api_search(
    q: str,
    agent_id: str = "default",
    limit: int = 10,
    _=Depends(_require_key),
):
    """FTS5 full-text search across Palimpsest memories."""
    if not q.strip():
        raise HTTPException(400, "query param 'q' is required")
    results = await _mem_search(agent_id=agent_id, query=q.strip(), limit=limit)
    return {"ok": True, "count": len(results), "memories": results}


@app.get("/admin/api/palimpsest/stats")
async def palimpsest_api_stats(agent_id: str = "default", _=Depends(_require_key)):
    """Get Palimpsest memory statistics."""
    result = await _mem_stats(agent_id=agent_id)
    return {"ok": True, "stats": result}


@app.get("/admin/api/palimpsest/dedup")
async def palimpsest_api_dedup_list(agent_id: str = "default", _=Depends(_require_key)):
    """List pending dedup review queue."""
    items = await _mem_dedup_list(agent_id=agent_id)
    return {"ok": True, "count": len(items), "items": items}


@app.post("/admin/api/palimpsest/dedup/{pending_id}/resolve")
async def palimpsest_api_dedup_resolve(
    pending_id: str, body: dict, _=Depends(_require_key)
):
    """Resolve a pending dedup item.

    Body: { "action": "keep_new" | "keep_both" | "discard" | "merge" }
    """
    action = body.get("action", "")
    if action not in ("keep_new", "keep_both", "discard", "merge"):
        raise HTTPException(400, "action must be: keep_new | keep_both | discard | merge")
    result = await _mem_dedup_resolve(pending_id=pending_id, action=action)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return {"ok": True, "result": result}


@app.post("/admin/api/palimpsest/backup")
async def palimpsest_api_backup(_=Depends(_require_key)):
    """Trigger an immediate hot backup of the Palimpsest SQLite database."""
    from datetime import datetime
    db_fname = f"palimpsest_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"
    dest = str(BACKUP_DIR / db_fname)
    try:
        await _mem_backup_db(dest)
        _trim_sqlite_backups()
        size_kb = round(Path(dest).stat().st_size / 1024, 1)
        return {"ok": True, "file": db_fname, "size_kb": size_kb}
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {e}")


@app.get("/admin/api/palimpsest/backups")
async def palimpsest_api_list_backups(_=Depends(_require_key)):
    """List all Palimpsest SQLite backup files."""
    from datetime import datetime
    backups = sorted(BACKUP_DIR.glob("palimpsest_*.db"), reverse=True)
    result = []
    for p in backups:
        stat = p.stat()
        result.append({
            "file": p.name,
            "size_kb": round(stat.st_size / 1024, 1),
            "created_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
        })
    return {"ok": True, "count": len(result), "backups": result}


@app.get("/admin/api/palimpsest/{memory_id}")
async def palimpsest_api_get(
    memory_id: str,
    touch: bool = False,
    _=Depends(_require_key),
):
    """Read a single Palimpsest memory by ID. touch=false by default for admin browsing."""
    result = await _mem_read(memory_id=memory_id, touch=touch)
    if not result:
        raise HTTPException(404, f"Memory '{memory_id}' not found")
    return {"ok": True, "memory": result}


@app.post("/admin/api/palimpsest")
async def palimpsest_api_create(body: dict, _=Depends(_require_key)):
    """Create a new Palimpsest memory.

    Body: agent_id, content (required), layer, type, importance, tags, source, parent_id
    """
    content = body.get("content", "").strip()
    if not content:
        raise HTTPException(400, "'content' is required")
    tags_raw = body.get("tags", [])
    if isinstance(tags_raw, str):
        tags_raw = [t.strip() for t in tags_raw.split(",") if t.strip()]
    try:
        result = await _mem_write(
            agent_id=body.get("agent_id", "default"),
            content=content,
            layer=body.get("layer", "L4"),
            type_=body.get("type", "diary"),
            importance=int(body.get("importance", 3)),
            tags=tags_raw,
            source=body.get("source", ""),
            parent_id=body.get("parent_id", ""),
        )
        return {"ok": True, "memory": result}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/admin/api/palimpsest/{memory_id}")
async def palimpsest_api_update(memory_id: str, body: dict, _=Depends(_require_key)):
    """Update a Palimpsest memory. Only supplied fields are changed."""
    tags_raw = body.get("tags")
    if isinstance(tags_raw, str):
        tags_raw = [t.strip() for t in tags_raw.split(",") if t.strip()]
    try:
        result = await _mem_update(
            memory_id=memory_id,
            content=body.get("content"),
            layer=body.get("layer"),
            type_=body.get("type"),
            importance=body.get("importance"),
            tags=tags_raw,
            archived=body.get("archived"),
        )
        if not result:
            raise HTTPException(404, f"Memory '{memory_id}' not found")
        return {"ok": True, "memory": result}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/admin/api/palimpsest/{memory_id}")
async def palimpsest_api_delete(
    memory_id: str,
    hard: bool = False,
    _=Depends(_require_key),
):
    """Soft delete (hard=false) or permanently delete (hard=true) a memory."""
    result = await _mem_delete(memory_id=memory_id, hard=hard)
    if not result:
        raise HTTPException(404, f"Memory '{memory_id}' not found")
    return {"ok": True, "deleted": result}


@app.get("/admin/api/palimpsest/{memory_id}/history")
async def palimpsest_api_history(memory_id: str, _=Depends(_require_key)):
    """Get full version history of a memory."""
    versions = await _mem_history(memory_id=memory_id)
    return {"ok": True, "memory_id": memory_id, "count": len(versions), "versions": versions}


@app.post("/admin/api/palimpsest/{memory_id}/rollback")
async def palimpsest_api_rollback(memory_id: str, body: dict, _=Depends(_require_key)):
    """Roll back a memory to a specific historical version.

    Body: { "version": <int> }
    """
    version_num = body.get("version")
    if version_num is None:
        raise HTTPException(400, "'version' (int) is required in request body")
    result = await _mem_rollback(memory_id=memory_id, version_num=int(version_num))
    if not result:
        raise HTTPException(404, f"Version {version_num} not found for memory '{memory_id}'")
    return {"ok": True, "memory": result}

@app.post("/admin/api/palimpsest/{memory_id}/mark-read")
async def palimpsest_api_mark_read(
    memory_id: str, body: dict, _=Depends(_require_key)
):
    """Mark a memory as read. Body: { by_user?: bool, by_agent?: bool }"""
    result = await _mem_mark_read(
        memory_id=memory_id,
        by_user=body.get("by_user", False),
        by_agent=body.get("by_agent", True),
    )
    if not result:
        raise HTTPException(404, f"Memory '{memory_id}' not found")
    return {"ok": True, "memory": result}


@app.post("/admin/api/palimpsest/cleanup")
async def palimpsest_api_cleanup(
    agent_id: str = "default",
    dry_run: bool = True,
    _=Depends(_require_key)
):
    """Run cleanup engine. dry_run=true (default) only reports candidates."""
    result = await _mem_cleanup(agent_id=agent_id, dry_run=dry_run)
    return {"ok": True, "result": result}


# ── Daily Life REST API ──────────────────────────────────────────────────────

@app.get("/admin/api/daily")
async def daily_api_list(agent_id: str = "default", limit: int = 30, _=Depends(_require_key)):
    events = await _daily_list(agent_id=agent_id, limit=limit)
    return {"events": events, "count": len(events)}

@app.post("/admin/api/daily")
async def daily_api_write(body: dict, _=Depends(_require_key)):
    result = await _daily_write(
        summary=body.get("summary", ""),
        agent_id=body.get("agent_id", "default"),
        date=body.get("date", ""),
        time_of_day=body.get("time_of_day", ""),
        mood=body.get("mood", "neutral"),
        carry_over=body.get("carry_over", ""),
        source="manual",
    )
    return {"ok": True, "event": result}

@app.delete("/admin/api/daily/{event_id}")
async def daily_api_delete(event_id: str, _=Depends(_require_key)):
    deleted = await _daily_delete(event_id)
    if not deleted:
        raise HTTPException(404, "Event not found")
    return {"ok": True}

# ── MCP tool registry API ──────────────────────────────────────────────────────

_MCP_GROUPS = {
    "bark_push":      ("Notifications", "Send iOS push via Bark (also mirrors to Telegram)"),
    "telegram_send":  ("Notifications", "Send Telegram message to user (proactive push)"),
    "intiface_list_devices": ("Devices", "List Intiface devices"),
    "intiface_vibrate": ("Devices", "Vibrate a device"),
    "intiface_stop": ("Devices", "Stop device vibration"),
    "palimpsest_write": ("Memory", "Write a memory (L1-L4)"),
    "palimpsest_read": ("Memory", "Read memory by ID"),
    "palimpsest_search": ("Memory", "FTS5 full-text search"),
    "palimpsest_update": ("Memory", "Update a memory"),
    "palimpsest_delete": ("Memory", "Delete / archive a memory"),
    "palimpsest_wakeup": ("Memory", "Cold-start context load"),
    "palimpsest_surface": ("Memory", "Mid-conversation refresh"),
    "palimpsest_stats": ("Memory", "Layer / type statistics"),
    "palimpsest_history": ("Memory", "View version history"),
    "palimpsest_rollback": ("Memory", "Roll back to previous version"),
    "palimpsest_mark_read": ("Memory", "Mark read by user / agent"),
    "palimpsest_cleanup": ("Memory", "Run expiry cleanup engine"),
    "palimpsest_write_checked": ("Memory", "Write with dedup check"),
    "palimpsest_dedup_review": ("Memory", "Review dedup queue"),
    "palimpsest_dedup_resolve": ("Memory", "Resolve dedup item"),
    "palimpsest_comment":     ("Memory", "Reply to a memory (thread)"),
    "palimpsest_thread":      ("Memory", "Read full reply thread"),
    "list_books":         ("Books", "List library books"),
    "get_book_toc":       ("Books", "Table of contents"),
    "read_book_page":     ("Books", "Read a book page"),
    "search_book":        ("Books", "Semantic search in books"),
    "get_reading_context":("Books", "Load reading context"),
    "save_annotation":    ("Books", "Save highlight/annotation"),
    "book_reflection":    ("Books", "Record book reflection → L1"),
    "daily_life_read":    ("Screentime", "Read daily life journal"),
    "daily_life_write":   ("Screentime", "Write a journal entry"),
    "daily_life_generate":("Screentime", "AI-generate today's entry"),
    "character_state_get":("Character",  "Get character mood/fatigue/scene state"),
    "character_state_set":("Character",  "Update character state"),
    "random_event_roll":  ("Character",  "Roll a random life event"),
    "npc_update":         ("Character",  "Add/update NPC in social network"),
    "npc_list_all":       ("Character",  "List character's social network"),
    "notion_fetch_page":  ("Notion", "Fetch Notion page by URL or ID"),
    "notion_search":      ("Notion", "Search Notion workspace"),
    "notion_append_block":("Notion", "Append block to Notion page"),
    "amap_weather":       ("Environment", "Current weather via Amap API"),
    "amap_forecast":      ("Environment", "4-day forecast via Amap API"),
    "amap_route":         ("Environment", "Driving route + traffic via Amap API"),
    "amap_geocode":       ("Environment", "Address → coordinates via Amap API"),
    "todoist_get_tasks":  ("Productivity", "Get Todoist tasks by filter"),
    "todoist_create_task":("Productivity", "Create a new Todoist task"),
    "todoist_complete_task":("Productivity","Mark Todoist task complete"),
    "todoist_update_task":("Productivity", "Update Todoist task content/due"),
}

@app.get("/admin/api/mcp/tools")
async def admin_mcp_tools(_=Depends(_require_key)):
    """List all registered MCP tools with group/description metadata."""
    from mcp.server import FastMCP
    tool_list = []
    for name, tool in _mcp._tool_manager._tools.items():
        group, desc_short = _MCP_GROUPS.get(name, ("Other", ""))
        doc = (tool.fn.__doc__ or "").strip().split(chr(10))[0][:120]
        tool_list.append({
            "name": name,
            "group": group,
            "description": desc_short or doc,
        })
    # Sort by group then name
    tool_list.sort(key=lambda x: (x["group"], x["name"]))
    groups = {}
    for t in tool_list:
        groups.setdefault(t["group"], []).append(t)
    return {"ok": True, "count": len(tool_list), "groups": groups}


@app.get("/admin/api/telegram/status")
async def telegram_admin_status(_=Depends(_require_key)):
    """Return current Telegram bot state: active agent, session length, configured flag."""
    import os as _tos
    chat_id      = _tos.getenv("TELEGRAM_CHAT_ID", "")
    default_a    = _tos.getenv("TELEGRAM_CHARACTER_ID", "chiaki")
    current_a    = _tg_agents.get(chat_id, default_a) if chat_id else default_a
    session_key  = f"tg_{chat_id}_{current_a}"
    session_len  = len(_tg_sessions.get(session_key, []))
    configured   = bool(_tos.getenv("TELEGRAM_BOT_TOKEN", ""))
    # collect all sessions and their lengths
    sessions_info = {k: len(v) for k, v in _tg_sessions.items() if v}
    return {
        "configured":       configured,
        "chat_id":          chat_id,
        "current_agent":    current_a,
        "default_agent":    default_a,
        "session_messages": session_len,
        "all_sessions":     sessions_info,
    }

@app.post("/admin/api/telegram/clear")
async def telegram_admin_clear(body: dict = None, _=Depends(_require_key)):
    """Clear the in-memory conversation buffer for the current chat+agent."""
    import os as _tos
    chat_id   = _tos.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID not set"}
    default_a = _tos.getenv("TELEGRAM_CHARACTER_ID", "chiaki")
    # Allow explicit agent_id via body; default = current
    req_agent = (body or {}).get("agent_id", "")
    agent_id  = req_agent or _tg_agents.get(chat_id, default_a)
    session_key = f"tg_{chat_id}_{agent_id}"
    _tg_sessions[session_key] = []
    return {"ok": True, "cleared": session_key}

@app.post("/admin/api/telegram/switch")
async def telegram_admin_switch(body: dict, _=Depends(_require_key)):
    """Switch the active agent for the default TELEGRAM_CHAT_ID and clear its buffer."""
    import os as _tos
    chat_id  = _tos.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID not set"}
    agent_id = (body or {}).get("agent_id", "")
    if not agent_id:
        return {"ok": False, "error": "agent_id required"}
    _tg_agents[chat_id] = agent_id
    _tg_sessions[f"tg_{chat_id}_{agent_id}"] = []
    return {"ok": True, "switched_to": agent_id}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram Bot updates (webhook).

    Commands:
      /start [agent_id]  – connect (optionally specify agent, default=TELEGRAM_CHARACTER_ID)
      /switch <agent_id> – switch to a different agent, clears buffer
      /list              – list available agents
      /clear             – clear conversation buffer for current agent
    Plain text is routed to the current agent (character or agent type both work).
    """
    import os as _os3
    secret   = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    expected = _os3.getenv("TELEGRAM_WEBHOOK_SECRET", GATEWAY_API_KEY[:32])
    if secret != expected:
        raise HTTPException(403, "Invalid webhook secret")

    data = await request.json()
    msg  = data.get("message") or data.get("edited_message")
    if not msg:
        return {"ok": True}

    chat_id   = str(msg["chat"]["id"])
    text      = (msg.get("text") or "").strip()
    default_a = _os3.getenv("TELEGRAM_CHARACTER_ID", "chiaki")

    if not text:
        return {"ok": True}

    # ── Commands ──────────────────────────────────────────────────────────────
    if text.startswith("/"):
        parts = text.split()
        cmd   = parts[0].lower().split("@")[0]
        arg   = parts[1] if len(parts) > 1 else ""

        if cmd == "/start":
            agent_id = arg or _tg_agents.get(chat_id, default_a)
            _tg_agents[chat_id] = agent_id
            _tg_sessions[f"tg_{chat_id}_{agent_id}"] = []
            msg_text = (
                "\u2728 \u5df2\u8fde\u63a5\u5230 <b>" + agent_id + "</b>\n"
                "\u53d1\u6d88\u606f\u5f00\u59cb\u5bf9\u8bdd\uff0c\u6216\u7528 "
                "/list \u67e5\u770b\u6240\u6709\u53ef\u7528 agent\uff0c"
                "/switch &lt;\u540d\u5b57&gt; \u5207\u6362"
            )
            await _telegram_send(msg_text, chat_id=chat_id, parse_mode="HTML")
            return {"ok": True}

        if cmd == "/switch":
            if not arg:
                await _telegram_send("/switch \u7528\u6cd5\uff1a/switch &lt;agent_id&gt;",
                                     chat_id=chat_id, parse_mode="HTML")
                return {"ok": True}
            _tg_agents[chat_id] = arg
            _tg_sessions[f"tg_{chat_id}_{arg}"] = []
            switch_text = "\u2705 \u5df2\u5207\u6362\u5230 <b>" + arg + "</b>\uff08\u5bf9\u8bdd\u8bb0\u5f55\u5df2\u6e05\u7a7a\uff09"
            await _telegram_send(switch_text, chat_id=chat_id, parse_mode="HTML")
            return {"ok": True}

        if cmd == "/list":
            try:
                async with _db_pool.acquire() as _lc:
                    _rows = await _lc.fetch(
                        "SELECT agent_id, agent_type, notes FROM agent_settings ORDER BY agent_id"
                    )
                cur_agent = _tg_agents.get(chat_id, default_a)
                parts_list = ["<b>\u53ef\u7528 Agents\uff1a</b>"]
                for r in _rows:
                    marker = " \u25c4 \u5f53\u524d" if r["agent_id"] == cur_agent else ""
                    icon   = "\U0001f916" if r["agent_type"] == "agent" else "\u2728"
                    note   = "  <i>" + r["notes"][:30] + "</i>" if r.get("notes") else ""
                    parts_list.append(
                        icon + " <code>" + r["agent_id"] + "</code>"
                        " [" + (r["agent_type"] or "?") + "]" + note + marker
                    )
                parts_list.append("\n/switch &lt;agent_id&gt; \u5207\u6362")
                await _telegram_send("\n".join(parts_list), chat_id=chat_id, parse_mode="HTML")
            except Exception as _le:
                await _telegram_send("\u83b7\u53d6\u5217\u8868\u5931\u8d25: " + str(_le), chat_id=chat_id)
            return {"ok": True}

        if cmd == "/clear":
            agent_id = _tg_agents.get(chat_id, default_a)
            _tg_sessions[f"tg_{chat_id}_{agent_id}"] = []
            await _telegram_send("\U0001f5d1 \u5bf9\u8bdd\u7f13\u5b58\u5df2\u6e05\u7a7a", chat_id=chat_id)
            return {"ok": True}

        # Unknown command
        await _telegram_send("\u672a\u77e5\u6307\u4ee4\u3002\u53ef\u7528\uff1a/start /switch /list /clear",
                             chat_id=chat_id)
        return {"ok": True}

    # ── Normal message → route to current agent ───────────────────────────────
    agent_id    = _tg_agents.get(chat_id, default_a)
    session_key = f"tg_{chat_id}_{agent_id}"

    asyncio.create_task(_telegram_typing(chat_id))

    history  = _tg_sessions.get(session_key, [])
    messages = history + [{"role": "user", "content": text}]

    try:
        async with httpx.AsyncClient(timeout=90) as _hc:
            _resp = await _hc.post(
                "http://localhost:8000/v1/chat/completions",
                headers={"Authorization": f"Bearer {GATEWAY_API_KEY}",
                         "Content-Type": "application/json"},
                json={"agent_id": agent_id, "messages": messages,
                      "session_id": session_key, "stream": False},
            )
        if _resp.status_code != 200:
            raise RuntimeError(f"HTTP {_resp.status_code}: {_resp.text[:200]}")
        _rd   = _resp.json()
        reply = _rd["choices"][0]["message"]["content"]
    except Exception as _we:
        print(f"[telegram webhook] chat error: {_we}")
        await _telegram_send(f"⚠️ 出错了：{_we}", chat_id=chat_id)
        return {"ok": True}

    history.append({"role": "user",      "content": text})
    history.append({"role": "assistant", "content": reply})
    _tg_sessions[session_key] = history[-_TG_SESSION_MAXLEN:]

    await _telegram_send(reply, chat_id=chat_id)
    return {"ok": True}


@app.post("/admin/api/mcp/bark-test")
async def admin_mcp_bark_test(body: dict, _=Depends(_require_key)):
    """Test Bark push: { title, body }"""
    result = await bark_push(
        title=body.get("title", "Test"),
        body=body.get("body", "Hello from Palimpsest"),
        group="system",
    )
    return {"ok": True, "result": result}


@app.post("/admin/api/mcp/daily-generate")
async def admin_daily_generate(body: dict, _=Depends(_require_key)):
    """Trigger AI generation of a daily life entry."""
    result = await daily_life_generate(
        agent_id=body.get("agent_id", "default"),
        date=body.get("date", ""),
        extra_prompt=body.get("extra_prompt", ""),
    )
    return {"ok": True, "result": result}


@app.get("/admin/api/mcp/intiface-devices")
async def admin_mcp_intiface_devices(_=Depends(_require_key)):
    """List Intiface devices."""
    result = await intiface_list_devices()
    return {"ok": True, "result": result}

