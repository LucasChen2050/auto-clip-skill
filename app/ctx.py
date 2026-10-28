"""运行模式与数据目录。

本地版：所有数据放在工具文件夹里（workspace / output / prefs.json …），和以前一样。
云端版（环境变量 KOUBO_CLOUD=1）：每个账号一个独立目录 data/users/<uid>/，互不可见。"""
import contextvars
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLOUD = os.environ.get("KOUBO_CLOUD", "") == "1"
DATA = os.environ.get("KOUBO_DATA") or os.path.join(ROOT, "data")

current = contextvars.ContextVar("uid", default=None)


class NoUser(Exception):
    pass


def _mk(p):
    os.makedirs(p, exist_ok=True)
    return p


def base(uid=None):
    if not CLOUD:
        return ROOT
    uid = uid or current.get()
    if not uid:
        raise NoUser("请先登录")
    return _mk(os.path.join(DATA, "users", uid))


def ws(uid=None):
    return _mk(os.path.join(base(uid), "workspace"))


def out(uid=None):
    return _mk(os.path.join(base(uid), "output"))


def prefs_path(uid=None):
    return os.path.join(base(uid), "prefs.json")


def corr_path(uid=None):
    return os.path.join(base(uid), "corrections.txt")


def music_user_dir(uid=None):
    if not CLOUD:
        return _mk(os.path.join(ROOT, "music", "user"))
    return _mk(os.path.join(base(uid), "music"))


def all_users():
    """后台队列要扫描的所有账号（本地版只有一个）。"""
    if not CLOUD:
        return [None]
    d = os.path.join(DATA, "users")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default
