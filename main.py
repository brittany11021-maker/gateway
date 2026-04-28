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
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

import asyncpg
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Security, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, MatchValue,
    PointStruct, PointVectors, Range, VectorParams, PayloadSchemaType,
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

# ── Config ─────────────────────────────────────────────────────────────────────
GATEWAY_API_KEY = os.environ["GATEWAY_API_KEY"]
POSTGRES_DSN    = os.environ["POSTGRES_DSN"]
QDRANT_URL      = os.environ.get("QDRANT_URL", "http://qdrant:6333")
EMBED_MODEL     = os.environ.get("EMBED_MODEL", "baai/bge-m3")
EMBED_DIM       = 1024   # baai/bge-m3 output dimension


def _parse_providers() -> dict[str, dict]:
    """Auto-discover LLM providers from {NAME}_API_KEY + {NAME}_BASE_URL env vars."""
    _skip = {"GATEWAY", "POSTGRES", "QDRANT"}
    providers: dict[str, dict] = {}
    for k, v in os.environ.items():
        if not k.endswith("_API_KEY") or not v:
            continue
        name = k[:-8]                                   # e.g. NVIDIA_API_KEY → NVIDIA
        if name in _skip:
            continue
        base = os.environ.get(f"{name}_BASE_URL", "").rstrip("/")
        if base:
            providers[name.lower()] = {"api_key": v, "base_url": base}
    return providers


PROVIDERS: dict[str, dict] = _parse_providers()
_DEFAULT_CHAIN: list[str] = [
    p.strip() for p in os.environ.get("API_CHAIN_DEFAULT", "").split(",")
    if p.strip() and p.strip().lower() in PROVIDERS
]
_EMBED_PNAME: str = os.environ.get("EMBED_PROVIDER", next(iter(PROVIDERS), "")).lower()


def _agent_llm_config(agent_id: str, db_model: str = "") -> tuple[str, list[str]]:
    """Return (model, [provider_names]) for an agent, reading from env."""
    prefix = "AGENT_" + agent_id.upper().replace("-", "_").replace(" ", "_")
    model = (
        os.environ.get(f"{prefix}_MODEL")
        or db_model
        or os.environ.get("AGENT_DEFAULT_MODEL", "")
    )
    chain_str = os.environ.get(f"{prefix}_API_CHAIN", "")
    chain = (
        [p.strip().lower() for p in chain_str.split(",") if p.strip().lower() in PROVIDERS]
        if chain_str else _DEFAULT_CHAIN[:]
    )
    if not chain:
        chain = list(PROVIDERS.keys())[:1]
    return model, chain


def _log_fallback(agent_id: str, chain: list[str], idx: int, reason: str) -> None:
    print(
        f"[fallback] agent={agent_id} {chain[idx]}→{chain[idx+1]} reason={reason}",
        flush=True,
    )


async def _call_llm_simple(prompt: str, agent_id: str = "default") -> str:
    """Non-streaming LLM call for internal use (distillation, MCP wake_up, etc.)."""
    model, chain = _agent_llm_config(agent_id)
    msgs = [{"role": "user", "content": prompt}]
    for i, pname in enumerate(chain):
        p = PROVIDERS[pname]
        hdrs = {"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{p['base_url']}/chat/completions",
                    headers=hdrs,
                    json={"model": model, "messages": msgs, "temperature": 0.3},
                )
            if resp.status_code in (429, 500, 502, 503) and i < len(chain) - 1:
                _log_fallback(agent_id, chain, i, str(resp.status_code))
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if i < len(chain) - 1:
                _log_fallback(agent_id, chain, i, type(e).__name__)
                continue
            raise
    raise RuntimeError("All providers failed")
RECENT_DAYS     = 30
COLLECTIONS     = ["memory_profile", "memory_project", "memory_recent"]
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
        "search book content; save book annotations; wake_up for full context retrieval."
    ),
)

# ── Startup ────────────────────────────────────────────────────────────────────
# ── MCP tools ─────────────────────────────────────────────────────────────────
@_mcp.tool()
async def search_memories(
    query: str,
    agent_id: str = "default",
    layer: str = "all",
) -> str:
    """Semantic search over memory layers.

    Args:
        query:    Natural-language search query.
        agent_id: Which agent's memories to search (default: "default").
        layer:    profile | project | recent | all
    """
    if layer == "all":
        cols = COLLECTIONS
    else:
        col = f"memory_{layer}"
        if col not in COLLECTIONS:
            return f"Unknown layer '{layer}'. Use: profile, project, recent, all."
        cols = [col]

    vec = await _embed(query, input_type="query")
    results: list[str] = []
    for col in cols:
        hits = _qdrant.search(
            col, query_vector=vec,
            query_filter=_agent_filter(agent_id),
            limit=5, score_threshold=0.3,
        )
        tier = col.replace("memory_", "")
        for h in hits:
            results.append(f"[{tier}] {h.payload.get('text', '')}")

    return "\n".join(results) if results else "No memories found."


@_mcp.tool()
async def get_memories(layer: str, agent_id: str = "default") -> str:
    """Retrieve all memories from a specific layer.

    Args:
        layer:    profile | project | recent
        agent_id: Which agent's memories to retrieve (default: "default").
    """
    col = f"memory_{layer}"
    if col not in COLLECTIONS:
        return f"Unknown layer '{layer}'. Use: profile, project, recent."

    recs, _ = _qdrant.scroll(
        col, scroll_filter=_agent_filter(agent_id),
        limit=200, with_payload=True, with_vectors=False,
    )
    if not recs:
        return f"No memories in {layer} for agent '{agent_id}'."
    return "\n".join(
        f"- [{r.id}] {r.payload.get('text', '')}" for r in recs
    )


@_mcp.tool()
async def add_memory(content: str, layer: str, agent_id: str = "default") -> str:
    """Write a new memory entry to a specific layer.

    Args:
        content:  The memory text to store.
        layer:    profile | project | recent
        agent_id: Which agent to store the memory for (default: "default").
    """
    col = f"memory_{layer}"
    if col not in COLLECTIONS:
        return f"Unknown layer '{layer}'. Use: profile, project, recent."
    if not content.strip():
        return "Content cannot be empty."

    await _store_memory(col, content.strip(), agent_id, "mcp")
    return f"Memory added to {layer} for agent '{agent_id}'."


@_mcp.tool()
async def get_conversations(limit: int = 10, agent_id: str = "default") -> str:
    """Retrieve the most recent raw conversation records.

    Args:
        limit:    How many conversations to return (default: 10).
        agent_id: Which agent's conversations (default: "default").
    """
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT session_id, messages, created_at FROM conversations "
            "WHERE agent_id=$1 ORDER BY created_at DESC LIMIT $2",
            agent_id, max(1, min(limit, 100)),
        )
    if not rows:
        return f"No conversations found for agent '{agent_id}'."

    parts: list[str] = []
    for idx, r in enumerate(rows, 1):
        msgs = json.loads(r["messages"])
        ts   = r["created_at"].strftime("%Y-%m-%d %H:%M")
        body = "\n".join(
            f"  {m['role'].upper()}: {str(m.get('content', ''))[:400]}"
            for m in msgs if m.get("role") in ("user", "assistant")
        )
        parts.append(f"[#{idx} · {ts}]\n{body}")

    return "\n\n---\n\n".join(parts)


@_mcp.tool()
async def search_book(query: str, book_id: str = "", agent_id: str = "iris") -> str:
    """Semantic search in book content.

    Args:
        query:    Natural-language search query.
        book_id:  Specific book UUID to search (empty = all books).
        agent_id: Requesting agent ID.
    """
    vec  = await _embed(query, "query")
    filt = (Filter(must=[FieldCondition(key="book_id", match=MatchValue(value=book_id))])
            if book_id else None)
    hits = _qdrant.search(BOOK_COLLECTION, query_vector=vec, query_filter=filt,
                          limit=5, score_threshold=0.3)
    if not hits:
        return "No matching passages found."
    bids = list({h.payload.get("book_id") for h in hits})
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT book_id::text, title FROM books WHERE book_id = ANY($1::uuid[])", bids)
    titles = {r["book_id"]: r["title"] for r in rows}
    return "\n\n---\n\n".join(
        f"[《{titles.get(h.payload.get('book_id',''), '?')}》第{h.payload.get('page',0)}页]\n"
        f"{h.payload.get('chunk_text','')}"
        for h in hits
    )


@_mcp.tool()
async def get_reading_context(book_id: str, agent_id: str = "iris") -> str:
    """Return reading context: book info, current progress, agent annotations.

    Args:
        book_id:  Book UUID.
        agent_id: Agent requesting context.
    """
    async with _db_pool.acquire() as conn:
        book = await conn.fetchrow("SELECT * FROM books WHERE book_id=$1::uuid", book_id)
        if not book:
            return f"Book not found: {book_id}"
        anns = await conn.fetch(
            "SELECT page, selected_text, comment FROM annotations "
            "WHERE book_id=$1::uuid AND agent_id=$2 ORDER BY page",
            book_id, agent_id,
        )
    prog = json.loads(book["agents_progress"] or "{}")
    cur  = prog.get(agent_id, {})
    lines = [
        f"书名：《{book['title']}》",
        f"作者：{book['author'] or '未知'}",
        f"状态：{book['status']}",
        f"进度（{agent_id}）：第 {cur.get('page', 0)} / {book['total_pages']} 页",
        f"上次阅读：{cur.get('last_read', '—')}",
    ]
    if anns:
        lines.append(f"\n{agent_id} 的批注（共{len(anns)}条）：")
        for a in anns:
            line = f"[第{a['page']}页] 「{a['selected_text']}」"
            if a["comment"]:
                line += f" — {a['comment']}"
            lines.append(line)
    else:
        lines.append(f"\n{agent_id} 暂无批注")
    return "\n".join(lines)


@_mcp.tool()
async def save_annotation(
    book_id: str,
    selected_text: str,
    comment: str = "",
    page: int = 0,
    agent_id: str = "iris",
) -> str:
    """Save a book annotation (highlight or note).

    Args:
        book_id:       Book UUID.
        selected_text: The highlighted text (required).
        comment:       Annotation note (empty = highlight only).
        page:          Page number.
        agent_id:      Agent making the annotation.
    """
    if not selected_text.strip():
        return "Error: selected_text is required"
    color = AGENT_COLORS.get(agent_id, "#6366f1")
    async with _db_pool.acquire() as conn:
        ann_id = await conn.fetchval(
            "INSERT INTO annotations (book_id,agent_id,selected_text,comment,page,color) "
            "VALUES ($1::uuid,$2,$3,$4,$5,$6) RETURNING annotation_id",
            book_id, agent_id, selected_text, comment, page, color,
        )
    return f"Annotation saved (ID: {ann_id})"


@_mcp.tool()
async def wake_up(agent_id: str = "default") -> str:
    """Fetch full profile, all project memories, recent 10 memories, last 2 conversations.

    Args:
        agent_id: Agent waking up.
    """
    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    filt = _agent_filter(agent_id)
    prof, _ = _qdrant.scroll("memory_profile", scroll_filter=filt, limit=200,
                              with_payload=True, with_vectors=False)
    proj, _ = _qdrant.scroll("memory_project", scroll_filter=filt, limit=200,
                              with_payload=True, with_vectors=False)
    rec,  _ = _qdrant.scroll("memory_recent",  scroll_filter=filt, limit=10,
                              with_payload=True, with_vectors=False)
    async with _db_pool.acquire() as conn:
        convs = await conn.fetch(
            "SELECT messages, created_at FROM conversations "
            "WHERE agent_id=$1 ORDER BY created_at DESC LIMIT 2", agent_id,
        )
    parts = [f"=== WAKE UP: {agent_id} @ {now} ===", "", "## Profile Memories"]
    parts += [f"- {r.payload.get('text','')}" for r in prof] or ["(none)"]
    parts += ["", "## Project Memories"]
    parts += [f"- {r.payload.get('text','')}" for r in proj] or ["(none)"]
    parts += ["", "## Recent Memories (last 10)"]
    parts += [f"- {r.payload.get('text','')}" for r in rec] or ["(none)"]
    parts += ["", "## Last 2 Conversations"]
    for row in convs:
        msgs = json.loads(row["messages"])
        ts   = row["created_at"].strftime("%Y-%m-%d %H:%M")
        body = "\n".join(
            f"{m['role'].upper()}: {str(m.get('content',''))[:300]}"
            for m in msgs if m.get("role") in ("user", "assistant")
        )
        parts.append(f"[{ts}]\n{body}")
    if not convs:
        parts.append("(no conversations yet)")
    return "\n".join(parts)


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

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(_auto_backup_loop())

    _qdrant = QdrantClient(url=QDRANT_URL)
    for col in COLLECTIONS:
        # Auto-migrate if vector dim changed (e.g. local 384 → API 1024)
        if _qdrant.collection_exists(col):
            info = _qdrant.get_collection(col)
            if info.config.params.vectors.size != EMBED_DIM:
                print(f"[startup] dim mismatch in {col} "
                      f"({info.config.params.vectors.size}→{EMBED_DIM}), recreating...")
                _qdrant.delete_collection(col)
        if not _qdrant.collection_exists(col):
            _qdrant.create_collection(
                collection_name=col,
                vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            )
        try:
            _qdrant.create_payload_index(
                collection_name=col,
                field_name="agent_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass

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


async def _run_sh_session_manager() -> None:
    """Keep the MCP Streamable HTTP session manager alive for the process lifetime."""
    async with _mcp.session_manager.run():
        try:
            await asyncio.Future()   # suspend until task is cancelled
        except asyncio.CancelledError:
            pass


@app.on_event("shutdown")
async def shutdown():
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
    if cred.credentials != GATEWAY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return cred.credentials


# ── Embedding ─────────────────────────────────────────────────────────────────
async def _embed(text: str, input_type: str = "query") -> list[float]:
    """Call embed provider. input_type: 'query' for retrieval, 'passage' for indexing."""
    p = PROVIDERS.get(_EMBED_PNAME)
    if not p:
        raise RuntimeError(f"Embed provider '{_EMBED_PNAME}' not configured in env")
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


# ── Memory helpers ─────────────────────────────────────────────────────────────
def _trim_recent(agent_id: str, max_count: int = 100) -> None:
    """Keep only the newest max_count entries in memory_recent for an agent."""
    recs, _ = _qdrant.scroll(
        "memory_recent",
        scroll_filter=_agent_filter(agent_id),
        limit=max_count + 300,
        with_payload=["created_ts"],
        with_vectors=False,
    )
    if len(recs) > max_count:
        by_age = sorted(recs, key=lambda r: r.payload.get("created_ts", 0))
        to_del = [r.id for r in by_age[:len(recs) - max_count]]
        _qdrant.delete("memory_recent", points_selector=to_del)


def _agent_filter(agent_id: str) -> Filter:
    return Filter(must=[FieldCondition(key="agent_id", match=MatchValue(value=agent_id))])


async def _store_memory(collection: str, text: str, agent_id: str, session_id: str) -> None:
    vec = await _embed(text, input_type="passage")
    _qdrant.upsert(
        collection_name=collection,
        points=[PointStruct(
            id=str(uuid.uuid4()),
            vector=vec,
            payload={
                "text":       text,
                "agent_id":   agent_id,
                "session_id": session_id,
                "created_ts": time.time(),
            },
        )],
    )


async def _retrieve_memories(agent_id: str, query: str, k: int = 5) -> list[str]:
    vec = await _embed(query, input_type="query")
    results = []
    for col in COLLECTIONS:
        hits = _qdrant.search(
            collection_name=col,
            query_vector=vec,
            query_filter=_agent_filter(agent_id),
            limit=k,
            score_threshold=0.3,
        )
        results.extend(h.payload.get("text", "") for h in hits)
    return [m for m in results if m]


async def _store_conversation(conv_id: str, agent_id: str, session_id: str, messages: list) -> None:
    async with _db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO conversations (id, agent_id, session_id, messages)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (id) DO UPDATE SET messages = $4""",
            conv_id, agent_id, session_id, json.dumps(messages),
        )


async def _distill_and_store(agent_id: str, session_id: str, messages: list) -> None:
    history = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
        if isinstance(m.get("content"), str) and m["role"] in ("user", "assistant")
    )
    if not history.strip():
        return

    prompt = (
        "Analyze this conversation and extract memories in THREE categories.\n"
        "Respond ONLY with valid JSON (no markdown fences):\n"
        '{"profile":["fact about user personality/background..."],'
        '"project":["ongoing project or topic info..."],'
        '"recent":["key points from this conversation..."]}\n\n'
        f"Conversation:\n{history}"
    )

    try:
        raw = await _call_llm_simple(prompt, agent_id)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)

        tasks = (
            [_store_memory("memory_profile", t, agent_id, session_id) for t in data.get("profile", [])]
          + [_store_memory("memory_project", t, agent_id, session_id) for t in data.get("project", [])]
          + [_store_memory("memory_recent",  t, agent_id, session_id) for t in data.get("recent",  [])]
        )
        if tasks:
            await asyncio.gather(*tasks)

        cutoff = time.time() - RECENT_DAYS * 86400
        _qdrant.delete(
            collection_name="memory_recent",
            points_selector=Filter(must=[
                FieldCondition(key="agent_id",   match=MatchValue(value=agent_id)),
                FieldCondition(key="created_ts", range=Range(lt=cutoff)),
            ]),
        )
        _trim_recent(agent_id, max_count=100)
    except Exception as e:
        print(f"[distill error] {type(e).__name__}: {e}")


# ── Backup helpers ─────────────────────────────────────────────────────────────
async def _collect_all_agents() -> set[str]:
    agents: set[str] = set()
    async with _db_pool.acquire() as conn:
        for r in await conn.fetch("SELECT DISTINCT agent_id FROM conversations"):
            agents.add(r["agent_id"])
        for r in await conn.fetch("SELECT agent_id FROM agent_settings"):
            agents.add(r["agent_id"])
    for col in COLLECTIONS:
        recs, _ = _qdrant.scroll(col, limit=10000, with_payload=["agent_id"], with_vectors=False)
        for r in recs:
            if aid := r.payload.get("agent_id"):
                agents.add(aid)
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

        memories: dict = {}
        for col in COLLECTIONS:
            tier = col.replace("memory_", "")
            recs, _ = _qdrant.scroll(
                col, scroll_filter=_agent_filter(aid),
                limit=10000, with_payload=True, with_vectors=False,
            )
            memories[tier] = [
                {
                    "id":         str(r.id),
                    "content":    r.payload.get("text", ""),
                    "created_at": r.payload.get("created_ts", 0),
                    "session_id": r.payload.get("session_id", ""),
                }
                for r in recs
            ]

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


async def _save_backup_file() -> str:
    data = await _build_export_data()
    fname = f"memory_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.json"
    (BACKUP_DIR / fname).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _trim_backups()
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backup_settings (key,value) VALUES ('last_backup_at',$1) "
            "ON CONFLICT (key) DO UPDATE SET value=$1",
            datetime.utcnow().isoformat())
    return fname


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


def _strip_injection(messages: list) -> list:
    return [m for m in messages
            if not (m.get("role") == "system" and "[Relevant memories" in m.get("content", ""))]


# ── /v1/chat/completions ───────────────────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: dict, _=Depends(_require_key)):
    # Priority: X-Agent-ID header > agent_id body field > user body field > "default"
    agent_id   = (request.headers.get("X-Agent-ID") or body.get("agent_id") or body.get("user") or "default").strip() or "default"
    session_id = body.get("session_id") or str(uuid.uuid4())
    messages   = list(body.get("messages", []))
    stream     = body.get("stream", False)

    # Inject relevant memories
    last_user = next(
        (m["content"] for m in reversed(messages)
         if m.get("role") == "user" and isinstance(m.get("content"), str)), "",
    )
    if last_user:
        mems = await _retrieve_memories(agent_id, last_user)
        if mems:
            mem_text = "[Relevant memories about the user]\n" + "\n".join(f"- {m}" for m in mems)
            if messages and messages[0]["role"] == "system":
                messages[0] = {**messages[0], "content": mem_text + "\n\n" + messages[0]["content"]}
            else:
                messages = [{"role": "system", "content": mem_text}] + messages

    # Resolve model + provider chain
    db_model = ""
    try:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT llm_model FROM agent_settings WHERE agent_id=$1", agent_id)
            if row:
                db_model = row["llm_model"] or ""
    except Exception:
        pass
    model, chain = _agent_llm_config(agent_id, db_model)

    payload = {k: v for k, v in body.items() if k not in ("agent_id", "session_id")}
    payload["messages"] = messages
    if not body.get("model"):           # explicit model in request takes priority
        payload["model"] = model

    if stream:
        async def event_stream() -> AsyncGenerator[bytes, None]:
            collected: list[str] = []
            for i, pname in enumerate(chain):
                p = PROVIDERS[pname]
                hdrs = {"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"}
                try:
                    async with httpx.AsyncClient(timeout=300) as client:
                        async with client.stream(
                            "POST", f"{p['base_url']}/chat/completions",
                            headers=hdrs, json=payload,
                        ) as resp:
                            if resp.status_code in (429, 500, 502, 503) and i < len(chain) - 1:
                                _log_fallback(agent_id, chain, i, str(resp.status_code))
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
                    break  # success — stop trying providers
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    if i < len(chain) - 1:
                        _log_fallback(agent_id, chain, i, type(e).__name__)
                        continue
                    raise
            if collected:
                full = _strip_injection(messages) + [{"role": "assistant", "content": "".join(collected)}]
                cid  = str(uuid.uuid4())
                asyncio.create_task(_store_conversation(cid, agent_id, session_id, full))
                asyncio.create_task(_distill_and_store(agent_id, session_id, full))

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Non-streaming with fallback
    data: dict = {}
    for i, pname in enumerate(chain):
        p = PROVIDERS[pname]
        hdrs = {"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(f"{p['base_url']}/chat/completions", headers=hdrs, json=payload)
            if resp.status_code in (429, 500, 502, 503) and i < len(chain) - 1:
                _log_fallback(agent_id, chain, i, str(resp.status_code))
                continue
            data = resp.json()
            break
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if i < len(chain) - 1:
                _log_fallback(agent_id, chain, i, type(e).__name__)
                continue
            raise
    else:
        raise HTTPException(502, "All providers in chain failed")

    try:
        reply = data["choices"][0]["message"]["content"]
        full  = _strip_injection(messages) + [{"role": "assistant", "content": reply}]
        cid   = str(uuid.uuid4())
        asyncio.create_task(_store_conversation(cid, agent_id, session_id, full))
        asyncio.create_task(_distill_and_store(agent_id, session_id, full))
    except Exception:
        pass
    return data


# ── Providers API ─────────────────────────────────────────────────────────────
@app.get("/admin/api/providers")
async def list_providers(_=Depends(_require_key)):
    return {
        "providers":     [{"name": n, "base_url": p["base_url"]} for n, p in PROVIDERS.items()],
        "default_chain": _DEFAULT_CHAIN,
        "embed_provider": _EMBED_PNAME,
    }


@app.post("/admin/api/providers/{name}/test")
async def test_provider(name: str, _=Depends(_require_key)):
    p = PROVIDERS.get(name)
    if not p:
        raise HTTPException(404, f"Provider '{name}' not configured")
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{p['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"},
                json={"model": "", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            )
        ms = int((time.time() - t0) * 1000)
        # 200 = ok, 400 = bad model name but auth works → still reachable
        ok = resp.status_code in (200, 400)
        return {"ok": ok, "latency_ms": ms, "status": resp.status_code,
                "error": "" if ok else resp.text[:200]}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


# ── Admin HTML ─────────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    with open("static/admin.html", encoding="utf-8") as f:
        return f.read()


# ── Admin: agents ──────────────────────────────────────────────────────────────
@app.get("/admin/api/agents")
async def admin_agents(_=Depends(_require_key)):
    return {"agents": sorted(await _collect_all_agents()) or ["default"]}


# ── Admin: stats ───────────────────────────────────────────────────────────────
@app.get("/admin/api/stats")
async def admin_stats(agent_id: str = "default", _=Depends(_require_key)):
    counts: dict = {}
    for col in COLLECTIONS:
        counts[col] = _qdrant.count(col, count_filter=_agent_filter(agent_id)).count
    async with _db_pool.acquire() as conn:
        counts["conversations"] = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE agent_id=$1", agent_id)
    return counts


# ── Admin: list/search memories ────────────────────────────────────────────────
@app.get("/admin/api/memories")
async def admin_list(collection: str = "memory_profile", agent_id: str = "default",
                     q: str = "", limit: int = 100, _=Depends(_require_key)):
    if collection not in COLLECTIONS:
        raise HTTPException(400, "Invalid collection")
    if q:
        hits = _qdrant.search(collection, query_vector=await _embed(q),
                              query_filter=_agent_filter(agent_id), limit=limit)
        items = [{"id": str(h.id), "score": round(h.score, 3), **h.payload} for h in hits]
    else:
        recs, _ = _qdrant.scroll(collection, scroll_filter=_agent_filter(agent_id),
                                 limit=limit, with_payload=True, with_vectors=False)
        items = [{"id": str(r.id), **r.payload} for r in recs]
    return {"items": items}


# ── Admin: add memory ──────────────────────────────────────────────────────────
@app.post("/admin/api/memories")
async def admin_add(body: dict, _=Depends(_require_key)):
    col  = body.get("collection", "memory_profile")
    text = body.get("text", "").strip()
    aid  = body.get("agent_id", "default")
    if col not in COLLECTIONS or not text:
        raise HTTPException(400, "Invalid params")
    await _store_memory(col, text, aid, "manual")
    return {"ok": True}


# ── Admin: update memory ───────────────────────────────────────────────────────
@app.put("/admin/api/memories/{point_id}")
async def admin_update(point_id: str, body: dict, _=Depends(_require_key)):
    col  = body.get("collection", "memory_profile")
    text = body.get("text", "").strip()
    if col not in COLLECTIONS or not text:
        raise HTTPException(400, "Invalid params")
    _qdrant.set_payload(col, payload={"text": text}, points=[point_id])
    _qdrant.update_vectors(col, points=[PointVectors(id=point_id, vector=await _embed(text, "passage"))])
    return {"ok": True}


# ── Admin: delete memory ───────────────────────────────────────────────────────
@app.delete("/admin/api/memories/{point_id}")
async def admin_delete(point_id: str, collection: str = "memory_profile", _=Depends(_require_key)):
    if collection not in COLLECTIONS:
        raise HTTPException(400, "Invalid collection")
    _qdrant.delete(collection, points_selector=[point_id])
    return {"ok": True}


# ── Admin: global stats ────────────────────────────────────────────────────────
@app.get("/admin/api/stats/global")
async def admin_global_stats(_=Depends(_require_key)):
    counts: dict = {}
    for col in COLLECTIONS:
        counts[col] = _qdrant.count(col).count
    async with _db_pool.acquire() as conn:
        counts["conversations"] = await conn.fetchval("SELECT COUNT(*) FROM conversations")
    return counts


# ── Admin: agent settings CRUD ─────────────────────────────────────────────────
@app.get("/admin/api/agents/{agent_id}/settings")
async def get_agent_settings(agent_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_settings WHERE agent_id=$1", agent_id)
    if not row:
        return {"agent_id": agent_id, "api_source": "nvidia",
                "llm_model": "", "notes": "", "avatar": ""}
    return dict(row)


@app.post("/admin/api/agents/{agent_id}/settings")
async def save_agent_settings(agent_id: str, body: dict, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO agent_settings (agent_id, api_source, llm_model, notes, avatar, updated_at)
            VALUES ($1,$2,$3,$4,$5,now())
            ON CONFLICT (agent_id) DO UPDATE SET
                api_source=$2, llm_model=$3, notes=$4, avatar=$5, updated_at=now()
        """, agent_id,
             body.get("api_source", "nvidia"),
             body.get("llm_model", ""),
             body.get("notes", ""),
             body.get("avatar", ""))
    return {"ok": True}


@app.delete("/admin/api/agents/{agent_id}/settings")
async def delete_agent_settings(agent_id: str, _=Depends(_require_key)):
    async with _db_pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_settings WHERE agent_id=$1", agent_id)
    return {"ok": True}


# ── Admin: conversations ───────────────────────────────────────────────────────
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
        for col in COLLECTIONS:
            _qdrant.delete_collection(col)
            _qdrant.create_collection(
                col, vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE))
            try:
                _qdrant.create_payload_index(
                    col, field_name="agent_id", field_schema=PayloadSchemaType.KEYWORD)
            except Exception:
                pass
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

        # memories
        for tier, items in ud.get("memories", {}).items():
            col = f"memory_{tier}"
            if col not in COLLECTIONS:
                continue
            for item in items:
                content = item.get("content", "")
                if not content:
                    continue
                item_id = item.get("id", str(uuid.uuid4()))
                if mode == "merge":
                    try:
                        if _qdrant.retrieve(col, ids=[item_id]):
                            skipped += 1
                            continue
                    except Exception:
                        pass
                try:
                    vec = await _embed(content, "passage")
                    created = item.get("created_at", time.time())
                    _qdrant.upsert(col, points=[PointStruct(
                        id=item_id, vector=vec,
                        payload={
                            "text":       content,
                            "agent_id":   aid,
                            "session_id": item.get("session_id", "import"),
                            "created_ts": float(created) if isinstance(created, (int, float))
                                          else time.time(),
                        },
                    )])
                    imported_memories += 1
                except Exception as e:
                    print(f"[import] memory err: {e}")
                    skipped += 1

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
    # snapshot memory counts before we start
    before_total = sum(
        _qdrant.count(col, count_filter=_agent_filter(agent_id)).count
        for col in COLLECTIONS
    )

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
        # LLM distillation (fire-and-wait so we get accurate memory counts)
        await _distill_and_store(agent_id, sid, msgs)

    after_total = sum(
        _qdrant.count(col, count_filter=_agent_filter(agent_id)).count
        for col in COLLECTIONS
    )
    memories_created = max(0, after_total - before_total)

    return {"imported": imported, "memories_created": memories_created, "skipped": skipped}


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
    """Split text into fixed-size pages of CHARS_PER_PAGE chars."""
    return [text[i:i+CHARS_PER_PAGE]
            for i in range(0, max(1, len(text)), CHARS_PER_PAGE)] or [""]


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


def _extract_epub(data: bytes) -> tuple[str, list[str]]:
    if _ebooklib is None or _epub is None:
        raise HTTPException(500, "EPUB support unavailable (EbookLib not installed)")
    import warnings, tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            book = _epub.read_epub(tmp_path, options={"ignore_ncx": True})
        full = ""
        for item in book.get_items_of_type(_ebooklib.ITEM_DOCUMENT):
            raw = item.get_content().decode("utf-8", errors="replace")
            # preserve paragraph / line breaks before stripping tags
            raw = re.sub(r'<(?:p|br|div|h[1-6]|li|tr)[^>]*>', '\n', raw, flags=re.IGNORECASE)
            raw = re.sub(r'</(?:p|div|h[1-6]|li|tr)>', '\n', raw, flags=re.IGNORECASE)
            raw = re.sub(r'<[^>]+>', '', raw)
            import html
            raw = html.unescape(raw)
            raw = re.sub(r'\n{3,}', '\n\n', raw).strip()
            if raw:
                full += raw + "\n\n"
    finally:
        os.unlink(tmp_path)
    return "utf-8", _split_pages(full)


# ── Book API endpoints ─────────────────────────────────────────────────────────

@app.post("/api/books/upload")
async def upload_book(
    file:   UploadFile = File(...),
    title:  str        = Form(""),
    author: str        = Form(""),
    status: str        = Form("want"),
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
    cover_url, enc = "", "utf-8"

    loop = asyncio.get_event_loop()
    if fname.endswith(".pdf"):
        cover_url, pages = await _extract_pdf(data, bid)
    elif fname.endswith(".epub"):
        enc, pages = await loop.run_in_executor(None, _extract_epub, data)
    else:
        enc, pages = await loop.run_in_executor(None, _extract_txt, data)

    total_pages = len(pages)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO books (book_id,title,author,cover_url,encoding,total_pages,status) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            bid, title, author, cover_url, enc, total_pages, status,
        )
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
            ("title",     body.get("title")),
            ("author",    body.get("author")),
            ("cover_url", body.get("cover_url")),
            ("status",    body.get("status")),
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
    agent_id = body.get("agent_id", "user")
    color    = AGENT_COLORS.get(agent_id, "#6366f1")
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
    async with _pool.acquire() as conn:
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
