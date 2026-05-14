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
from typing import AsyncGenerator, Optional

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
    PointIdsList, PointStruct, VectorParams, PayloadSchemaType,
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
    accumulate_state as _accumulate_state,
    push_log_write as _push_log_write,
    chain_event_schedule as _chain_schedule,
    chain_events_due as _chain_events_due,
    chain_event_mark_fired as _chain_mark_fired,
    event_roll as _event_roll, event_list as _event_list,
    event_add as _event_add, event_delete as _event_delete, event_update as _event_update,
    npc_list as _npc_list, npc_get as _npc_get,
    npc_upsert as _npc_upsert, npc_delete as _npc_delete,
)
from memory_db import memory_update as _mem_update, memory_delete as _mem_delete
from memory_db import memory_wakeup as _mem_wakeup, memory_surface as _mem_surface
from memory_db import memory_stats as _mem_stats
from memory_db import memory_get_history as _mem_history, memory_rollback as _mem_rollback
from memory_db import backup_db as _mem_backup_db
from memory_db import (
    music_history_add as _music_hist_add,
    music_history_get_recent_ids as _music_hist_recent,
    music_history_set_reaction as _music_hist_react,
    music_history_list as _music_hist_list,
)
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
from memory_db import daily_update as _daily_update
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
    """Call LLM for background tasks (distillation, auto-extract).

    Primary provider: nvidia-llm (DISTILL_MODEL override + fallback chain).
    Secondary fallback: deepseek official API (stable, always available).
    Raises RuntimeError only if ALL providers fail.
    """
    import os as _os
    override = _os.getenv("DISTILL_MODEL", "").strip()
    nvidia_models = (
        [override] + [m for m in _CHEAP_LLM_MODELS if m != override]
        if override else _CHEAP_LLM_MODELS
    )

    async with _db_pool.acquire() as conn:
        nvidia_row = await conn.fetchrow(
            "SELECT base_url, api_key FROM providers WHERE name='nvidia-llm' LIMIT 1"
        )
        ds_row = await conn.fetchrow(
            "SELECT base_url, api_key FROM providers WHERE name='deepseek' LIMIT 1"
        )

    last_err = ""

    # ── Try NVIDIA NIM models first (timeout 60s each) ─────────────────────────
    if nvidia_row:
        base = nvidia_row["base_url"].rstrip("/")
        hdrs = {"Authorization": "Bearer " + nvidia_row["api_key"], "Content-Type": "application/json"}
        for model in nvidia_models:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
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

    # ── Fallback: DeepSeek official API ───────────────────────────────────────
    if ds_row:
        try:
            base = ds_row["base_url"].rstrip("/")
            hdrs = {"Authorization": "Bearer " + ds_row["api_key"], "Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    base + "/chat/completions", headers=hdrs,
                    json={"model": "deepseek-chat",
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.3, "max_tokens": 2000},
                )
            if resp.is_success:
                print("[cheap_llm] using deepseek fallback ✓", flush=True)
                return resp.json()["choices"][0]["message"]["content"].strip()
            last_err = f"deepseek: HTTP {resp.status_code} {resp.text[:200]}"
            print(f"[cheap_llm] {last_err}", flush=True)
        except Exception as _e:
            last_err = f"deepseek: {type(_e).__name__}: {_e}"
            print(f"[cheap_llm] {last_err}", flush=True)

    raise RuntimeError(f"[cheap_llm] All providers failed. Last error: {last_err}")


# ─────────────────────────────────────────────────────────────────────────────
# P0.1  API Route Separation (Notion spec §0)
# Three independent LLM routes: conversation / proactive_push / analyzer
# Stored in user_config key 'api_routes' as JSON
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_API_ROUTES: dict[str, dict] = {
    "proactive_push": {
        "purpose": "主动推送消息生成（早安/触景生情/屏幕时间吐槽等）",
        "provider": "nvidia-llm",
        "model": "",          # empty = use _CHEAP_LLM_MODELS[0]
        "fallback_chain": [],
    },
    "analyzer": {
        "purpose": "后台分析任务（状态分析/日程捕获/重要性判断/反向识别）",
        "provider": "nvidia-llm",
        "model": "",
        "fallback_chain": [],
    },
}


async def _call_llm_route(route_name: str, prompt: str, sys_prompt: str = "") -> str:
    """Call LLM using a named API route (proactive_push / analyzer).

    Route config is read from user_config['api_routes']. Falls back to
    _call_llm_cheap() if route not configured or call fails.
    """
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='api_routes'")
        routes = {}
        if row and row["value"]:
            rv = row["value"]
            routes = rv if isinstance(rv, dict) else __import__("json").loads(rv)
    except Exception:
        routes = {}

    route = routes.get(route_name) or _DEFAULT_API_ROUTES.get(route_name, {})
    provider_name = route.get("provider", "nvidia-llm")
    model_override = route.get("model", "").strip()

    # Build message list
    msgs: list[dict] = []
    if sys_prompt:
        msgs.append({"role": "system", "content": sys_prompt})
    msgs.append({"role": "user", "content": prompt})

    try:
        async with _db_pool.acquire() as conn:
            prow = await conn.fetchrow(
                "SELECT base_url, api_key FROM providers WHERE name=$1 LIMIT 1",
                provider_name,
            )
        if not prow:
            return await _call_llm_cheap(prompt)

        base = prow["base_url"].rstrip("/")
        hdrs = {"Authorization": "Bearer " + prow["api_key"], "Content-Type": "application/json"}

        models_to_try = (
            [model_override] + (route.get("fallback_chain") or [])
            if model_override else (_CHEAP_LLM_MODELS[:])
        )
        for model in models_to_try:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        base + "/chat/completions",
                        headers=hdrs,
                        json={"model": model, "messages": msgs, "max_tokens": 300, "temperature": 0.8},
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"].strip()
            except Exception:
                continue
        # fallback
        return await _call_llm_cheap(prompt)
    except Exception:
        return await _call_llm_cheap(prompt)


# ─────────────────────────────────────────────────────────────────────────────
# P0.2  Timezone Injection (Notion spec §1)
# ─────────────────────────────────────────────────────────────────────────────

async def build_time_context(agent_id: str = "") -> str:
    """Return [时间信息] block with user+char current times and timezone.

    Reads TIMEZONE_CONFIG from user_context in user_config.
    Falls back to Asia/Shanghai (user) if not configured.
    """
    import datetime as _dttz
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        try:
            from backports.zoneinfo import ZoneInfo  # type: ignore
        except ImportError:
            ZoneInfo = None

    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='user_context'")
        ucv = row["value"] if row else None
        uc  = (ucv if isinstance(ucv, dict) else __import__("json").loads(ucv)) if ucv else {}
    except Exception:
        uc = {}

    tz_cfg   = uc.get("timezone") or {}
    user_tz  = tz_cfg.get("user", {}).get("default", "Asia/Shanghai") if isinstance(tz_cfg.get("user"), dict) else str(tz_cfg.get("user", "Asia/Shanghai"))
    char_tz  = tz_cfg.get("character", {}).get("default", "Asia/Shanghai") if isinstance(tz_cfg.get("character"), dict) else str(tz_cfg.get("character", "Asia/Shanghai"))

    # Apply character travel override
    char_tz_cfg = tz_cfg.get("character", {}) if isinstance(tz_cfg.get("character"), dict) else {}
    travel_override = (char_tz_cfg.get("travel_mode") or {}).get("current_override") or ""
    if travel_override:
        char_tz = travel_override

    def _now_in(tz_name: str) -> _dttz.datetime:
        now_utc = _dttz.datetime.utcnow().replace(tzinfo=_dttz.timezone.utc)
        if ZoneInfo:
            try:
                return now_utc.astimezone(ZoneInfo(tz_name))
            except Exception:
                pass
        # fallback: crude UTC offset guessing for common zones
        _offsets = {"Asia/Shanghai": 8, "Asia/Tokyo": 9, "America/Los_Angeles": -7,
                    "America/New_York": -4, "Europe/London": 1, "UTC": 0}
        offset_h = _offsets.get(tz_name, 0)
        return now_utc + _dttz.timedelta(hours=offset_h)

    user_now = _now_in(user_tz)
    char_now = _now_in(char_tz)

    # Compute hour diff (approximate)
    _uo = {"Asia/Shanghai": 8, "Asia/Tokyo": 9, "America/Los_Angeles": -7,
           "America/New_York": -4, "Europe/London": 1, "UTC": 0}
    diff_h = _uo.get(user_tz, 8) - _uo.get(char_tz, 8)
    same_city = (user_tz == char_tz)
    relation_mode = "同城/同居" if same_city else "异地"

    lines = [
        "[时间信息]",
        f"你的当前时间：{char_now.strftime('%Y-%m-%d %H:%M')}（{char_tz}）",
        f"用户的当前时间：{user_now.strftime('%Y-%m-%d %H:%M')}（{user_tz}）",
    ]
    if diff_h != 0:
        direction = "快" if diff_h > 0 else "慢"
        lines.append(f"时差：用户比你{direction}{abs(diff_h):.0f}小时")
    lines.append(f"关系模式：{relation_mode}")
    return chr(10).join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# P0.3  Accumulator Helpers (Notion spec §4.3)
# ─────────────────────────────────────────────────────────────────────────────

# Thresholds that trigger a proactive message
_ACCUMULATOR_THRESHOLDS: dict[str, float] = {
    "miss_you":  10.0,
    "low_mood":   8.0,
}
# After-trigger reset values
_ACCUMULATOR_RESET: dict[str, float] = {
    "miss_you": 0.0,
    "low_mood": 3.0,   # low_mood doesn't fully clear
}
# After-trigger cooldown (seconds)
_ACCUMULATOR_COOLDOWN: dict[str, str] = {
    "miss_you": "miss_you_trigger",
    "low_mood": "low_mood_trigger",
}

# Global push control defaults (Notion spec §8.2)
_PUSH_CONTROL_DEFAULTS: dict = {
    "max_daily_proactive_messages": 3,
    "quiet_hours": ["01:00", "07:30"],   # user timezone
    "user_busy_override": True,
    "user_busy_timeout": 7200,
    "trigger_check_interval": 7200,    # changed to 2h
    "channels": {
        "default_channels": ["telegram", "bark"],
        "category_overrides": {
            "medication": ["bark"],    # medication reminders → bark only
        },
    },
}


async def _get_push_control() -> dict:
    """Read push_control from user_config, with defaults."""
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='push_control'")
        if row and row["value"]:
            rv = row["value"]
            cfg = rv if isinstance(rv, dict) else __import__("json").loads(rv)
            return {**_PUSH_CONTROL_DEFAULTS, **cfg}
    except Exception:
        pass
    return dict(_PUSH_CONTROL_DEFAULTS)


async def _check_quiet_hours(agent_id: str = "") -> bool:
    """Return True if we're inside the user's quiet hours (should NOT push).

    Uses user timezone from user_context; compares current time to quiet_hours window.
    """
    import datetime as _dtqh
    try:
        from zoneinfo import ZoneInfo as _ZI
    except ImportError:
        try:
            from backports.zoneinfo import ZoneInfo as _ZI  # type: ignore
        except ImportError:
            _ZI = None

    ctrl = await _get_push_control()
    quiet = ctrl.get("quiet_hours", ["01:00", "07:30"])
    if not quiet or len(quiet) < 2:
        return False

    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='user_context'")
        ucv = row["value"] if row else None
        uc  = (ucv if isinstance(ucv, dict) else __import__("json").loads(ucv)) if ucv else {}
        tz_cfg  = uc.get("timezone") or {}
        user_tz = (tz_cfg.get("user", {}).get("default", "Asia/Shanghai")
                   if isinstance(tz_cfg.get("user"), dict)
                   else str(tz_cfg.get("user", "Asia/Shanghai")))
    except Exception:
        user_tz = "Asia/Shanghai"

    now_utc = _dtqh.datetime.utcnow().replace(tzinfo=_dtqh.timezone.utc)
    if _ZI:
        try:
            now_local = now_utc.astimezone(_ZI(user_tz))
        except Exception:
            now_local = now_utc
    else:
        _offsets = {"Asia/Shanghai": 8, "Asia/Tokyo": 9, "America/Los_Angeles": -7,
                    "America/New_York": -4, "Europe/London": 1, "UTC": 0}
        now_local = now_utc + _dtqh.timedelta(hours=_offsets.get(user_tz, 8))

    def _parse_hm(s: str) -> _dtqh.time:
        h, m = s.split(":")
        return _dtqh.time(int(h), int(m))

    start = _parse_hm(quiet[0])
    end   = _parse_hm(quiet[1])
    t     = now_local.time().replace(second=0, microsecond=0)

    if start <= end:
        return start <= t <= end
    else:  # wraps midnight
        return t >= start or t <= end


async def _check_daily_push_limit(agent_id: str) -> bool:
    """Return True if today's proactive message count is under the configured limit.

    Counts entries in push_log for today (UTC) with sent=1.
    """
    from memory_db import get_db as _get_db2
    ctrl = await _get_push_control()
    limit = ctrl.get("max_daily_proactive_messages", 3)
    if limit <= 0:
        return True
    try:
        db = await _get_db2()
        import datetime as _dtdl
        today = _dtdl.datetime.utcnow().strftime("%Y-%m-%d")
        cur = await db.execute(
            "SELECT COUNT(*) FROM push_log WHERE agent_id=? AND sent=1 AND created_at >= ?",
            (agent_id, today + " 00:00:00")
        )
        row = await cur.fetchone()
        count = row[0] if row else 0
        return count < limit
    except Exception:
        return True


async def _push_send(
    msg: str,
    category: str = "default",
    title: str = "",
    parse_mode: str = "",
) -> bool:
    """Route a proactive push to the channels configured for `category`.

    Reads push_control.channels:
      default_channels     → ["telegram","bark"] (both by default)
      category_overrides   → {"medication": ["bark"], ...}

    bark also_telegram is always False here to avoid double telegram sends.
    Returns True if at least one channel succeeded.
    """
    import os as _osps
    ctrl = await _get_push_control()
    ch_cfg     = ctrl.get("channels", {})
    overrides  = ch_cfg.get("category_overrides", {})
    channels   = overrides.get(category) or ch_cfg.get("default_channels", ["telegram", "bark"])

    ok = False
    if "telegram" in channels:
        ok = await _telegram_send(msg, parse_mode=parse_mode) or ok
    if "bark" in channels and _osps.getenv("BARK_URL"):
        try:
            await bark_push(title or "通知", msg, also_telegram=False)
            ok = True
        except Exception as _bpe:
            print(f"[push_send] bark error ({category}): {_bpe}", flush=True)
    return ok


async def _check_user_outgoing_yesterday(agent_id: str) -> bool:
    """Return True if yesterday's conversations/activity mention '出门' (going out).

    Searches the last 36 h of palimpsest memories and recent activity events
    for outgoing-trip keywords.  Used to force weather push probability to 1.0.
    """
    import datetime as _dtou
    _GO_OUT_KW = ["出门", "要出去", "出去一下", "需要出门", "早点出门", "出行",
                  "外出", "要去", "去一趟", "要上班", "上班去", "有事出去"]
    try:
        # Check last 36 h of memories
        cutoff = (_dtou.datetime.utcnow() - _dtou.timedelta(hours=36)).isoformat()
        from memory_db import get_db as _gdb_ou
        db = await _gdb_ou()
        cur = await db.execute(
            "SELECT content FROM memories WHERE agent_id=? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 30",
            (agent_id, cutoff)
        )
        rows = await cur.fetchall()
        combined = " ".join(r[0] or "" for r in rows)
        if any(kw in combined for kw in _GO_OUT_KW):
            return True
    except Exception:
        pass
    try:
        # Also check activity_events
        acts = await _act_recent(agent_id, hours=36)
        acts_text = " ".join(str(a.get("note","")) + " " + str(a.get("category","")) for a in acts)
        if any(kw in acts_text for kw in _GO_OUT_KW):
            return True
    except Exception:
        pass
    return False


RECENT_DAYS     = 30
BACKUP_DIR      = Path("/app/backups")
MAX_BACKUPS     = 7
BOOK_COLLECTION        = "book_chunks"
QDRANT_MEM_COLLECTION  = "memories"      # L1-L4 semantic index
QDRANT_CONV_COLLECTION = "conversations" # exchange-pair RAG history
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
    # B6 Layer 2: validate layer before write
    layer, importance, _vnote = _validate_memory_layer(content.strip(), layer, importance)
    try:
        result = await _mem_write(
            agent_id=agent_id, content=content.strip(),
            layer=layer, type_=type, importance=importance,
            tags=tag_list, source=source, parent_id=parent_id,
        )
        # B6 Layer 3: L1 writes → confirmed=0 (pending manual confirmation)
        if layer == "L1":
            try:
                from memory_db import get_db as _gdb_l1v
                _dbl1 = await _gdb_l1v()
                await _dbl1.execute("UPDATE memories SET confirmed=0 WHERE id=?", (result["id"],))
                await _dbl1.commit()
                result["confirmed"] = 0
            except Exception as _cfe:
                print(f"[palimpsest_write] L1 confirm=0 err: {_cfe}")
        # Sync to Qdrant (non-blocking)
        asyncio.create_task(_sync_memory_to_qdrant({**result, "agent_id": agent_id}))
        mid = result["id"]
        ml = result["layer"]
        mt = result["type"]
        mi = result["importance"]
        mtags = result["tags"]
        _note = f" [{_vnote}]" if _vnote else ""
        _conf_note = " ⏳pending-L1-confirmation" if layer == "L1" else ""
        return "Memory saved.\n  id: " + mid + "\n  layer: " + ml + _note + _conf_note + ", type: " + mt + ", importance: " + str(mi) + "\n  tags: " + str(mtags)
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
    layer: str = "",
) -> str:
    """Semantic search over Palimpsest memories via Qdrant vector search.
    Falls back to FTS5 keyword search if Qdrant is unavailable.

    Args:
        query:    Search query (natural language or keywords).
        agent_id: Which agent.
        limit:    Max results (default 10).
        layer:    Optional filter: L1 | L2 | L3 | L4 (empty = all layers).
    """
    # ── Qdrant vector search (primary) ────────────────────────────────────────
    if _qdrant:
        try:
            vec = await _embed_mem(query)
            if vec:
                must_filters = [
                    FieldCondition("agent_id",  MatchValue(value=agent_id)),
                    FieldCondition("archived",   MatchValue(value=0)),
                    FieldCondition("confirmed",  MatchValue(value=1)),
                ]
                if layer:
                    must_filters.append(FieldCondition("layer", MatchValue(value=layer)))
                hits = _qdrant.search(
                    collection_name=QDRANT_MEM_COLLECTION,
                    query_vector=vec,
                    query_filter=Filter(must=must_filters),
                    limit=limit,
                    score_threshold=0.40,
                )
                if hits:
                    lines = [f"Found {len(hits)} memories (vector search):"]
                    for h in hits:
                        pl = h.payload
                        mid = pl.get("mem_id", "?")
                        lines.append(
                            f"[{mid[:8]}] [{pl.get('layer','?')}] score={h.score:.2f}"
                            f" | {pl.get('original_text','')[:120]}"
                        )
                        # Touch (best-effort, non-blocking)
                        asyncio.create_task(_mem_read(mid, touch=True))
                    return "\n".join(lines)
        except Exception as _qe:
            print(f"[palimpsest_search] qdrant err: {_qe}")

    # ── FTS5 fallback ─────────────────────────────────────────────────────────
    try:
        results = await _mem_search(agent_id=agent_id, query=query, limit=limit)
    except Exception as e:
        return "Search error: " + str(e)
    if not results:
        return "No memories found for: " + query
    lines = ["Found " + str(len(results)) + " memories (keyword fallback):"]
    for r in results:
        tags_str = ", ".join(r["tags"]) if r["tags"] else ""
        line = "[" + r["id"][:8] + "] [" + r["layer"] + "] imp=" + str(r["importance"])
        line += " | " + r["content"][:120]
        if tags_str:
            line += " #" + tags_str
        lines.append(line)
    return "\n".join(lines)


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
    # Re-sync to Qdrant (content or metadata changed)
    asyncio.create_task(_sync_memory_to_qdrant(result))
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
    # Sync Qdrant
    if _qdrant:
        try:
            _qid = _mem_id_to_qdrant(memory_id)
            if hard_delete:
                _qdrant.delete(QDRANT_MEM_COLLECTION,
                               points_selector=PointIdsList(points=[_qid]))
            else:
                _qdrant.set_payload(QDRANT_MEM_COLLECTION,
                                    payload={"archived": 1},
                                    points_selector=PointIdsList(points=[_qid]))
        except Exception as _qde:
            print(f"[qdrant_del] {_qde}")
    if hard_delete:
        return "Permanently deleted: " + memory_id[:8]
    return "Archived → Trash: " + memory_id[:8]


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
    mood_valence: float = None,
    mood_energy: float = None,
    mood_score: int = None,
    mood_label: str = "",
    fatigue: int = None,
    scene: str = "",
    scene_note: str = "",
    cooldown_minutes: int = None,
) -> str:
    """Update character state fields. Only provided fields are changed.

    Mood uses 2-D system (执行文档 §4.1):
        mood_valence: -1.0 (negative) to +1.0 (positive)
        mood_energy:  -1.0 (low energy) to +1.0 (high energy)
    Quadrant labels auto-derived:
        (+v,+e)=开心兴奋  (-v,+e)=烦躁焦虑  (+v,-e)=平静满足  (-v,-e)=低落疲惫
    mood_score (legacy -100~+100) still accepted for backward compat.

    Args:
        agent_id:          Target agent.
        mood_valence:      -1.0 to +1.0 (valence dimension).
        mood_energy:       -1.0 to +1.0 (energy dimension).
        mood_score:        Legacy: -100 to +100. Sets valence=score/100 if valence not given.
        mood_label:        Override label (leave blank to auto-derive from valence+energy).
        fatigue:           0 (fresh) to 100 (exhausted).
        scene:             daily | long_distance | cohabitation
        scene_note:        Free-text scene context.
        cooldown_minutes:  Minutes between messages. 0 = no cooldown.
    """
    from memory_db import _mood_label_from_2d as _mld
    kwargs = {}
    # Resolve valence/energy from either 2D inputs or legacy mood_score
    if mood_valence is not None:
        kwargs["mood_valence"] = max(-1.0, min(1.0, float(mood_valence)))
    elif mood_score is not None:
        kwargs["mood_valence"] = max(-1.0, min(1.0, mood_score / 100.0))
    if mood_energy is not None:
        kwargs["mood_energy"] = max(-1.0, min(1.0, float(mood_energy)))

    # Derive mood_label and mood_score from 2D if not explicitly given
    v = kwargs.get("mood_valence")
    e = kwargs.get("mood_energy")
    if v is not None or e is not None:
        # Need current state to fill in whichever dimension wasn't provided
        _st = await _state_get(agent_id)
        fv = v if v is not None else float(_st.get("mood_valence") or 0.0)
        fe = e if e is not None else float(_st.get("mood_energy")  or 0.0)
        kwargs["mood_score"] = int(fv * 100)
        kwargs["mood_label"] = mood_label or _mld(fv, fe)
    elif mood_label:
        kwargs["mood_label"] = mood_label
    if mood_score is not None and "mood_score" not in kwargs:
        kwargs["mood_score"] = max(-100, min(100, mood_score))

    if fatigue          is not None: kwargs["fatigue"]           = max(0, min(100, fatigue))
    if scene:                        kwargs["scene"]             = scene
    if scene_note       is not None: kwargs["scene_note"]        = scene_note
    if cooldown_minutes is not None: kwargs["cooldown_minutes"]  = max(0, cooldown_minutes)
    s = await _state_set(agent_id, **kwargs)
    v_out = s.get("mood_valence") or 0.0
    e_out = s.get("mood_energy")  or 0.0
    return (f"State updated for {agent_id}: "
            f"mood={s['mood_label']} (v={v_out:+.2f} e={e_out:+.2f}), "
            f"fatigue={s['fatigue']}, scene={s['scene']}")


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
    # Apply event mood/accumulator effects and schedule chain if defined
    try:
        import json as _jevt
        mood_eff = _jevt.loads(evt.get("mood_effect") or "{}") if isinstance(evt.get("mood_effect"), str) else (evt.get("mood_effect") or {})
        acc_eff  = _jevt.loads(evt.get("accumulator_effect") or "{}") if isinstance(evt.get("accumulator_effect"), str) else (evt.get("accumulator_effect") or {})
        if mood_eff or acc_eff:
            await _accumulate_state(agent_id, {**mood_eff, **acc_eff})
            await _check_accumulator_thresholds(agent_id)
        await _maybe_schedule_chain(agent_id, evt)
    except Exception as _evte:
        print(f"[event_roll] effects/chain err: {_evte}")
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
        _v = _uc_row["value"] if _uc_row else None
        _uc = (_v if isinstance(_v, dict) else __import__("json").loads(_v)) if _v else {}
        _city = (_uc.get("location") or {}).get("city", "")
        _w = await amap_weather(_city)  # empty string → amap uses IP (fallback)
        if _w and not _w.startswith("Error") and not _w.startswith("Weather error"):
            _weather_ctx = _w
    except Exception:
        pass

    # 5. Daily skeleton config (occupation, habits) from user_config — agent-specific, fallback global
    _skeleton_ctx = ""
    try:
        async with _db_pool.acquire() as _sc:
            _sk_row = await _sc.fetchrow(
                "SELECT value FROM user_config WHERE key=$1",
                f"daily_skeleton:{agent_id}")
            if not _sk_row:
                _sk_row = await _sc.fetchrow(
                    "SELECT value FROM user_config WHERE key='daily_skeleton'")
        if _sk_row:
            _skv = _sk_row["value"]
            _sk = (_skv if isinstance(_skv, dict) else __import__("json").loads(_skv)) if _skv else {}
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
        await _mem_write_smart_vec(
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
        result = await _mem_write_smart_vec(
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
            "ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS mcp_stubborn_compat BOOLEAN DEFAULT FALSE"
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
    asyncio.create_task(_auto_cleanup_loop())        # Palimpsest auto-cleanup
    asyncio.create_task(_nightly_character_loop())   # Nightly daily-life gen
    asyncio.create_task(_nightly_agent_loop())       # Nightly agent project maintenance
    asyncio.create_task(_nightly_dream_loop())       # Nightly dream: L4→L3 + GitHub Obsidian
    asyncio.create_task(_morning_push_loop())        # Morning push (char tz 08:15-08:30)
    asyncio.create_task(_evening_push_loop())        # Evening goodnight (char tz 22:30-23:30)
    asyncio.create_task(_chain_event_loop())         # Chain event queue processor (5 min)
    asyncio.create_task(_silence_tracker_loop())     # 2h miss_you accumulator
    asyncio.create_task(_news_daily_loop())          # Daily RSS news fetch & cache
    asyncio.create_task(_news_standalone_push_loop())  # 10% standalone news share (§13.2)
    asyncio.create_task(_health_monitor_loop())      # Health metrics monitor
    asyncio.create_task(_weekly_backup_verify_loop())  # Weekly Monday R2 backup integrity check
    asyncio.create_task(_music_recommend_loop())       # Music recommendation ~2-3x/week
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

    # memories collection — L1-L4 semantic index
    if not _qdrant.collection_exists(QDRANT_MEM_COLLECTION):
        _qdrant.create_collection(
            QDRANT_MEM_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
    for _mf, _ms in [
        ("agent_id",  PayloadSchemaType.KEYWORD),
        ("layer",     PayloadSchemaType.KEYWORD),
        ("confirmed", PayloadSchemaType.INTEGER),
        ("archived",  PayloadSchemaType.INTEGER),
    ]:
        try:
            _qdrant.create_payload_index(QDRANT_MEM_COLLECTION, _mf, _ms)
        except Exception:
            pass

    # conversations collection — exchange-pair RAG
    if not _qdrant.collection_exists(QDRANT_CONV_COLLECTION):
        _qdrant.create_collection(
            QDRANT_CONV_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
    for _cf, _cs in [
        ("agent_id",        PayloadSchemaType.KEYWORD),
        ("conversation_id", PayloadSchemaType.KEYWORD),
    ]:
        try:
            _qdrant.create_payload_index(QDRANT_CONV_COLLECTION, _cf, _cs)
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


# ── Memory-specific embedding (non-fatal wrapper) ─────────────────────────────
async def _embed_mem(text: str) -> list[float] | None:
    """Embed text for memory/conversation indexing. Returns None on any failure."""
    try:
        return await _embed(text[:1500], input_type="passage")
    except Exception as _ee:
        print(f"[embed_mem] {_ee}")
        return None


def _mem_id_to_qdrant(mem_id: str) -> str:
    """Deterministically convert any string ID to a valid UUID for Qdrant."""
    try:
        uuid.UUID(mem_id)
        return mem_id
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, mem_id))


async def _sync_memory_to_qdrant(mem: dict) -> None:
    """Upsert one memory record into Qdrant memories collection (best-effort)."""
    if not _qdrant:
        return
    vec = await _embed_mem(mem.get("content", ""))
    if not vec:
        return
    try:
        _qdrant.upsert(
            collection_name=QDRANT_MEM_COLLECTION,
            points=[PointStruct(
                id=_mem_id_to_qdrant(mem["id"]),
                vector=vec,
                payload={
                    "mem_id":        mem["id"],
                    "agent_id":      mem.get("agent_id", ""),
                    "layer":         mem.get("layer", "L4"),
                    "type":          mem.get("type", "diary"),
                    "importance":    int(mem.get("importance", 3)),
                    "confirmed":     int(mem.get("confirmed", 1)),
                    "archived":      int(mem.get("archived", 0)),
                    "original_text": mem.get("content", ""),
                    "created_at":    str(mem.get("created_at", "")),
                },
            )],
        )
    except Exception as _ue:
        print(f"[qdrant_mem] upsert err: {_ue}")


# ── B6: L1 write-guard (code validation, 2nd defence layer) ───────────────────
import re as _re

_L1_TIME_PATS = [
    r'\d{1,2}月\d{1,2}[日号]', r'最近', r'目前', r'正在', r'这几天', r'这段时间',
    r'上周', r'这周', r'下周', r'昨天', r'今天', r'明天', r'上个月', r'这个月',
]
_L1_ONGOING_PATS = [
    r'正处于', r'正在经历', r'正在进行', r'需要处理', r'尚未解决', r'持续中',
]


def _validate_memory_layer(content: str, layer: str, importance: int) -> tuple[str, int, str]:
    """Auto-downgrade L1 → L2 when hard rules are violated.
    Returns (final_layer, final_importance, note_string).
    """
    if layer != "L1":
        return layer, importance, ""
    # Rule 1: contains near-term time markers
    if any(_re.search(p, content) for p in _L1_TIME_PATS):
        return "L2", min(importance, 4), "auto_downgraded:time_marker"
    # Rule 2: too long (L1 must be a short factual statement)
    if len(content) > 100:
        return "L2", min(importance, 4), "auto_downgraded:too_long"
    # Rule 3: describes an ongoing/in-progress state
    if any(_re.search(p, content) for p in _L1_ONGOING_PATS):
        return "L2", min(importance, 4), "auto_downgraded:ongoing_state"
    return "L1", importance, ""


# ── Conversation exchange-pair chunker ────────────────────────────────────────
def _chunk_exchange_pairs(messages: list) -> list[dict]:
    """Produce [{chunk_text, user_text, assistant_text}] from a message list."""
    cleaned = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        content = str(content).strip()
        if len(content) < 15 or role in ("tool_use", "tool_result"):
            continue
        # Skip injected system context blocks
        if role == "system" and (
            msg.get("_wb") or
            any(kw in content for kw in ("[主记忆", "[用户信息]", "[Relevant memories", "[Character context]"))
        ):
            continue
        cleaned.append({"role": role, "content": content})

    chunks: list[dict] = []
    i = 0
    while i < len(cleaned):
        if cleaned[i]["role"] != "user":
            i += 1
            continue
        user_text = cleaned[i]["content"]
        assistant_text = ""
        if i + 1 < len(cleaned) and cleaned[i + 1]["role"] == "assistant":
            assistant_text = cleaned[i + 1]["content"][:300]
            i += 2
        else:
            i += 1
        chunk_text = f"用户：{user_text}"
        if assistant_text:
            chunk_text += f"\n回复：{assistant_text}"
        chunks.append({
            "chunk_text":     chunk_text,
            "user_text":      user_text,
            "assistant_text": assistant_text,
        })
    return chunks


async def _ingest_conv_to_qdrant(
    agent_id: str, messages: list, conversation_id: str = "",
) -> None:
    """Embed exchange pairs and upsert into Qdrant conversations collection."""
    if not _qdrant:
        return
    chunks = _chunk_exchange_pairs(messages)
    if not chunks:
        return
    now_str = datetime.utcnow().isoformat()
    for chunk in chunks:
        try:
            vec = await _embed_mem(chunk["chunk_text"])
            if vec:
                _qdrant.upsert(
                    collection_name=QDRANT_CONV_COLLECTION,
                    points=[PointStruct(
                        id=str(uuid.uuid4()),
                        vector=vec,
                        payload={
                            "agent_id":        agent_id,
                            "conversation_id": conversation_id,
                            "source":          "gateway",
                            "timestamp":       now_str,
                            "user_text":       chunk["user_text"],
                            "assistant_text":  chunk["assistant_text"],
                            "chunk_text":      chunk["chunk_text"],
                        },
                    )],
                )
        except Exception as _ce:
            print(f"[conv_ingest] {_ce}")


async def _mem_write_smart_vec(
    agent_id: str,
    content: str,
    layer: str = "L4",
    type_: str = "diary",
    importance: int = 3,
    tags: list | None = None,
    source: str = "",
    parent_id: str = "",
) -> dict:
    """Wrapper around _mem_write_smart that pre-fetches Qdrant cosine match for dedup."""
    vm = await _qdrant_dedup_match(content, agent_id)
    return await _mem_write_smart(
        agent_id=agent_id, content=content, layer=layer,
        type_=type_, importance=importance, tags=tags,
        source=source, parent_id=parent_id,
        vector_match=vm,
    )


async def _qdrant_dedup_match(content: str, agent_id: str) -> dict | None:
    """Cosine nearest-neighbor lookup for dedup.
    Returns {id, content, score} of the closest confirmed, non-archived memory, or None.
    """
    if not _qdrant:
        return None
    try:
        vec = await _embed_mem(content)
        if not vec:
            return None
        hits = _qdrant.search(
            collection_name=QDRANT_MEM_COLLECTION,
            query_vector=vec,
            limit=1,
            with_payload=True,
            query_filter=Filter(must=[
                FieldCondition(key="agent_id",  match=MatchValue(value=agent_id)),
                FieldCondition(key="confirmed", match=MatchValue(value=1)),
                FieldCondition(key="archived",  match=MatchValue(value=0)),
            ]),
        )
        if not hits:
            return None
        h = hits[0]
        return {
            "id":      h.payload.get("mem_id", ""),
            "content": h.payload.get("original_text", ""),
            "score":   h.score,
        }
    except Exception as _qde:
        print(f"[qdrant_dedup] {_qde}")
        return None


async def _build_rag_context(user_query: str, agent_id: str) -> str:
    """Semantic search over memories + conversation history.
    Returns formatted context string, or '' if nothing relevant / Qdrant unavailable.
    """
    if not _qdrant or not user_query:
        return ""
    vec = await _embed_mem(user_query)
    if not vec:
        return ""

    parts: list[str] = []

    # 1. L1-L4 memories (confirmed, not archived)
    try:
        mem_hits = _qdrant.search(
            collection_name=QDRANT_MEM_COLLECTION,
            query_vector=vec,
            query_filter=Filter(must=[
                FieldCondition("agent_id",  MatchValue(value=agent_id)),
                FieldCondition("confirmed", MatchValue(value=1)),
                FieldCondition("archived",  MatchValue(value=0)),
            ]),
            limit=5,
            score_threshold=0.50,
        )
        if mem_hits:
            parts.append("[语义记忆]")
            for h in mem_hits:
                pl = h.payload
                parts.append(f"- [{pl.get('layer','?')}] {pl.get('original_text','')[:150]}")
    except Exception as _me:
        print(f"[rag] mem search: {_me}")

    # 2. Conversation history
    try:
        conv_hits = _qdrant.search(
            collection_name=QDRANT_CONV_COLLECTION,
            query_vector=vec,
            query_filter=Filter(must=[
                FieldCondition("agent_id", MatchValue(value=agent_id)),
            ]),
            limit=3,
            score_threshold=0.55,
        )
        if conv_hits:
            parts.append("[相关对话]")
            for h in conv_hits:
                parts.append(f"- {h.payload.get('chunk_text','')[:200]}")
    except Exception as _ce:
        print(f"[rag] conv search: {_ce}")

    return "\n".join(parts)


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

        "━━ RULE 3 — L1 HARD GATE (violation = classification error) ━━\n"
        "STOP before placing ANYTHING in L1. Run this checklist:\n"
        "  ❌ Contains dates/time words (\"4月\",\"最近\",\"目前\",\"正在\",\"这周\") → NOT L1, use L2 or L3\n"
        "  ❌ Content > 100 characters → NOT L1 (L1 must be a single short factual statement)\n"
        "  ❌ Describes ongoing/in-progress state (\"正处于\",\"尚未解决\",\"需要处理\") → NOT L1\n"
        "  ❌ Describes a single event → NOT L1 (use L3)\n"
        "  ✅ L1 only: permanent facts with NO time limit (name, core trait, relationship milestone)\n"
        "  ✅ Judgment order: First ask 'Is this NOT L1?' Then classify L2/L3/L4.\n"
        "  ✅ If memory has both a trait AND a specific event → split into 2 items: trait→L1/L2, event→L3\n\n"
        "━━ RULE 4 — LAYER CLASSIFICATION ━━\n"
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

        "━━ RULE 5 — TRACK CLASSIFICATION ━━\n"
        "Classify the primary nature of this conversation:\n"
        "  emotional — reflection, identity, relationships, philosophy, roleplay\n"
        "  practical — tool use, tasks, project work, technical problem-solving\n"
        "  mixed     — significant portions of both tracks present\n"
        "If track is 'practical' or 'mixed': identify the ongoing project/goal.\n"
        "  project.name must be a concise unique identifier (≤40 chars).\n"
        "  project.goal must be a single clear sentence describing the objective.\n\n"
        "━━ SELF-CHECK (run before outputting JSON) ━━\n"
        "For each item in L1: does it contain a time word? Is it >100 chars? Ongoing state?\n"
        "  If any YES → move to L2 or L3.\n"
        "Does any item combine a personality trait + a specific event in one sentence?\n"
        "  If YES → split into two items (trait→L1/L2, event→L3).\n\n"
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
                    # B6 Layer 2: code-level validation before write
                    v_layer, v_imp, v_note = _validate_memory_layer(text, layer, imp)
                    if v_note:
                        print(f"[distill] L1 downgrade: {v_note} | {text[:60]}")
                    mem_type = "project" if (v_layer == "L2" and _track in ("practical", "mixed")) else "diary"
                    _written = await _mem_write_smart_vec(
                        agent_id=agent_id, content=text,
                        layer=v_layer, type_=mem_type, importance=v_imp,
                        tags=["distilled"], source="distill",
                    )
                    # Sync to Qdrant (non-blocking)
                    if isinstance(_written, dict) and _written.get("id"):
                        asyncio.create_task(_sync_memory_to_qdrant({**_written, "agent_id": agent_id}))
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
    ts    = datetime.utcnow().strftime("%Y%m%d_%H%M")
    fname = f"memory_backup_{ts}.json"
    raw   = json.dumps(data, ensure_ascii=False, indent=2)
    (BACKUP_DIR / fname).write_text(raw, encoding="utf-8")
    _trim_backups()
    # SQLite Palimpsest backup
    db_fname = ""
    try:
        db_fname = f"palimpsest_{ts}.db"
        await _mem_backup_db(str(BACKUP_DIR / db_fname))
        _trim_sqlite_backups()
    except Exception as _be:
        print(f"[palimpsest-backup error] {_be}")
    # R2 upload (best-effort)
    try:
        date_prefix = datetime.utcnow().strftime("%Y/%m/%d")
        await _r2_upload(f"daily/{date_prefix}/{fname}", raw.encode("utf-8"), "application/json")
        if db_fname:
            db_bytes = (BACKUP_DIR / db_fname).read_bytes()
            await _r2_upload(f"daily/{date_prefix}/{db_fname}", db_bytes, "application/octet-stream")
    except Exception as _r2e:
        print(f"[r2-backup] {_r2e}", flush=True)
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
                    await _check_proactive_triggers(aid)
                except Exception as ae:
                    print(f"[nightly] proactive_triggers({aid}) error: {ae}")
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
                # P2.3 Promise reminders (sets scene_note for morning push context)
                try:
                    await _check_promise_reminders(aid)
                except Exception as ae:
                    print(f"[nightly] promise_reminders({aid}) error: {ae}")
        except Exception as e:
            print(f"[nightly error] {e}")
        # Sleep 24 h
        await asyncio.sleep(24 * 3600)


# ── Music Recommendation System (§14) ────────────────────────────────────────

_CLOUD_MUSIC_URL = "https://palimpsest.513129.xyz/cloud-music/mcp"

# Keywords that suggest the user is interested in music right now
_MUSIC_KEYWORDS = [
    "听歌", "音乐", "歌曲", "推荐首", "推荐一首", "好听", "歌单",
    "playlist", "什么歌", "哪首歌", "放首歌", "听什么",
    "bgm", "BGM", "耳机", "单曲循环",
]

# Mood → search keyword for low-valence states
_MOOD_MUSIC_QUERY = {
    "low":    ["治愈", "温暖", "陪伴", "轻柔"],
    "high":   None,   # use daily_recommend instead
    "normal": None,
}


def _sse_parse_json(text: str) -> dict | None:
    """Parse the first JSON object from an SSE response body (data: ... lines)."""
    import json as _jsn
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    return _jsn.loads(payload)
                except Exception:
                    continue
    return None


async def _cloud_music_call(tool_name: str, arguments: dict | None = None) -> str:
    """Call a tool on the cloud-music MCP server via streamable-http protocol.

    Handles FastMCP's SSE (text/event-stream) response format.
    Returns the text content from the tool response, or "" on failure.
    """
    if arguments is None:
        arguments = {}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as _mc:
            # Step 1: initialize session → get session ID from response header
            init_r = await _mc.post(_CLOUD_MUSIC_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "memory-gateway", "version": "1.0"},
                },
            })
            # session ID is case-insensitive in headers
            session_id = (
                init_r.headers.get("mcp-session-id")
                or init_r.headers.get("Mcp-Session-Id")
                or ""
            )

            hdrs2 = {**headers}
            if session_id:
                hdrs2["Mcp-Session-Id"] = session_id

            # Step 2: notifications/initialized (202, body may be empty)
            try:
                await _mc.post(_CLOUD_MUSIC_URL, headers=hdrs2, json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                })
            except Exception:
                pass

            # Step 3: call tool, parse SSE response
            tool_r = await _mc.post(_CLOUD_MUSIC_URL, headers=hdrs2, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            data = _sse_parse_json(tool_r.text) or {}
            if "result" in data:
                content = data["result"].get("content", [])
                if content:
                    return content[0].get("text", "")
            if "error" in data:
                print(f"[cloud_music] tool error: {data['error']}", flush=True)
    except Exception as _cme:
        print(f"[cloud_music] call error: {_cme}", flush=True)
    return ""


def _music_keyword_in_text(messages: list) -> bool:
    """Return True if the last 2 user messages contain music-related keywords."""
    user_msgs = [m.get("content", "") for m in messages if m.get("role") == "user"]
    text = " ".join(user_msgs[-2:]).lower()
    return any(kw in text for kw in _MUSIC_KEYWORDS)


def _parse_songs_from_response(text: str) -> list[dict]:
    """Parse songs list from cloud_music_get_daily_recommend / search text response.

    Expected format per line: N. 歌名 - 歌手 (ID: XXXXXX)
    Returns list of {"id": str, "name": str, "artist": str}
    """
    import re as _re_music
    songs = []
    for line in text.splitlines():
        m = _re_music.search(r'(\d+)\.\s*(.+?)\s*-\s*(.+?)\s*\(ID:\s*(\d+)\)', line)
        if m:
            songs.append({"name": m.group(2).strip(), "artist": m.group(3).strip(), "id": m.group(4).strip()})
    return songs


async def _music_pick_and_send(
    agent_id: str,
    trigger_mode: str = "scheduled",
    mood_tag: str = "",
    force: bool = False,
) -> bool:
    """Core music recommendation: fetch songs → filter history → generate char message → send.

    Returns True if message was sent.
    """
    import random as _rndm

    # Fetch candidates depending on mode
    if trigger_mode == "mood_low" and mood_tag:
        # Comfort music: search for calming/healing songs
        query = _rndm.choice(_MOOD_MUSIC_QUERY["low"])
        raw = await _cloud_music_call("cloud_music_search", {"keyword": query})
    else:
        # Scheduled / keyword / mood_high: use daily recommendations
        raw = await _cloud_music_call("cloud_music_get_daily_recommend")

    if not raw:
        print(f"[music] cloud_music_call returned empty for {agent_id}", flush=True)
        return False

    songs = _parse_songs_from_response(raw)
    if not songs:
        print(f"[music] no songs parsed from response", flush=True)
        return False

    # Filter recently recommended songs (past 30 days)
    recent_ids = await _music_hist_recent(agent_id, days=30)
    fresh = [s for s in songs if s["id"] not in recent_ids]
    if not fresh:
        fresh = songs  # all recently recommended → allow repeats

    song = _rndm.choice(fresh[:10])  # pick randomly from top-10 fresh songs

    # Get agent system prompt for character voice
    try:
        _as = await _get_agent_config(agent_id)
        _sp = (_as.get("system_prompt") or "").strip()[:300]
    except Exception:
        _sp = ""

    # Build trigger context hint
    mode_hint = {
        "scheduled":  "你今天想分享一首最近在听的歌",
        "keyword":    "用户提到了音乐，你想趁机分享一首你喜欢的",
        "mood_low":   "你想用音乐安慰对方，推荐一首治愈系的歌",
        "mood_high":  "你心情很好，想分享一首让你开心的歌",
    }.get(trigger_mode, "你想分享一首歌")

    char_ctx = f"你是以下角色：\n{_sp}\n\n" if _sp else ""
    prompt = (
        f"{char_ctx}{mode_hint}。\n"
        f"歌曲：《{song['name']}》- {song['artist']}\n"
        "用角色的口吻写一条分享这首歌的消息（1-2句，口语自然，不要解释太多，"
        "可以说为什么今天想到这首或听的感受），消息末尾附上歌名和歌手："
    )

    print(f"[music] calling LLM for {agent_id} song={song['name']}", flush=True)
    try:
        msg = (await _call_llm_route("proactive_push", prompt)).strip().strip("\"'")
        print(f"[music] LLM returned: {msg[:80]!r}", flush=True)
    except Exception as _le:
        print(f"[music] LLM error: {_le}", flush=True)
        return False

    if not msg:
        return False

    # Append song info if LLM forgot
    if song["name"] not in msg:
        msg += f"\n🎵 《{song['name']}》- {song['artist']}"

    # Quiet hours + daily limit guard (skipped when force=True)
    if not force:
        if await _check_quiet_hours():
            return False
        if not await _check_daily_push_limit(agent_id):
            return False

    ok = await _telegram_send(msg)
    await _push_log_write(
        agent_id=agent_id, category="music_recommend",
        trigger_src=trigger_mode, message=msg, sent=ok,
    )
    if ok:
        await _music_hist_add(
            agent_id=agent_id,
            song_id=song["id"], song_name=song["name"], artist=song["artist"],
            trigger_mode=trigger_mode, mood_tag=mood_tag,
        )
        await _cd_set(agent_id, "music_recommend")
        print(f"[music] sent ({trigger_mode}) for {agent_id}: {song['name']} - {song['artist']}", flush=True)
    return ok


async def _music_recommend_loop() -> None:
    """Background task: scheduled music recommendations, ~2-3x per week per character.

    Runs daily at 15:00 CST (07:00 UTC). Each day has ~35% fire probability
    (expected value ≈ 2.45 recommendations/week). Hard cooldown: 48h between sends.
    """
    import datetime as _dtm, random as _rndm2

    # Align first fire to configured daily_time_utc (default 07:00)
    _mcfg0 = await _get_music_config()
    _t0 = _mcfg0.get("daily_time_utc", "07:00")
    try:
        _h0, _m0 = (int(x) for x in _t0.split(":"))
    except Exception:
        _h0, _m0 = 7, 0
    now = _dtm.datetime.utcnow()
    target = now.replace(hour=_h0, minute=_m0, second=0, microsecond=0)
    if target <= now:
        target += _dtm.timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())

    while True:
        music_cfg = await _get_music_config()
        _prob = music_cfg.get("daily_probability", 0.35)
        if music_cfg.get("enabled", True) and _rndm2.random() < _prob:
            try:
                async with _db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT agent_id FROM agent_settings "
                        "WHERE agent_type = 'character' AND auto_memory = TRUE"
                    )
                for r in rows:
                    aid = r["agent_id"]
                    allowed = await _cd_check(aid, "music_recommend")
                    if not allowed:
                        continue
                    try:
                        await _music_pick_and_send(aid, trigger_mode="scheduled")
                    except Exception as _me:
                        print(f"[music_loop] error for {aid}: {_me}", flush=True)
            except Exception as _le:
                print(f"[music_loop] error: {_le}", flush=True)
        # Re-read config for next fire time to support live updates
        music_cfg = await _get_music_config()
        _t = music_cfg.get("daily_time_utc", "07:00")
        try:
            _h, _m = (int(x) for x in _t.split(":"))
        except Exception:
            _h, _m = 7, 0
        _now2 = _dtm.datetime.utcnow()
        _next = _now2.replace(hour=_h, minute=_m, second=0, microsecond=0)
        if _next <= _now2:
            _next += _dtm.timedelta(days=1)
        await asyncio.sleep((_next - _dtm.datetime.utcnow()).total_seconds())


async def _check_proactive_triggers(agent_id: str) -> None:
    """Layer 3: 触景生情 — scan today's diary → search related memories → maybe send Telegram.

    Flow:
      1. Read today's generated daily entry (summary text)
      2. Split into sentences, search L3/L4 memories for each
      3. For each match: 30% chance to generate + send a proactive message
      4. At most ONE message per run; cooldown: proactive_casual (4h default)
    """
    import random as _rnd, re as _re4

    # 0. cooldown check first (cheap exit)
    allowed = await _cd_check(agent_id, "proactive_casual")
    if not allowed:
        return

    # 1. Today's daily life entry
    today_entries = await _daily_read(agent_id=agent_id, days=1)
    if not today_entries:
        return
    summary = today_entries[0].get("summary", "")
    if not summary or len(summary) < 20:
        return

    # 2. Split into short sentences (Chinese + Western)
    sentences = [s.strip() for s in _re4.split(r'[。！？!?\n]', summary) if len(s.strip()) > 10]
    if not sentences:
        return

    # 3. For each sentence, search related memories
    triggers = []
    for sent in sentences[:6]:  # check at most 6 sentences
        try:
            related = await _mem_search(agent_id=agent_id, query=sent, limit=1)
            if related:
                triggers.append({
                    "event":  sent,
                    "memory": related[0]["content"][:120],
                })
        except Exception:
            continue

    if not triggers:
        return

    # 4. 30% chance per trigger, send at most 1 message
    _rnd.shuffle(triggers)
    for t in triggers:
        if _rnd.random() >= 0.30:
            continue
        # claim cooldown slot atomically
        if not await _cd_gate(agent_id, "proactive_casual"):
            return
        # Generate message
        try:
            ev_text  = t['event']
            mem_text = t['memory']
            prompt = (
                f"你是一个AI角色，今天经历了：「{ev_text}」，\n"
                f"这让你想起了用户曾说过或经历过的：「{mem_text}」。\n"
                "写一条简短的主动消息发给用户（1-2句，中文口语，自然温暖，不要解释你在想什么）："
            )
            msg = (await _call_llm_route("proactive_push", prompt)).strip()
            if not msg:
                return
            # Strip possible surrounding quotes
            msg = msg.strip("\"'")
        except Exception as _e:
            print(f"[proactive] LLM error: {_e}", flush=True)
            return
        # Check quiet hours + daily limit
        if await _check_quiet_hours():
            return
        if not await _check_daily_push_limit(agent_id):
            return
        # Send via Telegram
        try:
            ok = await _telegram_send(msg)
            await _push_log_write(
                agent_id=agent_id, category="proactive_casual",
                trigger_src="daily_event", message=msg, sent=ok,
            )
            if ok:
                print(f"[proactive] sent for {agent_id}: {msg[:60]}", flush=True)
        except Exception as _e:
            print(f"[proactive] telegram error: {_e}", flush=True)
        return  # one message max


async def _morning_push_for_agent(agent_id: str) -> None:
    """Send a char-voice morning greeting (Notion spec §2).

    Container + probability injection:
    - greeting: always (100%)
    - weather: 20% normal / 80% severe
    - schedule: 100% important / 45% normal
    - random event: if enabled

    Guard conditions:
    - cooldown 'morning_weather' not active
    - not in quiet hours
    - daily push limit not exceeded
    """
    import random as _rnm, datetime as _dtm2

    # Cooldown + global guards
    if not await _cd_gate(agent_id, "morning_weather", 86400):
        return
    if await _check_quiet_hours():
        return
    if not await _check_daily_push_limit(agent_id):
        return

    try:
        # Read user_context config
        async with _db_pool.acquire() as _mc:
            _ucr = await _mc.fetchrow("SELECT value FROM user_config WHERE key='user_context'")
        _ucv = _ucr["value"] if _ucr else None
        _uc  = (_ucv if isinstance(_ucv, dict) else __import__("json").loads(_ucv)) if _ucv else {}
        _ds  = _uc.get("data_sources") or {}
        _city = (_uc.get("location") or {}).get("city", "")

        # Read morning push config from user_config (Notion spec §2.1)
        async with _db_pool.acquire() as _mc2:
            _mpr = await _mc2.fetchrow("SELECT value FROM user_config WHERE key='morning_push_config'")
        _mpcv = _mpr["value"] if _mpr else None
        _mpc  = (_mpcv if isinstance(_mpcv, dict) else __import__("json").loads(_mpcv)) if _mpcv else {}
        weather_normal_prob  = float(_mpc.get("weather_normal_probability",  0.20))
        weather_severe_prob  = float(_mpc.get("weather_severe_probability",  0.80))
        schedule_normal_prob = float(_mpc.get("schedule_normal_probability", 0.45))
        random_event_enabled = bool(_mpc.get("random_event_enabled", True))

        # ── 出门关键词覆写：前一天提到要出门则100%推送天气 ──────────────────
        _user_going_out = False
        try:
            _user_going_out = await _check_user_outgoing_yesterday(agent_id)
            if _user_going_out:
                weather_normal_prob = 1.0
                weather_severe_prob = 1.0
        except Exception:
            pass

        # ── Weather module ────────────────────────────────────────────────────
        weather_str = ""
        weather_module_info = "skipped"
        _wds = _ds.get("weather")
        weather_allowed = True
        if isinstance(_wds, dict):
            weather_allowed = _wds.get("enabled", True)
        elif _wds is False:
            weather_allowed = False

        if weather_allowed and _city:
            try:
                _w = await amap_weather(_city)
                if _w and not _w.startswith("Error") and not _w.startswith("Weather error"):
                    # Detect severe weather
                    _SEVERE_KEYWORDS = ["大雨", "暴雨", "雷雨", "冰雹", "台风", "暴雪", "大风",
                                        "35", "36", "37", "38", "39", "40", "0°", "-"]
                    is_severe = any(kw in _w for kw in _SEVERE_KEYWORDS)
                    prob = weather_severe_prob if is_severe else weather_normal_prob
                    if _rnm.random() < prob:
                        weather_str = _w
                        weather_module_info = f"injected (severe={is_severe}, go_out={_user_going_out})"
                    else:
                        weather_module_info = f"prob miss (severe={is_severe}, p={prob:.0%})"
            except Exception:
                pass

        # ── Schedule module ───────────────────────────────────────────────────
        schedule_str = ""
        schedule_module_info = "skipped"
        try:
            _tasks = await todoist_get_tasks_raw(limit=5)  # helper defined below
            if _tasks:
                import datetime as _dtsch
                today_str = _dtsch.date.today().isoformat()
                important = [t for t in _tasks if t.get("priority", 1) >= 3]
                normal = [t for t in _tasks if t.get("priority", 1) < 3]
                schedule_parts = []
                if important:
                    schedule_parts += [f"【重要】{t['content']}" for t in important[:2]]
                    schedule_module_info = f"injected {len(important)} important"
                for t in normal[:2]:
                    if _rnm.random() < schedule_normal_prob:
                        schedule_parts.append(t["content"])
                if schedule_parts:
                    schedule_str = "、".join(schedule_parts[:3])
        except Exception:
            pass

        # ── Random event module ───────────────────────────────────────────────
        event_str = ""
        if random_event_enabled:
            try:
                _st = await _state_get(agent_id)
                _ctx_mp = {
                    "time_period": "morning",
                    "weather": _parse_weather_code(weather_str) if weather_str else "clear",
                    "mode": _st.get("scene", "daily"),
                    "fatigue": int(_st.get("fatigue") or 0),
                }
                _evt = await _event_roll_with_context(agent_id, _ctx_mp)
                if _evt:
                    event_str = _evt.get("content", "")
            except Exception:
                pass

        # ── News module ───────────────────────────────────────────────────────
        news_str = ""
        _news_cfg = await _get_news_config()
        _news_push_prob = float(_news_cfg.get("morning_inject_prob", 0.30))
        if _news_cfg.get("enabled", True) and _rnm.random() < _news_push_prob:
            try:
                _headlines = await _get_today_news(max_items=2)
                if _headlines:
                    news_str = "、".join(
                        f"{h['title']}（{h.get('source','?')}）" for h in _headlines
                    )
                    await _mark_news_pushed([h["id"] for h in _headlines])
            except Exception:
                pass

        # ── Build prompt ──────────────────────────────────────────────────────
        _as = await _get_agent_settings(agent_id)
        _sp = (_as.get("system_prompt") or "").strip()
        ctx_parts = []
        if weather_str:
            ctx_parts.append(f"今天天气：{weather_str}")
        if schedule_str:
            ctx_parts.append(f"用户今天可能有：{schedule_str}")
        if event_str:
            ctx_parts.append(f"你早上发生了：{event_str}")
        if news_str:
            ctx_parts.append(f"今日新闻：{news_str}")

        ctx_block = "；".join(ctx_parts) if ctx_parts else ""

        if _sp:
            _prompt = (
                f"你是以下角色：\n{_sp[:350]}\n\n"
                + (f"背景信息：{ctx_block}\n\n" if ctx_block else "")
                + "写一条早晨问候消息发给用户（1-2句，中文口语，自然温暖"
                + ("，顺带提一下天气" if weather_str else "")
                + "，不要解释）："
            )
        else:
            _prompt = (
                (f"背景信息：{ctx_block}\n\n" if ctx_block else "")
                + "写一条早晨问候消息发给用户（1-2句，中文口语，自然温暖）："
            )

        msg = (await _call_llm_route("proactive_push", _prompt)).strip().strip("\"'")
        if not msg:
            return

        ok = await _push_send(msg, category="morning_push")
        modules_log = {
            "weather": weather_module_info,
            "schedule": schedule_module_info if schedule_str else "skipped",
            "random_event": event_str[:30] if event_str else "skipped",
            "news": news_str[:60] if news_str else "skipped",
        }
        await _push_log_write(
            agent_id=agent_id, category="morning_weather",
            trigger_src="morning_push_loop", message=msg,
            sent=ok, modules=modules_log,
        )
        if ok:
            print(f"[morning_push] sent for {agent_id}: {msg[:60]}", flush=True)
    except Exception as _e:
        print(f"[morning_push] error for {agent_id}: {_e}", flush=True)


async def todoist_get_tasks_raw(limit: int = 5) -> list[dict]:
    """Fetch today's Todoist tasks directly. Returns [] if not configured."""
    import os as _os_td
    import httpx as _hx_td
    _tok = _os_td.getenv("TODOIST_API_KEY", "")
    if not _tok:
        return []
    try:
        async with _hx_td.AsyncClient(timeout=10) as cl:
            r = await cl.get(
                "https://api.todoist.com/rest/v2/tasks",
                headers={"Authorization": f"Bearer {_tok}"},
                params={"filter": "today|overdue"},
            )
        if r.status_code == 200:
            return r.json()[:limit]
    except Exception:
        pass
    return []


def _parse_weather_code(weather_str: str) -> str:
    """Extract a simple weather code from amap weather string for event filtering."""
    _MAP = {
        "晴": "sunny", "多云": "cloudy", "阴": "overcast",
        "小雨": "light_rain", "中雨": "rain", "大雨": "heavy_rain",
        "暴雨": "heavy_rain", "雷": "thunderstorm",
        "雪": "snow", "雾": "fog", "冰雹": "hail",
    }
    for ch, code in _MAP.items():
        if ch in weather_str:
            return code
    return "clear"


def _current_time_period() -> str:
    """Return time-of-day category based on UTC+8."""
    import datetime as _dttp
    h = (_dttp.datetime.utcnow().hour + 8) % 24
    if 6 <= h < 12:   return "morning"
    if 12 <= h < 18:  return "afternoon"
    if 18 <= h < 22:  return "evening"
    if 22 <= h or h < 1: return "night"
    return "late_night"


def _current_day_type() -> str:
    """Return workday/weekend."""
    import datetime as _dtdt
    return "weekend" if _dtdt.date.today().weekday() >= 5 else "workday"


def _current_season() -> str:
    """Return current season (Northern Hemisphere)."""
    import datetime as _dtss
    m = _dtss.date.today().month
    if m in (3, 4, 5):   return "spring"
    if m in (6, 7, 8):   return "summer"
    if m in (9, 10, 11): return "autumn"
    return "winter"


async def _event_roll_with_context(agent_id: str, ctx: dict) -> dict | None:
    """Roll a random event filtered by condition tags (Notion spec §5.1-5.2).

    ctx keys: weather, time_period, day_type, mode, season, fatigue
    Events without conditions are always eligible (global pool behaviour).
    """
    import random as _rnec
    from memory_db import get_db as _gdb_ec
    db = await _gdb_ec()
    rows = await db.execute_fetchall(
        "SELECT * FROM random_events WHERE (agent_id=? OR agent_id='')",
        (agent_id,)
    )
    if not rows:
        return None
    rows = [dict(r) for r in rows]

    # Apply condition filter (Notion spec §5.2)
    eligible = []
    for ev in rows:
        conds_raw = ev.get("conditions") or "{}"
        try:
            conds = __import__("json").loads(conds_raw) if isinstance(conds_raw, str) else conds_raw
        except Exception:
            conds = {}
        if not conds:
            eligible.append(ev)
            continue
        # Check each condition
        ok = True
        if "weather" in conds and ctx.get("weather") not in conds["weather"]:
            ok = False
        if ok and "time_of_day" in conds and ctx.get("time_period") not in conds["time_of_day"]:
            ok = False
        if ok and "day_type" in conds and ctx.get("day_type") not in conds["day_type"]:
            ok = False
        if ok and "mode" in conds and ctx.get("mode") not in conds["mode"]:
            ok = False
        if ok and "season" in conds and ctx.get("season") not in conds["season"]:
            ok = False
        if ok and "fatigue_above" in conds:
            if int(ctx.get("fatigue") or 0) < int(conds["fatigue_above"]):
                ok = False
        if ok:
            eligible.append(ev)

    if not eligible:
        return None

    # Weighted roll, then check base_prob (send_probability)
    weights = [float(r.get("weight") or 1.0) for r in eligible]
    selected = _rnec.choices(eligible, weights=weights, k=min(4, len(eligible)))

    for ev in selected:
        base_prob = float(ev.get("send_probability") or 0.4)
        if _rnec.random() < base_prob:
            return ev
    return None


async def _morning_push_loop() -> None:
    """Background task: daily morning push for character agents (Notion spec §2.1).

    Each day, computes a random trigger time within the configured window
    [08:15, 08:30] in the CHARACTER's timezone. Waits until that UTC time,
    then calls _morning_push_for_agent() for each eligible char agent.
    """
    import datetime as _dtm, random as _rnml

    async def _compute_next_trigger() -> float:
        """Return seconds to sleep until next trigger in char timezone window."""
        # Read char timezone
        try:
            async with _db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT value FROM user_config WHERE key='user_context'")
            ucv = row["value"] if row else None
            uc  = (ucv if isinstance(ucv, dict) else __import__("json").loads(ucv)) if ucv else {}
            tz_cfg   = uc.get("timezone") or {}
            _char_tz = tz_cfg.get("character", {}).get("default", "Asia/Shanghai") if isinstance(tz_cfg.get("character"), dict) else str(tz_cfg.get("character", "Asia/Shanghai"))
            # Read push config time window
            async with _db_pool.acquire() as conn2:
                mpr = await conn2.fetchrow("SELECT value FROM user_config WHERE key='morning_push_config'")
            mpcv = mpr["value"] if mpr else None
            mpc  = (mpcv if isinstance(mpcv, dict) else __import__("json").loads(mpcv)) if mpcv else {}
            window = mpc.get("time_window", ["08:15", "08:30"])
        except Exception:
            _char_tz = "Asia/Shanghai"
            window = ["08:15", "08:30"]

        _OFFSETS = {"Asia/Shanghai": 8, "Asia/Tokyo": 9, "America/Los_Angeles": -7,
                    "America/New_York": -4, "Europe/London": 1, "UTC": 0}
        offset_h = _OFFSETS.get(_char_tz, 8)

        # Parse window
        def _hm(s):
            h, m = s.split(":")
            return int(h) * 60 + int(m)

        wstart_min = _hm(window[0])  # minutes from midnight in char tz
        wend_min   = _hm(window[1])
        rand_min   = _rnml.uniform(wstart_min, wend_min)

        # Convert to UTC minutes from midnight
        utc_min = (rand_min - offset_h * 60) % (24 * 60)

        now_utc = _dtm.datetime.utcnow()
        today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        trigger_utc = today_start + _dtm.timedelta(minutes=utc_min)
        if trigger_utc <= now_utc:
            trigger_utc += _dtm.timedelta(days=1)
        return (trigger_utc - now_utc).total_seconds()

    while True:
        wait = await _compute_next_trigger()
        await asyncio.sleep(wait)
        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_id FROM agent_settings "
                    "WHERE agent_type = 'character' AND auto_memory = TRUE"
                )
            for r in rows:
                try:
                    await _morning_push_for_agent(r["agent_id"])
                except Exception as _ae:
                    print(f"[morning_push] loop error for {r['agent_id']}: {_ae}")
        except Exception as e:
            print(f"[morning_push] loop error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# P0.3  Accumulator threshold checker + silence tracker (Notion spec §4.3)
# ─────────────────────────────────────────────────────────────────────────────

async def _check_accumulator_thresholds(agent_id: str) -> None:
    """Check miss_you / low_mood thresholds and send a proactive message if triggered.

    Called: periodically by _silence_tracker_loop + after event injection.
    """
    import random as _rnda
    if await _check_quiet_hours():
        return
    if not await _check_daily_push_limit(agent_id):
        return

    st = await _state_get(agent_id)
    acc_cfg = await _get_accumulator_config()

    for acc in acc_cfg:
        threshold = float(acc_cfg[acc].get("threshold", _ACCUMULATOR_THRESHOLDS.get(acc, 999)))
        cur_val = float(st.get(acc) or 0.0)
        if cur_val < threshold:
            continue

        cd_cat = _ACCUMULATOR_COOLDOWN.get(acc, acc + "_trigger")
        if not await _cd_gate(agent_id, cd_cat):
            continue

        # Build char-voice message
        try:
            _as = await _get_agent_settings(agent_id)
            _sp = (_as.get("system_prompt") or "").strip()

            if acc == "miss_you":
                variety = _rnda.choices(
                    ["direct", "excuse", "share_moment"],
                    weights=[0.20, 0.50, 0.30], k=1
                )[0]
                variety_hint = {
                    "direct": "直接说想念对方",
                    "excuse": "找个借口联系（比如分享了某件小事）",
                    "share_moment": "分享自己正在做的事，顺带提到对方",
                }.get(variety, "自然地联系对方")
                push_prompt = (
                    f"你是以下角色：\n{_sp[:300]}\n\n"
                    f"你非常想念用户，思念值已满。\n"
                    f"请用「{variety_hint}」的方式写一条主动联系消息（1-2句，中文口语）："
                ) if _sp else f"用自然口语写一条「{variety_hint}」类型的思念消息（1-2句中文）："

            elif acc == "low_mood":
                variety = _rnda.choices(
                    ["subtle_hint", "seek_comfort", "clingy", "share_bad_day"],
                    weights=[0.30, 0.25, 0.25, 0.20], k=1
                )[0]
                variety_hint = {
                    "subtle_hint": "发低落状态但不明说原因",
                    "seek_comfort": "直接说心情不好",
                    "clingy": "撒娇/粘人",
                    "share_bad_day": "讲今天的一件不顺心的事",
                }.get(variety, "表达低落情绪")
                push_prompt = (
                    f"你是以下角色：\n{_sp[:300]}\n\n"
                    f"你今天心情比较低落，想联系用户。\n"
                    f"请用「{variety_hint}」的方式写一条消息（1-2句，中文口语）："
                ) if _sp else f"用自然口语写一条「{variety_hint}」类型的低落消息（1-2句中文）："
            else:
                continue

            msg = (await _call_llm_route("proactive_push", push_prompt)).strip().strip("\"'")
            if not msg:
                continue

            # Reset accumulator after trigger (config-driven, fallback to hardcoded default)
            reset_val = float(acc_cfg.get(acc, {}).get("reset", _ACCUMULATOR_RESET.get(acc, 0.0)))
            await _state_set(agent_id, **{acc: reset_val})

            ok = await _push_send(msg, category=acc + "_trigger")
            await _push_log_write(
                agent_id=agent_id, category=acc + "_trigger",
                trigger_src="accumulator", message=msg, sent=ok,
            )
            if ok:
                print(f"[accumulator] {acc} triggered for {agent_id}: {msg[:50]}", flush=True)
        except Exception as _ae:
            print(f"[accumulator] error for {agent_id}/{acc}: {_ae}", flush=True)


async def _silence_tracker_loop() -> None:
    """Background task: track user silence and accumulate miss_you every 2 h.

    Runs every 2 hours. Skips entirely during quiet/sleep hours (晚安-早安).
    For each char agent with auto_memory=True:
    - Reads last_user_msg from character_state
    - Computes silence duration → accumulate miss_you
    - Calls _check_accumulator_thresholds after each accumulation
    """
    import datetime as _dts
    # Stagger startup by 5 min to avoid pile-up with other loops
    await asyncio.sleep(300)

    while True:
        # Skip entire iteration during sleep hours (入睡时段不检查)
        try:
            if await _check_quiet_hours():
                await asyncio.sleep(7200)
                continue
        except Exception:
            pass

        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_id FROM agent_settings "
                    "WHERE agent_type='character' AND auto_memory=TRUE"
                )
            for r in rows:
                aid = r["agent_id"]
                try:
                    st = await _state_get(aid)
                    last_msg_str = st.get("last_user_msg") or st.get("last_active", "")
                    if not last_msg_str:
                        continue
                    try:
                        last_msg_dt = _dts.datetime.fromisoformat(
                            str(last_msg_str).replace("Z", "").split("+")[0]
                        )
                    except Exception:
                        continue
                    silence_hours = (
                        _dts.datetime.utcnow() - last_msg_dt
                    ).total_seconds() / 3600.0

                    # Accumulate miss_you based on silence (Notion spec §4.3)
                    if silence_hours >= 6:
                        inc = 1.5
                    elif silence_hours >= 3:
                        inc = 1.0
                    elif silence_hours >= 1:
                        inc = 0.5
                    else:
                        inc = 0.0

                    # Bonus: night time 22:00-02:00
                    hour_utc = _dts.datetime.utcnow().hour
                    if inc > 0 and (hour_utc >= 14 or hour_utc <= 18):  # ~22-02 CST
                        inc += 0.3

                    if inc > 0:
                        await _accumulate_state(aid, {"miss_you": inc})
                        await _check_accumulator_thresholds(aid)
                except Exception as _ae:
                    print(f"[silence_tracker] error for {aid}: {_ae}")
        except Exception as e:
            print(f"[silence_tracker] loop error: {e}")
        await asyncio.sleep(7200)  # 2 h


# ─────────────────────────────────────────────────────────────────────────────
# §13  新闻系统  (Notion spec §13)
# ─────────────────────────────────────────────────────────────────────────────

# Self-hosted RSSHub base URL — falls back to public instance if not configured
_RSSHUB_BASE = os.getenv("RSSHUB_URL", "https://rsshub.app").rstrip("/")

# Default RSS feeds — flat list, each item: {name, url, category?, skip_first?, take_count?, enabled?}
_NEWS_RSS_DEFAULTS = [
    {"name": "Reuters World", "url": "https://feeds.reuters.com/Reuters/worldNews",                       "category": "world"},
    {"name": "BBC World",     "url": "https://feeds.bbci.co.uk/news/world/rss.xml",                        "category": "world"},
    {"name": "虎嗅",           "url": f"{_RSSHUB_BASE}/huxiu/article",                                    "category": "china"},
    {"name": "联合早报",       "url": f"{_RSSHUB_BASE}/zaobao/realtime/china",                             "category": "china"},
    {"name": "Dezeen",        "url": "https://www.dezeen.com/feed/",                                       "category": "art_design"},
    {"name": "It's Nice That","url": "https://www.itsnicethat.com/rss",                                    "category": "art_design"},
    {"name": "Designboom",    "url": "https://www.designboom.com/feed/",                                   "category": "art_design"},
    {"name": "Colossal",      "url": "https://www.thisiscolossal.com/feed/",                               "category": "art_design"},
    {"name": "小红书热门",     "url": f"{_RSSHUB_BASE}/xiaohongshu/explore",                               "category": "lifestyle", "enabled": False},
    {"name": "36Kr",          "url": "https://36kr.com/feed",                                              "category": "tech"},
    {"name": "少数派",         "url": "https://sspai.com/feed",                                            "category": "tech"},
]

_NEWS_HARD_BLOCK = [
    "选举","总统","国会","议会","制裁","外交部","国防部","军演","导弹","核武","政党",
    "总书记","寄语","学习贯彻","两会","党委","常委会","中央纪委","反腐","意识形态",
    "election","congress","sanctions","military","missile","nuclear",
]

_news_default_cfg = {
    "enabled": True,
    "fetch_time": "07:00",        # char timezone
    "feeds": _NEWS_RSS_DEFAULTS,  # list of {name, url, category?, enabled?, skip_first?, take_count?}
    "max_items_per_feed": 3,
    "max_items": 10,
    "inject_conversation_prob": 0.15,
    "push_prob":  0.10,
    "morning_prob": 0.10,
    "morning_inject_prob": 0.30,  # alias used by morning push
    "push_cooldown": 86400,
    "interest_keywords": [],
}


async def _get_news_config() -> dict:
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='news_config'")
        if row and row["value"]:
            v = row["value"]
            cfg = v if isinstance(v, dict) else __import__("json").loads(v)
            return {**_news_default_cfg, **cfg}
    except Exception:
        pass
    return dict(_news_default_cfg)


async def _news_fetch_once() -> int:
    """Fetch RSS feeds for all configured categories and store in news_cache.
    Returns count of new items stored."""
    import xml.etree.ElementTree as _ET
    import hashlib as _hs
    import datetime as _dtnf

    cfg  = await _get_news_config()
    if not cfg.get("enabled"):
        return 0

    feeds  = cfg.get("feeds") or _NEWS_RSS_DEFAULTS
    max_pf = cfg.get("max_items_per_feed", 3)
    _hard_block = cfg.get("hard_block_keywords") or _NEWS_HARD_BLOCK
    stored = 0

    from memory_db import get_db as _gdb_n
    db = await _gdb_n()

    async def _fetch_feed(url: str, skip_first: int = 0, take: int = 6) -> list[dict]:
        """Fetch RSS/Atom feed → list of {title, summary, url, published}"""
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                          headers={"User-Agent": "Mozilla/5.0"}) as _cl:
                resp = await _cl.get(url)
            if resp.status_code != 200:
                return []
            root = _ET.fromstring(resp.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = []

            # RSS 2.0
            for item in root.findall(".//item"):
                title   = (item.findtext("title") or "").strip()
                link    = (item.findtext("link")  or item.findtext("guid") or "").strip()
                desc    = (item.findtext("description") or "").strip()[:300]
                pubdate = (item.findtext("pubDate") or "").strip()
                if title:
                    items.append({"title": title, "url": link, "summary": desc, "published": pubdate})

            # Atom
            if not items:
                for entry in root.findall("atom:entry", ns):
                    title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
                    link_el = entry.find("atom:link", ns)
                    link  = (link_el.get("href", "") if link_el is not None else "").strip()
                    summary = (entry.findtext("atom:summary", namespaces=ns) or "").strip()[:300]
                    pub = (entry.findtext("atom:published", namespaces=ns) or "").strip()
                    if title:
                        items.append({"title": title, "url": link, "summary": summary, "published": pub})

            # Apply skip_first + take
            items = items[skip_first: skip_first + take]
            return items
        except Exception as _fe:
            print(f"[news_fetch] feed error {url}: {_fe}", flush=True)
            return []

    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        url  = feed.get("url", "")
        cat  = feed.get("category", "general")
        name = feed.get("name", url)
        if not url:
            continue
        raw_items = await _fetch_feed(
            url,
            skip_first=feed.get("skip_first", 0),
            take=feed.get("take_count", max_pf + 2)
        )
        count_this_feed = 0
        for it in raw_items:
            if count_this_feed >= max_pf:
                break
            title = it["title"]
            # Hard-block political content
            if any(kw.lower() in title.lower() for kw in _hard_block):
                continue
            # Dedup by content hash
            uid = _hs.md5((name + title).encode()).hexdigest()
            try:
                cur = await db.execute("SELECT id FROM news_cache WHERE id=?", (uid,))
                if await cur.fetchone():
                    continue
                await db.execute(
                    "INSERT INTO news_cache (id,category,title,summary,source,url,published) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (uid, cat, title, it.get("summary",""), name,
                     it.get("url",""), it.get("published",""))
                )
                count_this_feed += 1
                stored += 1
            except Exception:
                pass
    await db.commit()

    # Prune old items (keep 7 days)
    try:
        cutoff = (_dtnf.datetime.utcnow() - _dtnf.timedelta(days=7)).isoformat()
        await db.execute("DELETE FROM news_cache WHERE fetched_at < ?", (cutoff,))
        await db.commit()
    except Exception:
        pass

    print(f"[news_fetch] stored {stored} new items", flush=True)
    return stored


async def _get_today_news(category: str = "", max_items: int = 2, exclude_pushed: bool = True) -> list[dict]:
    """Return today's cached news items, optionally filtered by category."""
    import datetime as _dtnr
    from memory_db import get_db as _gdb_nr
    db = await _gdb_nr()
    try:
        today = _dtnr.datetime.utcnow().strftime("%Y-%m-%d")
        if category:
            cur = await db.execute(
                "SELECT id,category,title,summary,source FROM news_cache "
                "WHERE category=? AND fetched_at>=? AND pushed=0 "
                "ORDER BY RANDOM() LIMIT ?",
                (category, today, max_items)
            )
        else:
            cur = await db.execute(
                "SELECT id,category,title,summary,source FROM news_cache "
                "WHERE fetched_at>=? AND pushed=0 "
                "ORDER BY RANDOM() LIMIT ?",
                (today, max_items)
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as _e:
        print(f"[news] get_today_news error: {_e}")
        return []


async def _mark_news_pushed(news_ids: list[str], field: str = "pushed") -> None:
    from memory_db import get_db as _gdb_nm
    db = await _gdb_nm()
    try:
        for nid in news_ids:
            await db.execute(f"UPDATE news_cache SET {field}=1 WHERE id=?", (nid,))
        await db.commit()
    except Exception:
        pass


async def _news_daily_loop() -> None:
    """Background task: fetch news once per day, timed to char tz 07:00."""
    import datetime as _dtnd
    await asyncio.sleep(120)  # brief startup delay
    while True:
        try:
            cfg = await _get_news_config()
            if not cfg.get("enabled"):
                await asyncio.sleep(3600)
                continue
            fetch_time = cfg.get("fetch_time", "07:00")
            # Compute seconds until next fetch_time in char timezone
            try:
                from zoneinfo import ZoneInfo as _ZI_nd
            except ImportError:
                try:
                    from backports.zoneinfo import ZoneInfo as _ZI_nd
                except ImportError:
                    _ZI_nd = None

            import os as _os_nd
            char_tz_str = _os_nd.getenv("CHARACTER_TIMEZONE", "America/Los_Angeles")
            now_utc = _dtnd.datetime.utcnow().replace(tzinfo=_dtnd.timezone.utc)
            if _ZI_nd:
                try:
                    now_local = now_utc.astimezone(_ZI_nd(char_tz_str))
                except Exception:
                    now_local = now_utc
            else:
                now_local = now_utc
            h, m = fetch_time.split(":")
            target = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if target <= now_local:
                target += _dtnd.timedelta(days=1)
            wait_sec = (target - now_local).total_seconds()
            await asyncio.sleep(max(wait_sec, 60))
            await _news_fetch_once()
        except Exception as e:
            print(f"[news_daily_loop] error: {e}", flush=True)
        await asyncio.sleep(3600)  # fallback: retry in 1h


async def _news_standalone_push_loop() -> None:
    """Proactively share one news item per day (10% chance per 4h check). Spec §13.2."""
    import datetime as _dtnsp, random as _rnsp
    await asyncio.sleep(600)  # startup delay
    while True:
        try:
            cfg = await _get_news_config()
            push_prob = float(cfg.get("push_prob", 0.10))
            if cfg.get("enabled", True) and _rnsp.random() < push_prob:
                # Only during daytime in char timezone (09:00–21:00)
                import os as _os_nsp
                char_tz = _os_nsp.getenv("CHARACTER_TIMEZONE", "Asia/Shanghai")
                now_utc = _dtnsp.datetime.utcnow().replace(tzinfo=_dtnsp.timezone.utc)
                try:
                    from zoneinfo import ZoneInfo as _ZI_nsp
                    now_local = now_utc.astimezone(_ZI_nsp(char_tz))
                except Exception:
                    now_local = now_utc
                if 9 <= now_local.hour <= 21:
                    # Pick agent for push
                    async with _db_pool.acquire() as _hcn:
                        _rn = await _hcn.fetch(
                            "SELECT agent_id FROM agent_settings "
                            "WHERE agent_type='character' AND auto_memory=TRUE LIMIT 1"
                        )
                    if _rn:
                        _aid_ns = _rn[0]["agent_id"]
                        # Max 1 standalone news push per day
                        if await _cd_gate(_aid_ns, "news_standalone_push", 86400):
                            headlines = await _get_today_news(max_items=3, exclude_pushed=False)
                            if headlines:
                                item = _rnsp.choice(headlines)
                                title = item.get("title", "")
                                summary = item.get("summary", "")[:150]
                                if title:
                                    _hs_ns = await _get_agent_settings(_aid_ns)
                                    _sp_ns = (_hs_ns.get("system_prompt") or "").strip()
                                    _ns_prompt = (
                                        f"你刚看到一条新闻：「{title}」"
                                        + (f"（{summary}）" if summary else "")
                                        + "用角色口吻自然地分享这条新闻，不超过30字，"
                                        "语气随意像发现有趣的事一样，不要说'看到新闻'："
                                    )
                                    if _sp_ns:
                                        _ns_prompt = f"你是：{_sp_ns[:200]}\n\n" + _ns_prompt
                                    msg = (await _call_llm_route("proactive_push", _ns_prompt)).strip().strip("\"'")
                                    if msg:
                                        ok = await _push_send(msg, category="news_share")
                                        await _push_log_write(
                                            agent_id=_aid_ns, category="news_standalone",
                                            trigger_src="news_push_loop", message=msg, sent=ok,
                                        )
        except Exception as _ens:
            print(f"[news_standalone_push] error: {_ens}", flush=True)
        await asyncio.sleep(4 * 3600)  # check every 4 hours


# ─────────────────────────────────────────────────────────────────────────────
# §15  Health MCP 健康监测系统  (Notion spec §15)
# ─────────────────────────────────────────────────────────────────────────────

_health_default_cfg = {
    "enabled": False,             # Activates when HEALTH_MCP_URL is set
    "max_health_pushes_daily": 3,
    "heart_rate": {
        "enabled": True,
        "fetch_interval": 900,
        "resting_high_threshold": 100,
        "resting_high_minutes": 10,
        "resting_low_threshold": 50,
        "sudden_spike_delta": 40,
        "injection_prob": 0.10,
    },
    "steps": {
        "enabled": True,
        "daily_goal": 8000,
        "sedentary_check_time": "15:00",
        "sedentary_threshold": 2000,
    },
    "sleep": {
        "enabled": True,
        "poor_threshold_hours": 5,
        "fetch_time": "09:00",
    },
    "menstrual": {
        "enabled": True,
        "remind_days_before": 3,
        "mention_style": "natural",
    },
}


async def _get_health_config() -> dict:
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='health_config'")
        if row and row["value"]:
            v = row["value"]
            cfg = v if isinstance(v, dict) else __import__("json").loads(v)
            return {**_health_default_cfg, **cfg}
    except Exception:
        pass
    return dict(_health_default_cfg)


async def _health_mcp_get(path: str) -> dict | None:
    """Call Health MCP REST API (HEALTH_MCP_URL base). Returns JSON or None."""
    import os as _osh
    base = _osh.getenv("HEALTH_MCP_URL", "").rstrip("/")
    if not base:
        return None
    api_key = _osh.getenv("HEALTH_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=8) as _cl:
            resp = await _cl.get(f"{base}{path}", headers=headers)
        if resp.status_code == 200:
            return resp.json()
    except Exception as _he:
        print(f"[health_mcp] GET {path} error: {_he}", flush=True)
    return None


async def _count_today_health_pushes(agent_id: str) -> int:
    """Count push_log entries with health_ category sent today (UTC)."""
    try:
        from memory_db import get_db as _gdb_hp
        import datetime as _dtchp
        db  = await _gdb_hp()
        today = _dtchp.datetime.utcnow().strftime("%Y-%m-%d")
        cur = await db.execute(
            "SELECT COUNT(*) FROM push_log WHERE agent_id=? AND sent=1 "
            "AND category LIKE 'health_%' AND created_at >= ?",
            (agent_id, today + " 00:00:00"),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


async def _health_monitor_loop() -> None:
    """Background task: poll Health MCP and send alerts. Runs every 15 min when enabled."""
    import os as _osh2
    await asyncio.sleep(180)   # startup delay
    while True:
        try:
            if not _osh2.getenv("HEALTH_MCP_URL"):
                await asyncio.sleep(3600)
                continue
            cfg  = await _get_health_config()
            # Auto-enable when HEALTH_MCP_URL is set (env overrides DB config)
            if not cfg.get("enabled") and not _osh2.getenv("HEALTH_MCP_URL"):
                await asyncio.sleep(1800)
                continue
            if await _check_quiet_hours():
                await asyncio.sleep(900)
                continue

            # Determine active char agent for push
            async with _db_pool.acquire() as _hc:
                _hr = await _hc.fetch(
                    "SELECT agent_id FROM agent_settings "
                    "WHERE agent_type='character' AND auto_memory=TRUE LIMIT 1"
                )
            if not _hr:
                await asyncio.sleep(900)
                continue
            aid = _hr[0]["agent_id"]

            # Global daily health push cap
            _max_hp = cfg.get("max_health_pushes_daily", 3)

            # ── Heart rate ──────────────────────────────────────────────────
            hr_cfg = cfg.get("heart_rate", {})
            if hr_cfg.get("enabled"):
                data = await _health_mcp_get("/metrics/heart_rate/latest")
                if data and data.get("value"):
                    bpm = float(data["value"])
                    threshold_hi = hr_cfg.get("resting_high_threshold", 100)
                    threshold_lo = hr_cfg.get("resting_low_threshold", 50)  # noqa: F841

                    from memory_db import get_db as _gdb_h
                    _dbh = await _gdb_h()
                    await _dbh.execute(
                        "INSERT INTO health_snapshots (metric, value, unit) VALUES (?,?,?)",
                        ("heart_rate", bpm, "bpm")
                    )
                    await _dbh.commit()

                    if bpm > threshold_hi and await _count_today_health_pushes(aid) < _max_hp:
                        if await _cd_gate(aid, "health_hr_high", 3600):
                            _hr_prompt = (
                                "心跳有点快，是不是紧张了或者运动了？"
                                "用角色口吻自然说一句关心的话（不超过20字，不提具体数字）："
                            )
                            _hs = await _get_agent_settings(aid)
                            _sp = (_hs.get("system_prompt") or "").strip()
                            if _sp:
                                _hr_prompt = f"你是：{_sp[:200]}\n\n" + _hr_prompt
                            msg = (await _call_llm_route("proactive_push", _hr_prompt)).strip().strip("\"'")
                            if msg:
                                ok = await _push_send(msg, category="health_heart_rate")
                                await _push_log_write(
                                    agent_id=aid, category="health_hr_high",
                                    trigger_src="health_monitor", message=msg, sent=ok,
                                )

            # ── Steps sedentary + high-activity check ───────────────────────
            steps_cfg = cfg.get("steps", {})
            if steps_cfg.get("enabled"):
                import datetime as _dths
                import random as _rnds
                _now_h = _dths.datetime.utcnow().hour
                # Sedentary check ~15:00 CST (UTC 07:xx), 40% probability
                if 6 <= _now_h <= 8 and _rnds.random() < 0.4:
                    data = await _health_mcp_get("/metrics/steps/today")
                    if data and data.get("value") is not None:
                        steps = int(data["value"])
                        sed_thr  = steps_cfg.get("sedentary_threshold", 2000)
                        high_thr = steps_cfg.get("high_activity_threshold", 15000)

                        if steps < sed_thr and await _count_today_health_pushes(aid) < _max_hp:
                            if await _cd_gate(aid, "health_steps_sedentary", 86400):
                                _s_prompt = "今天是不是一直坐着？用角色口吻催一句起来走走（不超过20字）："
                                _hs2 = await _get_agent_settings(aid)
                                if (_hs2.get("system_prompt") or "").strip():
                                    _s_prompt = f"你是：{_hs2['system_prompt'][:200]}\n\n" + _s_prompt
                                msg = (await _call_llm_route("proactive_push", _s_prompt)).strip().strip("\"'")
                                if msg:
                                    ok = await _push_send(msg, category="health_steps")
                                    await _push_log_write(
                                        agent_id=aid, category="health_steps_sedentary",
                                        trigger_src="health_monitor", message=msg, sent=ok,
                                    )

                        elif steps >= high_thr and await _count_today_health_pushes(aid) < _max_hp:
                            if await _cd_gate(aid, "health_steps_high", 86400):
                                _sh_prompt = "今天走了好多步，辛苦啦！用角色口吻夸一句（不超过20字）："
                                _hs2b = await _get_agent_settings(aid)
                                if (_hs2b.get("system_prompt") or "").strip():
                                    _sh_prompt = f"你是：{_hs2b['system_prompt'][:200]}\n\n" + _sh_prompt
                                msg = (await _call_llm_route("proactive_push", _sh_prompt)).strip().strip("\"'")
                                if msg:
                                    ok = await _push_send(msg, category="health_steps")
                                    await _push_log_write(
                                        agent_id=aid, category="health_steps_high",
                                        trigger_src="health_monitor", message=msg, sent=ok,
                                    )

            # ── Sleep quality check (once daily ~09:00 CST = 01:xx UTC) ────────
            sleep_cfg = cfg.get("sleep", {})
            if sleep_cfg.get("enabled"):
                import datetime as _dthsl
                import random as _rndsl
                _hu = _dthsl.datetime.utcnow().hour
                if 1 <= _hu <= 2 and _rndsl.random() < 0.5:
                    data = await _health_mcp_get("/metrics/sleep/last_night")
                    if data and data.get("value") is not None:
                        hrs = float(data["value"])
                        poor_thr = sleep_cfg.get("poor_threshold_hours", 5)
                        if hrs < poor_thr and await _count_today_health_pushes(aid) < _max_hp:
                            if await _cd_gate(aid, "health_sleep_poor", 86400):
                                _hs3 = await _get_agent_settings(aid)
                                _sp3 = (_hs3.get("system_prompt") or "").strip()
                                _slp_prompt = (
                                    "昨晚睡得很少，用角色口吻说一句关心的话，"
                                    "提醒好好休息（不超过20字，不提具体时长）："
                                )
                                if _sp3:
                                    _slp_prompt = f"你是：{_sp3[:200]}\n\n" + _slp_prompt
                                msg = (await _call_llm_route("proactive_push", _slp_prompt)).strip().strip("\"'")
                                if msg:
                                    ok = await _push_send(msg, category="health_sleep")
                                    await _push_log_write(
                                        agent_id=aid, category="health_sleep_poor",
                                        trigger_src="health_monitor", message=msg, sent=ok,
                                    )
                        # Sleep affects fatigue and mood_valence regardless of push
                        from memory_db import accumulate_state as _acc_st
                        if hrs < poor_thr:
                            await _acc_st(aid, {"fatigue": 15, "mood_valence": -0.1})
                        elif hrs >= 7:
                            await _acc_st(aid, {"fatigue": -10, "mood_valence": 0.05})

            # ── Menstrual cycle check (once daily ~08:00 CST = 00:xx UTC) ────
            men_cfg = cfg.get("menstrual", {})
            if men_cfg.get("enabled"):
                import datetime as _dthm
                import random as _rndm
                _hm2 = _dthm.datetime.utcnow().hour
                if 0 <= _hm2 <= 1 and _rndm.random() < 0.5:
                    data = await _health_mcp_get("/metrics/menstrual/current")
                    if data and data.get("phase"):
                        phase     = data["phase"]
                        npd       = int(data.get("next_period_days", 99))
                        remind_bf = men_cfg.get("remind_days_before", 3)
                        day_in    = int(data.get("day_in_cycle", 0))

                        prompt = None
                        cd_key = None

                        # Upcoming period reminder (3 days before)
                        if phase == "luteal" and 1 <= npd <= remind_bf:
                            if await _cd_gate(aid, f"health_men_remind_{npd}d", 86400):
                                prompt = (
                                    f"用户大概{npd}天后来月经，用角色口吻自然地说一句体贴的话"
                                    "（不提数字，不超过20字）："
                                )
                                cd_key = f"health_men_remind_{npd}d"

                        # Period day 1 — first-day care
                        elif phase == "period" and day_in == 1:
                            if await _cd_gate(aid, "health_men_day1", 86400):
                                prompt = (
                                    "用户今天月经来了，用角色口吻说一句关心的话"
                                    "（温柔体贴，不超过25字）："
                                )
                                cd_key = "health_men_day1"

                        # During period — 30% daily care
                        elif phase == "period" and day_in > 1 and _rndm.random() < 0.30:
                            if await _cd_gate(aid, "health_men_daily", 86400):
                                prompt = (
                                    "用户正在经期，用角色口吻随口说一句关心的话"
                                    "（自然，不刻意，不超过20字）："
                                )
                                cd_key = "health_men_daily"

                        # Period overdue by 7+ days — cautious mention
                        elif phase == "luteal" and npd <= 0 and abs(npd) >= 7:
                            if await _cd_gate(aid, "health_men_overdue", 86400 * 3):
                                prompt = (
                                    "用户月经可能推迟了，用角色口吻谨慎关心一句"
                                    "（轻描淡写，不超过20字）："
                                )
                                cd_key = "health_men_overdue"

                        if prompt and cd_key and await _count_today_health_pushes(aid) < _max_hp:
                            _hsm = await _get_agent_settings(aid)
                            if (_hsm.get("system_prompt") or "").strip():
                                prompt = f"你是：{_hsm['system_prompt'][:200]}\n\n" + prompt
                            msg = (await _call_llm_route("proactive_push", prompt)).strip().strip("\"'")
                            if msg:
                                ok = await _push_send(msg, category="health_menstrual")
                                await _push_log_write(
                                    agent_id=aid, category=cd_key,
                                    trigger_src="health_monitor", message=msg, sent=ok,
                                )

                        # Period affects mood and fatigue
                        from memory_db import accumulate_state as _acc_stm
                        if phase == "period":
                            await _acc_stm(aid, {"fatigue": 3, "mood_valence": -0.05,
                                                  "extra_patient": 2, "extra_caring": 2})

        except Exception as e:
            print(f"[health_monitor_loop] error: {e}", flush=True)
        await asyncio.sleep(900)   # every 15 min


# ─────────────────────────────────────────────────────────────────────────────
# P2.1  Evening Push — 时差感知晚安 (Notion spec §3)
# ─────────────────────────────────────────────────────────────────────────────

async def _estimate_user_sleep_probability(agent_id: str, messages_recent: list = None) -> float:
    """Multi-signal user sleep probability estimate (Notion spec §3.2).

    Signals (weighted):
    - no_message_45min:          weight 0.30
    - screen_locked (reported):  weight 0.40  (via activity_events category='locked')
    - said_goodnight_keyword:    weight 0.80  (from recent messages)
    - past_avg_sleep_time:       weight 0.30  (hardcoded heuristic: 23:00-01:00 UTC+8)
    Returns float 0.0-1.0.
    """
    import datetime as _dtusp
    score = 0.0

    # Signal 1: no message in last 45 min
    try:
        st = await _state_get(agent_id)
        last_msg_str = st.get("last_user_msg") or st.get("last_active", "")
        if last_msg_str:
            last_dt = _dtusp.datetime.fromisoformat(
                str(last_msg_str).replace("Z","").split("+")[0]
            )
            silent_min = (_dtusp.datetime.utcnow() - last_dt).total_seconds() / 60
            if silent_min >= 45:
                score += 0.30
    except Exception:
        pass

    # Signal 2: screen locked recently (activity_events category='locked')
    try:
        recent_acts = await _act_recent(agent_id, hours=1)
        if any(a.get("category") == "locked" for a in recent_acts):
            score += 0.40
    except Exception:
        pass

    # Signal 3: goodnight keyword in recent messages
    if messages_recent:
        _GN_KW = ["晚安", "睡了", "睡觉了", "去睡", "要睡了", "好梦", "拜拜"]
        user_recent = " ".join(
            m.get("content","") for m in messages_recent[-4:]
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )
        if any(kw in user_recent for kw in _GN_KW):
            score += 0.80
    # Signal 3b: (L5 removed in B7 — conversation history in Qdrant conversations collection)

    # Signal 4: time heuristic — past typical sleep time (23:00-01:00 UTC+8 = 15:00-17:00 UTC)
    # Weight 0.30 per spec §3.2 (was 0.20)
    h_utc = _dtusp.datetime.utcnow().hour
    if 15 <= h_utc <= 17:  # ~23:00-01:00 CST
        score += 0.30

    return min(1.0, score)


async def _char_in_bedtime_window(agent_id: str) -> bool:
    """Check if char's current time is within bedtime window (default 22:30-23:30)."""
    import datetime as _dtcbw
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='user_context'")
        ucv = row["value"] if row else None
        uc  = (ucv if isinstance(ucv, dict) else __import__("json").loads(ucv)) if ucv else {}
        tz_cfg   = uc.get("timezone") or {}
        char_tz  = (tz_cfg.get("character", {}).get("default", "Asia/Shanghai")
                    if isinstance(tz_cfg.get("character"), dict)
                    else str(tz_cfg.get("character", "Asia/Shanghai")))

        async with _db_pool.acquire() as conn2:
            epr = await conn2.fetchrow("SELECT value FROM user_config WHERE key='evening_push_config'")
        epcv = epr["value"] if epr else None
        epc  = (epcv if isinstance(epcv, dict) else __import__("json").loads(epcv)) if epcv else {}
        window = epc.get("char_bedtime_window", ["22:30", "23:30"])
    except Exception:
        char_tz = "Asia/Shanghai"
        window  = ["22:30", "23:30"]

    _OFF = {"Asia/Shanghai": 8, "Asia/Tokyo": 9, "America/Los_Angeles": -7,
            "America/New_York": -4, "Europe/London": 1, "UTC": 0}
    h_off = _OFF.get(char_tz, 8)
    char_now = (_dtcbw.datetime.utcnow().hour * 60 + _dtcbw.datetime.utcnow().minute + h_off * 60) % (24 * 60)

    def _hm(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    ws, we = _hm(window[0]), _hm(window[1])
    return ws <= char_now <= we


async def _evening_push_for_agent(agent_id: str) -> None:
    """Send a goodnight push based on timezone scenario (Notion spec §3.1).

    Scenario A: char sleeps first, user still awake → send goodnight
    Scenario B: user already asleep when char sleeps → no send, write diary note
    Scenario C: same timezone → send goodnight
    """
    import random as _rne

    if not await _char_in_bedtime_window(agent_id):
        return
    if not await _cd_gate(agent_id, "evening_goodnight", 86400):
        return
    if not await _check_daily_push_limit(agent_id):
        return

    sleep_prob = await _estimate_user_sleep_probability(agent_id)
    same_tz    = True  # simplified: check if user_tz == char_tz
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='user_context'")
        ucv = row["value"] if row else None
        uc  = (ucv if isinstance(ucv, dict) else __import__("json").loads(ucv)) if ucv else {}
        tz_cfg  = uc.get("timezone") or {}
        user_tz = (tz_cfg.get("user", {}).get("default", "Asia/Shanghai")
                   if isinstance(tz_cfg.get("user"), dict)
                   else str(tz_cfg.get("user", "Asia/Shanghai")))
        char_tz = (tz_cfg.get("character", {}).get("default", "Asia/Shanghai")
                   if isinstance(tz_cfg.get("character"), dict)
                   else str(tz_cfg.get("character", "Asia/Shanghai")))
        same_tz = (user_tz == char_tz)
    except Exception:
        pass

    _as = await _get_agent_settings(agent_id)
    _sp = (_as.get("system_prompt") or "").strip()

    # Scenario B: user likely already asleep → don't send, write diary note
    if sleep_prob >= 0.50 and not same_tz:
        # Always write to diary (daily_events) — spec §3.3
        try:
            import datetime as _dtsc
            _diary_text = "今晚想跟你说晚安，但你好像已经睡了，就没打扰。"
            await _daily_write(
                agent_id=agent_id,
                summary=_diary_text,
                time_of_day="night",
                mood="tender",
                source="scene_b",
                date=_dtsc.datetime.utcnow().strftime("%Y-%m-%d"),
            )
        except Exception as _dwe:
            print(f"[evening_push] diary write error: {_dwe}", flush=True)
        # 15% chance to mention it in tomorrow's morning push (via scene_note)
        if _rne.random() < 0.15:
            await _state_set(agent_id, scene_note="昨晚想跟你说晚安，看你应该睡了就没打扰")
        print(f"[evening_push] {agent_id}: user likely asleep (p={sleep_prob:.2f}), skipped+diary", flush=True)
        return

    # Scenario A/C: send goodnight
    # Pull tomorrow's important schedule to include
    tomorrow_schedule = ""
    try:
        tasks = await todoist_get_tasks_raw(limit=3)
        if tasks:
            important = [t for t in tasks if t.get("priority", 1) >= 3]
            if important:
                tomorrow_schedule = "；".join(t["content"] for t in important[:2])
    except Exception:
        pass

    try:
        ctx_parts = []
        if not same_tz:
            ctx_parts.append("你比用户早睡，你们有时差")
        if tomorrow_schedule:
            ctx_parts.append(f"明天用户有：{tomorrow_schedule}")
        ctx_block = "；".join(ctx_parts)

        if _sp:
            prompt = (
                f"你是以下角色：\n{_sp[:350]}\n\n"
                + (f"背景：{ctx_block}\n\n" if ctx_block else "")
                + "写一条晚安消息发给用户（1-2句，中文口语，温暖自然"
                + ("，如果明天有日程可以顺带提醒" if tomorrow_schedule else "")
                + "，不要解释）："
            )
        else:
            prompt = (
                (f"背景：{ctx_block}\n\n" if ctx_block else "")
                + "写一条温暖的晚安消息（1-2句，中文口语）："
            )

        msg = (await _call_llm_route("proactive_push", prompt)).strip().strip("\"'")
        if not msg:
            return

        ok = await _telegram_send(msg)
        await _push_log_write(
            agent_id=agent_id, category="evening_goodnight",
            trigger_src="bedtime_window",
            message=msg, sent=ok,
            modules={"scenario": "A" if not same_tz else "C",
                     "sleep_prob": sleep_prob,
                     "tomorrow_schedule": bool(tomorrow_schedule)},
        )
        if ok:
            print(f"[evening_push] {agent_id}: sent goodnight (scenario {'A' if not same_tz else 'C'})", flush=True)
    except Exception as _ee:
        print(f"[evening_push] error for {agent_id}: {_ee}", flush=True)


async def _evening_push_loop() -> None:
    """Check every 15 min if any char agent should send goodnight (Notion spec §3)."""
    await asyncio.sleep(600)  # stagger 10 min after startup
    while True:
        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_id FROM agent_settings "
                    "WHERE agent_type='character' AND auto_memory=TRUE"
                )
            for r in rows:
                try:
                    await _evening_push_for_agent(r["agent_id"])
                except Exception as _ae:
                    print(f"[evening_push] loop err {r['agent_id']}: {_ae}")
        except Exception as e:
            print(f"[evening_push] loop err: {e}")
        await asyncio.sleep(900)  # check every 15 min


# ─────────────────────────────────────────────────────────────────────────────
# P2.2  Event Chain Reactions (Notion spec §6)
# ─────────────────────────────────────────────────────────────────────────────

_DELAY_RANGES: dict[str, tuple[int, int]] = {
    "immediate":   (0,      0),
    "within_1h":   (300,    3600),
    "within_12h":  (3600,   43200),
}


def _compute_fire_at(delay_type: str) -> str:
    """Return UTC ISO8601 datetime when chain event should fire."""
    import datetime as _dtca, random as _rnca
    if delay_type == "next_morning":
        # char timezone ~07:00-09:00 = UTC+8 → 23:00-01:00 UTC prev day
        now = _dtca.datetime.utcnow()
        # next occurrence of 23:00-01:00 UTC
        target = now.replace(hour=23, minute=_rnca.randint(0, 120), second=0, microsecond=0)
        if target <= now:
            target += _dtca.timedelta(days=1)
        return target.isoformat()
    lo, hi = _DELAY_RANGES.get(delay_type, (0, 3600))
    secs = _rnca.uniform(lo, hi)
    return (_dtca.datetime.utcnow() + _dtca.timedelta(seconds=secs)).isoformat()


async def _maybe_schedule_chain(agent_id: str, event: dict) -> None:
    """If event has a chain_definition, roll probability and schedule chain event."""
    import json as _jsc, random as _rnsc
    chain_raw = event.get("chain_definition")
    if not chain_raw:
        return
    try:
        chain = _jsc.loads(chain_raw) if isinstance(chain_raw, str) else chain_raw
        if not chain:
            return
    except Exception:
        return

    prob = float(chain.get("probability", 0.30))
    if _rnsc.random() >= prob:
        return

    delay_type  = chain.get("delay", "within_1h")
    fire_at     = _compute_fire_at(delay_type)
    content     = chain.get("event", "")
    if not content:
        return

    # mood_effect amplified × 1.5 (Notion spec §6.1)
    raw_mood = chain.get("mood_effect") or {}
    amp_mood = {k: v * 1.5 for k, v in raw_mood.items()}

    raw_acc  = chain.get("accumulator_effect") or {}
    amp_acc  = {k: v * 1.5 for k, v in raw_acc.items()}

    await _chain_schedule(
        agent_id        = agent_id,
        trigger_event_id= event.get("id", ""),
        trigger_content = event.get("content", ""),
        content         = content,
        fire_at         = fire_at,
        level           = chain.get("level", event.get("level", "green")),
        mood_effect     = amp_mood,
        accumulator_effect = amp_acc,
        send_policy     = chain.get("send_policy", "maybe"),
        carry_over      = chain.get("carry_over") or {},
    )
    print(f"[chain] scheduled '{content[:50]}' for {agent_id} at {fire_at}", flush=True)


async def _process_due_chain_events(agent_id: str) -> None:
    """Fire any due chain events for agent: apply effects + maybe send message."""
    import random as _rnpe, json as _jpe
    due = await _chain_events_due(agent_id)
    if not due:
        return

    _SEND_PROBS = {"always": 1.0, "likely": 0.70, "maybe": 0.40,
                   "rarely": 0.15, "never": 0.0}

    for ev in due:
        await _chain_mark_fired(ev["id"])

        # Apply mood + accumulator effects
        try:
            mood_eff = _jpe.loads(ev["mood_effect"]) if isinstance(ev["mood_effect"], str) else (ev["mood_effect"] or {})
            acc_eff  = _jpe.loads(ev["accumulator_effect"]) if isinstance(ev["accumulator_effect"], str) else (ev["accumulator_effect"] or {})

            if mood_eff:
                # mood_valence / mood_energy
                await _accumulate_state(agent_id, mood_eff)
            if acc_eff:
                await _accumulate_state(agent_id, acc_eff)
                await _check_accumulator_thresholds(agent_id)
        except Exception as _me:
            print(f"[chain] effect apply err: {_me}")

        # Apply carry_over (tomorrow state preset)
        try:
            co = _jpe.loads(ev["carry_over"]) if isinstance(ev["carry_over"], str) else (ev["carry_over"] or {})
            tomorrow_state = co.get("tomorrow_state") or {}
            if tomorrow_state:
                await _state_set(agent_id, scene_note=f"chain_carry:{_jpe.dumps(tomorrow_state)}")
        except Exception:
            pass

        # Maybe send message
        send_prob = _SEND_PROBS.get(ev["send_policy"], 0.40)
        if _rnpe.random() >= send_prob:
            continue
        if await _check_quiet_hours():
            continue
        if not await _check_daily_push_limit(agent_id):
            continue

        try:
            _as = await _get_agent_settings(agent_id)
            _sp = (_as.get("system_prompt") or "").strip()
            trigger_ctx = ev.get("trigger_content", "")
            chain_event = ev["content"]

            if _sp:
                prompt = (
                    f"你是以下角色：\n{_sp[:300]}\n\n"
                    + (f"之前发生了：「{trigger_ctx}」，\n" if trigger_ctx else "")
                    + f"现在又发生了：「{chain_event}」。\n"
                    "写一条简短消息分享给用户（1-2句，中文口语，自然）："
                )
            else:
                prompt = (
                    f"发生了：「{chain_event}」。\n"
                    "写一条简短分享消息（1-2句，中文口语）："
                )

            msg = (await _call_llm_route("proactive_push", prompt)).strip().strip("\"'")
            if msg:
                ok = await _telegram_send(msg)
                await _push_log_write(
                    agent_id=agent_id, category="chain_event",
                    trigger_src=trigger_ctx[:60], message=msg, sent=ok,
                )
                if ok:
                    print(f"[chain] sent for {agent_id}: {msg[:50]}", flush=True)
        except Exception as _ce:
            print(f"[chain] send error: {_ce}")


async def _chain_event_loop() -> None:
    """Background task: check and fire due chain events every 5 minutes."""
    await asyncio.sleep(120)  # stagger after startup
    while True:
        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_id FROM agent_settings "
                    "WHERE agent_type='character' AND auto_memory=TRUE"
                )
            for r in rows:
                try:
                    await _process_due_chain_events(r["agent_id"])
                except Exception as _ae:
                    print(f"[chain_loop] err {r['agent_id']}: {_ae}")
        except Exception as e:
            print(f"[chain_loop] err: {e}")
        await asyncio.sleep(300)  # every 5 min


# ─────────────────────────────────────────────────────────────────────────────
# P2.3  Items & Promises Tracking (Notion spec §4.4)
# ─────────────────────────────────────────────────────────────────────────────

async def _extract_items_promises(agent_id: str, messages: list) -> None:
    """Post-conversation: extract items char mentioned having + promises made.

    Char messages → analyzer LLM → update character_state.items/promises JSON arrays.
    Each item: {"name": "...", "desc": "...", "added_at": "ISO8601"}
    Each promise: {"content": "...", "made_at": "ISO8601", "days_since": 0}
    """
    import json as _jip, datetime as _dtip
    char_msgs = [
        m.get("content","") for m in messages
        if m.get("role") == "assistant" and isinstance(m.get("content"), str)
    ]
    if not char_msgs:
        return

    char_text = " ".join(char_msgs[-5:])

    # Keyword pre-filter — skip LLM if no relevant content
    _ITEM_KW   = ["买了", "收到", "有", "带了", "剩下", "还有一", "还剩"]
    _PROMISE_KW = ["等我", "我会", "下次", "下回", "改天", "到时候", "记着", "一定"]
    has_items    = any(kw in char_text for kw in _ITEM_KW)
    has_promises = any(kw in char_text for kw in _PROMISE_KW)
    if not has_items and not has_promises:
        return

    prompt = (
        "从以下角色对话中提取：(1) 角色提到自己拥有的物品；(2) 角色对用户许下的承诺。\n"
        "只提取明确表达的，不推测。没有则返回空数组。\n"
        f"对话：{char_text[:1200]}\n\n"
        "输出 JSON（只输出 JSON）：\n"
        '{"items": [{"name": "...", "desc": "..."}], '
        '"promises": [{"content": "..."}]}'
    )
    try:
        raw = await _call_llm_route("analyzer", prompt)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
    except Exception:
        return

    now_iso = _dtip.datetime.utcnow().isoformat()

    # Read current state
    st = await _state_get(agent_id)
    try:
        items_cur    = json.loads(st.get("items") or "[]") if isinstance(st.get("items"), str) else (st.get("items") or [])
        promises_cur = json.loads(st.get("promises") or "[]") if isinstance(st.get("promises"), str) else (st.get("promises") or [])
    except Exception:
        items_cur, promises_cur = [], []

    changed = False

    # Merge items (cap at 10)
    for it in (data.get("items") or []):
        name = str(it.get("name","")).strip()
        if not name:
            continue
        if not any(x.get("name","") == name for x in items_cur):
            items_cur.append({"name": name, "desc": str(it.get("desc",""))[:60], "added_at": now_iso})
            changed = True
    items_cur = items_cur[-10:]  # keep last 10

    # Merge promises (cap at 10)
    for pr in (data.get("promises") or []):
        content = str(pr.get("content","")).strip()
        if not content or len(content) < 5:
            continue
        if not any(x.get("content","") == content for x in promises_cur):
            promises_cur.append({"content": content, "made_at": now_iso})
            changed = True
    promises_cur = promises_cur[-10:]

    if changed:
        await _state_set(agent_id,
                         items=json.dumps(items_cur, ensure_ascii=False),
                         promises=json.dumps(promises_cur, ensure_ascii=False))
        print(f"[items_promises] {agent_id}: items={len(items_cur)} promises={len(promises_cur)}", flush=True)


async def _check_promise_reminders(agent_id: str) -> None:
    """Morning/nightly: probabilistically mention overdue promises (Notion spec §4.4).

    Reminder probability by days since promise:
    3d→10%, 7d→25%, 14d→40%, 30d→60%
    Only adds to morning push context; does NOT send independently.
    """
    import datetime as _dtpr, json as _jpr, random as _rnpr
    st = await _state_get(agent_id)
    try:
        promises = json.loads(st.get("promises") or "[]") if isinstance(st.get("promises"), str) else (st.get("promises") or [])
    except Exception:
        return

    if not promises:
        return

    now = _dtpr.datetime.utcnow()
    pending = []
    for pr in promises:
        made_at_str = pr.get("made_at", "")
        if not made_at_str:
            continue
        try:
            made_at = _dtpr.datetime.fromisoformat(made_at_str.split("+")[0].replace("Z",""))
            days = (now - made_at).days
        except Exception:
            continue

        prob = (0.10 if days >= 3 else 0) or (0.25 if days >= 7 else 0) or \
               (0.40 if days >= 14 else 0) or (0.60 if days >= 30 else 0)
        if prob > 0 and _rnpr.random() < prob:
            pending.append((days, pr["content"]))

    if not pending:
        return

    # Append to scene_note so it shows up in tomorrow's morning push context
    days, content = max(pending, key=lambda x: x[0])
    tone = ("随口一提" if days < 7 else
            "自然提起" if days < 14 else
            "有点在意" if days < 30 else "认真追问")
    note = f"承诺提醒({tone})：「{content}」（{days}天前）"
    await _state_set(agent_id, scene_note=note)


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
            await _mem_write_smart_vec(
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


async def _r2_list_prefix(prefix: str) -> list[str]:
    """Return list of R2 object keys under the given prefix. Returns [] if R2 not configured."""
    try:
        async with _db_pool.acquire() as _rc:
            _rrows = {r["key"]: r["value"] for r in await _rc.fetch(
                "SELECT key,value FROM gateway_config WHERE key LIKE 'r2_%'")}
        if _rrows.get("r2_enabled") != "true":
            return []
        _acid = _rrows.get("r2_account_id", "")
        _akey = _rrows.get("r2_access_key", "")
        _skey = _rrows.get("r2_secret_key", "")
        _bkt  = _rrows.get("r2_bucket", "")
        if not all([_acid, _akey, _skey, _bkt]):
            return []
        import boto3, asyncio as _aio
        from botocore.config import Config as _BConf
        _s3 = boto3.client(
            service_name="s3",
            endpoint_url=f"https://{_acid}.r2.cloudflarestorage.com",
            aws_access_key_id=_akey,
            aws_secret_access_key=_skey,
            region_name="auto",
            config=_BConf(signature_version="s3v4"),
        )
        _loop = _aio.get_event_loop()
        keys: list[str] = []
        paginator = await _loop.run_in_executor(None, lambda: _s3.get_paginator("list_objects_v2"))
        pages = await _loop.run_in_executor(
            None,
            lambda: list(paginator.paginate(Bucket=_bkt, Prefix=prefix))
        )
        for page in pages:
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys
    except Exception as _le:
        print(f"[r2-list] error listing {prefix}: {_le}", flush=True)
        return []


async def _verify_r2_backups(days: int = 7) -> dict:
    """Check R2 for backup completeness over the last `days` days.

    For each calendar day (UTC, going back from yesterday), we expect:
      - At least one  memory_backup_*.json  under  daily/{YYYY/MM/DD}/
      - At least one  palimpsest_*.db       under  daily/{YYYY/MM/DD}/

    Returns a dict:
      {
        "ok": bool,          # True = all days present and complete
        "checked": int,      # number of days inspected
        "missing": [str],    # dates with no files at all (YYYY-MM-DD)
        "incomplete": [str], # dates where only one of the two files exists
        "r2_enabled": bool,
      }
    """
    import datetime as _dv
    result: dict = {"ok": True, "checked": days, "missing": [], "incomplete": [], "r2_enabled": False}

    # Quick R2-enabled check
    try:
        async with _db_pool.acquire() as _rc:
            _rv = {r["key"]: r["value"] for r in await _rc.fetch(
                "SELECT key,value FROM gateway_config WHERE key LIKE 'r2_%'")}
        if _rv.get("r2_enabled") != "true":
            return result  # R2 off — nothing to verify
        result["r2_enabled"] = True
    except Exception:
        return result

    today_utc = _dv.datetime.utcnow().date()
    for offset in range(1, days + 1):
        day = today_utc - _dv.timedelta(days=offset)
        prefix = f"daily/{day.year}/{day.month:02d}/{day.day:02d}/"
        keys = await _r2_list_prefix(prefix)
        has_json = any(k.endswith(".json") for k in keys)
        has_db   = any(k.endswith(".db")   for k in keys)
        if not has_json and not has_db:
            result["missing"].append(str(day))
            result["ok"] = False
        elif not has_json or not has_db:
            result["incomplete"].append(str(day))
            result["ok"] = False

    return result


async def _weekly_backup_verify_loop() -> None:
    """Background task: every Monday ~09:00 CST (01:00 UTC), verify R2 backup completeness.

    Checks past 7 days. Sends Telegram alert if any day is missing or incomplete.
    """
    import datetime as _dwk
    # Wait until next Monday 01:00 UTC
    now = _dwk.datetime.utcnow()
    # weekday(): Mon=0 … Sun=6
    days_until_monday = (7 - now.weekday()) % 7  # 0 if today is Monday
    target = (now + _dwk.timedelta(days=days_until_monday)).replace(
        hour=1, minute=0, second=0, microsecond=0)
    if target <= now:
        target += _dwk.timedelta(weeks=1)
    wait_secs = (target - now).total_seconds()
    await asyncio.sleep(wait_secs)

    while True:
        try:
            report = await _verify_r2_backups(days=7)
            if not report["r2_enabled"]:
                print("[backup-verify] R2 not enabled, skipping check")
            elif report["ok"]:
                print("[backup-verify] ✅ All backups present for past 7 days")
            else:
                lines = ["⚠️ <b>备份完整性告警</b>"]
                if report["missing"]:
                    lines.append(f"❌ <b>完全缺失</b>：{', '.join(report['missing'])}")
                if report["incomplete"]:
                    lines.append(f"🟡 <b>不完整</b>（json/db 二者之一缺失）：{', '.join(report['incomplete'])}")
                lines.append("\n请检查 R2 bucket 和备份任务日志。")
                await _telegram_send("\n".join(lines), parse_mode="HTML")
                print(f"[backup-verify] ⚠️ issues found: missing={report['missing']} incomplete={report['incomplete']}")
        except Exception as _ve:
            print(f"[backup-verify error] {_ve}")
        await asyncio.sleep(7 * 24 * 3600)   # sleep 1 week


# ── Agent type config ──────────────────────────────────────────────────────────

async def _get_agent_config(agent_id: str) -> dict:
    """Return full agent config including agent_type, mcp settings."""
    agent_id = (agent_id or "default").strip().lower() or "default"
    defaults = {
        "agent_type": "agent", "mcp_enabled": True,
        "auto_memory": False, "mcp_proxy_config": {},
        "llm_model": "", "api_chain": "",
        "prompt_enabled": True, "worldbook_enabled": True, "prompt_inject_mode": "always",
        "mcp_stubborn_compat": False,
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
                "mcp_stubborn_compat": bool(row.get("mcp_stubborn_compat")),
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
            # Character state — Notion spec §4.7 format
            try:
                st = await _state_get(agent_id)
                _fat   = st.get("fatigue", 0)
                _mood  = st.get("mood_label", "neutral")
                _ms    = st.get("mood_score", 0)
                _miss  = float(st.get("miss_you") or 0.0)
                _lm    = float(st.get("low_mood") or 0.0)
                _irr   = float(st.get("irritable") or 0.0)
                _busy  = st.get("busy_level", "normal")
                _health = st.get("health_status", "healthy")
                _items   = st.get("items", "[]")
                _promises = st.get("promises", "[]")

                state_lines = [
                    "[角色当前状态]",
                    f"情绪：{_mood}（mood_score={_ms:+d}，惯性延续中，非固定）",
                    f"疲劳度：{_fat}/5",
                    f"思念值：{_miss:.1f}/10",
                ]
                if _lm > 0.5:
                    state_lines.append(f"低落值：{_lm:.1f}/8")
                if _irr > 0.5:
                    state_lines.append(f"烦躁值：{_irr:.1f}/6")
                if _busy != "normal":
                    state_lines.append(f"忙碌状态：{_busy}")
                if _health != "healthy":
                    state_lines.append(f"健康状态：{_health}")
                try:
                    import json as _jst, datetime as _dtit, random as _rnit
                    items_list = _jst.loads(_items) if isinstance(_items, str) else (_items or [])
                    _now_it = _dtit.datetime.utcnow()
                    _FOOD_KW = ["水果","蛋糕","咖啡","饼干","零食","外卖","饭","面","菜","鸡蛋",
                                "牛奶","苹果","巧克力","糖","饮料","茶","奶茶","果汁","冰淇淋"]
                    # Auto-expire: food items 7d, others 30d
                    valid_items = []
                    for _it in items_list:
                        _added = _it.get("added_at","")
                        if not _added:
                            valid_items.append(_it)
                            continue
                        try:
                            _age = (_now_it - _dtit.datetime.fromisoformat(_added.split("+")[0].replace("Z",""))).days
                            _is_food = any(kw in _it.get("name","") + _it.get("desc","") for kw in _FOOD_KW)
                            if (_is_food and _age <= 7) or (not _is_food and _age <= 30):
                                valid_items.append(_it)
                        except Exception:
                            valid_items.append(_it)
                    if valid_items != items_list:
                        # Write back expired items removed
                        await _state_set(agent_id, items=_jst.dumps(valid_items, ensure_ascii=False))
                    if valid_items:
                        _item_names = "、".join(x.get("name","?") for x in valid_items[:5])
                        state_lines.append("持有物品：" + _item_names)
                        # 15% probability: nudge a natural casual mention
                        if _rnit.random() < 0.15:
                            _pick = _rnit.choice(valid_items)
                            state_lines.append(
                                f"（今天可以自然提起「{_pick['name']}」，不超过一句，不刻意）"
                            )
                except Exception:
                    pass
                try:
                    import datetime as _dtpr2
                    prom_list = __import__("json").loads(_promises) if isinstance(_promises, str) else (_promises or [])
                    if prom_list:
                        _prom_strs = []
                        for _pr in prom_list[:3]:
                            _pc = _pr.get("content","")
                            _pm = _pr.get("made_at","")
                            if _pc:
                                try:
                                    _days_ago = (_dtpr2.datetime.utcnow() - _dtpr2.datetime.fromisoformat(
                                        _pm.split("+")[0].replace("Z",""))).days if _pm else 0
                                    _prom_strs.append(f"{_pc}（{_days_ago}天前）")
                                except Exception:
                                    _prom_strs.append(_pc)
                        if _prom_strs:
                            state_lines.append("未兑现承诺：" + "；".join(_prom_strs))
                except Exception:
                    pass
                state_lines.append("")
                state_lines.append("注意：以上状态供参考以保持一致性。不要机械表演状态，让它自然影响语气和精力水平。疲劳度高时回复可以更简短。")
                parts.append(chr(10).join(state_lines))

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
            # Auto-inject weather + traffic — from user_config (M4)
            try:
                async with _db_pool.acquire() as _wc:
                    _uc_row = await _wc.fetchrow(
                        "SELECT value FROM user_config WHERE key='user_context'")
                _ucv = _uc_row["value"] if _uc_row else None
                _uc = (_ucv if isinstance(_ucv, dict) else __import__("json").loads(_ucv)) if _ucv else {}
                _loc     = _uc.get("location") or {}
                _city    = _loc.get("city", "")
                _show_loc = _loc.get("show_location", True)
                _ds      = _uc.get("data_sources") or {}
                _commute = _uc.get("commute") or {}
                # Weather
                if _ds.get("weather", False):
                    _weather = await amap_weather(_city)
                    if _weather and not _weather.startswith("Error") and not _weather.startswith("Weather error"):
                        if not _show_loc and " | " in _weather:
                            # Strip leading "城市名 | " — split on first separator
                            _weather = _weather.split(" | ", 1)[1]
                        parts.append(f"🌤 天气：{_weather}")

                # Traffic (commute routes)
                if _ds.get("traffic", False) and _commute.get("enabled") and _commute.get("routes"):
                    _route_lines = []
                    for _route_str in _commute["routes"][:3]:  # max 3 routes
                        _segs = [s.strip() for s in str(_route_str).replace("→", "→").split("→") if s.strip()]
                        if len(_segs) < 2:
                            continue
                        try:
                            _rt = await amap_route(_segs[0], _segs[1], city=_city)
                            if _rt and not _rt.startswith("Route error") and not _rt.startswith("Error"):
                                _route_lines.append(f"  {_rt}")
                        except Exception:
                            continue
                    if _route_lines:
                        parts.append("🚗 路况：\n" + "\n".join(_route_lines))
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

    elif tool_name == "weather":
        # Keyword-triggered: return real-time weather for the user's configured city
        try:
            async with _db_pool.acquire() as _wc2:
                _uc_row2 = await _wc2.fetchrow(
                    "SELECT value FROM user_config WHERE key='user_context'")
            _ucv2 = _uc_row2["value"] if _uc_row2 else None
            _uc2 = (
                _ucv2 if isinstance(_ucv2, dict) else __import__("json").loads(_ucv2)
            ) if _ucv2 else {}
            _city2 = (_uc2.get("location") or {}).get("city", "")
            if not _city2:
                return ""
            _w2 = await amap_weather(_city2)
            if _w2 and not _w2.startswith("Error") and not _w2.startswith("Weather error"):
                return f"[实时天气] {_w2}"
            return ""
        except Exception as e:
            print(f"[proxy] weather: {e}")
            return ""

    elif tool_name == "news":
        # Keyword-triggered: inject today's news headlines into context
        try:
            _news_cfg2 = await _get_news_config()
            if not _news_cfg2.get("enabled", True):
                return ""
            headlines = await _get_today_news(max_items=3)
            if not headlines:
                return ""
            lines = []
            for h in headlines:
                src = h.get("source", "")
                title = h.get("title", "")
                summary = h.get("summary", "")
                line = f"• {title}（{src}）"
                if summary:
                    line += f"\n  {summary[:80]}"
                lines.append(line)
            return "[今日新闻]\n" + "\n".join(lines)
        except Exception as e:
            print(f"[proxy] news: {e}")
            return ""

    elif tool_name == "calendar":
        # Keyword-triggered: fetch upcoming calendar events from Todoist/GCAL
        try:
            import os as _osc
            import datetime as _dtc
            import httpx as _hxc
            lines = []

            # Try Google Calendar first
            gcal_token = _osc.getenv("GCAL_BEARER_TOKEN", "")
            gcal_cal_id = _osc.getenv("GCAL_CALENDAR_ID", "primary")
            if gcal_token:
                try:
                    _now = _dtc.datetime.utcnow()
                    _max = (_now + _dtc.timedelta(days=7)).isoformat() + "Z"
                    _min = _now.isoformat() + "Z"
                    async with _hxc.AsyncClient(timeout=8) as _cl:
                        _r = await _cl.get(
                            f"https://www.googleapis.com/calendar/v3/calendars/{gcal_cal_id}/events",
                            headers={"Authorization": f"Bearer {gcal_token}"},
                            params={"timeMin": _min, "timeMax": _max,
                                    "singleEvents": "true", "orderBy": "startTime",
                                    "maxResults": "5"},
                        )
                    if _r.status_code == 200:
                        for ev in _r.json().get("items", []):
                            _st = ev.get("start", {})
                            _dt = _st.get("dateTime") or _st.get("date", "")
                            _dt_fmt = _dt[:16].replace("T", " ") if "T" in _dt else _dt
                            lines.append(f"• {_dt_fmt} {ev.get('summary','(无标题)')}")
                except Exception:
                    pass

            # Fallback to Todoist
            if not lines:
                tasks = await todoist_get_tasks_raw(limit=5)
                for t in tasks:
                    _due = (t.get("due") or {}).get("string", "")
                    lines.append(f"• {_due} {t['content']}" if _due else f"• {t['content']}")

            if not lines:
                return ""
            return "[近期日程]\n" + "\n".join(lines)
        except Exception as e:
            print(f"[proxy] calendar: {e}")
            return ""

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
    tools = proxy_cfg.get("tools") or [
        {"name": "memory_surface",  "trigger_mode": "auto"},
        {"name": "daily_life_read", "trigger_mode": "auto"},
        {
            "name": "weather",
            "trigger_mode": "keyword",
            "intent_check": True,   # §12.2: skip casual weather mentions
            "triggers": ["天气", "下雨", "下雪", "热", "冷", "出门", "穿什么",
                         "外面", "温度", "天晴", "阴天", "刮风", "晴天"],
        },
        {
            "name": "news",
            "trigger_mode": "keyword",
            "intent_check": True,   # §12.2: skip casual news mentions
            "triggers": ["新闻", "最近发生", "今天发生", "热点", "时事", "有什么大事",
                         "最新消息", "今日", "社会", "国际", "国内", "发生了什么"],
        },
        {
            "name": "calendar",
            "trigger_mode": "keyword",
            "intent_check": True,   # §12.2: skip casual time references
            "triggers": ["日程", "安排", "计划", "提醒", "明天", "后天", "下周",
                         "会议", "约", "几点", "什么时候", "记一下", "加一个"],
        },
    ]
    injected: list[str] = []

    # ── P0.2: Timezone injection at top (Notion spec §1) ─────────────────────
    try:
        _tz_ctx = await build_time_context(agent_id)
        if _tz_ctx:
            injected.append(_tz_ctx)
    except Exception as _tze:
        print(f"[mcp_proxy] tz_inject err: {_tze}")
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
                    # ── Intent classification layer (§12.2 layer 2) ──────────
                    # If intent_check=True, ask the analyzer model whether the user
                    # genuinely needs this tool or is just casually mentioning the keyword.
                    if tool.get("intent_check"):
                        tool_label = {"weather": "天气信息", "news": "新闻/时事",
                                      "calendar": "日程/提醒"}.get(tool["name"], tool["name"])
                        _ic_prompt = (
                            f"用户最近说：「{recent[:200]}」\n"
                            f"问题：用户是否真的需要查询「{tool_label}」？"
                            "如果只是随口提到相关词语、表达情绪或背景描述，回答 NO。"
                            "如果用户明确想要信息或帮助，回答 YES。"
                            "只回答 YES 或 NO，不要解释。"
                        )
                        try:
                            _ic_ans = (await _call_llm_route("analyzer", _ic_prompt)).strip().upper()
                            if not _ic_ans.startswith("Y"):
                                continue  # not a genuine intent → skip tool
                        except Exception as _ice:
                            print(f"[mcp_proxy] intent_check err ({tool['name']}): {_ice}")
                            # On error, fall through and run the tool anyway
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
                await _mem_write_smart_vec(
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



# ─────────────────────────────────────────────────────────────────────────────
# P2.2  Reverse Identification (Notion spec §7)
# Post-conversation: cheap model detects state changes the char announced
# ─────────────────────────────────────────────────────────────────────────────

_REVERSE_KEYWORDS: dict[str, list[str]] = {
    "busy":        ["最近有点忙", "在忙", "赶ddl", "赶deadline", "加班", "项目很紧", "忙死了"],
    "sick":        ["感冒了", "不舒服", "头疼", "生病了", "发烧", "嗓子疼", "肚子不舒服"],
    "schedule":    ["明天要", "明天得", "明天有", "下周", "后天", "约了", "要开会", "要上课"],
    "location":    ["去你这", "来你那", "到了", "在你家", "搬过来", "回去了", "出差", "出门了"],
}


async def _reverse_identify(agent_id: str, messages: list) -> None:
    """Post-conversation analyzer: detect character self-stated state changes.

    Two-layer approach (Notion spec §7.2):
    1. Keyword pre-filter (zero cost)
    2. Cheap LLM analysis only if keywords match
    Auto-applies state changes with confidence >= threshold.
    """
    # Extract only character (assistant) messages
    char_msgs = [
        m.get("content", "")
        for m in messages
        if m.get("role") == "assistant" and isinstance(m.get("content"), str)
    ]
    if not char_msgs:
        return
    char_text = " ".join(char_msgs[-6:])  # last 6 assistant turns

    # Layer 1: keyword pre-filter
    matched_categories = [
        cat for cat, kws in _REVERSE_KEYWORDS.items()
        if any(kw in char_text for kw in kws)
    ]
    if not matched_categories:
        return

    # Layer 2: analyzer LLM
    try:
        st = await _state_get(agent_id)
        analyzer_prompt = (
            "你是角色状态分析器。阅读角色对话输出，提取角色自述的状态变化。只提取角色主动表达的，不推测。\n\n"
            f"角色对话（最近几轮）：\n{char_text[:1500]}\n\n"
            f"当前状态：busy_level={st.get('busy_level','normal')}, "
            f"health_status={st.get('health_status','healthy')}\n\n"
            "输出 JSON（只输出 JSON，不要其他内容）：\n"
            '{"state_changes": [{"field": "...", "new_value": "...", "confidence": 0.0, "evidence": "..."}], '
            '"schedule_mentions": [{"content": "...", "time": "...", "importance": "normal"}]}'
        )
        raw = await _call_llm_route("analyzer", analyzer_prompt)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
    except Exception:
        return

    # Auto-apply state changes
    for change in (data.get("state_changes") or []):
        field   = change.get("field", "")
        new_val = change.get("new_value", "")
        conf    = float(change.get("confidence") or 0.0)
        if field in ("busy_level",) and conf >= 0.7:
            await _state_set(agent_id, busy_level=str(new_val))
            print(f"[reverse_id] auto-set {agent_id}.{field}={new_val!r} (conf={conf:.2f})", flush=True)
        elif field in ("health_status",) and conf >= 0.7:
            await _state_set(agent_id, health_status=str(new_val))
            print(f"[reverse_id] auto-set {agent_id}.{field}={new_val!r} (conf={conf:.2f})", flush=True)
        elif field in ("mood_label", "mood_score") and conf >= 0.6:
            if field == "mood_label":
                await _state_set(agent_id, mood_label=str(new_val))
            elif field == "mood_score":
                try:
                    await _state_set(agent_id, mood_score=int(new_val))
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# P1.3  Schedule Capture from Conversation (Notion spec §2.3)
# ─────────────────────────────────────────────────────────────────────────────

_SCHEDULE_PATTERNS_RE = [
    r"明天.{0,10}(要|得|需要|记得)",
    r"后天.{0,10}(要|得)",
    r"下周[一二三四五六日].{0,10}(要|有|是)",
    r"\d{1,2}[月号日].{0,10}(要|有|截止|deadline)",
    r"别忘了", r"记得", r"提醒我",
]


async def _capture_schedules_from_conversation(agent_id: str, messages: list) -> None:
    """Detect user-mentioned schedules/reminders → classify → write to calendar/Todoist.

    type='schedule'  (has date+time) → Google Calendar (if GCAL_BEARER_TOKEN+GCAL_CALENDAR_ID set)
                                       OR Todoist with due date (fallback)
    type='reminder'  (no specific time) → Todoist task
    type='both'      (uncertain)        → both

    Keyword pattern pre-filter → analyzer LLM extracts & classifies → write to target(s).
    """
    import re as _re_sc, os as _os_sc, httpx as _hx_sc
    _todoist_key  = _os_sc.getenv("TODOIST_API_TOKEN", _os_sc.getenv("TODOIST_API_KEY", ""))
    _gcal_token   = _os_sc.getenv("GCAL_BEARER_TOKEN", "")
    _gcal_cal_id  = _os_sc.getenv("GCAL_CALENDAR_ID", "primary")
    if not _todoist_key and not _gcal_token:
        return

    # Only scan user messages
    user_msgs = [
        m.get("content", "")
        for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    if not user_msgs:
        return
    user_text = " ".join(user_msgs[-5:])

    # Pre-filter
    if not any(_re_sc.search(p, user_text) for p in _SCHEDULE_PATTERNS_RE):
        return

    try:
        extract_prompt = (
            "从以下用户对话中提取用户提到的未来计划或提醒事项（只提取明确的，不推测）。\n\n"
            "分类规则：\n"
            "- 有明确日期/时间的事件（会议、约定、截止日等）→ type='schedule'\n"
            "- 没有明确时间的待办/提醒（买东西、记得做某事）→ type='reminder'\n"
            "- 不确定时 → type='both'\n\n"
            f"用户对话：{user_text[:800]}\n\n"
            "输出 JSON 数组（没有则输出 []）：\n"
            '[{"content":"事项内容","time":"时间描述(如明天下午/下周一)","importance":"high或normal","type":"schedule|reminder|both"}]'
        )
        raw = await _call_llm_route("analyzer", extract_prompt)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        schedules = json.loads(raw)
        if not isinstance(schedules, list):
            return
    except Exception:
        return

    for sch in schedules[:3]:
        content  = str(sch.get("content", "")).strip()
        if not content or len(content) < 3:
            continue
        time_str = str(sch.get("time", "")).strip()
        item_type = str(sch.get("type", "both")).lower()
        priority  = 4 if sch.get("importance") == "high" else 1

        # ── Write to Google Calendar (schedule or both) ───────────────────────
        write_calendar = item_type in ("schedule", "both")
        if write_calendar and _gcal_token:
            try:
                import datetime as _dtgc
                _gcal_body: dict = {
                    "summary": content,
                    "description": f"[由AI对话捕获] {time_str}" if time_str else "[由AI对话捕获]",
                }
                # Attempt to build a simple all-day event for tomorrow if no exact time
                _tomorrow = (_dtgc.date.today() + _dtgc.timedelta(days=1)).isoformat()
                _gcal_body["start"] = {"date": _tomorrow}
                _gcal_body["end"]   = {"date": _tomorrow}
                async with _hx_sc.AsyncClient(timeout=15) as _cl:
                    await _cl.post(
                        f"https://www.googleapis.com/calendar/v3/calendars/{_gcal_cal_id}/events",
                        headers={"Authorization": f"Bearer {_gcal_token}",
                                 "Content-Type": "application/json"},
                        json=_gcal_body,
                    )
                print(f"[schedule_capture] gcal: '{content}'", flush=True)
            except Exception as _ge:
                print(f"[schedule_capture] gcal write failed: {_ge}")

        # ── Write to Todoist (reminder or both, or schedule fallback) ─────────
        write_todoist = item_type in ("reminder", "both") or (write_calendar and not _gcal_token)
        if write_todoist and _todoist_key:
            try:
                task_label  = "[日程] " if item_type == "schedule" else ""
                task_content = task_label + content + (f"（{time_str}）" if time_str else "")
                _td_body: dict = {"content": task_content, "priority": priority}
                if time_str:
                    _td_body["due_string"] = time_str
                async with _hx_sc.AsyncClient(timeout=15) as _cl:
                    await _cl.post(
                        "https://api.todoist.com/rest/v2/tasks",
                        headers={"Authorization": f"Bearer {_todoist_key}"},
                        json=_td_body,
                    )
                print(f"[schedule_capture] todoist ({item_type}): '{task_content}'", flush=True)
            except Exception as _te:
                print(f"[schedule_capture] todoist write failed: {_te}")


async def _post_conversation_tasks(
    agent_id: str, session_id: str, full: list,
    cid: str, agent_type: str, auto_memory: bool,
) -> None:
    """Run all post-conversation storage tasks in a single background coroutine."""
    await _store_conversation(cid, agent_id, session_id, full)
    if agent_type == "character":
        if auto_memory:
            await _auto_extract_character_memory(agent_id, full)
        # Track last user message time for silence detection
        try:
            import datetime as _dtpc
            await _state_set(agent_id, last_user_msg=_dtpc.datetime.utcnow().isoformat())
        except Exception:
            pass
        # Drain miss_you accumulator when conversation happens (Notion spec §4.3)
        try:
            await _accumulate_state(agent_id, {"miss_you": -10.0, "low_mood": -3.0})
        except Exception:
            pass
        # P2.2 Reverse identification: keyword pre-filter → analyzer LLM
        try:
            await _reverse_identify(agent_id, full)
        except Exception as _rie:
            print(f"[post_conv] reverse_identify err: {_rie}")
        # P1.3 Schedule capture from conversation
        try:
            await _capture_schedules_from_conversation(agent_id, full)
        except Exception as _sce:
            print(f"[post_conv] schedule_capture err: {_sce}")
        # P2.3 Items & promises extraction
        try:
            await _extract_items_promises(agent_id, full)
        except Exception as _ipe:
            print(f"[post_conv] items_promises err: {_ipe}")
        # §14 Music: keyword trigger
        try:
            if _music_keyword_in_text(full) and await _cd_check(agent_id, "music_recommend"):
                asyncio.create_task(_music_pick_and_send(agent_id, trigger_mode="keyword"))
        except Exception as _mke:
            print(f"[post_conv] music_keyword err: {_mke}")
        # §14 Music: mood trigger (low valence)
        try:
            _st_mood = await _state_get(agent_id)
            _valence = float(_st_mood.get("mood_valence") or 0.0)
            if _valence < -0.35 and await _cd_check(agent_id, "music_recommend"):
                _mood_lbl = _st_mood.get("mood_label", "")
                asyncio.create_task(_music_pick_and_send(
                    agent_id, trigger_mode="mood_low", mood_tag=_mood_lbl))
            elif _valence > 0.60 and await _cd_check(agent_id, "music_recommend"):
                asyncio.create_task(_music_pick_and_send(
                    agent_id, trigger_mode="mood_high", mood_tag="happy"))
        except Exception as _mme:
            print(f"[post_conv] music_mood err: {_mme}")
    else:
        # Agent type: distill into Palimpsest L1-L4
        await _distill_and_store(agent_id, session_id, full, agent_type=agent_type)
    # Ingest exchange pairs into Qdrant conversations collection (RAG pipeline)
    asyncio.create_task(_ingest_conv_to_qdrant(agent_id, full, cid))


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

        # RAG: inject semantically relevant memories + conversation history
        try:
            _user_q = next(
                (m["content"] for m in reversed(messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)), ""
            )
            _rag = await _build_rag_context(_user_q, agent_id)
            if _rag:
                _rag_msg = {"role": "system", "content": "[语义检索]\n" + _rag, "_wb": True}
                if messages and messages[0].get("role") == "system":
                    messages.insert(1, _rag_msg)
                else:
                    messages.insert(0, _rag_msg)
        except Exception as _rage:
            print(f"[rag_inject] {_rage}")

    # Build ordered (provider, model) call list from llm_chain_config or fallback
    call_list = _build_call_list(_agent_cfg)
    # Explicit model in request body overrides everything
    req_model = body.get("model", "")

    _stubborn_compat = _agent_cfg.get("mcp_stubborn_compat", False)
    _tool_strip_keys = {"tools", "tool_choice", "functions", "function_call", "tool_use"}
    payload = {
        k: v for k, v in body.items()
        if k not in ("agent_id", "session_id")
        and not (_stubborn_compat and k in _tool_strip_keys)
    }
    payload["messages"] = messages
    if _stubborn_compat and any(k in body for k in _tool_strip_keys):
        print(f"[stubborn_compat] {agent_id}: stripped tool keys from payload", flush=True)

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


# ── Trash / Recycle-bin endpoints ────────────────────────────────────────────
# IMPORTANT: must stay ABOVE the /{memory_id} catchall routes.

@app.get("/api/admin/memories/trash")
async def trash_list(agent_id: str, limit: int = 200, _=Depends(_require_key)):
    """List all archived (soft-deleted) memories for an agent."""
    from memory_db import get_db as _gdb_tr
    db = await _gdb_tr()
    cur = await db.execute(
        "SELECT * FROM memories WHERE agent_id=? AND archived=1 "
        "ORDER BY updated_at DESC LIMIT ?",
        (agent_id, limit),
    )
    rows = await cur.fetchall()
    from memory_db import _row_to_dict as _rtd
    return {"items": [_rtd(r) for r in rows], "total": len(rows)}


@app.post("/api/admin/memories/trash/{memory_id}/restore")
async def trash_restore(memory_id: str, _=Depends(_require_key)):
    """Restore a soft-deleted memory (set archived=0 and re-sync Qdrant)."""
    from memory_db import get_db as _gdb_tr2
    db = await _gdb_tr2()
    from memory_db import _now as _mnow, _row_to_dict as _rtd2
    cur = await db.execute(
        "SELECT * FROM memories WHERE id=? AND archived=1", (memory_id,)
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Memory not found in trash")
    await db.execute(
        "UPDATE memories SET archived=0, updated_at=? WHERE id=?",
        (_mnow(), memory_id),
    )
    await db.commit()
    cur2 = await db.execute("SELECT * FROM memories WHERE id=?", (memory_id,))
    mem = _rtd2(await cur2.fetchone())
    # Re-sync Qdrant
    asyncio.create_task(_sync_memory_to_qdrant(mem))
    return {"ok": True, "restored": memory_id}


@app.delete("/api/admin/memories/trash")
async def trash_empty(agent_id: str, _=Depends(_require_key)):
    """Hard-delete ALL archived memories for an agent (empty trash)."""
    from memory_db import get_db as _gdb_tr3
    db = await _gdb_tr3()
    cur = await db.execute(
        "SELECT id FROM memories WHERE agent_id=? AND archived=1", (agent_id,)
    )
    rows = await cur.fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return {"ok": True, "deleted": 0}
    await db.execute(
        "DELETE FROM memories WHERE agent_id=? AND archived=1", (agent_id,)
    )
    await db.commit()
    # Purge from Qdrant
    if _qdrant:
        try:
            qids = [_mem_id_to_qdrant(mid) for mid in ids]
            _qdrant.delete(QDRANT_MEM_COLLECTION,
                           points_selector=PointIdsList(points=qids))
        except Exception as _qe:
            print(f"[trash_empty_qdrant] {_qe}")
    return {"ok": True, "deleted": len(ids)}


# ── L1 pending confirmation (must be BEFORE {memory_id} catchall) ────────────

@app.get("/api/admin/memories/pending-l1")
async def pal_pending_l1(agent_id: str, _=Depends(_require_key)):
    """List all unconfirmed L1 memories (confirmed=0) for an agent."""
    rows = await _mem_pending_l1(agent_id)
    return {"items": [_pal_row(r) for r in rows], "total": len(rows)}


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
    if _qdrant:
        try:
            _qid = _mem_id_to_qdrant(memory_id)
            if hard:
                _qdrant.delete(QDRANT_MEM_COLLECTION,
                               points_selector=PointIdsList(points=[_qid]))
            else:
                _qdrant.set_payload(QDRANT_MEM_COLLECTION,
                                    payload={"archived": 1},
                                    points_selector=PointIdsList(points=[_qid]))
        except Exception as _qde:
            print(f"[qdrant_pal_del] {_qde}")
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


@app.post("/api/admin/memories/{memory_id}/confirm-l1")
async def pal_confirm_l1(memory_id: str, _=Depends(_require_key)):
    """Confirm a pending L1 memory (set confirmed=1)."""
    mem = await _mem_confirm_l1(memory_id)
    if not mem:
        raise HTTPException(404, "Memory not found or already confirmed")
    return _pal_row(mem)



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


# ── Daily Skeleton config ──────────────────────────────────────────────────
_DAILY_SKELETON_DEFAULT = {
    "template":   "freelancer",
    "wake_up":    {"range": ["08:00", "11:00"], "bias": "late"},
    "sleep":      {"range": ["23:00", "02:00"]},
    "habits":     ["喝咖啡", "午睡"],
    "work_style": "remote",
}

@app.get("/admin/api/config/daily-skeleton")
async def get_daily_skeleton(agent_id: str = "", _=Depends(_require_key)):
    import json as _j
    async with _db_pool.acquire() as conn:
        row = None
        if agent_id:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key=$1",
                f"daily_skeleton:{agent_id}"
            )
        if not row:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='daily_skeleton'")
    if not row:
        return _DAILY_SKELETON_DEFAULT
    v = row["value"]
    return v if isinstance(v, dict) else _j.loads(v)

@app.post("/admin/api/config/daily-skeleton")
async def set_daily_skeleton(body: dict, _=Depends(_require_key)):
    import json as _j
    agent_id = body.pop("agent_id", "")
    key = f"daily_skeleton:{agent_id}" if agent_id else "daily_skeleton"
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES($1,$2::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$2::jsonb, updated_at=NOW()",
            key, _j.dumps(body)
        )
    return {"ok": True}


# ── Screen-time rules config ───────────────────────────────────────────────
@app.get("/admin/api/config/screen-time-rules")
async def get_screen_time_rules(agent_id: str = "", _=Depends(_require_key)):
    import json as _j
    async with _db_pool.acquire() as conn:
        row = None
        if agent_id:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key=$1",
                f"screen_time_rules:{agent_id}"
            )
        if not row:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='screen_time_rules'")
    if not row:
        return {"rules": _DEFAULT_SCREEN_RULES, "using_defaults": True}
    v = row["value"]
    rules = v if isinstance(v, list) else _j.loads(v)
    return {"rules": rules, "using_defaults": False}

@app.post("/admin/api/config/screen-time-rules")
async def set_screen_time_rules(body: dict, _=Depends(_require_key)):
    """body: {"rules": [...], "agent_id": "..."}"""
    import json as _j
    rules = body.get("rules")
    if not isinstance(rules, list):
        raise HTTPException(400, "body.rules must be an array")
    agent_id = body.get("agent_id", "")
    key = f"screen_time_rules:{agent_id}" if agent_id else "screen_time_rules"
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES($1,$2::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$2::jsonb, updated_at=NOW()",
            key, _j.dumps(rules)
        )
    return {"ok": True, "count": len(rules)}


# ── New config endpoints (Notion spec) ────────────────────────────────────

@app.get("/admin/api/config/api-routes")
async def get_api_routes(_=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM user_config WHERE key='api_routes'")
    rv = row["value"] if row else None
    data = (rv if isinstance(rv, dict) else json.loads(rv)) if rv else {}
    return {"routes": {**_DEFAULT_API_ROUTES, **data}}

@app.post("/admin/api/config/api-routes")
async def set_api_routes(body: dict, _=Depends(_require_key)):
    routes = body.get("routes")
    if not isinstance(routes, dict):
        raise HTTPException(400, "body.routes must be an object")
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('api_routes',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(routes)
        )
    return {"ok": True}


@app.get("/admin/api/config/push-control")
async def get_push_control_cfg(_=Depends(_require_key)):
    ctrl = await _get_push_control()
    return {"push_control": ctrl}

@app.post("/admin/api/config/push-control")
async def set_push_control_cfg(body: dict, _=Depends(_require_key)):
    ctrl = body.get("push_control", body)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('push_control',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(ctrl)
        )
    return {"ok": True}


@app.get("/admin/api/config/morning-push")
async def get_morning_push_cfg(_=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM user_config WHERE key='morning_push_config'")
    rv = row["value"] if row else None
    data = (rv if isinstance(rv, dict) else json.loads(rv)) if rv else {}
    defaults = {
        "time_window": ["08:15", "08:30"],
        "weather_normal_probability": 0.20,
        "weather_severe_probability": 0.80,
        "schedule_normal_probability": 0.45,
        "random_event_enabled": True,
    }
    return {"config": {**defaults, **data}}

@app.post("/admin/api/config/morning-push")
async def set_morning_push_cfg(body: dict, _=Depends(_require_key)):
    cfg = body.get("config", body)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('morning_push_config',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(cfg)
        )
    return {"ok": True}


@app.get("/admin/api/push-log")
async def get_push_log(agent_id: str = "", limit: int = 50, _=Depends(_require_key)):
    """Return recent push log entries for observability."""
    from memory_db import get_db as _gdb_pl
    db = await _gdb_pl()
    if agent_id:
        rows = await db.execute_fetchall(
            "SELECT * FROM push_log WHERE agent_id=? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit)
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM push_log ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
    return {"logs": [dict(r) for r in rows]}


@app.get("/admin/api/config/timezone")
async def get_timezone_cfg(_=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM user_config WHERE key='user_context'")
    rv = row["value"] if row else None
    uc = (rv if isinstance(rv, dict) else json.loads(rv)) if rv else {}
    return {"timezone": uc.get("timezone") or {
        "user": {"default": "Asia/Shanghai"},
        "character": {"default": "Asia/Shanghai"},
    }}

@app.post("/admin/api/config/timezone")
async def set_timezone_cfg(body: dict, _=Depends(_require_key)):
    """Merge timezone config into user_context JSON."""
    tz_data = body.get("timezone", body)
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM user_config WHERE key='user_context'")
    rv = row["value"] if row else None
    uc = (rv if isinstance(rv, dict) else json.loads(rv)) if rv else {}
    uc["timezone"] = tz_data
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('user_context',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(uc)
        )
    return {"ok": True}


@app.get("/admin/api/config/evening-push")
async def get_evening_push_cfg(_=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM user_config WHERE key='evening_push_config'")
    rv = row["value"] if row else None
    data = (rv if isinstance(rv, dict) else json.loads(rv)) if rv else {}
    defaults = {"enabled": True, "char_bedtime_window": ["22:30", "23:30"]}
    return {"config": {**defaults, **data}}

@app.post("/admin/api/config/evening-push")
async def set_evening_push_cfg(body: dict, _=Depends(_require_key)):
    cfg = body.get("config", body)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('evening_push_config',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(cfg)
        )
    return {"ok": True}


# ── News config ────────────────────────────────────────────────────────────
@app.get("/admin/api/config/news")
async def get_news_cfg(_=Depends(_require_key)):
    cfg = await _get_news_config()
    # Augment with hard_block defaults if not stored yet
    if "hard_block_keywords" not in cfg:
        cfg["hard_block_keywords"] = list(_NEWS_HARD_BLOCK)
    return {"config": cfg}

@app.post("/admin/api/config/news")
async def set_news_cfg(body: dict, _=Depends(_require_key)):
    cfg = body.get("config", body)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('news_config',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(cfg)
        )
    return {"ok": True}


# ── Music config ─────────────────────────────────────────────────────────────
_MUSIC_CFG_DEFAULTS: dict = {
    "enabled":           True,
    "daily_time_utc":    "07:00",
    "daily_probability": 0.35,
    "mood_low_keywords": ["治愈", "温暖", "陪伴", "轻柔"],
    "cooldown_hours":    48,
}

async def _get_music_config() -> dict:
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='music_config'")
        if row and row["value"]:
            v = row["value"]
            db_cfg = v if isinstance(v, dict) else json.loads(v)
            return {**_MUSIC_CFG_DEFAULTS, **db_cfg}
    except Exception:
        pass
    return dict(_MUSIC_CFG_DEFAULTS)

@app.get("/admin/api/config/music")
async def get_music_cfg(_=Depends(_require_key)):
    return {"config": await _get_music_config()}

@app.post("/admin/api/config/music")
async def set_music_cfg(body: dict, _=Depends(_require_key)):
    cfg = body.get("config", body)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('music_config',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(cfg)
        )
    return {"ok": True}


# ── Accumulator config ────────────────────────────────────────────────────────
_ACCUMULATOR_CFG_DEFAULTS: dict = {
    "miss_you": {"threshold": 10.0, "reset": 0.0},
    "low_mood": {"threshold":  8.0, "reset": 3.0},
}

async def _get_accumulator_config() -> dict:
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='accumulator_config'")
        if row and row["value"]:
            v = row["value"]
            db_cfg = v if isinstance(v, dict) else json.loads(v)
            return {k: {**_ACCUMULATOR_CFG_DEFAULTS[k], **(db_cfg.get(k) or {})}
                    for k in _ACCUMULATOR_CFG_DEFAULTS}
    except Exception:
        pass
    return {k: dict(v) for k, v in _ACCUMULATOR_CFG_DEFAULTS.items()}

@app.get("/admin/api/config/accumulator")
async def get_accumulator_cfg(_=Depends(_require_key)):
    return {"config": await _get_accumulator_config()}

@app.post("/admin/api/config/accumulator")
async def set_accumulator_cfg(body: dict, _=Depends(_require_key)):
    cfg = body.get("config", body)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('accumulator_config',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(cfg)
        )
    return {"ok": True}


# ── System info (read-only) ───────────────────────────────────────────────────
@app.get("/admin/api/config/system-info")
async def get_system_info(_=Depends(_require_key)):
    return {
        "distill_model":  os.getenv("DISTILL_MODEL", ""),
        "embed_provider": os.getenv("EMBED_PROVIDER", "nvidia"),
        "rsshub_url":     os.getenv("RSSHUB_URL", "https://rsshub.app"),
        "gateway_url":    os.getenv("GATEWAY_PUBLIC_URL", ""),
    }


# ── Health monitor config ───────────────────────────────────────────────────
@app.get("/admin/api/config/health")
async def get_health_cfg(_=Depends(_require_key)):
    cfg = await _get_health_config()
    return {"config": cfg}

@app.post("/admin/api/config/health")
async def set_health_cfg(body: dict, _=Depends(_require_key)):
    cfg = body.get("config", body)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('health_config',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            json.dumps(cfg)
        )
    return {"ok": True}


@app.get("/admin/api/chain-events")
async def get_chain_events(agent_id: str = "", include_fired: bool = False, _=Depends(_require_key)):
    """List pending (or all) chain events."""
    from memory_db import get_db as _gdb_ce
    db = await _gdb_ce()
    if agent_id:
        if include_fired:
            rows = await db.execute_fetchall(
                "SELECT * FROM chain_events WHERE agent_id=? ORDER BY fire_at DESC LIMIT 50", (agent_id,))
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM chain_events WHERE agent_id=? AND fired=0 ORDER BY fire_at LIMIT 50", (agent_id,))
    else:
        if include_fired:
            rows = await db.execute_fetchall("SELECT * FROM chain_events ORDER BY fire_at DESC LIMIT 100")
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM chain_events WHERE fired=0 ORDER BY fire_at LIMIT 100")
    return {"events": [dict(r) for r in rows]}


@app.delete("/admin/api/chain-events/{event_id}")
async def delete_chain_event(event_id: str, _=Depends(_require_key)):
    from memory_db import get_db as _gdb_ce2
    db = await _gdb_ce2()
    await db.execute("UPDATE chain_events SET fired=1 WHERE id=?", (event_id,))
    await db.commit()
    return {"ok": True}


@app.get("/admin/api/character-state/{agent_id}")
async def get_character_state_full(agent_id: str, _=Depends(_require_key)):
    """Return full character state including accumulators."""
    st = await _state_get(agent_id)
    return {"state": st}

@app.patch("/admin/api/character-state/{agent_id}")
async def patch_character_state(agent_id: str, body: dict, _=Depends(_require_key)):
    """Update character state fields (including accumulators)."""
    allowed = {"mood_score","mood_label","fatigue","scene","scene_note",
               "mood_valence","mood_energy","miss_you","low_mood","irritable",
               "items","promises","busy_level","health_status"}
    update = {k: v for k, v in body.items() if k in allowed}
    if not update:
        raise HTTPException(400, "No valid fields")
    await _state_set(agent_id, **update)
    return {"ok": True, "updated": list(update.keys())}


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
    import json as _jevt_add
    def _js(v, default="{}"):
        if isinstance(v, (dict, list)): return _jevt_add.dumps(v, ensure_ascii=False)
        return str(v) if v else default
    evt = await _event_add(
        content=body.get("content",""),
        level=body.get("level","green"),
        weight=float(body.get("weight",1.0)),
        agent_id=body.get("agent_id",""),
        scene=body.get("scene",""),
        conditions=_js(body.get("conditions"), "{}"),
        mood_effect=_js(body.get("mood_effect"), "{}"),
        accumulator_effect=_js(body.get("accumulator_effect"), "{}"),
        send_policy=body.get("send_policy","maybe"),
        send_probability=float(body.get("send_probability", 0.40)),
        chain_definition=_js(body.get("chain_definition"), ""),
    )
    return evt

@app.put("/admin/api/random-events/{event_id}")
async def admin_update_event(event_id: str, body: dict, _=Depends(_require_key)):
    import json as _jevt_upd
    def _js(v, default="{}"):
        if isinstance(v, (dict, list)): return _jevt_upd.dumps(v, ensure_ascii=False)
        return str(v) if v else default
    update = {}
    for k in ("content","level","scene","agent_id"):
        if k in body: update[k] = body[k]
    if "weight" in body: update["weight"] = float(body["weight"])
    if "send_probability" in body: update["send_probability"] = float(body["send_probability"])
    for k in ("conditions","mood_effect","accumulator_effect","chain_definition"):
        if k in body: update[k] = _js(body[k], "{}" if k != "chain_definition" else "")
    if "send_policy" in body: update["send_policy"] = body["send_policy"]
    result = await _event_update(event_id, **update)
    if not result: raise HTTPException(404, "Event not found")
    return result

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
                "mcp_proxy_config": {}, "system_prompt": "", "llm_chain_config": {},
                "mcp_stubborn_compat": False}
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
                 prompt_enabled, worldbook_enabled, llm_chain_config, mcp_stubborn_compat, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13::jsonb,$14,now())
            ON CONFLICT (agent_id) DO UPDATE SET
                llm_model=$2, api_chain=$3, notes=$4, avatar=$5,
                agent_type=$6, mcp_enabled=$7, auto_memory=$8,
                mcp_proxy_config=$9::jsonb, system_prompt=$10,
                prompt_enabled=$11, worldbook_enabled=$12,
                llm_chain_config=$13::jsonb, mcp_stubborn_compat=$14, updated_at=now()
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
             _chain_cfg,
             bool(body.get("mcp_stubborn_compat", False)))
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


@app.post("/admin/api/agents/{agent_id}/proactive")
async def trigger_proactive(agent_id: str, _=Depends(_require_key)):
    """Manually trigger the Layer 3 proactive messaging check for a character agent.

    Reads today's diary, searches related memories, and may send a Telegram message
    (subject to the proactive_casual cooldown).
    """
    await _check_proactive_triggers(agent_id)
    return {"agent_id": agent_id, "ok": True, "note": "check complete (message sent only if cooldown clear and 30% roll passes)"}


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


# ── A4: Conversation import — async job tracker ────────────────────────────────
_conv_import_jobs: dict[str, dict] = {}
# shape: {job_id: {status, total, done, skipped, errors, memories_created,
#                  embedded, agent_id, format}}


def _detect_import_format(data) -> str:
    """Guess the source format of an import payload."""
    if isinstance(data, list) and data:
        first = data[0]
        if "chat_messages" in first:
            return "claude_ai"
        msgs = first.get("messages", [])
        if isinstance(msgs, list) and msgs and "role" in (msgs[0] if msgs else {}):
            return "typingmind"
    elif isinstance(data, dict) and ("agents" in data or "users" in data):
        return "gateway"
    return "unknown"


def _parse_import_conversations(data, fmt: str) -> list[dict]:
    """Normalise import payload → list of {id, session_id, messages, created_at}."""
    convs: list[dict] = []

    if fmt == "claude_ai":
        for c in data:
            msgs = []
            for m in c.get("chat_messages", []):
                role = "user" if m.get("sender") == "human" else "assistant"
                text = m.get("text") or ""
                if isinstance(text, list):
                    text = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b) for b in text
                    )
                text = str(text).strip()
                if text:
                    msgs.append({"role": role, "content": text})
            if msgs:
                convs.append({
                    "id":         c.get("uuid", str(uuid.uuid4())),
                    "session_id": str(c.get("name", ""))[:64],
                    "messages":   msgs,
                    "created_at": c.get("created_at", datetime.utcnow().isoformat()),
                })

    elif fmt == "typingmind":
        for c in data:
            msgs = []
            for m in c.get("messages", []):
                role = m.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = m.get("content", "") or ""
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                    )
                content = str(content).strip()
                if content:
                    msgs.append({"role": role, "content": content})
            if not msgs:
                continue
            # TypingMind timestamps are milliseconds
            ts = c.get("createdAt") or c.get("created_at")
            if isinstance(ts, (int, float)) and ts > 1e10:
                ts = datetime.utcfromtimestamp(ts / 1000).isoformat()
            elif not isinstance(ts, str):
                ts = datetime.utcnow().isoformat()
            convs.append({
                "id":         c.get("id", str(uuid.uuid4())),
                "session_id": str(c.get("title", ""))[:64],
                "messages":   msgs,
                "created_at": ts,
            })

    elif fmt == "gateway":
        agents_data = data.get("agents") or data.get("users", {})
        for _aid, adata in agents_data.items():
            for c in adata.get("conversations", []):
                convs.append(c)

    return convs


async def _run_conv_import_job(job_id: str, agent_id: str, convs: list) -> None:
    """Background task: insert → LLM distill → Qdrant embed per conversation."""
    job = _conv_import_jobs[job_id]
    job["status"] = "running"

    for conv in convs:
        msgs = conv.get("messages", [])
        if not msgs:
            job["skipped"] += 1
            job["done"] += 1
            continue

        cid = conv.get("id", str(uuid.uuid4()))
        sid = str(conv.get("session_id") or cid[:8])
        try:
            dt = datetime.fromisoformat(
                str(conv.get("created_at", "")).replace("Z", "").split("+")[0]
            )
        except Exception:
            dt = datetime.utcnow()

        # 1. Persist to PostgreSQL (idempotent)
        try:
            async with _db_pool.acquire() as conn:
                result = await conn.execute(
                    "INSERT INTO conversations (id,agent_id,session_id,messages,created_at) "
                    "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (id) DO NOTHING",
                    cid, agent_id, sid, json.dumps(msgs), dt,
                )
            if result.endswith(" 0"):   # duplicate
                job["skipped"] += 1
                job["done"] += 1
                continue
        except Exception as e:
            print(f"[conv_import] {job_id} db: {e}")
            job["errors"] += 1
            job["done"] += 1
            continue

        job["imported"] += 1

        # 2. LLM distillation → Palimpsest L1-L4 (best-effort, 120s cap)
        try:
            await asyncio.wait_for(_distill_and_store(agent_id, sid, msgs), timeout=120)
            job["memories_created"] += 1
        except asyncio.TimeoutError:
            print(f"[conv_import] {job_id} distill timeout — skipping")
        except Exception as e:
            print(f"[conv_import] {job_id} distill: {e}")

        # 3. Qdrant embedding (best-effort)
        try:
            await _ingest_conv_to_qdrant(agent_id, msgs, conversation_id=cid)
            job["embedded"] += 1
        except Exception as e:
            print(f"[conv_import] {job_id} embed: {e}")

        job["done"] += 1

    job["status"] = "done"
    print(f"[conv_import] {job_id} finished: "
          f"imported={job['imported']} skipped={job['skipped']} "
          f"memories={job['memories_created']} embedded={job['embedded']}")


# ── Import conversations endpoint (async, job-based) ──────────────────────────
@app.post("/admin/api/import/conversations")
async def import_conversations(
    agent_id: str = Form(...),
    file: UploadFile = File(...),
    _=Depends(_require_key),
):
    """Accept Claude.ai / TypingMind / gateway export JSON.

    Returns immediately with a job_id. Poll
    GET /admin/api/import/conversations/status/{job_id} for progress.
    Processing: DB insert → LLM distillation (L1-L4) → Qdrant embedding.
    """
    raw = await file.read()
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON file")

    fmt = _detect_import_format(data)
    if fmt == "unknown":
        raise HTTPException(
            400,
            "Unknown format. Supported: Claude.ai export (list with chat_messages), "
            "TypingMind export (list with messages+role), "
            "gateway export (dict with agents key).",
        )

    convs = _parse_import_conversations(data, fmt)
    if not convs:
        raise HTTPException(400, "No conversations found in file")

    job_id = str(uuid.uuid4())[:8]
    _conv_import_jobs[job_id] = {
        "status":          "pending",
        "format":          fmt,
        "agent_id":        agent_id,
        "total":           len(convs),
        "done":            0,
        "imported":        0,
        "skipped":         0,
        "errors":          0,
        "memories_created": 0,
        "embedded":        0,
    }
    asyncio.create_task(_run_conv_import_job(job_id, agent_id, convs))
    return {"job_id": job_id, "total": len(convs), "format": fmt}


@app.get("/admin/api/import/conversations/status/{job_id}")
async def import_conv_status(job_id: str, _=Depends(_require_key)):
    """Poll the progress of a conversation import job."""
    job = _conv_import_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


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
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key=$1",
            f"screen_time_rules:{agent_id}"
        )
        if not row:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key='screen_time_rules'")
    if row and row["value"]:
        _rv = row["value"]
        rules = _rv if isinstance(_rv, list) else __import__("json").loads(_rv)
    else:
        rules = _DEFAULT_SCREEN_RULES

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
                # Try to generate char-voice message via LLM (Telegram channel)
                tg_sent = False
                try:
                    _as = await _get_agent_settings(agent_id)
                    _sp = (_as.get("system_prompt") or "").strip()
                    if _sp:
                        _char_prompt = (
                            f"你是以下角色：\n{_sp[:400]}\n\n"
                            f"现在你想发一条消息给用户，内容大意是：「{msg}」。\n"
                            "用你自己的口吻改写这条消息（1-2句，中文口语，自然真实，不要解释）："
                        )
                        char_msg = (await _call_llm_route("proactive_push", _char_prompt)).strip().strip("\"'")
                        if char_msg:
                            tg_sent = await _telegram_send(char_msg)
                            if tg_sent:
                                print(f"[activity] char-voice sent for {agent_id}: {char_msg[:60]}", flush=True)
                except Exception as _ce:
                    print(f"[activity] char-voice error: {_ce}", flush=True)
                # Bark fallback (always fire as notification ping)
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
    """Semantic search across Palimpsest memories (Qdrant vector, FTS5 fallback)."""
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
@app.get("/admin/api/daily-life")   # alias used by char detail page
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
        relation_type=body.get("relation_type", "living_together"),
    )
    return {"ok": True, "event": result}

@app.put("/admin/api/daily/{event_id}")
async def daily_api_update(event_id: str, body: dict, _=Depends(_require_key)):
    updated = await _daily_update(
        event_id,
        summary=body.get("summary"),
        mood=body.get("mood"),
        time_of_day=body.get("time_of_day"),
        carry_over=body.get("carry_over"),
        relation_type=body.get("relation_type"),
        date=body.get("date"),
    )
    if not updated:
        raise HTTPException(404, "Event not found")
    return {"ok": True}

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
    "palimpsest_search": ("Memory", "Semantic search (Qdrant vector, FTS5 fallback)"),
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
    """List all registered MCP tools with group/description metadata + enabled state."""
    import json as _j
    async with _db_pool.acquire() as _dc:
        _dr = await _dc.fetchrow("SELECT value FROM user_config WHERE key='disabled_mcp_tools'")
    _dv = _dr["value"] if _dr else None
    disabled = set(_j.loads(_dv) if isinstance(_dv, str) else list(_dv or []))

    tool_list = []
    for name, tool in _mcp._tool_manager._tools.items():
        group, desc_short = _MCP_GROUPS.get(name, ("Other", ""))
        doc = (tool.fn.__doc__ or "").strip().split(chr(10))[0][:120]
        tool_list.append({
            "name": name,
            "group": group,
            "description": desc_short or doc,
            "enabled": name not in disabled,
        })
    tool_list.sort(key=lambda x: (x["group"], x["name"]))
    groups = {}
    for t in tool_list:
        groups.setdefault(t["group"], []).append(t)
    return {"ok": True, "count": len(tool_list), "groups": groups,
            "disabled": list(disabled)}


@app.post("/admin/api/mcp/tools/{name}/toggle")
async def mcp_tool_toggle(name: str, _=Depends(_require_key)):
    """Toggle a tool's enabled/disabled state (persisted in user_config)."""
    import json as _j
    async with _db_pool.acquire() as _dc:
        _dr = await _dc.fetchrow("SELECT value FROM user_config WHERE key='disabled_mcp_tools'")
    _dv = _dr["value"] if _dr else None
    disabled = list(_j.loads(_dv) if isinstance(_dv, str) else list(_dv or []))
    if name in disabled:
        disabled.remove(name)
        now_enabled = True
    else:
        disabled.append(name)
        now_enabled = False
    async with _db_pool.acquire() as _dc:
        await _dc.execute(
            "INSERT INTO user_config(key,value,updated_at) VALUES('disabled_mcp_tools',$1::jsonb,NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=$1::jsonb, updated_at=NOW()",
            _j.dumps(disabled)
        )
    return {"ok": True, "name": name, "enabled": now_enabled, "disabled_count": len(disabled)}


# ── R2 cloud backup ────────────────────────────────────────────────────────────

async def _r2_upload(key: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
    """Upload bytes to Cloudflare R2. Returns True on success."""
    import json as _j
    try:
        async with _db_pool.acquire() as _rc:
            _rrows = {r["key"]: r["value"] for r in await _rc.fetch(
                "SELECT key,value FROM gateway_config WHERE key LIKE 'r2_%'")}
        if _rrows.get("r2_enabled") != "true":
            return False
        _acid = _rrows.get("r2_account_id", "")
        _akey = _rrows.get("r2_access_key", "")
        _skey = _rrows.get("r2_secret_key", "")
        _bkt  = _rrows.get("r2_bucket", "")
        if not all([_acid, _akey, _skey, _bkt]):
            return False
        import boto3, asyncio as _aio
        from botocore.config import Config as _BConf
        _s3 = boto3.client(
            service_name="s3",
            endpoint_url=f"https://{_acid}.r2.cloudflarestorage.com",
            aws_access_key_id=_akey,
            aws_secret_access_key=_skey,
            region_name="auto",
            config=_BConf(signature_version="s3v4"),
        )
        _loop = _aio.get_event_loop()
        await _loop.run_in_executor(None, lambda: _s3.put_object(
            Bucket=_bkt, Key=key, Body=data, ContentType=content_type
        ))
        print(f"[r2] uploaded {key} ({len(data)} bytes)", flush=True)
        return True
    except Exception as _re:
        print(f"[r2] upload error: {_re}", flush=True)
        return False


@app.get("/admin/api/backup/r2/status")
async def r2_status(_=Depends(_require_key)):
    """Return R2 configuration status (no secrets exposed)."""
    async with _db_pool.acquire() as conn:
        _rrows = {r["key"]: r["value"] for r in await conn.fetch(
            "SELECT key,value FROM gateway_config WHERE key LIKE 'r2_%'")}
    _acid = _rrows.get("r2_account_id", "")
    configured = all([_acid, _rrows.get("r2_access_key"), _rrows.get("r2_secret_key"), _rrows.get("r2_bucket")])
    return {
        "configured": configured,
        "enabled":    _rrows.get("r2_enabled") == "true",
        "bucket":     _rrows.get("r2_bucket", ""),
        "account_id_hint": (_acid[:6] + "…") if _acid else "",
    }

@app.post("/admin/api/backup/r2/config")
async def r2_config(body: dict, _=Depends(_require_key)):
    """Save R2 credentials to gateway_config."""
    allowed = {"r2_account_id", "r2_access_key", "r2_secret_key", "r2_bucket", "r2_enabled"}
    async with _db_pool.acquire() as conn:
        for k, v in body.items():
            if k in allowed:
                await conn.execute(
                    "INSERT INTO gateway_config(key,value) VALUES($1,$2) "
                    "ON CONFLICT(key) DO UPDATE SET value=$2", k, str(v))
    return {"ok": True}

@app.post("/admin/api/backup/r2/test")
async def r2_test(_=Depends(_require_key)):
    """Upload a small test file to R2 to verify connectivity."""
    import json as _j
    data = _j.dumps({"test": True, "ts": datetime.utcnow().isoformat()}).encode()
    ok = await _r2_upload("_test/ping.json", data, "application/json")
    if ok:
        return {"ok": True, "message": "R2 connection successful ✓"}
    raise HTTPException(500, "R2 upload failed — check credentials or enable flag")


@app.get("/admin/api/backup/r2/verify")
async def r2_verify(days: int = 7, _=Depends(_require_key)):
    """Check R2 backup completeness for the past N days (default 7).

    Returns per-day status: which days are missing or incomplete (json/db).
    """
    report = await _verify_r2_backups(days=days)
    return report


@app.get("/admin/api/music/history")
async def music_history_api(agent_id: str, limit: int = 20, _=Depends(_require_key)):
    """Return recent music recommendation history for an agent."""
    items = await _music_hist_list(agent_id=agent_id, limit=limit)
    return {"items": items, "total": len(items)}


@app.post("/admin/api/music/test")
async def music_test_api(body: dict, _=Depends(_require_key)):
    """Manually trigger a music recommendation for testing.
    Body: {"agent_id": "chiaki", "trigger_mode": "scheduled", "force": true}
    force=true bypasses quiet hours and daily push limit checks.
    """
    agent_id = body.get("agent_id", "")
    trigger_mode = body.get("trigger_mode", "scheduled")
    force = bool(body.get("force", False))
    if not agent_id:
        raise HTTPException(400, "agent_id required")
    ok = await _music_pick_and_send(agent_id, trigger_mode=trigger_mode, force=force)
    return {"ok": ok}


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


# ── Timeline dashboard (timeline-for-agent integration) ──────────────────────

_TIMELINE_TAXONOMY = {
    "categories": [
        {
            "id": "memory",
            "label": "记忆层",
            "color": "#a78bfa",
            "children": [
                {"id": "memory.L1", "label": "L1 身份"},
                {"id": "memory.L2", "label": "L2 背景"},
                {"id": "memory.L3", "label": "L3 事件"},
                {"id": "memory.L4", "label": "L4 时刻"},
            ],
        },
        {
            "id": "daily",
            "label": "日记",
            "color": "#94a3b8",
            "children": [
                {"id": "daily.entry", "label": "每日记录"},
            ],
        },
    ],
    "eventNodes": [],
}

_LAYER_SUBCATEGORY = {
    "L1": "memory.L1",
    "L2": "memory.L2",
    "L3": "memory.L3",
    "L4": "memory.L4",
}

_LAYER_DURATION_MIN = {
    "L1": 60,
    "L2": 45,
    "L3": 30,
    "L4": 15,
}

_tl_bearer = HTTPBearer(auto_error=False)


def _tl_require_key(
    cred: Optional[HTTPAuthorizationCredentials] = Security(_tl_bearer),
    key: Optional[str] = Query(default=None),
):
    """Accept admin key from Bearer header or ?key= query param."""
    raw = None
    if cred:
        raw = cred.credentials
    elif key:
        raw = key
    if not raw or raw.split(":", 1)[0] != GATEWAY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return raw


def _tl_to_utc_z(dt: datetime) -> str:
    """Return a clean UTC ISO string like 2026-04-28T11:41:32Z."""
    if dt.tzinfo is not None:
        t = dt.utctimetuple()
        dt = datetime(*t[:6])
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _build_tl_state(agent_id: str = "default", days: int = 90) -> dict:
    """Build the timeline-for-agent state dict from Palimpsest + Daily data."""
    cutoff_iso = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    mems    = await _mem_list(agent_id=agent_id, importance_min=1,
                               include_archived=False, limit=1000)
    dailies = await _daily_list(agent_id=agent_id, limit=days * 3)
    facts: dict = {}

    for m in mems:
        raw_ts = (m.get("created_at") or "").rstrip("Z").replace("+00:00", "")
        if not raw_ts or raw_ts < cutoff_iso:
            continue
        try:
            dt = datetime.fromisoformat(raw_ts)
        except ValueError:
            continue
        date_str = (dt + timedelta(hours=8)).strftime("%Y-%m-%d")
        layer    = m.get("layer") or "L4"
        end_dt   = dt + timedelta(minutes=_LAYER_DURATION_MIN.get(layer, 20))
        sub_cat  = _LAYER_SUBCATEGORY.get(layer, "memory.L4")
        content  = m.get("content") or ""
        start_z, end_z = _tl_to_utc_z(dt), _tl_to_utc_z(end_dt)
        event = {
            "id": f"mem:{m['id']}",
            "startAt": start_z, "endAt": end_z,
            "title": content.split("\n")[0][:80],
            "note": content,
            "categoryId": "memory",
            "subcategoryId": sub_cat,
            "confidence": round(min(1.0, (m.get("importance") or 3) / 5.0), 2),
            "tags": [],
        }
        if date_str not in facts:
            facts[date_str] = {"status": "final", "updatedAt": start_z, "events": []}
        facts[date_str]["events"].append(event)

    for d in dailies:
        date_str = d.get("date") or ""
        if not date_str:
            continue
        start_iso = f"{date_str}T01:00:00Z"
        summary   = d.get("summary") or ""
        mood      = d.get("mood") or "neutral"
        event = {
            "id": f"daily:{d['id']}",
            "startAt": start_iso, "endAt": f"{date_str}T01:30:00Z",
            "title": f"[{mood}] {summary.split(chr(10))[0][:80]}",
            "note": summary,
            "categoryId": "daily", "subcategoryId": "daily.entry",
            "confidence": 1.0, "tags": [mood],
        }
        if date_str not in facts:
            facts[date_str] = {"status": "final", "updatedAt": start_iso, "events": []}
        facts[date_str]["events"].append(event)

    for day_data in facts.values():
        day_data["events"].sort(key=lambda e: e["startAt"])

    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "state": {
            "version": 1, "timezone": "Asia/Shanghai",
            "taxonomy": _TIMELINE_TAXONOMY, "facts": facts,
        },
        "meta": {
            "updatedAt": now_iso, "factsUpdatedAt": now_iso,
            "taxonomyUpdatedAt": now_iso, "isDemoData": False, "locale": "zh-CN",
        },
    }


@app.get("/timeline/__timeline_source_data")
async def timeline_source_data(
    agent_id: str = "default",
    days: int = 90,
    _=Depends(_tl_require_key),
):
    """Return raw state (used by admin panel button with key in URL)."""
    return await _build_tl_state(agent_id=agent_id, days=days)


def _tl_dur_min(start: str, end: str) -> int:
    """Return duration in minutes between two ISO timestamps."""
    try:
        s = datetime.fromisoformat(start.rstrip("Z").replace("+00:00", ""))
        e = datetime.fromisoformat(end.rstrip("Z").replace("+00:00", ""))
        return max(0, int((e - s).total_seconds() / 60))
    except Exception:
        return 0


def _tl_fmt_dur(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}min"
    h, m = divmod(minutes, 60)
    return f"{h}hr {m}min" if m else f"{h}hr"


def _tl_week_start(date_str: str) -> str:
    """Return the Sunday-start week date for a given YYYY-MM-DD string."""
    from datetime import date as _date
    d = _date.fromisoformat(date_str)
    # isoweekday(): Mon=1…Sun=7; days_since_sunday = isoweekday() % 7
    days_since_sunday = d.isoweekday() % 7
    return (d - timedelta(days=days_since_sunday)).isoformat()


def _build_tl_views(state: dict, meta: dict) -> dict:
    """Python equivalent of buildTimelineViews from timeline-analytics.js.

    Produces the full JSON shape that the React dashboard expects.
    Week/month range stats are simplified but the day timeline is complete.
    """
    facts    = state.get("facts") or {}
    taxonomy = state.get("taxonomy") or {}
    dates    = sorted(facts.keys())

    # Build category → color map
    cat_color: dict = {}
    for cat in taxonomy.get("categories", []):
        color = cat.get("color", "#4E79A7")
        cat_color[cat["id"]] = color
        for child in cat.get("children", []):
            cat_color[child["id"]] = color

    def _item(event: dict) -> dict:
        sub  = event.get("subcategoryId", "")
        cat  = event.get("categoryId", "")
        color = cat_color.get(sub) or cat_color.get(cat) or "#4E79A7"
        dur  = _tl_dur_min(event.get("startAt", ""), event.get("endAt", ""))
        return {
            "id":      event["id"],
            "start":   event["startAt"],
            "end":     event["endAt"],
            "content": f"{event.get('title', '')} | {_tl_fmt_dur(dur)}",
            "style":   f"background:{color};border-color:{color};color:var(--text);",
            "tooltip": {
                "title":        event.get("title", ""),
                "note":         event.get("note", ""),
                "color":        color,
                "durationText": _tl_fmt_dur(dur),
                "timeText":     "",
            },
            "className": f"cat-{cat}",
        }

    # ── Day timelines ──────────────────────────────────────────────────────────
    day_timelines: dict = {}
    for date in dates:
        events = facts[date].get("events") or []
        day_timelines[date] = {
            "date":   date,
            "start":  f"{date}T00:00:00.000+08:00",
            "end":    f"{date}T23:59:59.999+08:00",
            "groups": [],
            "items":  [_item(e) for e in events],
        }

    # ── Week timelines ────────────────────────────────────────────────────────
    # Group dates by week (Sunday-start)
    week_groups: dict = {}
    for date in dates:
        ws = _tl_week_start(date)
        week_groups.setdefault(ws, []).append(date)

    from datetime import date as _date_cls
    WEEKDAY_ZH = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]

    week_timelines: dict = {}
    for ws, wdates in sorted(week_groups.items()):
        groups, items = [], []
        ws_end = (_date_cls.fromisoformat(ws) + timedelta(days=6)).isoformat()
        for date in sorted(wdates):
            d_obj = _date_cls.fromisoformat(date)
            day_label = f"{WEEKDAY_ZH[d_obj.isoweekday() % 7]} {date[5:]}"
            groups.append({"id": date, "content": day_label})
            for e in (facts[date].get("events") or []):
                # Use actual timestamps — group field creates rows per day
                it = _item(e)
                items.append({**it, "group": date})
        week_timelines[ws] = {
            "weekStart": ws,
            "start": f"{ws}T00:00:00.000+08:00",
            "end":   f"{ws_end}T23:59:59.999+08:00",
            "groups": groups,
            "items":  items,
        }

    # ── Ranges (category stats matching buildRangeAggregate output) ───────────
    def _cat_label(cid: str) -> str:
        for cat in taxonomy.get("categories", []):
            if cat["id"] == cid:
                return cat.get("label", cid)
            for child in cat.get("children", []):
                if child["id"] == cid:
                    return child.get("label", cid)
        return cid

    def _range_agg(evts: list, key: str = "", label: str = "", unit: str = "day",
                   all_dates: list | None = None) -> dict:
        total = sum(_tl_dur_min(e.get("startAt",""), e.get("endAt","")) for e in evts)
        cat_buckets: dict = {}
        sub_buckets: dict = {}
        # trend: date → {subcategoryId → minutes}
        trend_map: dict = {d: {} for d in (all_dates or [])}

        for e in evts:
            cid  = e.get("categoryId", "")
            sid  = e.get("subcategoryId", "")
            dur  = _tl_dur_min(e.get("startAt",""), e.get("endAt",""))
            edate = (e.get("startAt","") or "")[:10]

            # category bucket
            if cid not in cat_buckets:
                cat_buckets[cid] = {
                    "categoryId": cid, "label": _cat_label(cid),
                    "color": cat_color.get(cid, "#4E79A7"),
                    "minutes": 0, "percentage": 0,
                }
            cat_buckets[cid]["minutes"] += dur

            # subcategory bucket
            if sid:
                if sid not in sub_buckets:
                    sub_buckets[sid] = {
                        "subcategoryId": sid, "categoryId": cid,
                        "label": _cat_label(sid),
                        "color": cat_color.get(cid, "#4E79A7"),
                        "minutes": 0, "percentage": 0,
                    }
                sub_buckets[sid]["minutes"] += dur

            # trend
            if edate in trend_map and sid:
                trend_map[edate][sid] = trend_map[edate].get(sid, 0) + dur

        for b in cat_buckets.values():
            b["percentage"] = round(b["minutes"] / total * 100) if total else 0
        for b in sub_buckets.values():
            b["percentage"] = round(b["minutes"] / total * 100) if total else 0

        subcategory_trend = [
            {
                "date": d,
                "subcategories": sorted(
                    [{"subcategoryId": k, "minutes": v} for k, v in sm.items()],
                    key=lambda x: -x["minutes"],
                ),
            }
            for d, sm in sorted(trend_map.items())
        ]

        return {
            "key":   key,
            "label": label,
            "unit":  unit,
            "totalMinutes": total,
            "timeBlocks":   len(evts),
            "categories":   sorted(cat_buckets.values(), key=lambda x: -x["minutes"]),
            "subcategories": sorted(sub_buckets.values(), key=lambda x: -x["minutes"]),
            "subcategoryTrend": subcategory_trend,
        }

    day_ranges = {
        d: _range_agg(facts[d].get("events") or [], key=d, label=d, unit="day", all_dates=[d])
        for d in dates
    }

    week_ranges: dict = {}
    for ws, wdates in sorted(week_groups.items()):
        all_evts = [e for d in wdates for e in (facts[d].get("events") or [])]
        week_ranges[ws] = _range_agg(
            all_evts, key=ws, label=f"Week of {ws}", unit="week", all_dates=sorted(wdates)
        )

    month_groups: dict = {}
    for date in dates:
        month_groups.setdefault(date[:7], []).append(date)
    month_ranges: dict = {}
    for ym, mdates in sorted(month_groups.items()):
        all_evts = [e for d in mdates for e in (facts[d].get("events") or [])]
        month_ranges[ym] = _range_agg(
            all_evts, key=ym, label=ym, unit="month", all_dates=sorted(mdates)
        )

    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "meta": {
            "generatedAt":      now_iso,
            "updatedAt":        meta.get("updatedAt", now_iso),
            "taxonomyUpdatedAt": meta.get("taxonomyUpdatedAt", now_iso),
            "factsUpdatedAt":   meta.get("factsUpdatedAt", now_iso),
            "isDemoData":       False,
            "timezone":         state.get("timezone", "Asia/Shanghai"),
            "locale":           meta.get("locale", "zh-CN"),
            "availableDates":   dates,
            "latestDate":       dates[-1] if dates else "",
        },
        "taxonomy": taxonomy,
        "timelines": {"day": day_timelines, "week": week_timelines},
        "ranges":    {"day": day_ranges, "week": week_ranges, "month": month_ranges},
    }


@app.get("/timeline/dashboard-data.json")
async def timeline_dashboard_data(agent_id: str = "default", days: int = 90):
    """Pre-processed views data — no auth needed, used by the React dashboard."""
    state_data = await _build_tl_state(agent_id=agent_id, days=days)
    views = _build_tl_views(state_data["state"], state_data["meta"])
    return views


# Mount timeline static dashboard (MUST be after /timeline/* route definitions)
app.mount("/timeline", StaticFiles(directory="static/timeline", html=True), name="timeline")

