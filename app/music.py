"""背景音乐库：内置原创曲目（music/library.json）+ 用户上传（music/user/）。"""
import json
import os
import re
import shutil
import time

import media

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "music")
SFX = os.path.join(ROOT, "sfx")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg")


def builtin():
    try:
        with open(os.path.join(DIR, "library.json"), encoding="utf-8") as f:
            lib = json.load(f)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in lib:
        if os.path.exists(os.path.join(DIR, m["file"])):
            out.append(dict(m, kind="builtin", path=os.path.join(DIR, m["file"])))
    return out


def user():
    import ctx
    USER = ctx.music_user_dir()
    out = []
    for f in sorted(os.listdir(USER), key=lambda x: -os.path.getmtime(os.path.join(USER, x))):
        if f.lower().endswith(AUDIO_EXT):
            out.append({"id": "user:" + f, "name": os.path.splitext(f)[0], "kind": "user", "start": 0,
                        "desc": "我上传的音乐", "path": os.path.join(USER, f)})
    return out


def all_music():
    return builtin() + user()


def get(mid):
    for m in all_music():
        if m["id"] == mid:
            return m
    return None


def resolve(choice, tpl):
    """choice: 'auto'（按模板推荐）/ 'none' / 曲目 id。返回曲目或 None。"""
    if not choice or choice == "none":
        return None
    if choice == "auto":
        choice = tpl.get("music", "huoli")
    m = get(choice)
    if m is None and builtin():
        m = builtin()[0]
    return m


def save_upload(filename, fileobj, max_mb=50):
    import ctx
    USER = ctx.music_user_dir()
    base, ext = os.path.splitext(os.path.basename(filename or "music"))
    ext = ext.lower()
    if ext not in AUDIO_EXT:
        raise ValueError("请上传 mp3 / m4a / wav / aac / flac 格式的音乐")
    base = re.sub(r'[\\/:*?"<>|]+', "_", base).strip()[:40] or "音乐"
    path = os.path.join(USER, base + ext)
    if os.path.exists(path):
        path = os.path.join(USER, f"{base}_{time.strftime('%H%M%S')}{ext}")
    with open(path, "wb") as f:
        shutil.copyfileobj(fileobj, f)
    if os.path.getsize(path) > max_mb * 1024 * 1024:
        os.remove(path)
        raise ValueError(f"音乐文件太大（上限 {max_mb}MB）")
    r = media.run(["-i", path])
    if " Audio:" not in r.stderr.decode("utf-8", "ignore"):
        os.remove(path)
        raise ValueError("没有读到音频，请确认是音乐文件")
    return "user:" + os.path.basename(path)


def delete(mid):
    m = get(mid)
    if m and m["kind"] == "user":
        os.remove(m["path"])


def public(m):
    return {k: m.get(k) for k in ("id", "name", "desc", "kind", "bpm", "duration")}
