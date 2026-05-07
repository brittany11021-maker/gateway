"""
auth.py — VPS edition
Loads pre-baked cookies only. QR login removed (not interactive on server).
"""
import os
import json
from pyncm import apis
from pyncm import GetCurrentSession

STORAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")
COOKIE_FILE = os.path.join(STORAGE_DIR, "cookies.json")


def ensure_storage_dir():
    os.makedirs(STORAGE_DIR, exist_ok=True)


def load_session():
    """Load cookies from file and verify login status."""
    ensure_storage_dir()
    if not os.path.exists(COOKIE_FILE):
        return False, None
    try:
        with open(COOKIE_FILE) as f:
            cookies = json.load(f)
        GetCurrentSession().cookies.update(cookies)
        user_info = apis.login.GetCurrentLoginStatus()
        if user_info.get("code") == 200 and user_info.get("profile"):
            return True, user_info["profile"]["nickname"]
        return False, None
    except Exception:
        return False, None


def check_login_status():
    is_logged_in, nickname = load_session()
    return {"logged_in": is_logged_in, "nickname": nickname}
