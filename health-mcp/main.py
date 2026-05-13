"""
Health MCP — iPhone Health data gateway (执行文档 §15/§16)

iPhone Shortcuts push data via GET /push  (simple, works with Shortcuts URL action):
  GET /push?metric=heart_rate&value=72&unit=bpm&key=xxx
  GET /push?metric=steps&value=3500&key=xxx
  GET /push?metric=sleep_hours&value=6.5&key=xxx
  GET /push?metric=menstrual_start&value=1&key=xxx   (1 = period started today)

Batch push via POST:
  POST /push  {"metrics": [{"metric":"heart_rate","value":72,"unit":"bpm"}, ...]}

Gateway polls:
  GET /metrics/heart_rate/latest   → {"value":72,"unit":"bpm","sampled_at":"..."}
  GET /metrics/steps/today         → {"value":3500,"date":"2026-05-07"}
  GET /metrics/sleep/last_night    → {"value":6.5,"unit":"hours","date":"...","quality":"ok"}
  GET /metrics/menstrual/current   → {"phase":"period","day":2,"next_period_days":26,"cycle_length":28}
  GET /metrics/summary             → all latest values in one call
  GET /history?metric=heart_rate&hours=24&limit=100  → list of readings

All endpoints (except /health) require:  ?key=HEALTH_API_KEY  or  Authorization: Bearer KEY
"""

import os, json, asyncio, datetime
import aiosqlite
from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.responses import JSONResponse

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY  = os.getenv("HEALTH_API_KEY", "")
DB_PATH  = os.getenv("DB_PATH", "/app/data/health.db")

app = FastAPI(title="Health MCP", version="1.0.0")

# ── DB init ───────────────────────────────────────────────────────────────────
_db: aiosqlite.Connection | None = None

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        import pathlib
        pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _init_schema(_db)
    return _db

async def _init_schema(db: aiosqlite.Connection):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            metric      TEXT    NOT NULL,
            value       REAL    NOT NULL,
            unit        TEXT    DEFAULT '',
            note        TEXT    DEFAULT '',
            sampled_at  TEXT    DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_metric_time ON metrics(metric, sampled_at DESC);

        CREATE TABLE IF NOT EXISTS menstrual_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event       TEXT    NOT NULL,   -- 'period_start' | 'period_end' | 'cycle_length_update'
            value       REAL    DEFAULT 0,
            note        TEXT    DEFAULT '',
            logged_at   TEXT    DEFAULT (datetime('now'))
        );
    """)
    await db.commit()


@app.on_event("startup")
async def startup():
    await get_db()


@app.on_event("shutdown")
async def shutdown():
    global _db
    if _db:
        await _db.close()
        _db = None


# ── Auth ──────────────────────────────────────────────────────────────────────
def _check_key(key: str = "", request: Request = None) -> bool:
    if not API_KEY:
        return True
    if key == API_KEY:
        return True
    if request:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == API_KEY:
            return True
    return False


def _require_key(key: str = Query(default=""), request: Request = None):
    if not _check_key(key, request):
        raise HTTPException(401, "Invalid API key")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _today() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def _yesterday() -> str:
    return (datetime.datetime.utcnow() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")


async def _insert_metric(metric: str, value: float, unit: str = "", note: str = ""):
    db = await get_db()
    await db.execute(
        "INSERT INTO metrics (metric, value, unit, note) VALUES (?,?,?,?)",
        (metric, value, unit, note),
    )
    await db.commit()


async def _latest(metric: str) -> dict | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM metrics WHERE metric=? ORDER BY sampled_at DESC LIMIT 1",
        (metric,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def _today_agg(metric: str, agg: str = "MAX") -> float | None:
    """Return MAX or SUM of a metric for today (UTC date)."""
    db = await get_db()
    cur = await db.execute(
        f"SELECT {agg}(value) as v FROM metrics "
        "WHERE metric=? AND date(sampled_at)=date('now')",
        (metric,),
    )
    row = await cur.fetchone()
    v = row["v"] if row else None
    return float(v) if v is not None else None


# ── Push endpoints ────────────────────────────────────────────────────────────

@app.get("/push")
async def push_get(
    metric: str = Query(..., description="Metric name"),
    value:  str = Query(..., description="Numeric value"),
    unit:   str = Query(default=""),
    note:   str = Query(default=""),
    key:    str = Query(default=""),
    request: Request = None,
):
    """iPhone Shortcuts push: GET /push?metric=heart_rate&value=72&unit=bpm&key=xxx"""
    _require_key(key, request)
    try:
        fval = float(value)
    except ValueError:
        raise HTTPException(400, f"value must be numeric, got: {value!r}")

    # Special handling for menstrual events
    if metric in ("menstrual_start", "period_start"):
        db = await get_db()
        await db.execute(
            "INSERT INTO menstrual_log (event, value, note) VALUES (?,?,?)",
            ("period_start", 1, note),
        )
        await db.commit()
        return {"ok": True, "metric": "period_start", "sampled_at": _now_iso()}

    if metric in ("menstrual_end", "period_end"):
        db = await get_db()
        await db.execute(
            "INSERT INTO menstrual_log (event, value, note) VALUES (?,?,?)",
            ("period_end", 1, note),
        )
        await db.commit()
        return {"ok": True, "metric": "period_end", "sampled_at": _now_iso()}

    if metric == "cycle_length":
        db = await get_db()
        await db.execute(
            "INSERT INTO menstrual_log (event, value, note) VALUES (?,?,?)",
            ("cycle_length_update", fval, note),
        )
        await db.commit()
        return {"ok": True, "metric": "cycle_length", "value": fval, "sampled_at": _now_iso()}

    await _insert_metric(metric, fval, unit, note)
    return {"ok": True, "metric": metric, "value": fval, "sampled_at": _now_iso()}


@app.post("/push")
async def push_post(
    body: dict = Body(...),
    key:  str  = Query(default=""),
    request: Request = None,
):
    """Batch push: POST /push {"metrics": [{"metric":"...","value":...,"unit":"..."}]}"""
    _require_key(key, request)
    items = body.get("metrics") or []
    saved = 0
    for item in items:
        m = item.get("metric", "")
        v = item.get("value")
        if not m or v is None:
            continue
        try:
            await _insert_metric(m, float(v), item.get("unit",""), item.get("note",""))
            saved += 1
        except Exception:
            pass
    return {"ok": True, "saved": saved, "sampled_at": _now_iso()}


# ── Query endpoints ───────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "time": _now_iso()}


@app.get("/metrics/heart_rate/latest")
async def hr_latest(key: str = Query(default=""), request: Request = None):
    _require_key(key, request)
    row = await _latest("heart_rate")
    if not row:
        return JSONResponse({"value": None}, status_code=200)
    return {"value": row["value"], "unit": row["unit"] or "bpm", "sampled_at": row["sampled_at"]}


@app.get("/metrics/steps/today")
async def steps_today(key: str = Query(default=""), request: Request = None):
    _require_key(key, request)
    # Steps: take MAX of today's pushes (cumulative total)
    val = await _today_agg("steps", "MAX")
    return {"value": val or 0, "date": _today()}


@app.get("/metrics/sleep/last_night")
async def sleep_last_night(key: str = Query(default=""), request: Request = None):
    """Return last sleep_hours entry (pushed in the morning after waking up)."""
    _require_key(key, request)
    db = await get_db()
    # Look at yesterday or today
    cur = await db.execute(
        "SELECT * FROM metrics WHERE metric='sleep_hours' "
        "AND date(sampled_at) IN (date('now'), date('now','-1 day')) "
        "ORDER BY sampled_at DESC LIMIT 1",
    )
    row = await cur.fetchone()
    if not row:
        return {"value": None, "unit": "hours", "date": _yesterday()}
    val = float(row["value"])
    quality = "good" if val >= 7 else ("ok" if val >= 5 else "poor")
    return {
        "value": val,
        "unit": "hours",
        "quality": quality,
        "date": str(row["sampled_at"])[:10],
    }


@app.get("/metrics/menstrual/current")
async def menstrual_current(key: str = Query(default=""), request: Request = None):
    """Return current menstrual phase info."""
    _require_key(key, request)
    db = await get_db()

    # Get last cycle_length
    cur = await db.execute(
        "SELECT value FROM menstrual_log WHERE event='cycle_length_update' "
        "ORDER BY logged_at DESC LIMIT 1",
    )
    row = await cur.fetchone()
    cycle_len = int(row["value"]) if row else 28

    # Get last period_start
    cur2 = await db.execute(
        "SELECT logged_at FROM menstrual_log WHERE event='period_start' "
        "ORDER BY logged_at DESC LIMIT 1",
    )
    row2 = await cur2.fetchone()
    if not row2:
        return {"phase": "unknown", "cycle_length": cycle_len, "last_period_start": None}

    last_start = datetime.datetime.fromisoformat(str(row2["logged_at"]).replace("Z",""))
    today = datetime.datetime.utcnow()
    day_in_cycle = (today - last_start).days + 1  # 1-indexed

    # Check if period has ended
    cur3 = await db.execute(
        "SELECT logged_at FROM menstrual_log WHERE event='period_end' "
        "AND logged_at > ? ORDER BY logged_at DESC LIMIT 1",
        (str(row2["logged_at"]),),
    )
    row3 = await cur3.fetchone()
    period_duration = 5  # default
    if row3:
        end_dt  = datetime.datetime.fromisoformat(str(row3["logged_at"]).replace("Z",""))
        period_duration = max(1, (end_dt - last_start).days + 1)

    # Determine phase
    if 1 <= day_in_cycle <= period_duration:
        phase = "period"
    elif day_in_cycle <= 13:
        phase = "follicular"
    elif day_in_cycle <= 16:
        phase = "ovulation"
    else:
        phase = "luteal"

    next_period_days = max(0, cycle_len - day_in_cycle + 1)
    days_since_start = day_in_cycle - 1

    return {
        "phase":            phase,
        "day_in_cycle":     day_in_cycle,
        "cycle_length":     cycle_len,
        "next_period_days": next_period_days,
        "days_since_start": days_since_start,
        "last_period_start": str(row2["logged_at"])[:10],
        "period_duration":  period_duration,
    }


@app.get("/metrics/summary")
async def summary(key: str = Query(default=""), request: Request = None):
    """Return all latest values in one call."""
    _require_key(key, request)
    hr   = await _latest("heart_rate")
    st   = await _today_agg("steps", "MAX")
    slp  = await _latest("sleep_hours")
    men  = await menstrual_current.__wrapped__(key, request) if hasattr(menstrual_current, "__wrapped__") else None
    return {
        "heart_rate":   {"value": hr["value"] if hr else None, "sampled_at": hr["sampled_at"] if hr else None},
        "steps_today":  {"value": st or 0, "date": _today()},
        "sleep_last":   {"value": slp["value"] if slp else None, "sampled_at": slp["sampled_at"] if slp else None},
        "generated_at": _now_iso(),
    }


@app.get("/history")
async def history(
    metric: str = Query(...),
    hours:  int = Query(default=24),
    limit:  int = Query(default=100),
    key:    str = Query(default=""),
    request: Request = None,
):
    """Return recent readings for a metric."""
    _require_key(key, request)
    db = await get_db()
    cur = await db.execute(
        "SELECT metric,value,unit,sampled_at FROM metrics "
        "WHERE metric=? AND sampled_at >= datetime('now',?) "
        "ORDER BY sampled_at DESC LIMIT ?",
        (metric, f"-{hours} hours", limit),
    )
    rows = await cur.fetchall()
    return {"metric": metric, "items": [dict(r) for r in rows], "total": len(rows)}
