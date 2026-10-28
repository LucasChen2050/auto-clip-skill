"""云端版账号：邀请码注册、登录、会话 Cookie。只在 KOUBO_CLOUD=1 时启用。

账号保存在 data/users.json，密码用 PBKDF2-SHA256 加盐哈希；会话是带签名的 Cookie（30 天有效）。"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid

import ctx

COOKIE = "koubo_session"
TTL = 30 * 24 * 3600
_lock = threading.Lock()


def _users_file():
    os.makedirs(ctx.DATA, exist_ok=True)
    return os.path.join(ctx.DATA, "users.json")


def _secret():
    s = os.environ.get("KOUBO_SECRET", "")
    if s:
        return s.encode()
    p = os.path.join(ctx.DATA, "secret.key")
    os.makedirs(ctx.DATA, exist_ok=True)
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
    with open(p) as f:
        return f.read().strip().encode()


def load_users():
    try:
        with open(_users_file(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_users(u):
    _cache["t"] = 0
    tmp = _users_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(u, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _users_file())


def _hash(pw, salt):
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)).decode()


def _norm(name):
    return (name or "").strip()


def invite_required():
    return bool(os.environ.get("KOUBO_INVITE", ""))


def register(username, password, invite="", display=""):
    username = _norm(username)
    if not re.fullmatch(r"[\w一-鿿.@\-]{2,32}", username):
        raise ValueError("账号用 2–32 个字：中文、字母、数字都可以（例如手机号或姓名）")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    code = os.environ.get("KOUBO_INVITE", "")
    if code and not hmac.compare_digest((invite or "").strip(), code):
        raise ValueError("邀请码不对，请向活动负责人确认")
    if os.environ.get("KOUBO_REGISTER", "1") == "0":
        raise ValueError("暂不开放注册，请联系管理员开通账号")
    with _lock:
        users = load_users()
        if username.lower() in {k.lower() for k in users}:
            raise ValueError("这个账号已被注册，换一个，或直接登录")
        salt = secrets.token_hex(8)
        users[username] = {"uid": uuid.uuid4().hex[:12], "salt": salt, "pw": _hash(password, salt),
                           "name": (display or username).strip()[:20], "created": time.time(), "disabled": False}
        _save_users(users)
        return users[username]


def login(username, password):
    users = load_users()
    username = _norm(username)
    u = next((v for k, v in users.items() if k.lower() == username.lower()), None)
    if not u or not hmac.compare_digest(u["pw"], _hash(password or "", u["salt"])):
        raise ValueError("账号或密码不对")
    if u.get("disabled"):
        raise ValueError("这个账号已被停用，请联系管理员")
    return u


def set_password(username, password):
    with _lock:
        users = load_users()
        u = users[username]
        u["salt"] = secrets.token_hex(8)
        u["pw"] = _hash(password, u["salt"])
        _save_users(users)


def set_disabled(username, flag):
    with _lock:
        users = load_users()
        users[username]["disabled"] = bool(flag)
        _save_users(users)


def make_token(uid):
    exp = int(time.time()) + TTL
    msg = f"{uid}.{exp}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{msg}.{sig}"


def verify(token):
    """返回 uid；无效、过期或账号被停用返回 None。"""
    if not token or token.count(".") != 2:
        return None
    uid, exp, sig = token.split(".")
    good = hmac.new(_secret(), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return None
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    if not re.fullmatch(r"[0-9a-f]{12}", uid):
        return None
    u = user_by_uid(uid)
    if not u or u.get("disabled"):
        return None
    return uid


_cache = {"t": 0, "users": {}}


def user_by_uid(uid):
    if time.time() - _cache["t"] > 5:
        _cache["users"] = {v["uid"]: dict(v, username=k) for k, v in load_users().items()}
        _cache["t"] = time.time()
    return _cache["users"].get(uid)
