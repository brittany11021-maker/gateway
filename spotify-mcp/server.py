#!/usr/bin/env python3
"""
spotify-mcp  — FastMCP Streamable-HTTP server for Spotify Web API
Requires env vars: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
Listens on 0.0.0.0:3002
"""
import os
import json
import logging
import httpx
import asyncio
from datetime import datetime, timedelta

logging.getLogger("fastmcp").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)

from fastmcp import FastMCP

mcp = FastMCP("Spotify-MCP")

CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")

_access_token: str = ""
_token_expiry: datetime = datetime.utcnow()


async def _get_token() -> str:
    """Auto-refresh Spotify access token via client_credentials or refresh_token."""
    global _access_token, _token_expiry
    if _access_token and datetime.utcnow() < _token_expiry - timedelta(seconds=60):
        return _access_token
    if not CLIENT_ID or not CLIENT_SECRET:
        return ""
    async with httpx.AsyncClient(timeout=10) as cl:
        if REFRESH_TOKEN:
            r = await cl.post(
                "https://accounts.spotify.com/api/token",
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": REFRESH_TOKEN,
                    "client_id":     CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
            )
        else:
            import base64
            creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            r = await cl.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
            )
    if r.status_code == 200:
        d = r.json()
        _access_token = d["access_token"]
        _token_expiry = datetime.utcnow() + timedelta(seconds=d.get("expires_in", 3600))
        return _access_token
    return ""


async def _api(path: str, params: dict = None) -> dict:
    token = await _get_token()
    if not token:
        return {"error": "Spotify not configured (need SPOTIFY_CLIENT_ID/SECRET/REFRESH_TOKEN)"}
    async with httpx.AsyncClient(timeout=10) as cl:
        r = await cl.get(
            f"https://api.spotify.com/v1{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
    if r.status_code == 200:
        return r.json()
    return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}


@mcp.tool()
async def spotify_status() -> str:
    """检查 Spotify 连接状态及当前播放"""
    if not CLIENT_ID:
        return "未配置：需要设置 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / SPOTIFY_REFRESH_TOKEN 环境变量"
    token = await _get_token()
    if not token:
        return "获取 Token 失败，请检查凭据"
    # Try currently playing
    d = await _api("/me/player/currently-playing")
    if d.get("error"):
        return f"已连接（token OK）。当前播放信息: {d.get('error')}"
    if not d:
        return "已连接，当前没有正在播放的曲目"
    item = d.get("item") or {}
    name    = item.get("name", "?")
    artists = ", ".join(a["name"] for a in item.get("artists", []))
    return f"🎵 正在播放: {name} — {artists}"


@mcp.tool()
async def spotify_search(query: str, type: str = "track", limit: int = 5) -> str:
    """搜索 Spotify 曲目、歌手或专辑
    args:
        query: 搜索关键词
        type:  track / artist / album (默认 track)
        limit: 返回数量（默认 5）
    """
    d = await _api("/search", {"q": query, "type": type, "limit": min(limit, 10), "market": "CN"})
    if d.get("error"):
        return f"搜索失败: {d['error']}"
    results = []
    if type == "track":
        for t in (d.get("tracks") or {}).get("items", []):
            artists = ", ".join(a["name"] for a in t.get("artists", []))
            results.append(f"• {t['name']} — {artists}  (ID: {t['id']})")
    elif type == "artist":
        for a in (d.get("artists") or {}).get("items", []):
            results.append(f"• {a['name']}  粉丝: {a.get('followers',{}).get('total',0):,}  (ID: {a['id']})")
    elif type == "album":
        for a in (d.get("albums") or {}).get("items", []):
            artists = ", ".join(x["name"] for x in a.get("artists", []))
            results.append(f"• {a['name']} — {artists}  (ID: {a['id']})")
    return "\n".join(results) if results else "无结果"


@mcp.tool()
async def spotify_get_recommendations(seed_genres: str = "", seed_artists: str = "",
                                       limit: int = 10, mood: str = "") -> str:
    """基于风格/心情获取 Spotify 推荐曲目
    args:
        seed_genres:  逗号分隔的风格 (如 'pop,chill,indie')
        seed_artists: 逗号分隔的歌手 ID
        limit:        返回数量（默认 10）
        mood:         心情关键词 (happy/sad/energetic/calm) 自动映射音乐参数
    """
    params: dict = {"limit": min(limit, 20), "market": "CN"}
    if seed_genres:
        params["seed_genres"] = seed_genres[:5]  # Spotify max 5 seeds
    if seed_artists:
        params["seed_artists"] = ",".join(seed_artists.split(",")[:5])
    # No seeds → use defaults
    if not params.get("seed_genres") and not params.get("seed_artists"):
        params["seed_genres"] = "pop,chill"
    # Mood → audio features
    _MOOD_MAP = {
        "happy":     {"min_valence": 0.6, "min_energy": 0.5},
        "sad":       {"max_valence": 0.4, "max_energy": 0.5},
        "energetic": {"min_energy": 0.7, "min_tempo": 120},
        "calm":      {"max_energy": 0.4, "max_tempo": 100},
        "angry":     {"min_energy": 0.8, "max_valence": 0.4},
        "romantic":  {"min_valence": 0.5, "max_energy": 0.6},
    }
    for kw, feats in _MOOD_MAP.items():
        if kw in mood.lower():
            params.update(feats)
            break
    d = await _api("/recommendations", params)
    if d.get("error"):
        return f"推荐失败: {d['error']}"
    tracks = d.get("tracks", [])
    lines = []
    for t in tracks:
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        lines.append(f"• {t['name']} — {artists}  (ID: {t['id']})")
    return "\n".join(lines) if lines else "无推荐结果"


@mcp.tool()
async def spotify_recently_played(limit: int = 5) -> str:
    """获取最近播放的曲目（需要用户授权 token）"""
    d = await _api("/me/player/recently-played", {"limit": min(limit, 10)})
    if d.get("error"):
        return f"获取失败: {d['error']}"
    items = d.get("items", [])
    lines = []
    for item in items:
        t = item.get("track") or {}
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        played_at = item.get("played_at", "")[:16].replace("T", " ")
        lines.append(f"• {played_at}  {t.get('name','?')} — {artists}")
    return "\n".join(lines) if lines else "暂无记录"


@mcp.tool()
async def spotify_get_playlist_tracks(playlist_id: str, limit: int = 20) -> str:
    """获取指定 Spotify 歌单的曲目列表"""
    d = await _api(f"/playlists/{playlist_id}/tracks", {"limit": min(limit, 50), "market": "CN"})
    if d.get("error"):
        return f"获取失败: {d['error']}"
    items = d.get("items", [])
    lines = []
    for i, item in enumerate(items, 1):
        t = (item.get("track") or {})
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        lines.append(f"{i}. {t.get('name','?')} — {artists}  (ID: {t.get('id','')})")
    return "\n".join(lines) if lines else "空歌单"


@mcp.tool()
async def spotify_my_playlists(limit: int = 20) -> str:
    """获取我的 Spotify 歌单列表"""
    d = await _api("/me/playlists", {"limit": min(limit, 50)})
    if d.get("error"):
        return f"获取失败: {d['error']}"
    items = d.get("items", [])
    lines = []
    for pl in items:
        lines.append(f"• {pl.get('name','?')}  ({pl.get('tracks',{}).get('total',0)} 首)  ID: {pl.get('id','')}")
    return "\n".join(lines) if lines else "无歌单"


def main():
    port = int(os.environ.get("SPOTIFY_MCP_PORT", "3002"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
