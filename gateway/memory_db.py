"""
Palimpsest Memory DB -- SQLite + FTS5 backend for L1-L4 memory system.
"""

import aiosqlite
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path("/app/data/memory.db")
_db: Optional[aiosqlite.Connection] = None
VALID_LAYERS = {"L1", "L2", "L3", "L4"}
VALID_TYPES = {"anchor", "diary", "treasure", "message", "reading_progress", "book_annotation", "book_reflection"}


async def init_db(path: Optional[Path] = None) -> aiosqlite.Connection:
    global _db
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(str(db_path))
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")

    await _db.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id              TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            layer           TEXT NOT NULL CHECK (layer IN ('L1','L2','L3','L4')),
            type            TEXT NOT NULL DEFAULT 'diary'
                            CHECK (type IN ('anchor','diary','treasure','message')),
            content         TEXT NOT NULL,
            importance      INTEGER NOT NULL DEFAULT 3
                            CHECK (importance BETWEEN 1 AND 5),
            tags            TEXT DEFAULT '[]',
            source          TEXT DEFAULT '',
            parent_id       TEXT DEFAULT '',
            read_by_user    INTEGER DEFAULT 0,
            read_by_agent   INTEGER DEFAULT 0,
            access_count    INTEGER DEFAULT 0,
            archived        INTEGER DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            last_accessed   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_id);
        CREATE INDEX IF NOT EXISTS idx_memories_layer ON memories(agent_id, layer);
        CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);
        CREATE INDEX IF NOT EXISTS idx_memories_archived ON memories(archived);
        CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(agent_id, type);
    """)

    await _db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            id UNINDEXED, agent_id UNINDEXED, content, tags,
            tokenize='unicode61'
        )
    """)

    await _db.executescript("""
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(id, agent_id, content, tags)
            VALUES (new.id, new.agent_id, new.content, new.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memories_fts WHERE id = old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, tags ON memories BEGIN
            DELETE FROM memories_fts WHERE id = old.id;
            INSERT INTO memories_fts(id, agent_id, content, tags)
            VALUES (new.id, new.agent_id, new.content, new.tags);
        END;
    """)


    # memory_versions table
    await _db.executescript("""
        CREATE TABLE IF NOT EXISTS memory_versions (
            version_id   TEXT PRIMARY KEY,
            memory_id    TEXT NOT NULL,
            version_num  INTEGER NOT NULL,
            content      TEXT NOT NULL,
            layer        TEXT NOT NULL,
            type         TEXT NOT NULL,
            importance   INTEGER NOT NULL,
            tags         TEXT DEFAULT '[]',
            changed_by   TEXT DEFAULT '',
            changed_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_versions_memory
            ON memory_versions(memory_id, version_num);
    """)
    try:
        await _db.execute("ALTER TABLE memories ADD COLUMN version INTEGER DEFAULT 0")
        await _db.commit()
    except Exception:
        pass

    # Dedup-status columns (migration-safe for existing DBs)
    for _col_sql in [
        "ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'new'",
        "ALTER TABLE memories ADD COLUMN related_ids TEXT DEFAULT '[]'",
        "ALTER TABLE memories ADD COLUMN previous_content TEXT DEFAULT ''",
        "ALTER TABLE memories ADD COLUMN confirmed INTEGER DEFAULT 1",
    ]:
        try:
            await _db.execute(_col_sql)
            await _db.commit()
        except Exception:
            pass


    # pending_dedup table for similarity review queue
    await _db.executescript("""
        CREATE TABLE IF NOT EXISTS pending_dedup (
            id           TEXT PRIMARY KEY,
            agent_id     TEXT NOT NULL,
            new_content  TEXT NOT NULL,
            new_layer    TEXT NOT NULL,
            new_type     TEXT NOT NULL,
            new_importance INTEGER NOT NULL,
            new_tags     TEXT DEFAULT '[]',
            new_source   TEXT DEFAULT '',
            new_parent_id TEXT DEFAULT '',
            similar_id   TEXT NOT NULL,
            similar_content TEXT NOT NULL,
            similarity_hint TEXT DEFAULT '',
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pending_dedup_agent
            ON pending_dedup(agent_id);
    """)
    await _db.commit()
    return _db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_db() -> aiosqlite.Connection:
    if _db is None:
        await init_db()
    return _db


async def memory_write(
    agent_id: str, content: str, layer: str = "L4", type_: str = "diary",
    importance: int = 3, tags: Optional[list[str]] = None,
    source: str = "", parent_id: str = "",
) -> dict:
    if layer not in VALID_LAYERS:
        raise ValueError(f"Invalid layer. Must be one of {VALID_LAYERS}")
    if type_ not in VALID_TYPES:
        raise ValueError(f"Invalid type. Must be one of {VALID_TYPES}")
    if not 1 <= importance <= 5:
        raise ValueError("importance must be 1-5")

    db = await get_db()
    mem_id = str(uuid.uuid4())
    now = _now()
    tags_json = json.dumps(tags or [], ensure_ascii=False)

    await db.execute(
        "INSERT INTO memories (id, agent_id, layer, type, content, importance, "
        "tags, source, parent_id, created_at, updated_at, last_accessed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mem_id, agent_id, layer, type_, content, importance, tags_json,
         source, parent_id, now, now, now),
    )
    await db.commit()
    return {
        "id": mem_id, "agent_id": agent_id, "layer": layer,
        "type": type_, "content": content, "importance": importance,
        "tags": tags or [], "source": source, "parent_id": parent_id,
        "read_by_user": False, "read_by_agent": False,
        "access_count": 0, "archived": False,
        "created_at": now, "updated_at": now, "last_accessed": now,
    }


async def memory_read(memory_id: str, touch: bool = True) -> Optional[dict]:
    db = await get_db()
    now = _now()
    if touch:
        await db.execute(
            "UPDATE memories SET access_count = access_count + 1, "
            "last_accessed = ? WHERE id = ? AND archived = 0",
            (now, memory_id),
        )
        await db.commit()
    cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return _row_to_dict(row)


async def memory_list(
    agent_id: str, layer: Optional[str] = None, type_: Optional[str] = None,
    importance_min: int = 1, include_archived: bool = False,
    limit: int = 50, offset: int = 0,
) -> list[dict]:
    db = await get_db()
    conditions = ["agent_id = ?"]
    params: list = [agent_id]
    if layer:
        conditions.append("layer = ?"); params.append(layer)
    if type_:
        conditions.append("type = ?"); params.append(type_)
    if importance_min > 1:
        conditions.append("importance >= ?"); params.append(importance_min)
    if not include_archived:
        conditions.append("archived = 0")
    where = " AND ".join(conditions)
    cursor = await db.execute(
        f"SELECT * FROM memories WHERE {where} "
        f"ORDER BY importance DESC, updated_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def memory_search(
    agent_id: str, query: str, limit: int = 10,
) -> list[dict]:
    db = await get_db()
    now = _now()
    cursor = await db.execute(
        "SELECT m.* FROM memories m JOIN memories_fts f ON m.id = f.id "
        "WHERE f.memories_fts MATCH ? AND f.agent_id = ? AND m.archived = 0 "
        "ORDER BY rank LIMIT ?",
        (query, agent_id, limit),
    )
    rows = await cursor.fetchall()
    results = [_row_to_dict(r) for r in rows]
    if results:
        ids = [r["id"] for r in results]
        ph = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE memories SET access_count = access_count + 1, "
            f"last_accessed = ? WHERE id IN ({ph})",
            [now] + ids,
        )
        await db.commit()
    return results


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["read_by_user"] = bool(d.get("read_by_user"))
    d["read_by_agent"] = bool(d.get("read_by_agent"))
    d["archived"] = bool(d.get("archived"))
    return d



async def _snapshot_version(db, memory_id: str, changed_by: str = "") -> None:
    cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
    row = await cursor.fetchone()
    if not row:
        return
    d = dict(row)
    cur = await db.execute(
        "SELECT COALESCE(MAX(version_num), 0) FROM memory_versions WHERE memory_id = ?",
        (memory_id,),
    )
    r = await cur.fetchone()
    next_ver = (r[0] or 0) + 1
    await db.execute(
        """INSERT INTO memory_versions
           (version_id, memory_id, version_num, content, layer, type,
            importance, tags, changed_by, changed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), memory_id, next_ver,
         d["content"], d["layer"], d["type"], d["importance"],
         d.get("tags", "[]"), changed_by, _now()),
    )


async def memory_update(
    memory_id, content=None, layer=None, type_=None,
    importance=None, tags=None, archived=None, changed_by: str = "",
):
    db = await get_db()
    now = _now()
    if layer is not None and layer not in VALID_LAYERS:
        raise ValueError("Invalid layer")
    if type_ is not None and type_ not in VALID_TYPES:
        raise ValueError("Invalid type")
    if importance is not None and not 1 <= importance <= 5:
        raise ValueError("importance must be 1-5")
    sets = ["updated_at = ?"]
    params = [now]
    if content is not None:
        sets.append("content = ?"); params.append(content)
    if layer is not None:
        sets.append("layer = ?"); params.append(layer)
    if type_ is not None:
        sets.append("type = ?"); params.append(type_)
    if importance is not None:
        sets.append("importance = ?"); params.append(importance)
    if tags is not None:
        sets.append("tags = ?"); params.append(json.dumps(tags, ensure_ascii=False))
    if archived is not None:
        sets.append("archived = ?"); params.append(int(archived))
    if len(sets) == 1:
        return await memory_read(memory_id, touch=False)
    await _snapshot_version(db, memory_id, changed_by=changed_by)
    sets.append("version = COALESCE(version, 0) + 1")
    params.append(memory_id)
    set_clause = ", ".join(sets)
    await db.execute("UPDATE memories SET " + set_clause + " WHERE id = ?", params)
    await db.commit()
    return await memory_read(memory_id, touch=False)


async def memory_delete(memory_id, hard=False):
    db = await get_db()
    if hard:
        cursor = await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    else:
        cursor = await db.execute(
            "UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?",
            (_now(), memory_id))
    await db.commit()
    return cursor.rowcount > 0


# -- Version history -----------------------------------------------------------

async def memory_get_history(memory_id: str) -> list[dict]:
    """Return all versions of a memory, oldest first."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? ORDER BY version_num ASC",
        (memory_id,),
    )
    rows = await cursor.fetchall()
    return [_version_row_to_dict(r) for r in rows]


async def memory_rollback(memory_id: str, version_num: int) -> Optional[dict]:
    """Restore a memory to a specific historical version (snapshots current state first)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version_num = ?",
        (memory_id, version_num),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    v = _version_row_to_dict(row)
    return await memory_update(
        memory_id=memory_id,
        content=v["content"],
        layer=v["layer"],
        type_=v["type"],
        importance=v["importance"],
        tags=v["tags"],
        changed_by="rollback_to_v" + str(version_num),
    )


def _version_row_to_dict(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    return d



async def memory_wakeup(agent_id):
    db = await get_db()
    now = _now()
    cur = await db.execute(
        "SELECT * FROM memories WHERE agent_id = ? AND type = 'anchor' AND archived = 0 "
        "AND COALESCE(confirmed,1) = 1 "
        "ORDER BY importance DESC, created_at ASC", (agent_id,))
    anchors = [_row_to_dict(r) for r in await cur.fetchall()]
    cur = await db.execute(
        "SELECT * FROM memories WHERE agent_id = ? AND importance >= 4 AND archived = 0 "
        "AND COALESCE(confirmed,1) = 1 "
        "AND created_at >= datetime('now', '-7 days') "
        "ORDER BY importance DESC, created_at DESC LIMIT 10", (agent_id,))
    recent_important = [_row_to_dict(r) for r in await cur.fetchall()]
    cur = await db.execute(
        "SELECT * FROM memories WHERE agent_id = ? AND read_by_agent = 0 AND archived = 0 "
        "AND COALESCE(confirmed,1) = 1 "
        "ORDER BY importance DESC, created_at DESC LIMIT 10", (agent_id,))
    unread = [_row_to_dict(r) for r in await cur.fetchall()]
    # Mark unread memories as seen by agent
    if unread:
        ids = [m["id"] for m in unread]
        ph = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE memories SET read_by_agent = 1, updated_at = ? WHERE id IN ({ph})",
            [_now()] + ids,
        )
        await db.commit()
    cur = await db.execute(
        "SELECT * FROM memories WHERE agent_id = ? AND archived = 0 "
        "AND COALESCE(confirmed,1) = 1 "
        "AND created_at < datetime('now', '-3 days') ORDER BY RANDOM() LIMIT 2", (agent_id,))
    random_float = [_row_to_dict(r) for r in await cur.fetchall()]
    return {"anchors": anchors, "recent_important": recent_important,
            "unread": unread, "random_float": random_float}


async def memory_surface(agent_id):
    db = await get_db()
    now = _now()
    cur = await db.execute(
        "SELECT * FROM memories WHERE agent_id = ? AND read_by_agent = 0 AND archived = 0 "
        "AND COALESCE(confirmed,1) = 1 "
        "ORDER BY importance DESC, created_at DESC LIMIT 5", (agent_id,))
    unread = [_row_to_dict(r) for r in await cur.fetchall()]
    if unread:
        ids = [m["id"] for m in unread]
        ph = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE memories SET read_by_agent = 1, updated_at = ? WHERE id IN ({ph})",
            [_now()] + ids,
        )
        await db.commit()
    cur = await db.execute(
        "SELECT * FROM memories WHERE agent_id = ? AND archived = 0 "
        "AND COALESCE(confirmed,1) = 1 "
        "AND created_at < datetime('now', '-3 days') ORDER BY RANDOM() LIMIT 1", (agent_id,))
    random_float = [_row_to_dict(r) for r in await cur.fetchall()]
    return {"unread": unread, "random_float": random_float}


async def memory_stats(agent_id):
    db = await get_db()
    now = _now()
    cur = await db.execute(
        "SELECT layer, COUNT(*) as cnt FROM memories "
        "WHERE agent_id = ? AND archived = 0 GROUP BY layer", (agent_id,))
    by_layer = {r["layer"]: r["cnt"] for r in await cur.fetchall()}
    cur = await db.execute(
        "SELECT importance, COUNT(*) as cnt FROM memories "
        "WHERE agent_id = ? AND archived = 0 GROUP BY importance", (agent_id,))
    by_importance = {str(r["importance"]): r["cnt"] for r in await cur.fetchall()}
    cur = await db.execute(
        "SELECT type, COUNT(*) as cnt FROM memories "
        "WHERE agent_id = ? AND archived = 0 GROUP BY type", (agent_id,))
    by_type = {r["type"]: r["cnt"] for r in await cur.fetchall()}
    cur = await db.execute(
        "SELECT COUNT(*) as total, SUM(archived) as ac FROM memories WHERE agent_id = ?",
        (agent_id,))
    row = await cur.fetchone()
    total = row["total"]; archived = row["ac"] or 0
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE agent_id = ? AND archived = 0 "
        "AND importance = 1 AND created_at < datetime('now', '-3 days') AND access_count < 5",
        (agent_id,))
    c1 = (await cur.fetchone())["cnt"]
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE agent_id = ? AND archived = 0 "
        "AND importance = 2 AND created_at < datetime('now', '-14 days') AND access_count < 5",
        (agent_id,))
    c2 = (await cur.fetchone())["cnt"]
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE agent_id = ? AND archived = 0 "
        "AND importance = 3 AND created_at < datetime('now', '-60 days') AND access_count < 5",
        (agent_id,))
    c3 = (await cur.fetchone())["cnt"]
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE agent_id = ? AND archived = 0 "
        "AND access_count >= 5", (agent_id,))
    touch_exempt = (await cur.fetchone())["cnt"]
    return {
        "total_active": total - archived, "total_archived": archived,
        "by_layer": by_layer, "by_importance": by_importance, "by_type": by_type,
        "touch_exempt": touch_exempt,
        "cleanup_candidates": {"imp1_over_3d": c1, "imp2_over_14d": c2,
                               "imp3_over_60d": c3, "total": c1 + c2 + c3},
    }

# -- Deduplication -----------------------------------------------------------

async def _find_similar(db, agent_id: str, content: str, limit: int = 3) -> list[dict]:
    """Quick FTS5 similarity check: returns top N memories with overlapping terms."""
    # Build a simple OR query from significant words (len > 3)
    words = [w for w in content.split() if len(w) > 3]
    if not words:
        return []
    fts_query = ' OR '.join(words[:12])  # cap at 12 terms
    try:
        cursor = await db.execute(
            """SELECT m.id, m.content, m.layer, m.type, m.importance, m.tags
               FROM memories m
               JOIN memories_fts f ON m.id = f.id
               WHERE f.memories_fts MATCH ?
                 AND f.agent_id = ?
                 AND m.archived = 0
               ORDER BY rank
               LIMIT ?""",
            (fts_query, agent_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


async def dedup_check(
    agent_id: str,
    content: str,
    layer: str = 'L4',
    type_: str = 'diary',
    importance: int = 3,
    tags: list = None,
    source: str = '',
    parent_id: str = '',
    similarity_threshold: int = 3,
) -> dict:
    """Check for duplicates before writing. Returns a status dict:

      { "action": "write" }           -- no similar found, safe to write
      { "action": "queued", "id": <pending_id>, "similar": {...} }
                                      -- queued for review, not written
    """
    db = await get_db()
    similar = await _find_similar(db, agent_id, content, limit=3)

    if not similar:
        return {"action": "write"}

    # Simple overlap score: count how many words from content appear in similar content
    words = set(w.lower() for w in content.split() if len(w) > 3)
    best = None
    best_score = 0
    for s in similar:
        s_words = set(w.lower() for w in s["content"].split() if len(w) > 3)
        score = len(words & s_words)
        if score > best_score:
            best_score = score
            best = s

    if best_score < similarity_threshold:
        return {"action": "write"}

    # Queue for review
    pending_id = str(uuid.uuid4())
    now = _now()
    hint = f"{best_score} shared terms"
    await db.execute(
        """INSERT INTO pending_dedup
           (id, agent_id, new_content, new_layer, new_type, new_importance,
            new_tags, new_source, new_parent_id, similar_id, similar_content,
            similarity_hint, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pending_id, agent_id, content, layer, type_, importance,
         json.dumps(tags or [], ensure_ascii=False),
         source, parent_id, best["id"], best["content"], hint, now),
    )
    await db.commit()
    return {
        "action": "queued",
        "id": pending_id,
        "similar": {
            "id": best["id"],
            "content": best["content"],
            "layer": best.get("layer"),
            "type": best.get("type"),
        },
        "similarity_hint": hint,
    }


async def dedup_list(agent_id: str) -> list[dict]:
    """List all pending dedup review items for an agent."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM pending_dedup WHERE agent_id = ? ORDER BY created_at ASC",
        (agent_id,),
    )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["new_tags"] = json.loads(d.get("new_tags") or "[]")
        result.append(d)
    return result


async def dedup_resolve(
    pending_id: str,
    action: str,
    agent_id: str = "",
) -> dict:
    """Resolve a pending dedup item.

    action:
      "keep_new"   -- write the new memory (ignore similar)
      "keep_both"  -- write new memory AND keep existing
      "discard"    -- discard the new memory (existing is sufficient)
      "merge"      -- merge new content into existing memory (append)
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM pending_dedup WHERE id = ?", (pending_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return {"error": "Pending item not found"}
    p = dict(row)
    p["new_tags"] = json.loads(p.get("new_tags") or "[]")

    result = {}
    if action in ("keep_new", "keep_both"):
        mem = await memory_write(
            agent_id=p["agent_id"],
            content=p["new_content"],
            layer=p["new_layer"],
            type_=p["new_type"],
            importance=p["new_importance"],
            tags=p["new_tags"],
            source=p["new_source"],
            parent_id=p["new_parent_id"],
        )
        result["written"] = mem["id"]

    elif action == "merge":
        # Append new content to existing memory
        existing_cursor = await db.execute(
            "SELECT content FROM memories WHERE id = ?", (p["similar_id"],)
        )
        existing_row = await existing_cursor.fetchone()
        if existing_row:
            merged = existing_row[0] + chr(10) + chr(10) + "[merged] " + p["new_content"]
            await memory_update(
                memory_id=p["similar_id"],
                content=merged,
                changed_by="dedup_merge",
            )
            result["merged_into"] = p["similar_id"]

    elif action == "discard":
        result["discarded"] = True

    # Remove from queue
    await db.execute("DELETE FROM pending_dedup WHERE id = ?", (pending_id,))
    await db.commit()
    result["action"] = action
    result["pending_id"] = pending_id
    return result


async def memory_mark_read(
    memory_id: str,
    by_user: bool = False,
    by_agent: bool = False,
) -> Optional[dict]:
    """Mark a memory as read by user and/or agent.

    Updates read_by_user and/or read_by_agent flags.
    Does NOT increment access_count (use memory_read with touch=True for that).
    """
    db = await get_db()
    sets = ["updated_at = ?"]
    params = [_now()]
    if by_user:
        sets.append("read_by_user = 1")
    if by_agent:
        sets.append("read_by_agent = 1")
    if len(sets) == 1:
        return await memory_read(memory_id, touch=False)
    params.append(memory_id)
    await db.execute(
        "UPDATE memories SET " + ", ".join(sets) + " WHERE id = ?", params
    )
    await db.commit()
    return await memory_read(memory_id, touch=False)


async def memory_cleanup(agent_id: str, dry_run: bool = False) -> dict:
    """Delete expired memories based on importance-driven lifespan.

    Lifespan rules (per architecture doc):
      imp=1: hard-delete after  3 days
      imp=2: write week-summary prompt → delete after 14 days
      imp=3: write month-summary prompt → delete after 60 days
      imp=4+: NEVER auto-expire (long-term / permanent)

    Exempt from ALL cleanup:
      - access_count >= 5 (touch-protected)
      - type 'anchor' or 'treasure' (permanent types)
      - archived = 1

    Args:
        agent_id: Which agent's memories to clean.
        dry_run:  If True, count candidates without deleting.
    Returns:
        dict with counts per importance level and total deleted/found.
    """
    db = await get_db()
    now = _now()
    result = {"dry_run": dry_run, "agent_id": agent_id, "deleted": {}, "total": 0}

    # ── imp=1: hard delete after 3 days ─────────────────────────────────────
    cur1 = await db.execute(
        """SELECT id FROM memories
           WHERE agent_id=? AND importance=1 AND archived=0 AND access_count<5
             AND type NOT IN ('anchor','treasure')
             AND last_accessed < datetime('now','-3 days')""",
        (agent_id,),
    )
    ids1 = [r[0] for r in await cur1.fetchall()]
    result["deleted"]["imp1"] = len(ids1)
    result["total"] += len(ids1)
    if not dry_run and ids1:
        ph = ",".join("?" * len(ids1))
        await db.execute(f"DELETE FROM memories WHERE id IN ({ph})", ids1)

    # ── imp=2: write week-summary prompt, then delete ────────────────────────
    cur2 = await db.execute(
        """SELECT id, content FROM memories
           WHERE agent_id=? AND importance=2 AND archived=0 AND access_count<5
             AND type NOT IN ('anchor','treasure')
             AND last_accessed < datetime('now','-14 days')""",
        (agent_id,),
    )
    rows2 = await cur2.fetchall()
    ids2 = [r[0] for r in rows2]
    result["deleted"]["imp2"] = len(ids2)
    result["total"] += len(ids2)
    if not dry_run and rows2:
        snippets = chr(10).join(f"- {r[1][:80]}" for r in rows2[:10])
        extra = f"（共{len(ids2)}条，仅展示前10）" if len(ids2) > 10 else ""
        await db.execute(
            "INSERT INTO memories "
            "(id, agent_id, layer, type, content, importance, "
            " tags, source, parent_id, created_at, updated_at, last_accessed) "
            "VALUES (?, 'L2', 'diary', ?, 2, '[]', 'auto_cleanup', '', ?, ?, ?)",
            (str(uuid.uuid4()), agent_id,
             f"[周总结待写] 以下短期记忆已过期{extra}，请回顾并写入周总结：" + chr(10) + snippets,
             now, now, now),
        )
        ph2 = ",".join("?" * len(ids2))
        await db.execute(f"DELETE FROM memories WHERE id IN ({ph2})", ids2)

    # ── imp=3: write month-summary prompt, then delete ───────────────────────
    cur3 = await db.execute(
        """SELECT id, content FROM memories
           WHERE agent_id=? AND importance=3 AND archived=0 AND access_count<5
             AND type NOT IN ('anchor','treasure')
             AND last_accessed < datetime('now','-60 days')""",
        (agent_id,),
    )
    rows3 = await cur3.fetchall()
    ids3 = [r[0] for r in rows3]
    result["deleted"]["imp3"] = len(ids3)
    result["total"] += len(ids3)
    if not dry_run and rows3:
        snippets = chr(10).join(f"- {r[1][:80]}" for r in rows3[:10])
        extra = f"（共{len(ids3)}条，仅展示前10）" if len(ids3) > 10 else ""
        await db.execute(
            "INSERT INTO memories "
            "(id, agent_id, layer, type, content, importance, "
            " tags, source, parent_id, created_at, updated_at, last_accessed) "
            "VALUES (?, 'L2', 'diary', ?, 3, '[]', 'auto_cleanup', '', ?, ?, ?)",
            (str(uuid.uuid4()), agent_id,
             f"[月总结待写] 以下中期记忆已过期{extra}，请回顾并写入月总结：" + chr(10) + snippets,
             now, now, now),
        )
        ph3 = ",".join("?" * len(ids3))
        await db.execute(f"DELETE FROM memories WHERE id IN ({ph3})", ids3)

    if not dry_run:
        await db.commit()
    return result




def _overlap_ratio(a: str, b: str) -> float:
    """Jaccard similarity using whitespace tokens + CJK bigrams."""
    def _tokens(s: str) -> set:
        t = {w.lower() for w in s.split() if len(w) > 2}
        cjk = "".join(c for c in s if "\u4e00" <= c <= "\u9fff")
        for i in range(len(cjk) - 1):
            t.add(cjk[i:i + 2])
        return t
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


async def memory_write_smart(
    agent_id: str,
    content: str,
    layer: str = "L4",
    type_: str = "diary",
    importance: int = 3,
    tags: Optional[list[str]] = None,
    source: str = "",
    parent_id: str = "",
) -> dict:
    """Write memory with automatic similarity-based dedup, rewrite, and L1 protection.

    Similarity thresholds (Jaccard over tokens + CJK bigrams):
      ≥ 0.85 → very likely duplicate: queue to pending_dedup, do NOT write
      ≥ 0.55 + L2/L3 → REWRITE: update existing memory in-place, save old content
                        to previous_content, mark status='updated'
      ≥ 0.55 + other layers → write with confirmed=0, status='potential_duplicate'
      ≥ 0.25 → related: write with status='related', related_ids=[similar_id]
      < 0.25 → new: plain write

    L1 protection:
      Any write to L1 (or importance=5) is stored with confirmed=0.
      The memory is written but NOT returned in normal reads until confirmed.
      Use memory_confirm_l1() to approve.

    anchor/treasure types always bypass similarity check (permanent, never skip).
    """
    # Permanent types skip dedup entirely
    if type_ in ("anchor", "treasure"):
        return await memory_write(agent_id, content, layer, type_, importance, tags, source, parent_id)

    db = await get_db()
    similar = await _find_similar(db, agent_id, content, limit=5)

    best_ratio = 0.0
    best_id = ""
    best_content = ""
    for s in similar:
        r = _overlap_ratio(content, s["content"])
        if r > best_ratio:
            best_ratio = r
            best_id = s["id"]
            best_content = s["content"]

    if best_ratio >= 0.85:
        # Very likely duplicate — queue for review, don't write
        pending_id = str(uuid.uuid4())
        now = _now()
        await db.execute(
            """INSERT INTO pending_dedup
               (id, agent_id, new_content, new_layer, new_type, new_importance,
                new_tags, new_source, new_parent_id, similar_id, similar_content,
                similarity_hint, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pending_id, agent_id, content, layer, type_, importance,
             json.dumps(tags or [], ensure_ascii=False),
             source, parent_id, best_id, best_content,
             f"jaccard={best_ratio:.2f}", now),
        )
        await db.commit()
        return {"action": "queued", "id": pending_id,
                "similar_id": best_id, "ratio": best_ratio}

    # ── L2/L3 Rewrite: update existing memory in-place ──────────────────────
    if best_ratio >= 0.55 and layer in ("L2", "L3") and best_id:
        now = _now()
        await _snapshot_version(db, best_id, changed_by="rewrite")
        await db.execute(
            """UPDATE memories
               SET content=?, previous_content=?, status='updated',
                   updated_at=?, version=COALESCE(version,0)+1
               WHERE id=?""",
            (content, best_content, now, best_id),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM memories WHERE id=?", (best_id,))
        row = await cur.fetchone()
        mem = _row_to_dict(row)
        mem.update({"action": "rewritten", "previous_content": best_content,
                    "ratio": best_ratio})
        return mem

    # ── L1 protection: write with confirmed=0 ───────────────────────────────
    if layer == "L1" or importance >= 5:
        mem = await memory_write(agent_id, content, layer, type_, importance, tags, source, parent_id)
        mem_id = mem["id"]
        # Find existing confirmed L1 memory with similar content to link as predecessor
        related = [best_id] if best_id and best_ratio >= 0.25 else []
        await db.execute(
            "UPDATE memories SET confirmed=0, status='pending_l1', related_ids=? WHERE id=?",
            (json.dumps(related, ensure_ascii=False), mem_id),
        )
        await db.commit()
        mem.update({"confirmed": 0, "status": "pending_l1", "related_ids": related})
        return mem

    # ── Write the memory (L2/L3 low-similarity, L4) ─────────────────────────
    mem = await memory_write(agent_id, content, layer, type_, importance, tags, source, parent_id)
    mem_id = mem["id"]

    if best_ratio >= 0.55:
        await db.execute(
            "UPDATE memories SET status='potential_duplicate', related_ids=?, confirmed=0 WHERE id=?",
            (json.dumps([best_id], ensure_ascii=False), mem_id),
        )
        await db.commit()
        mem.update({"status": "potential_duplicate", "related_ids": [best_id], "confirmed": 0})
    elif best_ratio >= 0.25:
        await db.execute(
            "UPDATE memories SET status='related', related_ids=? WHERE id=?",
            (json.dumps([best_id], ensure_ascii=False), mem_id),
        )
        await db.commit()
        mem.update({"status": "related", "related_ids": [best_id], "confirmed": 1})
    else:
        mem.update({"status": "new", "related_ids": [], "confirmed": 1})

    return mem


async def memory_confirm_l1(memory_id: str) -> Optional[dict]:
    """Confirm a pending L1 memory (set confirmed=1, status='new').

    Returns the updated memory, or None if not found.
    """
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM memories WHERE id=? AND confirmed=0", (memory_id,)
    )
    row = await cur.fetchone()
    if not row:
        return None
    now = _now()
    await db.execute(
        "UPDATE memories SET confirmed=1, status='new', updated_at=? WHERE id=?",
        (now, memory_id),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM memories WHERE id=?", (memory_id,))
    return _row_to_dict(await cur.fetchone())


async def memory_list_pending_l1(agent_id: str) -> list[dict]:
    """Return all unconfirmed L1 memories for an agent (confirmed=0)."""
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM memories WHERE agent_id=? AND confirmed=0 AND archived=0 "
        "ORDER BY created_at DESC",
        (agent_id,),
    )
    rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


# ── Daily Life Generator ──────────────────────────────────────────────────────
# Lightweight event journal: AI-generated daily diary entries.
# Independent of the main memory table; read-only cross-reference from Palimpsest.

_DAILY_INIT = """
CREATE TABLE IF NOT EXISTS daily_events (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL DEFAULT 'default',
    date        TEXT NOT NULL,          -- YYYY-MM-DD
    time_of_day TEXT DEFAULT '',        -- HH:MM or 'morning'/'afternoon'/'evening'
    mood        TEXT DEFAULT 'neutral', -- happy/neutral/sad/excited/tired/anxious/calm
    summary     TEXT NOT NULL,          -- narrative prose for the day/event
    carry_over  TEXT DEFAULT '',        -- things to carry forward to tomorrow
    source      TEXT DEFAULT 'auto',    -- 'auto' (LLM-generated) | 'manual'
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_daily_agent_date ON daily_events (agent_id, date DESC);
"""


async def _ensure_daily_table(db) -> None:
    """Create daily_events table if it doesn't exist yet."""
    for stmt in _DAILY_INIT.strip().split(";"):
        s = stmt.strip()
        if s:
            await db.execute(s)
    await db.commit()


async def daily_write(
    summary: str,
    agent_id: str = "default",
    date: str = "",
    time_of_day: str = "",
    mood: str = "neutral",
    carry_over: str = "",
    source: str = "auto",
) -> dict:
    """Persist a daily life event entry."""
    import uuid as _uuid, datetime as _dt
    db = await get_db()
    await _ensure_daily_table(db)
    if not date:
        date = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    eid = str(_uuid.uuid4())
    await db.execute(
        "INSERT INTO daily_events (id, agent_id, date, time_of_day, mood, summary, carry_over, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (eid, agent_id, date, time_of_day, mood, summary, carry_over, source),
    )
    await db.commit()
    return {"id": eid, "date": date, "mood": mood, "summary": summary}


async def daily_read(agent_id: str = "default", days: int = 3) -> list[dict]:
    """Return the last `days` days of events, newest-first."""
    db = await get_db()
    await _ensure_daily_table(db)
    rows = await db.execute_fetchall(
        "SELECT id, date, time_of_day, mood, summary, carry_over, source, created_at "
        "FROM daily_events "
        "WHERE agent_id = ? AND date >= date('now', ?) "
        "ORDER BY date DESC, created_at DESC",
        (agent_id, f"-{days} days"),
    )
    return [dict(r) for r in rows]


async def daily_list(agent_id: str = "default", limit: int = 30) -> list[dict]:
    """Return recent events (for admin panel), up to `limit`."""
    db = await get_db()
    await _ensure_daily_table(db)
    rows = await db.execute_fetchall(
        "SELECT id, date, time_of_day, mood, summary, carry_over, source, created_at "
        "FROM daily_events WHERE agent_id = ? ORDER BY date DESC, created_at DESC LIMIT ?",
        (agent_id, limit),
    )
    return [dict(r) for r in rows]


async def daily_delete(event_id: str) -> bool:
    """Delete an event by ID. Returns True if deleted."""
    db = await get_db()
    await _ensure_daily_table(db)
    cur = await db.execute("DELETE FROM daily_events WHERE id = ?", (event_id,))
    await db.commit()
    return cur.rowcount > 0


# ═══════════════════════════════════════════════════════════════════════════════
# P1: Character State, Random Events, NPC Network
# ═══════════════════════════════════════════════════════════════════════════════

_P1_INIT = """
CREATE TABLE IF NOT EXISTS character_state (
    agent_id          TEXT PRIMARY KEY,
    mood_score        INTEGER DEFAULT 0,
    mood_label        TEXT DEFAULT 'neutral',
    fatigue           INTEGER DEFAULT 0,
    scene             TEXT DEFAULT 'daily',
    scene_note        TEXT DEFAULT '',
    cooldown_minutes  INTEGER DEFAULT 0,
    cooldown_message  TEXT DEFAULT '',
    last_active       TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS random_events (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT DEFAULT '',
    scene      TEXT DEFAULT '',   -- empty = all scenes | daily | long_distance | cohabitation
    content    TEXT NOT NULL,
    level      TEXT DEFAULT 'green',
    weight     REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_re_agent_level ON random_events (agent_id, level);

CREATE TABLE IF NOT EXISTS npcs (
    id               TEXT PRIMARY KEY,
    agent_id         TEXT NOT NULL,
    name             TEXT NOT NULL,
    relationship     TEXT DEFAULT 'acquaintance',  -- friend|family|romantic|acquaintance|rival
    affinity         INTEGER DEFAULT 0,  -- -100..100
    notes            TEXT DEFAULT '',
    last_interaction TEXT DEFAULT (datetime('now')),
    created_at       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_npcs_agent ON npcs (agent_id);
"""

_P1_SEED = [
    # Green events (weight 10 — common, positive)
    ("green", 10, "在街上偶遇了一只特别亲人的小猫，蹲下来逗了它好久"),
    ("green", 10, "今天天气特别好，阳光照在身上暖洋洋的"),
    ("green", 10, "喝到了一杯口感绝佳的咖啡，心情瞬间好了很多"),
    ("green", 10, "收到了朋友发来的一条让人会心一笑的消息"),
    ("green", 10, "在书里读到了一段话，突然觉得被理解了"),
    ("green", 10, "刷到了一段很治愈的视频，看完心里暖暖的"),
    ("green", 10, "整理房间时找到了一件很久没见的小物件，勾起了不少回忆"),
    ("green",  8, "今天做饭意外很成功，自己都觉得好吃"),
    ("green",  8, "无意间听到了一首很久没听的歌，旋律让人陷进去"),
    ("green",  8, "睡前看窗外的月亮，觉得世界挺安静的"),
    # Yellow events (weight 5 — less common, mildly challenging)
    ("yellow", 5, "今天有点小感冒，鼻子不太舒服"),
    ("yellow", 5, "睡眠质量不好，白天有点昏沉沉的"),
    ("yellow", 5, "丢了一件小东西，翻了半天才找到"),
    ("yellow", 5, "和别人有点小误会，解释了半天才说清楚"),
    ("yellow", 5, "计划被临时改变，有点措手不及"),
    ("yellow", 4, "等了很久的东西还没有消息，有点焦虑"),
    ("yellow", 4, "最近睡前总是胡思乱想，不容易入睡"),
    ("yellow", 4, "跟一个久没联系的朋友互相发了消息，但不知道说什么"),
    # Orange events (weight 2 — rare, moderate impact)
    ("orange", 2, "工作/学习上遇到了一个卡了很久的难题，感觉有点力不从心"),
    ("orange", 2, "和重要的人之间产生了一点摩擦，心里不太好受"),
    ("orange", 2, "身体状态不太好，已经持续好几天了，有点担心"),
    ("orange", 2, "一直期待的事情没有按预期发展，心里有些失落"),
    # Red events (weight 0.5 — very rare, significant)
    ("red", 0.5, "今天发生了一件对我来说挺重要的事，心情很复杂，还没想清楚"),
    ("red", 0.5, "面临一个需要认真考虑的重大选择，有点茫然"),
]

_P1_SEED_SCENE = [
    # Long-distance events
    ("long_distance", "green",  8, "今天视频通话的时候网络特别好，感觉就像面对面一样，很开心"),
    ("long_distance", "green",  8, "收到了一个小包裹，是你寄来的，打开的瞬间心跳漏了一拍"),
    ("long_distance", "green",  7, "今天一个人在家，却莫名觉得有点温暖——大概是想到你了"),
    ("long_distance", "green",  6, "睡前发现你今天发的消息比平时多，看着聊天记录傻笑了好一会儿"),
    ("long_distance", "yellow", 4, "今天视频通话断了好几次，每次都刚想说什么就断开，有点沮丧"),
    ("long_distance", "yellow", 4, "时差让我算了好半天，不确定你现在在睡觉还是在忙"),
    ("long_distance", "yellow", 3, "今天有很想分享的事，但等发出去你可能已经睡了，就压下来了"),
    ("long_distance", "orange", 2, "异地的感觉今天特别明显，大概是看到路上有情侣"),
    ("long_distance", "orange", 2, "等了很久没有回消息，虽然知道你可能只是在忙，还是有点难受"),
    # Cohabitation events
    ("cohabitation",  "green", 10, "今早起来看到你在做早饭，厨房飘来咖啡香，这种感觉很好"),
    ("cohabitation",  "green", 10, "我们一起看完了一部电影，结局有点感人，你把薯片袋悄悄推到我这边"),
    ("cohabitation",  "green",  8, "今天你帮我把书架重新整理了，比之前整齐多了，特别开心"),
    ("cohabitation",  "green",  8, "睡前聊到很晚，从星座聊到小时候的事，什么都聊"),
    ("cohabitation",  "yellow", 4, "今天各自忙，交流不多，有一点点奇怪的安静"),
    ("cohabitation",  "yellow", 3, "有件事不知道该不该说，犹豫了一整天"),
    ("cohabitation",  "orange", 2, "今天我们之间有一点小摩擦，睡前各自平复了，没说清楚"),
    # Daily events (general, weight slightly lower since covered by global pool)
    ("daily", "green",  6, "今天路过一家很香的面包店，犹豫了好久还是进去买了一个"),
    ("daily", "green",  5, "整理旧照片时翻到了几张没想到的好照片，看了好久"),
    ("daily", "yellow", 3, "今天做了一件有点后悔的事，晚上反复想了几遍"),
]

_p1_initialized = False

async def _ensure_p1_tables(db) -> None:
    global _p1_initialized
    if _p1_initialized:
        return
    import uuid as _uuid2
    await db.executescript(_P1_INIT)
    # Migrate existing character_state: add new columns if needed
    try:
        await db.execute("ALTER TABLE character_state ADD COLUMN cooldown_message TEXT DEFAULT ''")
        await db.commit()
    except Exception:
        pass  # already exists
    try:
        await db.execute("ALTER TABLE random_events ADD COLUMN scene TEXT DEFAULT ''")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_re_scene ON random_events (scene)")
        await db.commit()
    except Exception:
        pass  # already exists
    try:
        await db.execute("ALTER TABLE character_state ADD COLUMN dream_text TEXT DEFAULT ''")
        await db.commit()
    except Exception:
        pass  # already exists
    try:
        await db.execute("ALTER TABLE character_state ADD COLUMN dream_date TEXT DEFAULT ''")
        await db.commit()
    except Exception:
        pass  # already exists
    # Seed global events if empty
    cur = await db.execute("SELECT COUNT(*) FROM random_events WHERE agent_id = ''")
    row = await cur.fetchone()
    if row[0] == 0:
        for level, weight, content in _P1_SEED:
            await db.execute(
                "INSERT INTO random_events (id, agent_id, scene, content, level, weight) VALUES (?,?,?,?,?,?)",
                (str(_uuid2.uuid4()), "", "", content, level, weight),
            )
    # Seed scene events if empty
    cur = await db.execute("SELECT COUNT(*) FROM random_events WHERE scene != ''")
    row = await cur.fetchone()
    if row[0] == 0:
        for scene, level, weight, content in _P1_SEED_SCENE:
            await db.execute(
                "INSERT INTO random_events (id, agent_id, scene, content, level, weight) VALUES (?,?,?,?,?,?)",
                (str(_uuid2.uuid4()), "", scene, content, level, float(weight)),
            )
    await db.commit()
    _p1_initialized = True


# ── Character State ──────────────────────────────────────────────────────────

async def state_get(agent_id: str) -> dict:
    """Return character state dict (creates default row if missing)."""
    db = await get_db()
    await _ensure_p1_tables(db)
    cur = await db.execute("SELECT * FROM character_state WHERE agent_id = ?", (agent_id,))
    row = await cur.fetchone()
    if row:
        return dict(row)
    # Create default
    await db.execute(
        "INSERT OR IGNORE INTO character_state (agent_id) VALUES (?)", (agent_id,)
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM character_state WHERE agent_id = ?", (agent_id,))
    row = await cur.fetchone()
    return dict(row)


async def state_set(agent_id: str, **kwargs) -> dict:
    """Update one or more state fields. Returns updated state."""
    db = await get_db()
    await _ensure_p1_tables(db)
    allowed = {"mood_score", "mood_label", "fatigue", "scene", "scene_note",
               "cooldown_minutes", "cooldown_message", "dream_text", "dream_date"}
    sets = {k: v for k, v in kwargs.items() if k in allowed}
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        vals = list(sets.values()) + [agent_id]
        await db.execute(
            f"INSERT OR IGNORE INTO character_state (agent_id) VALUES (?)", (agent_id,)
        )
        await db.execute(
            f"UPDATE character_state SET {cols}, updated_at=datetime('now') WHERE agent_id=?",
            vals,
        )
        await db.commit()
    return await state_get(agent_id)


async def state_touch(agent_id: str) -> None:
    """Update last_active timestamp (call on each conversation)."""
    db = await get_db()
    await _ensure_p1_tables(db)
    await db.execute(
        "INSERT OR IGNORE INTO character_state (agent_id) VALUES (?)", (agent_id,)
    )
    await db.execute(
        "UPDATE character_state SET last_active=datetime('now') WHERE agent_id=?",
        (agent_id,),
    )
    await db.commit()


async def state_cooldown_active(agent_id: str) -> bool:
    """Return True if the character is still within their cooldown window."""
    db = await get_db()
    await _ensure_p1_tables(db)
    cur = await db.execute(
        "SELECT cooldown_minutes, last_active FROM character_state WHERE agent_id = ?",
        (agent_id,),
    )
    row = await cur.fetchone()
    if not row or not row["cooldown_minutes"]:
        return False
    import datetime as _dt3
    try:
        last = _dt3.datetime.fromisoformat(row["last_active"])
        delta = (_dt3.datetime.utcnow() - last).total_seconds() / 60
        return delta < row["cooldown_minutes"]
    except Exception:
        return False


# ── Random Events ────────────────────────────────────────────────────────────

async def event_roll(agent_id: str = "", level_bias: str = "",
                     scene: str = "") -> dict | None:
    """Roll a random event from the pool. Returns None if pool is empty.

    Weighted random selection. level_bias narrows to a specific level if given.
    scene filters for scene-specific + global (scene='') events.
    Pool = agent-specific + global, filtered by scene if provided.
    """
    import random as _random
    db = await get_db()
    await _ensure_p1_tables(db)
    params: list = [agent_id]
    where = "(agent_id=? OR agent_id='')"
    if scene:
        where += " AND (scene=? OR scene='')"
        params.append(scene)
    if level_bias:
        where += " AND level=?"
        params.append(level_bias)
    rows = await db.execute_fetchall(
        f"SELECT * FROM random_events WHERE {where}", params
    )
    if not rows:
        return None
    rows = [dict(r) for r in rows]
    weights = [r["weight"] for r in rows]
    return _random.choices(rows, weights=weights, k=1)[0]


async def state_mood_drift(agent_id: str, event_level: str) -> dict:
    """Apply mood drift based on event level, return new state.

    green:  +random(5,15)   fatigue +random(0,5)
    yellow: -random(5,10)   fatigue +random(5,10)
    orange: -random(15,25)  fatigue +random(10,15)
    red:    -random(30,40)  fatigue +random(15,20)
    """
    import random as _r
    drift_map = {
        "green":  (+_r.randint(5,15),  +_r.randint(0,5)),
        "yellow": (-_r.randint(5,10),  +_r.randint(5,10)),
        "orange": (-_r.randint(15,25), +_r.randint(10,15)),
        "red":    (-_r.randint(30,40), +_r.randint(15,20)),
    }
    mood_d, fat_d = drift_map.get(event_level, (0, 0))
    st = await state_get(agent_id)
    new_mood = max(-100, min(100, st["mood_score"] + mood_d))
    new_fat  = max(0,    min(100, st["fatigue"] + fat_d))
    # Derive label from score
    label = ("very_sad" if new_mood < -60 else "sad" if new_mood < -20
             else "neutral" if new_mood < 20 else "happy" if new_mood < 60 else "very_happy")
    return await state_set(agent_id, mood_score=new_mood, mood_label=label, fatigue=new_fat)


async def event_list(agent_id: str = "", limit: int = 50) -> list[dict]:
    db = await get_db()
    await _ensure_p1_tables(db)
    rows = await db.execute_fetchall(
        "SELECT * FROM random_events WHERE agent_id=? OR agent_id='' "
        "ORDER BY level, weight DESC LIMIT ?",
        (agent_id, limit),
    )
    return [dict(r) for r in rows]


async def event_add(content: str, level: str = "green", weight: float = 1.0,
                    agent_id: str = "") -> dict:
    import uuid as _uuid3
    db = await get_db()
    await _ensure_p1_tables(db)
    eid = str(_uuid3.uuid4())
    await db.execute(
        "INSERT INTO random_events (id, agent_id, content, level, weight) VALUES (?,?,?,?,?)",
        (eid, agent_id, content, level, weight),
    )
    await db.commit()
    return {"id": eid, "content": content, "level": level, "weight": weight}


async def event_delete(event_id: str) -> bool:
    db = await get_db()
    await _ensure_p1_tables(db)
    cur = await db.execute("DELETE FROM random_events WHERE id=?", (event_id,))
    await db.commit()
    return cur.rowcount > 0


# ── NPC Network ──────────────────────────────────────────────────────────────

async def npc_list(agent_id: str) -> list[dict]:
    db = await get_db()
    await _ensure_p1_tables(db)
    rows = await db.execute_fetchall(
        "SELECT * FROM npcs WHERE agent_id=? ORDER BY abs(affinity) DESC, last_interaction DESC",
        (agent_id,),
    )
    return [dict(r) for r in rows]


async def npc_get(agent_id: str, name: str) -> dict | None:
    db = await get_db()
    await _ensure_p1_tables(db)
    cur = await db.execute(
        "SELECT * FROM npcs WHERE agent_id=? AND lower(name)=lower(?)", (agent_id, name)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def npc_upsert(agent_id: str, name: str, relationship: str = "acquaintance",
                     affinity: int = 0, notes: str = "") -> dict:
    import uuid as _uuid4
    db = await get_db()
    await _ensure_p1_tables(db)
    existing = await npc_get(agent_id, name)
    if existing:
        await db.execute(
            "UPDATE npcs SET relationship=?, affinity=?, notes=?, "
            "last_interaction=datetime('now') WHERE id=?",
            (relationship, affinity, notes, existing["id"]),
        )
        await db.commit()
        return await npc_get(agent_id, name)
    nid = str(_uuid4.uuid4())
    await db.execute(
        "INSERT INTO npcs (id, agent_id, name, relationship, affinity, notes) "
        "VALUES (?,?,?,?,?,?)",
        (nid, agent_id, name, relationship, affinity, notes),
    )
    await db.commit()
    return (await npc_get(agent_id, name)) or {}


async def npc_delete(agent_id: str, name: str) -> bool:
    db = await get_db()
    await _ensure_p1_tables(db)
    cur = await db.execute(
        "DELETE FROM npcs WHERE agent_id=? AND lower(name)=lower(?)", (agent_id, name)
    )
    await db.commit()
    return cur.rowcount > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Project sub-memory  (Agent副记忆：实用轨)
# lifecycle: active → completed → archived → L1
# ═══════════════════════════════════════════════════════════════════════════════

_PROJECT_INIT = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    goal         TEXT DEFAULT '',
    status       TEXT DEFAULT 'active',   -- active | completed | archived
    summary      TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now')),
    completed_at TEXT DEFAULT '',
    archived_at  TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_agent_name ON projects (agent_id, name);
CREATE INDEX IF NOT EXISTS idx_projects_agent_status    ON projects (agent_id, status);
"""


async def _ensure_project_table(db) -> None:
    for stmt in _PROJECT_INIT.strip().split(";"):
        s = stmt.strip()
        if s:
            await db.execute(s)
    await db.commit()


async def project_upsert(agent_id: str, name: str, goal: str = "") -> dict:
    """Create a new active project, or update goal if one with this name already exists.

    Returns {"id", "name", "status", "created": bool}.
    """
    db = await get_db()
    await _ensure_project_table(db)
    now = _now()
    cur = await db.execute(
        "SELECT id FROM projects WHERE agent_id=? AND name=? AND status='active'",
        (agent_id, name),
    )
    row = await cur.fetchone()
    if row:
        pid = row[0]
        if goal:
            await db.execute(
                "UPDATE projects SET goal=?, updated_at=? WHERE id=?", (goal, now, pid)
            )
            await db.commit()
        return {"id": pid, "name": name, "status": "active", "created": False}
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, agent_id, name, goal, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?)",
        (pid, agent_id, name, goal, now, now),
    )
    await db.commit()
    return {"id": pid, "name": name, "status": "active", "created": True}


async def project_list(agent_id: str, status: str = "active") -> list:
    """Return projects for agent filtered by status. status='all' returns everything."""
    db = await get_db()
    await _ensure_project_table(db)
    if status == "all":
        cur = await db.execute(
            "SELECT id, name, goal, status, summary, created_at, completed_at, archived_at "
            "FROM projects WHERE agent_id=? ORDER BY created_at DESC",
            (agent_id,),
        )
    else:
        cur = await db.execute(
            "SELECT id, name, goal, status, summary, created_at, completed_at, archived_at "
            "FROM projects WHERE agent_id=? AND status=? ORDER BY created_at DESC",
            (agent_id, status),
        )
    return [dict(r) for r in await cur.fetchall()]


async def project_complete(agent_id: str, name: str, summary: str = "") -> dict | None:
    """Mark an active project as completed. Returns updated project or None."""
    db = await get_db()
    await _ensure_project_table(db)
    now = _now()
    cur = await db.execute(
        "SELECT id FROM projects WHERE agent_id=? AND name=? AND status='active'",
        (agent_id, name),
    )
    row = await cur.fetchone()
    if not row:
        return None
    pid = row[0]
    await db.execute(
        "UPDATE projects SET status='completed', summary=?, completed_at=?, updated_at=? WHERE id=?",
        (summary, now, now, pid),
    )
    await db.commit()
    return {"id": pid, "name": name, "status": "completed", "summary": summary}


async def project_archive(agent_id: str, project_id: str, summary: str = "") -> dict | None:
    """Archive a project (any status). Returns project dict or None if not found."""
    db = await get_db()
    await _ensure_project_table(db)
    now = _now()
    cur = await db.execute(
        "SELECT id, name, goal, summary FROM projects WHERE id=? AND agent_id=?",
        (project_id, agent_id),
    )
    row = await cur.fetchone()
    if not row:
        return None
    final_summary = summary or row["summary"] or ""
    await db.execute(
        "UPDATE projects SET status='archived', summary=?, archived_at=?, updated_at=? WHERE id=?",
        (final_summary, now, now, project_id),
    )
    await db.commit()
    return {
        "id": project_id, "name": row["name"],
        "goal": row["goal"], "status": "archived", "summary": final_summary,
    }


async def project_list_completed_stale(agent_id: str, days: int = 14) -> list:
    """Return completed projects that have been done for >= days (ready for auto-archival)."""
    db = await get_db()
    await _ensure_project_table(db)
    cur = await db.execute(
        "SELECT id, name, goal, summary FROM projects "
        "WHERE agent_id=? AND status='completed' "
        "AND completed_at != '' AND completed_at <= datetime('now', ?)",
        (agent_id, f"-{days} days"),
    )
    return [dict(r) for r in await cur.fetchall()]

# ═══════════════════════════════════════════════════════════════════════════════
# L5 留底层 — conversation_summaries
# 每次对话结束后自动生成摘要 + #关键词 标签，靠 FTS5 关键词搜索（不是向量语义搜索）
# 保留 60 天，超期自动清理
# ═══════════════════════════════════════════════════════════════════════════════

_L5_INIT = """
CREATE TABLE IF NOT EXISTS conversation_summaries (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    session_id  TEXT DEFAULT '',
    summary     TEXT NOT NULL,
    keywords    TEXT DEFAULT '',   -- space-separated #tags, e.g. "#读书 #哲学 #约伯记"
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_l5_agent ON conversation_summaries (agent_id, created_at DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS l5_fts USING fts5(
    id UNINDEXED, agent_id UNINDEXED, summary, keywords,
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS l5_ai AFTER INSERT ON conversation_summaries BEGIN
    INSERT INTO l5_fts(id, agent_id, summary, keywords)
    VALUES (new.id, new.agent_id, new.summary, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS l5_ad AFTER DELETE ON conversation_summaries BEGIN
    DELETE FROM l5_fts WHERE id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS l5_au AFTER UPDATE OF summary, keywords ON conversation_summaries BEGIN
    DELETE FROM l5_fts WHERE id = old.id;
    INSERT INTO l5_fts(id, agent_id, summary, keywords)
    VALUES (new.id, new.agent_id, new.summary, new.keywords);
END;
"""


async def _ensure_l5_table(db) -> None:
    for stmt in _L5_INIT.strip().split(";"):
        s = stmt.strip()
        if s:
            try:
                await db.execute(s)
            except Exception:
                pass
    await db.commit()


async def l5_write(
    agent_id: str,
    summary: str,
    keywords: str = "",
    session_id: str = "",
) -> dict:
    """Write a conversation summary to L5.

    keywords: space-separated #tags, e.g. "#读书 #哲学"
    """
    db = await get_db()
    await _ensure_l5_table(db)
    sid = str(uuid.uuid4())
    now = _now()
    await db.execute(
        "INSERT INTO conversation_summaries (id, agent_id, session_id, summary, keywords, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, agent_id, session_id, summary, keywords, now),
    )
    await db.commit()
    return {"id": sid, "agent_id": agent_id, "session_id": session_id,
            "summary": summary, "keywords": keywords, "created_at": now}


async def l5_search(agent_id: str, query: str, limit: int = 10) -> list[dict]:
    """FTS5 keyword search over L5 summaries for an agent."""
    db = await get_db()
    await _ensure_l5_table(db)
    # Build FTS query: split on spaces, join with OR
    terms = [t.lstrip("#") for t in query.split() if t]
    if not terms:
        return []
    fts_query = " OR ".join(terms)
    try:
        cursor = await db.execute(
            "SELECT s.* FROM conversation_summaries s "
            "JOIN l5_fts f ON s.id = f.id "
            "WHERE f.l5_fts MATCH ? AND f.agent_id = ? "
            "ORDER BY rank LIMIT ?",
            (fts_query, agent_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


async def l5_list(agent_id: str, limit: int = 30) -> list[dict]:
    """Return recent L5 summaries for an agent, newest first."""
    db = await get_db()
    await _ensure_l5_table(db)
    cursor = await db.execute(
        "SELECT * FROM conversation_summaries WHERE agent_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (agent_id, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def l5_cleanup(agent_id: str, days: int = 60) -> int:
    """Delete L5 summaries older than `days`. Returns count deleted."""
    db = await get_db()
    await _ensure_l5_table(db)
    cur = await db.execute(
        "DELETE FROM conversation_summaries "
        "WHERE agent_id = ? AND created_at < datetime('now', ?)",
        (agent_id, f"-{days} days"),
    )
    await db.commit()
    return cur.rowcount


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None

# -- Backup -------------------------------------------------------------------

async def backup_db(dest_path: str) -> str:
    """Create a hot backup of the SQLite database using VACUUM INTO.

    VACUUM INTO is safe to run while the DB is in use (WAL mode).
    Returns the destination path on success.
    """
    db = await get_db()
    await db.execute("VACUUM INTO ?", (dest_path,))
    return dest_path

