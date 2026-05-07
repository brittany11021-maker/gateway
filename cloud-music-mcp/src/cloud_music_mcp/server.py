#!/usr/bin/env python3
"""
cloud-music-mcp  VPS edition
FastMCP Streamable-HTTP server wrapping pyncm (NetEase Cloud Music API)
Listens on 0.0.0.0:3001 by default.
"""
import os
import sys
import json
import base64
import logging

# suppress noisy third-party loggers before imports
logging.getLogger("fastmcp").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("pyncm").setLevel(logging.WARNING)

from fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cloud_music_mcp.auth import check_login_status, load_session
from cloud_music_mcp.api import (
    get_daily_recommendations,
    get_user_playlists,
    search_song,
    get_playlist_tracks,
)

mcp = FastMCP("Cloud-Music-MCP")


@mcp.tool()
def cloud_music_status() -> str:
    """检查网易云音乐当前是否已登录"""
    status = check_login_status()
    if status["logged_in"]:
        return f"已登录，当前用户: {status['nickname']}"
    return "未登录"


@mcp.tool()
def cloud_music_get_daily_recommend() -> str:
    """获取今日推荐歌曲\n返回歌曲列表 (包含 ID, 歌名, 歌手)"""
    result = get_daily_recommendations()
    if result["success"]:
        text = f"📅 今日推荐 ({len(result['songs'])}首):\n"
        for i, song in enumerate(result["songs"][:15], 1):
            text += f"{i}. {song['name']} - {song['artist']} (ID: {song['id']})\n"
        return text
    return f"获取失败: {result.get('error')}"


@mcp.tool()
def cloud_music_my_playlists() -> str:
    """获取我的歌单 (包括创建的歌单和红心歌单)"""
    result = get_user_playlists()
    if result["success"]:
        text = "我的歌单:\n"
        for pl in result["playlists"]:
            mark = "❤️ " if "喜欢" in pl["name"] else ("👤 " if pl["is_mine"] else "收藏 ")
            text += f"{mark}{pl['name']} (ID: {pl['id']}, {pl['count']}首)\n"
        return text
    return f"获取失败: {result.get('error')}"


@mcp.tool()
def cloud_music_search(keyword: str) -> str:
    """搜索歌曲\nargs:\n    keyword: 歌名或歌手"""
    result = search_song(keyword)
    if result["success"]:
        songs = result["songs"]
        lines = [f"{i+1}. {s['name']} - {s.get('artist','?')} (ID: {s['id']})"
                 for i, s in enumerate(songs)]
        return "\n".join(lines)
    return f"搜索失败: {result.get('error')}"


@mcp.tool()
def cloud_music_get_playlist_tracks(playlist_id: str, limit: int = 50) -> str:
    """获取指定歌单的曲目列表，参数为歌单ID"""
    result = get_playlist_tracks(playlist_id, limit)
    if result["success"]:
        songs = result["songs"]
        text = f"歌单 {playlist_id} 共 {len(songs)} 首:\n"
        for i, s in enumerate(songs, 1):
            text += f"{i}. {s['name']} - {s['artists']} (ID: {s['id']})\n"
        return text
    return f"获取失败: {result.get('error')}"


@mcp.tool()
def cloud_music_play(id: str, type: str = "song") -> str:
    """唤起客户端播放指定歌曲或歌单\nargs:\n    id: 歌曲/歌单ID\n    type: 'song' 或 'playlist'"""
    command = {"type": type, "id": str(id), "cmd": "play"}
    json_str = json.dumps(command, separators=(",", ":"))
    encoded = base64.b64encode(json_str.encode()).decode()
    web_type = "song" if type == "song" else "playlist"
    web_url = f"https://music.163.com/#/{web_type}?id={id}"
    app_url = f"orpheus://{encoded}"
    return f"播放链接: {app_url}\n网页版: {web_url}"


def main():
    port = int(os.environ.get("CLOUD_MUSIC_PORT", "3001"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
