"""
Additions to /opt/health-api/server.py — paste ABOVE the last line (the lifespan/app setup
is already there, so append these after the existing @app routes).

These endpoints are called by the gateway's _health_monitor_loop.
Auth: Bearer token OR ?key= query param (same HEALTH_API_KEY as existing service).
"""

# ── Add Query to existing fastapi import line manually ────────────────────────
# Change: from fastapi import Depends, FastAPI, HTTPException, Request, Security
# To:     from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security

# ── Optional-auth helper (query param OR Bearer) ──────────────────────────────

def _check_opt_auth(key: str = "", request: Request = None) -> bool:
    if not API_KEY:
        return True
    if key == API_KEY:
        return True
    if request:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == API_KEY:
            return True
    return False


def _require_opt_auth(key: str = "", request: Request = None):
    if not _check_opt_auth(key, request):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _today_cst() -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


# ── /metrics/heart_rate/latest ────────────────────────────────────────────────

@app.get("/metrics/heart_rate/latest")
async def metrics_hr_latest(key: str = Query(default=""), request: Request = None):
    _require_opt_auth(key, request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ts, bpm FROM heart_rate ORDER BY ts DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"value": None, "unit": "bpm", "sampled_at": None}
    return {"value": float(row["bpm"]), "unit": "bpm", "sampled_at": row["ts"]}


# ── /metrics/steps/today ──────────────────────────────────────────────────────

@app.get("/metrics/steps/today")
async def metrics_steps_today(key: str = Query(default=""), request: Request = None):
    _require_opt_auth(key, request)
    today = _today_cst()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Timestamps stored as UTC; shift +8h to get CST date
        async with db.execute(
            "SELECT SUM(count) AS total FROM steps WHERE date(ts, '+8 hours')=?",
            (today,),
        ) as cur:
            row = await cur.fetchone()
    total = float(row["total"]) if row and row["total"] is not None else 0.0
    return {"value": total, "date": today}


# ── /metrics/sleep/last_night ─────────────────────────────────────────────────

@app.get("/metrics/sleep/last_night")
async def metrics_sleep_last_night(key: str = Query(default=""), request: Request = None):
    _require_opt_auth(key, request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT date, asleep_hours, in_bed_hours FROM sleep_analysis "
            "ORDER BY date DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"value": None, "unit": "hours", "date": None}
    # Prefer asleep_hours; fall back to in_bed_hours when asleep is 0/null
    hrs = float(row["asleep_hours"] or 0)
    if hrs < 0.5:
        hrs = float(row["in_bed_hours"] or 0)
    quality = "good" if hrs >= 7 else ("ok" if hrs >= 5 else "poor")
    return {"value": round(hrs, 2), "unit": "hours", "quality": quality, "date": row["date"]}


# ── /metrics/menstrual/current ────────────────────────────────────────────────

@app.get("/metrics/menstrual/current")
async def metrics_menstrual_current(key: str = Query(default=""), request: Request = None):
    _require_opt_auth(key, request)
    from datetime import date as _dt_date, timedelta as _dt_td

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT date FROM menstrual_cycle "
            "WHERE flow IS NOT NULL AND LOWER(TRIM(flow)) NOT IN ('none','') "
            "ORDER BY date ASC"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        return {"phase": "unknown", "cycle_length": 28, "last_period_start": None}

    all_dates = [r["date"] for r in rows]

    # Find most recent contiguous streak (current or last period)
    streak = [all_dates[-1]]
    for d in reversed(all_dates[:-1]):
        expected_prev = (_dt_date.fromisoformat(streak[0]) - _dt_td(days=1)).isoformat()
        if d == expected_prev:
            streak.insert(0, d)
        else:
            break
    last_period_start = streak[0]
    period_duration = len(streak)

    # Estimate cycle length from gap between period starts (if >1 streak available)
    cycle_len = 28
    pre = [d for d in all_dates if d < streak[0]]
    if pre:
        prev_streak = [pre[-1]]
        for d in reversed(pre[:-1]):
            expected_prev = (_dt_date.fromisoformat(prev_streak[0]) - _dt_td(days=1)).isoformat()
            if d == expected_prev:
                prev_streak.insert(0, d)
            else:
                break
        gap = (_dt_date.fromisoformat(last_period_start) - _dt_date.fromisoformat(prev_streak[0])).days
        if 21 <= gap <= 40:
            cycle_len = gap

    today = datetime.now(timezone.utc).date()
    start_date = _dt_date.fromisoformat(last_period_start)
    day_in_cycle = (today - start_date).days + 1

    if 1 <= day_in_cycle <= period_duration:
        phase = "period"
    elif day_in_cycle <= 13:
        phase = "follicular"
    elif day_in_cycle <= 16:
        phase = "ovulation"
    else:
        phase = "luteal"

    return {
        "phase": phase,
        "day_in_cycle": day_in_cycle,
        "cycle_length": cycle_len,
        "next_period_days": max(0, cycle_len - day_in_cycle + 1),
        "days_since_start": day_in_cycle - 1,
        "last_period_start": last_period_start,
        "period_duration": period_duration,
    }


# ── /metrics/summary ──────────────────────────────────────────────────────────

@app.get("/metrics/summary")
async def metrics_summary(key: str = Query(default=""), request: Request = None):
    _require_opt_auth(key, request)
    hr  = await metrics_hr_latest(key, request)
    st  = await metrics_steps_today(key, request)
    slp = await metrics_sleep_last_night(key, request)
    return {
        "heart_rate":  {"value": hr.get("value"),  "sampled_at": hr.get("sampled_at")},
        "steps_today": {"value": st.get("value"),  "date": st.get("date")},
        "sleep_last":  {"value": slp.get("value"), "date": slp.get("date")},
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── /history ──────────────────────────────────────────────────────────────────

@app.get("/history")
async def metrics_history(
    metric: str = Query(...),
    hours:  int  = Query(default=24),
    limit:  int  = Query(default=100),
    key:    str  = Query(default=""),
    request: Request = None,
):
    _require_opt_auth(key, request)
    TABLE_MAP = {
        "heart_rate": ("heart_rate", "ts", "bpm"),
        "steps":      ("steps",      "ts", "count"),
    }
    if metric not in TABLE_MAP:
        return {"metric": metric, "items": [], "total": 0}
    table, ts_col, val_col = TABLE_MAP[metric]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {ts_col} AS sampled_at, {val_col} AS value FROM {table} "
            f"WHERE {ts_col} >= datetime('now', ?) ORDER BY {ts_col} DESC LIMIT ?",
            (f"-{hours} hours", limit),
        ) as cur:
            rows = await cur.fetchall()
    return {"metric": metric, "items": [dict(r) for r in rows], "total": len(rows)}


# ── /push  (iPhone Shortcuts simple push) ─────────────────────────────────────

@app.get("/push")
async def push_get(
    metric:  str = Query(...),
    value:   str = Query(...),
    unit:    str = Query(default=""),
    note:    str = Query(default=""),
    key:     str = Query(default=""),
    request: Request = None,
):
    """GET /push?metric=heart_rate&value=72&key=xxx — iPhone Shortcuts friendly."""
    _require_opt_auth(key, request)
    try:
        fval = float(value)
    except ValueError:
        raise HTTPException(400, f"value must be numeric, got {value!r}")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    today   = _today_cst()

    async with aiosqlite.connect(DB_PATH) as db:
        if metric == "heart_rate":
            await db.execute(
                "INSERT INTO heart_rate (ts, bpm, bpm_avg, source) VALUES (?,?,?,?)",
                (now_utc, fval, fval, "shortcuts"),
            )
        elif metric in ("steps", "step_count"):
            await db.execute(
                "INSERT INTO steps (ts, count, source) VALUES (?,?,?)",
                (now_utc, fval, "shortcuts"),
            )
        elif metric in ("sleep_hours", "sleep"):
            await db.execute(
                "INSERT OR REPLACE INTO sleep_analysis "
                "(date, asleep_hours, in_bed_hours, source) VALUES (?,?,?,?)",
                (today, fval, fval, "shortcuts"),
            )
        elif metric in ("menstrual_start", "period_start"):
            await db.execute(
                "INSERT OR IGNORE INTO menstrual_cycle (date, flow, source) VALUES (?,?,?)",
                (today, "unspecified", "shortcuts"),
            )
        elif metric == "active_energy":
            await db.execute(
                "INSERT INTO active_energy (ts, kcal, source) VALUES (?,?,?)",
                (now_utc, fval, "shortcuts"),
            )
        # menstrual_end / cycle_length / unknown → accepted silently
        await db.commit()

    return {"ok": True, "metric": metric, "value": fval, "sampled_at": now_utc}
