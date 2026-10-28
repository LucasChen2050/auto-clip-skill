"""口播工作台 本地服务。浏览器打开 http://127.0.0.1:端口 使用。

剪辑采用自动流水线：防抖 → 识别 → 智能整理 → 提炼标题 → 封面 → 导出。
每一步的结果都单独保存，某一步失败只需从那一步重试；批量导入的视频在后台排队逐条处理。"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import cleanup  # noqa: E402
import downloader  # noqa: E402
import engine  # noqa: E402
import media  # noqa: E402
import previews  # noqa: E402
import render  # noqa: E402
import titles  # noqa: E402
import effects  # noqa: E402
import music  # noqa: E402
import auth  # noqa: E402
import ctx  # noqa: E402

ROOT = engine.ROOT
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CLOUD = ctx.CLOUD
LIMITS = {  # 云端版的使用限制（环境变量可调）
    "upload_mb": ctx.env_int("KOUBO_MAX_UPLOAD_MB", 2048),
    "max_minutes": ctx.env_int("KOUBO_MAX_MINUTES", 10),
    "max_queue": ctx.env_int("KOUBO_MAX_QUEUE", 20),
    "keep_days": ctx.env_int("KOUBO_KEEP_DAYS", 7),
    "workers": ctx.env_int("KOUBO_WORKERS", 2 if ctx.CLOUD else 1),
}

def _edition():
    """版本信息：品牌名称和功能开关，可在根目录的 edition.json 里修改。"""
    e = {"id": "lingjun", "brand": "全自动化剪辑 Skill", "short": "全自动化剪辑", "by": "@灵鋆智能", "logo": "剪",
         "modules": False, "music": False}
    try:
        with open(os.path.join(ROOT, "edition.json"), encoding="utf-8") as f:
            e.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as ex:  # noqa: BLE001
        print("edition.json 读取失败：", ex)
    if not os.path.isdir(os.path.join(STATIC, "modules")):
        e["modules"] = False
    return e


EDITION = _edition()
app = FastAPI(title=EDITION["brand"])


class AuthMiddleware:
    """云端版：从 Cookie 识别账号，设置当前用户；未登录访问接口返回 401。本地版不做任何限制。"""
    OPEN = ("/api/auth/", "/api/templates/")

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not CLOUD:
            return await self.app(scope, receive, send)
        uid = None
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    if part.strip().startswith(auth.COOKIE + "="):
                        uid = auth.verify(part.strip()[len(auth.COOKIE) + 1:])
        path = scope.get("path", "")
        if path.startswith("/api/") and not path.startswith(self.OPEN) and not uid:
            resp = JSONResponse({"detail": "请先登录", "login": True}, status_code=401)
            return await resp(scope, receive, send)
        tok = ctx.current.set(uid)
        try:
            return await self.app(scope, receive, send)
        finally:
            ctx.current.reset(tok)


app.add_middleware(AuthMiddleware)
if EDITION["modules"]:
    app.mount("/modules", StaticFiles(directory=os.path.join(STATIC, "modules"), html=True), name="modules")
_locks = {}

STEPS = ["stab", "asr", "clean", "title", "cover", "export"]
STEP_NAMES = {"stab": "防抖", "asr": "语音识别", "clean": "智能整理", "title": "提炼标题", "cover": "封面", "export": "导出"}
DEFAULT_SETTINGS = {"template": "template1", "pace": "normal", "remove_fillers": True, "use_ai": True,
                    "aspect": "original", "loudnorm": True, "stabilize": "auto", "enhance": True, "denoise": True,
                    "ip_name": "", "top_title": True,
                    "text_fx": True, "bigwords": True, "sfx": False, "bgm": "auto" if EDITION["music"] else "none",
                    "bgm_volume": 40, "speaker_card": True, "speaker_name": "", "speaker_titles": ""}


def prefs():
    try:
        with open(ctx.prefs_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_prefs(**kw):
    p = prefs()
    p.update(kw)
    with open(ctx.prefs_path(), "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)


def global_ai():
    """云端版：管理员可用环境变量给所有人配置一个默认的 AI 服务。"""
    if not CLOUD or not os.environ.get("KOUBO_AI_KEY"):
        return None
    return {"enabled": True, "preset": "admin", "base_url": os.environ.get("KOUBO_AI_URL", "https://api.deepseek.com/v1"),
            "model": os.environ.get("KOUBO_AI_MODEL", "deepseek-chat"), "api_key": os.environ["KOUBO_AI_KEY"]}


def ai_cfg():
    c = prefs().get("ai")
    if ai.ready(c):
        return c
    g = global_ai()
    if g and not (c and c.get("enabled") is False and c.get("api_key")):
        return g
    return c


SETTINGS_REV = 2  # 默认值有大调整时加 1：老用户保存的设置里这几项会换成新的默认值


def batch_settings():
    s = dict(DEFAULT_SETTINGS)
    b = prefs().get("batch") or {}
    if b and b.get("rev", 0) < SETTINGS_REV:
        b.update(stabilize="auto", sfx=False, rev=SETTINGS_REV)  # v2：防抖改为自动判断、音效默认关闭
        save_prefs(batch=b)
    s.update(b)
    s["ip_name"] = prefs().get("ip_name", s.get("ip_name", ""))
    if not EDITION["music"]:
        s["bgm"] = "none"
    return s


threading.Thread(target=previews.ensure_all, daemon=True).start()  # 新加的模板自动生成预览图


# ---------------------------------------------------------------- 项目读写
def pdir(pid):
    if not re.fullmatch(r"[0-9a-f]{10}", pid):
        raise HTTPException(404, "项目不存在")
    d = os.path.join(ctx.ws(), pid)
    if not os.path.isdir(d):
        raise HTTPException(404, "项目不存在")
    return d


def load(pid):
    with open(os.path.join(pdir(pid), "project.json"), encoding="utf-8") as f:
        return json.load(f)


def save(p):
    d = os.path.join(ctx.ws(), p["id"])
    tmp = os.path.join(d, "project.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(d, "project.json"))


def lock(pid):
    return _locks.setdefault(pid, threading.Lock())


def update(pid, **kw):
    with lock(pid):
        p = load(pid)
        p.update(kw)
        save(p)
        return p


def set_step(pid, step, **kw):
    with lock(pid):
        p = load(pid)
        st = p.setdefault("steps", {}).setdefault(step, {})
        st.update(kw)
        save(p)
        return p


def _safe(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:40] or "视频"


def suggest_name(p):
    """导出文件名：日期前缀 + 标题（标题由流水线从文稿提炼，也可以手动改）。"""
    title = (p.get("cover") or {}).get("title", "").strip() or cleanup.summarize(p.get("lines", []))
    title = re.sub(r"[\\/:*?\"<>|\s，。！？!?、,.；;：:“”\"'‘’（）()【】\[\]]+", "", title)[:24]
    return time.strftime("%y%m%d") + (title or _safe(p.get("name", "视频")))


def _tpl_for(p):
    tpl = render.get_template(p["settings"]["template"])
    tpl["subtitle"] = dict(tpl["subtitle"], pos_y=float(p["settings"].get("sub_pos", tpl["subtitle"]["pos_y"])),
                           emph=p["settings"].get("text_fx", True))
    return tpl


def _overlay_text(p):
    cv = p.get("cover") or {}
    s = p["settings"]
    if s.get("top_title", True):
        return cv.get("title", ""), cv.get("subtitle", ""), s.get("ip_name", "")
    return "", "", s.get("ip_name", "")


def work_src(p):
    d = os.path.join(ctx.ws(), p["id"])
    w = p.get("work_src")
    if w and os.path.exists(os.path.join(d, w)):
        return os.path.join(d, w)
    return os.path.join(d, p["src"])


def summary(p):
    if p.get("lines"):
        iv, _, total = engine.cut_plan(p["lines"], p["info"]["duration"], p["settings"]["pace"], 30)
        p["out_duration"] = round(total, 2)
        p["intervals"] = iv
        p["suggest_name"] = suggest_name(p)
    tpl = _tpl_for(p)
    p["template_info"] = {k: v for k, v in tpl.items() if k in ("layout", "top_title", "ip_tag", "name")}
    info = p["info"]
    W, H = render.output_size(info["width"], info["height"], p["settings"].get("aspect", "original"), tpl)
    p["geom"] = {"W": W, "H": H, "region": render.video_region(tpl, W, H, info["width"], info["height"]),
                 "focus": render._focus(tpl)}
    p["sub_style"] = tpl["subtitle"]
    return p


# ---------------------------------------------------------------- 流水线步骤
class StepSkip(Exception):
    """这一步不需要做（如关闭了防抖），或失败但不影响后续（会标成“已跳过”）。"""


def _progress(pid, step, base_msg):
    last = [0.0]

    def cb(x, msg=None):
        if x - last[0] >= 0.02 or x >= 1:
            last[0] = x
            set_step(pid, step, progress=round(min(1, x), 3), msg=msg or f"{base_msg} {int(x * 100)}%")
    return cb


def step_stab(pid, p):
    mode = render._stab_mode(p["settings"])
    if not mode:
        update(pid, work_src="", stab_done=None)
        raise StepSkip("未开启防抖")
    d = pdir(pid)
    src = os.path.join(d, p["src"])
    if mode == "auto":
        # 先看背景有没有在晃：固定机位（手机支架、三脚架）不做防抖，避免人一动画面反而跟着飘
        set_step(pid, "stab", msg="正在判断镜头是否晃动…")
        try:
            sh = media.measure_shake(src, p["info"]["duration"])
        except Exception as e:  # noqa: BLE001
            sh = {"level": "fixed", "jitter": 0}
            print("晃动检测失败：", e)
        update(pid, shake=sh)
        if sh["level"] == "fixed":
            update(pid, work_src="", stab_done=None)
            raise StepSkip(f"镜头是固定的（晃动 {sh['jitter']}），不需要防抖")
        mode = "gentle" if sh["level"] == "light" else "standard"
    try:
        render.stabilize_source(src, p["info"], os.path.join(d, "stab.mp4"),
                                os.path.join(d, "stabwork"), mode, _progress(pid, "stab", "正在防抖"))
    except Exception as e:  # noqa: BLE001  防抖失败不影响出片，用原片继续
        update(pid, work_src="", stab_done=None)
        raise StepSkip("防抖失败，已用原片继续：" + str(e)[-200:]) from None
    update(pid, work_src="stab.mp4", stab_done=mode)
    auto = p["settings"].get("stabilize") in ("auto", True, None)
    return ("检测到镜头晃动，" if auto else "") + "画面已稳定（" + render.STAB_NAMES.get(mode, mode) + "）"


def step_asr(pid, p):
    d = pdir(pid)
    src = os.path.join(d, p["src"])
    cb = _progress(pid, "asr", "正在识别语音")
    prog = {"proxy": 0.0}

    wsrc = work_src(p)  # 在当前账号上下文里先算好路径，子线程里不再依赖“当前用户”

    def proxy_job():
        media.make_proxy(wsrc, os.path.join(d, "proxy.mp4"), p["info"]["duration"],
                         lambda x: prog.update(proxy=x))
    th = threading.Thread(target=proxy_job, daemon=True)
    th.start()
    cb(0.02, "正在读取音频…")
    audio = media.load_audio_16k(src)
    cb(0.05, "正在加载识别模型…")
    engine._load()
    segs = engine.transcribe(audio, lambda x: cb(0.05 + 0.85 * x))
    th.join()
    if not segs:
        raise RuntimeError("没有识别到说话内容，请确认视频里有人声。")
    with open(os.path.join(d, "segments.json"), "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False)
    n = sum(len(s["tokens"]) for s in segs)
    return f"识别出 {n} 个字"


def step_clean(pid, p):
    d = pdir(pid)
    with open(os.path.join(d, "segments.json"), encoding="utf-8") as f:
        segs = json.load(f)
    tpl = render.get_template(p["settings"]["template"])
    cfg = ai_cfg() if p["settings"].get("use_ai", True) else None
    cb = _progress(pid, "clean", "AI 正在判断重说和错别字" if ai.ready(cfg) else "正在整理")
    cb(0.05, "AI 正在判断重说和错别字…" if ai.ready(cfg) else "正在分析重说、重复和语气词…")
    lines, note = cleanup.process(segs, tpl.get("max_chars", 13), p["settings"]["remove_fillers"], cfg,
                                  lambda f: cb(0.05 + 0.8 * f))
    cb(0.9, "正在处理气口…")
    engine.refine_lines(media.load_audio_16k(os.path.join(d, p["src"])), lines)
    lines = engine.auto_highlight(lines)
    first = [l for l in lines if not l["deleted"]]
    cover = p.get("cover") or {}
    if not cover.get("manual"):
        cover["t"] = round(min(p["info"]["duration"] - 0.1, (first[0]["start"] + 0.3) if first else 1.0), 2)
    n_del = sum(1 for l in lines if l["deleted"])
    update(pid, lines=lines, ai_note=note, cover=cover, stage="ready")
    return (note + "；" if note else "") + f"剪掉 {n_del} 处重说、重复和语气词"


def step_title(pid, p):
    cover = p.get("cover") or {}
    if cover.get("manual"):
        raise StepSkip("使用你填写的标题：" + cover.get("title", ""))
    cfg = ai_cfg() if p["settings"].get("use_ai", True) else None
    t, s, how = titles.extract(p["lines"], cfg)
    cover.update(title=t, subtitle=s)
    update(pid, cover=cover)
    return f"{how}：{t}" + (f" / {s}" if s else "")


def _cover_size(p, ratio, scale=1.0):
    if ratio == "3:4":
        W, H = 1080, 1440
    else:
        W, H = 1080, 1920
    return int(W * scale) // 2 * 2, int(H * scale) // 2 * 2


def _ensure_stab(pid, p):
    """在修改页改了防抖档位：只重做防抖，不重新识别、不覆盖已校对的字幕。"""
    want = render._stab_mode(p["settings"])
    have = p.get("stab_done")
    if want == have or (want is None and not p.get("work_src")):
        return p
    try:
        msg = step_stab(pid, p)
        set_step(pid, "stab", status="done", msg=msg, progress=1)
    except StepSkip as e:
        set_step(pid, "stab", status="skipped", msg=str(e), progress=1)
    return load(pid)


def step_cover(pid, p):
    p = _ensure_stab(pid, p)
    d = pdir(pid)
    cv = p["cover"]
    frame = os.path.join(d, "cover_frame_full.jpg")
    media.grab_frame(work_src(p), cv.get("t", 1.0), frame)
    os.makedirs(os.path.join(d, "covers"), exist_ok=True)
    tpl = _tpl_for(p)
    for ratio, fname in (("9:16", "cover_9x16.jpg"), ("3:4", "cover_3x4.jpg")):
        W, H = _cover_size(p, ratio)
        render.render_cover(frame, cv.get("title", ""), cv.get("subtitle", ""), tpl, W, H,
                            os.path.join(d, "covers", fname), cv.get("zoom", 1.0), p["settings"].get("ip_name", ""),
                            (p["info"]["width"], p["info"]["height"]))
    return "已生成 9:16 和 3:4 两个封面"


def step_export(pid, p):
    d = pdir(pid)
    tpl = _tpl_for(p)
    s = dict(p["settings"], stabilize="off")  # 防抖已在第一步完成
    iv, tl, total = engine.cut_plan(p["lines"], p["info"]["duration"], s["pace"], 30)
    if not iv:
        raise RuntimeError("所有句子都被删掉了，没有可导出的内容。请打开修改，恢复需要的句子。")
    by_id = {l["id"]: l for l in p["lines"]}
    for t in tl:
        t["punct"] = by_id[t["id"]].get("punct", "")
    tl = engine.split_timeline(tl, tpl.get("max_chars", 13) + 2)
    name = (p.get("export_name") or "").strip()
    base = _safe(name) if name else suggest_name(p)
    old = (p.get("export") or {}).get("folder")
    if old and os.path.isdir(old) and os.path.basename(old).startswith(base):
        shutil.rmtree(old, ignore_errors=True)  # 重新导出时覆盖上一版
    folder = os.path.join(ctx.out(), base)
    k = 2
    while os.path.exists(folder):
        folder = os.path.join(ctx.out(), f"{base}_{k}")
        k += 1
    os.makedirs(folder, exist_ok=True)
    work = os.path.join(d, "export")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    nm = os.path.basename(folder)
    files = []
    for fname, suffix in (("cover_9x16.jpg", "_封面9比16.jpg"), ("cover_3x4.jpg", "_封面3比4.jpg")):
        c = os.path.join(d, "covers", fname)
        if os.path.exists(c):
            shutil.copy(c, os.path.join(folder, nm + suffix))
            files.append(nm + suffix)
    src = work_src(p)
    info = media.probe(src) if src != os.path.join(d, p["src"]) else p["info"]
    info = dict(info, duration=p["info"]["duration"])
    vname = nm + ".mp4"
    # 动效：重点大字 + 音效 + 背景音乐
    speaker = None
    if s.get("speaker_card", True):
        titles_ = [x.strip() for x in re.split(r"[\n|｜]", s.get("speaker_titles", "") or "") if x.strip()]
        name_ = (s.get("speaker_name") or "").strip()
        if titles_ or name_:  # 填了姓名或头衔才显示介绍卡；没填姓名时用账号名
            speaker = (name_ or (s.get("ip_name") or "").strip().lstrip("@"), titles_)
    big = effects.pick_bigwords(tl, total, s.get("bigwords", True), avoid_until=5.2 if speaker else 0.0)
    sfx_path = None
    if s.get("sfx", False):
        sfx_path = effects.sfx_track(big, total, os.path.join(work, "sfx.wav"),
                                     intro=bool(s.get("top_title", True)))
    bgm = music.resolve(s.get("bgm", "auto"), tpl) if EDITION["music"] else None
    if bgm and not os.path.exists(bgm["path"]):
        bgm = None
    extra = []
    # 中英双语模板：用 AI 把字幕逐句翻译成英文（结果缓存，改字幕后只翻新的句子）
    if tpl["subtitle"].get("bilingual"):
        cfg = ai_cfg()
        if ai.ready(cfg):
            cache = dict(p.get("trans") or {})
            zh = [engine.plain(t["text"]) for t in tl]
            todo = list(dict.fromkeys(z for z in zh if z and z not in cache))  # 保持说话顺序，翻译更连贯
            if todo:
                set_step(pid, "export", msg="正在翻译英文字幕…")
                try:
                    for z, en in zip(todo, ai.translate(todo, cfg)):
                        if en:
                            cache[z] = en
                    update(pid, trans=cache)
                except Exception as e:  # noqa: BLE001
                    set_step(pid, "export", note="英文翻译失败，本次只有中文：" + str(e)[:120])
            for t, z in zip(tl, zh):
                t["en"] = cache.get(z, "")
            if any(t.get("en") for t in tl):
                extra.append("中英双语")
        else:
            extra.append("未接入 AI，仅中文字幕")
    render.export_video(src, info, iv, tl, total, tpl, s, _overlay_text(p), os.path.join(folder, vname), work,
                        _progress(pid, "export", "正在合成成片"), log=lambda m: set_step(pid, "export", note=m),
                        bigwords=big, bgm=bgm, sfx_path=sfx_path, speaker=speaker)
    if speaker:
        extra.append("主讲人介绍")
    if big:
        extra.append(f"{len(big)} 处重点大字")
    if bgm:
        extra.append("音乐「" + bgm["name"] + "」")
    update(pid, fx_summary="、".join(extra), bigwords=big)
    sname = nm + ".srt"
    render.write_srt(tl, os.path.join(folder, sname))
    files = [vname] + files + [sname]
    update(pid, export={"folder": folder, "files": files, "duration": round(total, 2)}, stage="done")
    fxs = load(pid).get("fx_summary")
    return f"成片 {total:.1f} 秒 → {nm}" + (f"（{fxs}）" if fxs else "")


STEP_FN = {"stab": step_stab, "asr": step_asr, "clean": step_clean, "title": step_title, "cover": step_cover,
           "export": step_export}


def run_pipeline(pid):
    """按顺序跑完还没完成的步骤；遇到错误停下，等用户查看或重试。"""
    p = load(pid)
    end = p.get("queue", {}).get("until") or "export"
    for step in STEPS:
        st = (p.get("steps") or {}).get(step, {})
        if st.get("status") in ("done", "skipped"):
            if step == end:
                break
            continue
        set_step(pid, step, status="running", progress=0, msg="开始…", t0=time.time(), note="")
        stage = {"stab": "processing", "asr": "processing", "clean": "processing", "export": "exporting"}.get(step)
        if stage:
            update(pid, stage=stage)
        try:
            msg = STEP_FN[step](pid, load(pid))
            set_step(pid, step, status="done", progress=1, msg=msg or "完成", t1=time.time())
        except StepSkip as e:
            set_step(pid, step, status="skipped", progress=1, msg=str(e), t1=time.time())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            set_step(pid, step, status="error", msg=str(e)[-600:], t1=time.time())
            p = load(pid)
            done_clean = (p.get("steps") or {}).get("clean", {}).get("status") == "done"
            update(pid, queue={"state": "error", "step": step}, stage="ready" if done_clean else "error",
                   message=f"{STEP_NAMES[step]}失败：{str(e)[-200:]}")
            return
        p = load(pid)
        if step == end:
            break
    final = "done" if end == "export" else "paused"
    update(pid, queue={"state": final}, message="")
    if end != "export":
        update(pid, stage="ready")


# ---------------------------------------------------------------- 后台队列（逐条处理）
_wake = threading.Event()


def enqueue(pid, from_step="stab", until="export"):
    with lock(pid):
        p = load(pid)
        steps = p.setdefault("steps", {})
        idx = STEPS.index(from_step)
        for s in STEPS[idx:STEPS.index(until) + 1]:
            steps[s] = {"status": "pending"}
        p["queue"] = {"state": "queued", "t": time.time(), "until": until}
        p["message"] = ""
        save(p)
    _wake.set()


_claim = threading.Lock()


def _scan():
    """所有账号里的项目：(uid, project)"""
    for uid in ctx.all_users():
        try:
            d = ctx.ws(uid)
        except ctx.NoUser:
            continue
        for name in os.listdir(d):
            f = os.path.join(d, name, "project.json")
            try:
                with open(f, encoding="utf-8") as fp:
                    yield uid, json.load(fp)
            except Exception:  # noqa: BLE001
                continue


def _recover():
    """启动时把上次中断（关机、重启）的任务重新排队。"""
    for uid, p in list(_scan()):
        if p.get("queue", {}).get("state") == "running":
            tok = ctx.current.set(uid)
            try:
                p["queue"]["state"] = "queued"
                for s_ in STEPS:
                    if p.get("steps", {}).get(s_, {}).get("status") == "running":
                        p["steps"][s_]["status"] = "pending"
                save(p)
            finally:
                ctx.current.reset(tok)


def _take():
    """取出最早排队的一条，标记为处理中。多个处理线程共用，同一条不会被处理两次。"""
    with _claim:
        best = None
        for uid, p in _scan():
            q = p.get("queue") or {}
            if q.get("state") == "queued" and (best is None or q.get("t", 0) < best[2]):
                best = (uid, p["id"], q.get("t", 0))
        if not best:
            return None
        uid, pid, _ = best
        tok = ctx.current.set(uid)
        try:
            q = load(pid).get("queue", {})
            update(pid, queue=dict(q, state="running"))
        finally:
            ctx.current.reset(tok)
        return uid, pid


def _worker():
    while True:
        if not engine.models_ready():
            _wake.wait(5)
            _wake.clear()
            continue
        job = _take()
        if not job:
            _wake.wait(3)
            _wake.clear()
            continue
        uid, pid = job
        tok = ctx.current.set(uid)
        try:
            run_pipeline(pid)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:
                update(pid, queue={"state": "error"})
            except Exception:  # noqa: BLE001
                pass
        finally:
            ctx.current.reset(tok)


def _janitor():
    """云端版：定期删除超过保留天数的项目和成片，避免磁盘被占满。"""
    while True:
        try:
            cutoff = time.time() - LIMITS["keep_days"] * 86400
            for uid, p in list(_scan()):
                if p.get("created", time.time()) < cutoff and (p.get("queue") or {}).get("state") != "running":
                    tok = ctx.current.set(uid)
                    try:
                        folder = (p.get("export") or {}).get("folder")
                        if folder and os.path.isdir(folder) and folder.startswith(ctx.out()):
                            shutil.rmtree(folder, ignore_errors=True)
                        shutil.rmtree(os.path.join(ctx.ws(), p["id"]), ignore_errors=True)
                    finally:
                        ctx.current.reset(tok)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(3600)


_recover()
for _ in range(max(1, LIMITS["workers"])):
    threading.Thread(target=_worker, daemon=True).start()
if CLOUD:
    threading.Thread(target=_janitor, daemon=True).start()


# ---------------------------------------------------------------- 基础
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/status")
def status():
    return {"models_ready": engine.models_ready(), "download": downloader.state,
            "templates": [{"id": t["id"], "name": t["name"], "description": t.get("description", ""),
                           "has_preview": t.get("has_preview", False)} for t in render.list_templates()],
            "prefs": {k: v for k, v in prefs().items() if k != "ai"},
            "batch": batch_settings(), "steps": [{"id": s, "name": STEP_NAMES[s]} for s in STEPS],
            "ai_ready": ai.ready(ai_cfg()), "ai_presets": ai.PRESETS, "ai_global": bool(global_ai()),
            "cloud": CLOUD, "limits": LIMITS if CLOUD else None, "edition": EDITION,
            "music": [music.public(m) for m in music.all_music()],
            "template_music": {t["id"]: t.get("music", "huoli") for t in render.list_templates()}}


# ---------------------------------------------------------------- 背景音乐库
@app.get("/api/music")
def music_list():
    return [music.public(m) for m in music.all_music()]


@app.post("/api/music")
async def music_upload(file: UploadFile = File(...)):
    try:
        mid = music.save_upload(file.filename, file.file)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"id": mid, "music": [music.public(m) for m in music.all_music()]}


@app.delete("/api/music/{mid:path}")
def music_delete(mid: str):
    music.delete(mid)
    return [music.public(m) for m in music.all_music()]


@app.get("/api/music/{mid:path}/file")
def music_file(mid: str):
    m = music.get(mid)
    if not m:
        raise HTTPException(404)
    return FileResponse(m["path"])


@app.put("/api/settings/batch")
def put_batch(s: dict):
    cur = batch_settings()
    for k in DEFAULT_SETTINGS:
        if k in s:
            cur[k] = s[k]
    cur["rev"] = SETTINGS_REV
    save_prefs(batch=cur, ip_name=str(cur.get("ip_name", ""))[:30])
    return cur


# ---------------------------------------------------------------- 我的词典
@app.get("/api/dictionary")
def get_dictionary():
    import lexicon
    text = lexicon.load_text()
    terms, pairs = lexicon.parse(text)
    return {"text": text, "terms": len(terms), "pairs": len(pairs)}


class DictReq(BaseModel):
    text: str


@app.put("/api/dictionary")
def put_dictionary(req: DictReq):
    import lexicon
    if len(req.text) > 200000:
        raise HTTPException(400, "词典太长了")
    lexicon.save_text(req.text)
    terms, pairs = lexicon.parse()
    return {"ok": True, "terms": len(terms), "pairs": len(pairs)}


class AISettings(BaseModel):
    enabled: bool = True
    preset: str = "deepseek"
    base_url: str = ""
    model: str = ""
    api_key: str = ""   # 留空表示不修改已保存的 Key


# ---------------------------------------------------------------- 账号（云端版）
class AuthReq(BaseModel):
    username: str
    password: str
    invite: str = ""
    name: str = ""


def _login_resp(u, request):
    r = JSONResponse({"ok": True, "user": {"name": u["name"]}})
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    r.set_cookie(auth.COOKIE, auth.make_token(u["uid"]), max_age=auth.TTL, httponly=True, samesite="lax",
                 secure=https)
    return r


@app.get("/api/auth/me")
def me(request: Request):
    if not CLOUD:
        return {"cloud": False, "user": {"name": "本机"}, "edition": EDITION}
    uid = auth.verify(request.cookies.get(auth.COOKIE))
    u = auth.user_by_uid(uid) if uid else None
    return {"cloud": True, "invite": auth.invite_required(), "edition": EDITION,
            "register": os.environ.get("KOUBO_REGISTER", "1") != "0",
            "user": {"name": u["name"], "username": u["username"]} if u else None}


@app.post("/api/auth/register")
def register(req: AuthReq, request: Request):
    if not CLOUD:
        raise HTTPException(400, "本地版不需要注册")
    try:
        u = auth.register(req.username, req.password, req.invite, req.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return _login_resp(u, request)


@app.post("/api/auth/login")
def do_login(req: AuthReq, request: Request):
    if not CLOUD:
        raise HTTPException(400, "本地版不需要登录")
    try:
        u = auth.login(req.username, req.password)
    except ValueError as e:
        time.sleep(0.5)
        raise HTTPException(400, str(e)) from None
    return _login_resp(u, request)


@app.post("/api/auth/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE)
    return r


@app.get("/api/settings/ai")
def get_ai():
    c = dict(prefs().get("ai") or {})
    k = c.pop("api_key", "")
    c["has_key"] = bool(k)
    c["key_tail"] = k[-4:] if k else ""
    c["global"] = bool(global_ai())
    return c


@app.put("/api/settings/ai")
def put_ai(req: AISettings):
    old = prefs().get("ai") or {}
    c = req.model_dump()
    if not c["api_key"]:
        c["api_key"] = old.get("api_key", "")
    save_prefs(ai=c)
    return get_ai()


@app.post("/api/settings/ai/test")
def test_ai(req: AISettings):
    c = req.model_dump()
    if not c["api_key"]:
        c["api_key"] = (prefs().get("ai") or {}).get("api_key", "")
    if not (c["api_key"] and c["base_url"] and c["model"]):
        raise HTTPException(400, "请填写完整：接口地址、模型名称、API Key")
    try:
        ai.test(c)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


@app.get("/api/templates/{tid}/preview.jpg")
def template_preview(tid: str):
    t = render.get_template(tid)
    path = os.path.join(render.TPL_DIR, t["id"], "preview.jpg")
    if not os.path.exists(path):
        previews.make(t)
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/models/download")
def download_models():
    downloader.start()
    return downloader.state


# ---------------------------------------------------------------- 项目 / 队列
def _job_view(p):
    q = p.get("queue") or {}
    steps = p.get("steps") or {}
    cur = next((s for s in STEPS if steps.get(s, {}).get("status") == "running"), None)
    return {"id": p["id"], "name": p["name"], "created": p.get("created"), "stage": p.get("stage"),
            "queue": q.get("state", "idle"), "steps": {s: steps.get(s, {"status": "pending"}) for s in STEPS},
            "current": cur, "title": (p.get("cover") or {}).get("title", ""),
            "export": p.get("export"), "duration": p["info"]["duration"],
            "template": p["settings"].get("template"), "message": p.get("message", "")}


@app.get("/api/jobs")
def jobs():
    out = []
    for d in os.listdir(ctx.ws()):
        f = os.path.join(ctx.ws(), d, "project.json")
        if os.path.exists(f):
            try:
                with open(f, encoding="utf-8") as fp:
                    out.append(_job_view(json.load(fp)))
            except Exception:  # noqa: BLE001
                continue
    out.sort(key=lambda x: -(x["created"] or 0))
    return out[:60]


@app.get("/api/projects")
def projects():
    return [{k: j[k] for k in ("id", "name", "stage", "created")} for j in jobs()][:30]


@app.post("/api/projects")
async def create_project(file: UploadFile = File(...), settings: str = Form(""), auto: str = Form("")):
    """上传一条视频。auto=1 时按 settings（缺省用批量设置）自动跑完整条流水线。"""
    if CLOUD:
        waiting = sum(1 for j in jobs() if j["queue"] in ("queued", "running"))
        if waiting >= LIMITS["max_queue"]:
            raise HTTPException(429, f"你已有 {waiting} 条在排队，等处理完一些再上传（每人最多 {LIMITS['max_queue']} 条）")
    pid = uuid.uuid4().hex[:10]
    d = os.path.join(ctx.ws(), pid)
    os.makedirs(d)
    ext = os.path.splitext(file.filename or "")[1].lower() or ".mp4"
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = ".mp4"
    src = os.path.join(d, "source" + ext)
    size, cap = 0, LIMITS["upload_mb"] * 1024 * 1024
    with open(src, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if CLOUD and size > cap:
                f.close()
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(413, f"{file.filename}：文件超过 {LIMITS['upload_mb']}MB，请先压缩或剪短")
            f.write(chunk)
    try:
        info = media.probe(src)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f"{file.filename}：{e}") from None
    if CLOUD and info["duration"] > LIMITS["max_minutes"] * 60:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f"{file.filename}：视频超过 {LIMITS['max_minutes']} 分钟，在线版请上传更短的口播")
    name = os.path.splitext(os.path.basename(file.filename or "视频"))[0]
    s = batch_settings()
    if settings:
        try:
            s.update({k: v for k, v in json.loads(settings).items() if k in DEFAULT_SETTINGS})
        except Exception:  # noqa: BLE001
            pass
    s["sub_pos"] = render.get_template(s["template"])["subtitle"]["pos_y"]
    p = {"id": pid, "uid": ctx.current.get(), "name": name, "src": os.path.basename(src), "info": info,
         "created": time.time(),
         "stage": "uploaded", "progress": 0, "message": "", "lines": [], "settings": s,
         "cover": {"t": 1.0, "title": "", "subtitle": "", "zoom": 1.0}, "steps": {}, "queue": {"state": "idle"}}
    save(p)
    media.grab_frame(src, min(1.0, info["duration"] / 2), os.path.join(d, "thumb.jpg"), 540)
    if auto:
        enqueue(pid)
    return p


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return summary(load(pid))


class RunReq(BaseModel):
    from_step: str = "stab"
    until: str = "export"
    name: str | None = None


@app.post("/api/projects/{pid}/run")
def run(pid: str, req: RunReq):
    """从某一步开始（重新）跑流水线。前面已完成的步骤不会重做。"""
    if req.from_step not in STEPS or req.until not in STEPS:
        raise HTTPException(400, "未知步骤")
    p = load(pid)
    if (p.get("queue") or {}).get("state") == "running":
        raise HTTPException(409, "这条视频正在处理中")
    steps = p.get("steps") or {}
    for s in STEPS[:STEPS.index(req.from_step)]:  # 前面的步骤必须已完成
        if steps.get(s, {}).get("status") not in ("done", "skipped"):
            req.from_step = s
            break
    if req.name is not None:
        update(pid, export_name=req.name)
    enqueue(pid, req.from_step, req.until)
    return {"ok": True, "from": req.from_step}


@app.post("/api/projects/{pid}/cancel")
def cancel(pid: str):
    p = load(pid)
    if (p.get("queue") or {}).get("state") == "queued":
        steps = p.get("steps") or {}
        for s in STEPS:
            if steps.get(s, {}).get("status") == "pending":
                steps[s] = {"status": "pending", "msg": "已取消"}
        update(pid, queue={"state": "idle"}, steps=steps)
    return {"ok": True}


# 兼容单条手动流程：开始智能剪辑 = 跑到“提炼标题”为止
class ProcessReq(BaseModel):
    template: str = "template1"
    pace: str = "normal"
    remove_fillers: bool = True
    use_ai: bool = True


@app.post("/api/projects/{pid}/process")
def process(pid: str, req: ProcessReq):
    p = load(pid)
    s = p["settings"]
    tpl = render.get_template(req.template)
    s.update(template=tpl["id"], pace=req.pace, remove_fillers=req.remove_fillers, use_ai=req.use_ai,
             sub_pos=tpl["subtitle"]["pos_y"])
    update(pid, settings=s, stage="processing")
    enqueue(pid, "stab", "export")
    return {"ok": True}


class RecleanReq(BaseModel):
    use_ai: bool = True
    remove_fillers: bool = True


@app.post("/api/projects/{pid}/reclean")
def reclean(pid: str, req: RecleanReq):
    """不重新识别，只用已有的识别结果重新整理（比如配好 AI 后再跑一遍）。"""
    p = load(pid)
    if not os.path.exists(os.path.join(pdir(pid), "segments.json")):
        raise HTTPException(400, "没有识别结果，请先做智能剪辑")
    s = p["settings"]
    s.update(use_ai=req.use_ai, remove_fillers=req.remove_fillers)
    update(pid, settings=s, stage="processing")
    enqueue(pid, "clean", "title")
    return {"ok": True}


class LinesReq(BaseModel):
    lines: list
    settings: dict | None = None


@app.put("/api/projects/{pid}/lines")
def put_lines(pid: str, req: LinesReq):
    with lock(pid):
        p = load(pid)
        old = {l["id"]: l for l in p["lines"]}
        for l in req.lines:
            if l.get("id") in old:
                o = old[l["id"]]
                o["text"] = str(l.get("text", o["text"]))[:200]
                o["deleted"] = bool(l.get("deleted", o["deleted"]))
        if req.settings:
            st = req.settings
            if "template" in st and st["template"] != p["settings"].get("template"):
                p["settings"]["sub_pos"] = render.get_template(st["template"])["subtitle"]["pos_y"]
            for k in ("pace", "aspect", "sub_pos", "loudnorm", "template", "stabilize", "enhance", "denoise",
                      "ip_name", "top_title", "text_fx", "bigwords", "sfx", "bgm", "bgm_volume",
                      "speaker_card", "speaker_name", "speaker_titles"):
                if k in st:
                    p["settings"][k] = st[k]
            if "ip_name" in st:
                save_prefs(ip_name=str(st["ip_name"])[:30])
        save(p)
    return summary(p)


class ReplaceReq(BaseModel):
    find: str
    replace: str
    remember: bool = True


@app.post("/api/projects/{pid}/replace")
def replace_all(pid: str, req: ReplaceReq):
    if not req.find:
        raise HTTPException(400, "请输入要替换的词")
    n = 0
    with lock(pid):
        p = load(pid)
        for l in p["lines"]:
            if req.find in l["text"]:
                n += l["text"].count(req.find)
                l["text"] = l["text"].replace(req.find, req.replace)
        save(p)
    if req.remember and req.replace:
        cleanup.remember_correction(req.find, req.replace)
    return {"count": n, "project": summary(p)}


def _frame(pid, t):
    d = pdir(pid)
    p = load(pid)
    proxy = os.path.join(d, "proxy.mp4")
    src = proxy if os.path.exists(proxy) else work_src(p)
    out = os.path.join(d, "frame_pv.jpg")
    media.grab_frame(src, t, out)
    return out, p


@app.get("/api/projects/{pid}/overlay.png")
def overlay_png(pid: str):
    """当前模板的常驻层（色带、顶部标题、账号名），给播放预览叠在视频上。"""
    p = load(pid)
    tpl = _tpl_for(p)
    info = p["info"]
    W, H = render.output_size(info["width"], info["height"], p["settings"].get("aspect", "original"), tpl)
    from PIL import Image
    ov = render.static_overlay(tpl, W, H, info["width"], info["height"], *_overlay_text(p)) \
        or Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ov.thumbnail((720, 720))
    out = os.path.join(pdir(pid), f"ov_{uuid.uuid4().hex[:6]}.png")
    ov.save(out)
    threading.Timer(5, lambda: os.path.exists(out) and os.remove(out)).start()
    return FileResponse(out, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/projects/{pid}/preview")
def preview(pid: str, line: int = 0, t: float = -1):
    """某一句字幕在成片里的完整效果（版式、常驻标题、账号名、字幕），与导出一致。"""
    p = load(pid)
    l = next((x for x in p["lines"] if x["id"] == line), None)
    tt = t if t >= 0 else ((l["start"] + l["end"]) / 2 if l else 1.0)
    frame, p = _frame(pid, tt)
    d = pdir(pid)
    tpl = _tpl_for(p)
    info = p["info"]
    W, H = render.output_size(info["width"], info["height"], p["settings"].get("aspect", "original"), tpl)
    from PIL import Image
    img = render.compose_frame(Image.open(frame), tpl, W, H, info["width"], info["height"],
                               l["text"] if l and not l["deleted"] else "", l.get("punct", "") if l else "",
                               *_overlay_text(p))
    img.thumbnail((720, 720))
    out = os.path.join(d, f"preview_{uuid.uuid4().hex[:6]}.jpg")
    img.save(out, quality=88)
    resp = FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    threading.Timer(5, lambda: os.path.exists(out) and os.remove(out)).start()
    return resp


class CoverReq(BaseModel):
    t: float
    title: str = ""
    subtitle: str = ""
    zoom: float = 1.0
    ratio: str = "9:16"


@app.post("/api/projects/{pid}/cover_preview")
def cover_preview(pid: str, req: CoverReq):
    d = pdir(pid)
    p = load(pid)
    old = p.get("cover") or {}
    manual = old.get("manual") or req.title != old.get("title", "") or req.subtitle != old.get("subtitle", "")
    p = update(pid, cover={"t": req.t, "title": req.title, "subtitle": req.subtitle, "zoom": req.zoom,
                           "manual": bool(manual)})
    frame = os.path.join(d, "cover_frame.jpg")
    src = os.path.join(d, "proxy.mp4") if os.path.exists(os.path.join(d, "proxy.mp4")) else work_src(p)
    media.grab_frame(src, req.t, frame)
    W, H = _cover_size(p, req.ratio, 0.5)
    out = os.path.join(d, f"cover_pv_{uuid.uuid4().hex[:6]}.jpg")
    render.render_cover(frame, req.title, req.subtitle, _tpl_for(p), W, H, out, req.zoom,
                        p["settings"].get("ip_name", ""), (p["info"]["width"], p["info"]["height"]))
    threading.Timer(5, lambda: os.path.exists(out) and os.remove(out)).start()
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class ExportReq(BaseModel):
    name: str = ""


@app.post("/api/projects/{pid}/export")
def export(pid: str, req: ExportReq | None = None):
    """修改后重新导出：重新生成封面并合成（防抖、识别、整理不重做）。"""
    p = load(pid)
    if (p.get("queue") or {}).get("state") == "running":
        raise HTTPException(409, "这条视频正在处理中")
    update(pid, export_name=req.name if req else "", stage="exporting")
    enqueue(pid, "cover", "export")
    return {"ok": True}


@app.get("/api/projects/{pid}/file/{name}")
def get_file(pid: str, name: str, download: int = 0):
    d = pdir(pid)
    p = load(pid)
    if name in ("proxy.mp4", "thumb.jpg"):
        path = os.path.join(d, name)
    elif name in ("cover_9x16.jpg", "cover_3x4.jpg"):
        path = os.path.join(d, "covers", name)
    else:
        exp = p.get("export") or {}
        if name not in exp.get("files", []):
            raise HTTPException(404)
        path = os.path.join(exp["folder"], name)
    if not os.path.exists(path):
        raise HTTPException(404)
    kw = {"filename": name} if download else {}
    return FileResponse(path, headers={"Cache-Control": "no-store"}, **kw)


def _zip_response(paths, zipname):
    """把文件打包成 zip 下载（视频不再压缩，打包很快）。"""
    import tempfile
    import zipfile
    from starlette.background import BackgroundTask
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=ctx.base())
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for arc, path in paths:
            z.write(path, arc)
    return FileResponse(tmp, filename=zipname, media_type="application/zip",
                        background=BackgroundTask(lambda: os.path.exists(tmp) and os.remove(tmp)))


@app.get("/api/projects/{pid}/download.zip")
def download_one(pid: str):
    p = load(pid)
    exp = p.get("export") or {}
    folder = exp.get("folder")
    if not folder or not os.path.isdir(folder):
        raise HTTPException(404, "还没有导出的成片")
    nm = os.path.basename(folder)
    files = [(f"{nm}/{f}", os.path.join(folder, f)) for f in exp.get("files", []) if os.path.exists(os.path.join(folder, f))]
    return _zip_response(files, nm + ".zip")


@app.get("/api/download_all.zip")
def download_all():
    files = []
    for j in jobs():
        exp = j.get("export") or {}
        folder = exp.get("folder")
        if folder and os.path.isdir(folder):
            nm = os.path.basename(folder)
            files += [(f"{nm}/{f}", os.path.join(folder, f)) for f in exp.get("files", [])
                      if os.path.exists(os.path.join(folder, f))]
    if not files:
        raise HTTPException(404, "还没有导出的成片")
    return _zip_response(files, time.strftime("%y%m%d") + "成片合集.zip")


def _open(folder):
    if sys.platform == "win32":
        os.startfile(folder)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", folder])
    else:
        subprocess.Popen(["xdg-open", folder])


@app.post("/api/projects/{pid}/open_folder")
def open_folder(pid: str):
    p = load(pid)
    if CLOUD:
        raise HTTPException(400, "在线版请用“下载”")
    _open((p.get("export") or {}).get("folder") or ctx.out())
    return {"ok": True}


@app.post("/api/open_output")
def open_output():
    if CLOUD:
        raise HTTPException(400, "在线版请用“下载”")
    _open(ctx.out())
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    p = load(pid)
    if (p.get("queue") or {}).get("state") == "running":
        raise HTTPException(409, "正在处理中，等这条处理完再删除")
    shutil.rmtree(pdir(pid), ignore_errors=True)
    return {"ok": True}


@app.exception_handler(ctx.NoUser)
async def nouser(_, exc):
    return JSONResponse({"detail": "请先登录", "login": True}, status_code=401)


@app.exception_handler(Exception)
async def err(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=500)
