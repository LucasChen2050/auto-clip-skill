"""智能剪辑核心：静音检测 → 语音识别 → 断句 → 关键词高亮 → 剪辑方案。
全部在本机运行，不调用任何云端接口。"""
import difflib
import os
import re
import sys
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

if sys.platform == "win32":  # 便携版：确保能找到语音引擎的 DLL
    try:
        import importlib.util
        _spec = importlib.util.find_spec("sherpa_onnx")
        if _spec and _spec.submodule_search_locations:
            _lib = os.path.join(list(_spec.submodule_search_locations)[0], "lib")
            if os.path.isdir(_lib):
                os.add_dll_directory(_lib)
                os.environ["PATH"] = _lib + os.pathsep + os.environ.get("PATH", "")
    except Exception:  # noqa: BLE001
        pass

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))

import jieba  # noqa: E402
import jieba.analyse  # noqa: E402
import jieba.posseg as pseg  # noqa: E402

jieba.setLogLevel(60)

MODEL_DIR = os.path.join(ROOT, "models")
SR = 16000
PUNCT = set("，。！？、；：,.!?;:…—“”\"'‘’（）()《》 ")
FILLERS = set("嗯呃额啊哦唔呐嘛诶哎")

# 节奏：说话前后各保留多少秒（越小越紧凑）
PACES = {  # 句首、句尾各保留多少秒（已按音量修剪掉吸气声，所以可以留得很少）
    "fast": (0.04, 0.08),
    "normal": (0.07, 0.15),
    "slow": (0.12, 0.26),
}

_recognizer = None
_vad_cfg = None


def model_paths():
    sv = os.path.join(MODEL_DIR, "sense-voice")
    return {
        "asr": os.path.join(sv, "model.int8.onnx"),
        "tokens": os.path.join(sv, "tokens.txt"),
        "vad": os.path.join(MODEL_DIR, "silero_vad.onnx"),
    }


def models_ready():
    p = model_paths()
    return (os.path.exists(p["asr"]) and os.path.getsize(p["asr"]) > 200_000_000
            and os.path.exists(p["tokens"]) and os.path.exists(p["vad"]))


def _load():
    global _recognizer, _vad_cfg
    import sherpa_onnx
    p = model_paths()
    if _recognizer is None:
        _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=p["asr"], tokens=p["tokens"], use_itn=True,
            num_threads=max(1, min(8, (os.cpu_count() or 2))), language="zh")
    if _vad_cfg is None:
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = p["vad"]
        cfg.silero_vad.threshold = 0.45
        cfg.silero_vad.min_silence_duration = 0.22
        cfg.silero_vad.min_speech_duration = 0.12
        cfg.silero_vad.max_speech_duration = 18
        cfg.silero_vad.window_size = 512
        cfg.sample_rate = SR
        _vad_cfg = cfg
    return _recognizer, _vad_cfg


# ---------------------------------------------------------------- 识别
def detect_speech(audio):
    import sherpa_onnx
    _, cfg = _load()
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    segs = []
    w = cfg.silero_vad.window_size
    for i in range(0, len(audio), w):
        vad.accept_waveform(audio[i:i + w])
        while not vad.empty():
            segs.append((vad.front.start / SR, vad.front.start / SR + len(vad.front.samples) / SR))
            vad.pop()
    vad.flush()
    while not vad.empty():
        segs.append((vad.front.start / SR, vad.front.start / SR + len(vad.front.samples) / SR))
        vad.pop()
    return segs


def transcribe(audio, progress=lambda f: None):
    """返回语音段列表：[{start, end, tokens:[(文字, 开始秒)]}]"""
    rec, _ = _load()
    segs = detect_speech(audio)
    out = []
    batch = 6
    for b in range(0, len(segs), batch):
        chunk = segs[b:b + batch]
        streams = []
        for s, e in chunk:
            a = max(0, int((s - 0.08) * SR))
            z = min(len(audio), int((e + 0.08) * SR))
            st = rec.create_stream()
            st.accept_waveform(SR, audio[a:z])
            streams.append((st, a / SR))
        rec.decode_streams([x[0] for x in streams])
        for (s, e), (st, off) in zip(chunk, streams):
            toks = [(t, off + ts) for t, ts in zip(st.result.tokens, st.result.timestamps)]
            toks = [(t.strip(), ts) for t, ts in toks if t.strip()]
            if any(t not in PUNCT for t, _ in toks):
                out.append({"start": s, "end": e, "tokens": toks})
        progress(min(1.0, (b + batch) / max(1, len(segs))))
    return out


# ---------------------------------------------------------------- 断句
def _split_long(chars, max_chars):
    """chars: [(字, 开始, 结束)]，按词边界拆成不超过 max_chars 的若干行。"""
    text = "".join(c[0] for c in chars)
    if len(text) <= max_chars:
        return [chars]
    n_lines = -(-len(text) // max_chars)
    target = len(text) / n_lines
    # 词边界位置（以字符计）及断开的“代价”：不在专有名词/数量词中间断，不在介词后断
    bounds, pos = [], 0
    words = [(w.word, w.flag) for w in pseg.cut(text)]
    penalty = {}
    for i, (w, f) in enumerate(words):
        pos += len(w)
        bounds.append(pos)
        nf = words[i + 1][1] if i + 1 < len(words) else ""
        p = 0
        if f[:1] in "nm" and nf[:1] in "nq":
            p += 4
        if f in ("p", "d", "c") or w in ("将", "在", "把", "被", "和", "与", "跟"):
            p += 4
        if f.startswith("u") or nf.startswith("u"):  # 的/了 前后
            p += 2 if nf.startswith("u") else 0
        penalty[pos] = p
    # token 可能是多字（英文、数字），建立 字符位置→token 下标 映射
    char_to_tok, acc = [], 0
    for i, c in enumerate(chars):
        acc += len(c[0])
        char_to_tok.append(acc)
    pieces, start_c = [], 0
    while len(text) - start_c > max_chars:
        best = None
        best_cost = None
        for bd in bounds:
            if start_c < bd < len(text) and bd - start_c <= max_chars and len(text) - bd >= 2:
                cost = abs((bd - start_c) - target) + penalty.get(bd, 0)
                if best_cost is None or cost < best_cost:
                    best, best_cost = bd, cost
        if best is None:
            best = start_c + max_chars
        pieces.append((start_c, best))
        start_c = best
    pieces.append((start_c, len(text)))
    res = []
    for a, b in pieces:
        ti = [i for i, acc in enumerate(char_to_tok) if a < acc <= b]
        if ti:
            res.append(chars[ti[0]:ti[-1] + 1])
    return res


def build_lines(segments, max_chars=13):
    lines = []
    for si, seg in enumerate(segments):
        toks = seg["tokens"]
        chars = []
        for i, (t, ts) in enumerate(toks):
            nxt = toks[i + 1][1] if i + 1 < len(toks) else seg["end"]
            end = min(max(nxt, ts + 0.12), ts + 0.6, seg["end"] + 0.1)
            chars.append((t, ts, end))
        phrases, cur = [], []
        for c in chars:
            if c[0] in PUNCT or all(ch in PUNCT for ch in c[0]):
                if cur:
                    phrases.append((cur, c[0]))
                cur = []
            else:
                cur.append(c)
        if cur:
            phrases.append((cur, ""))
        for ph, punct in phrases:
            parts = _split_long(ph, max_chars)
            for k, part in enumerate(parts):
                text = "".join(c[0] for c in part)
                is_last_in_seg = (ph is phrases[-1][0]) and k == len(parts) - 1
                lines.append({
                    "text": text,
                    "punct": punct if k == len(parts) - 1 else "",
                    "start": round(max(seg["start"] - 0.02, part[0][1] - 0.04), 3),
                    "end": round(seg["end"] if is_last_in_seg else part[-1][2], 3),
                    "seg": si,
                })
    for i, l in enumerate(lines):
        l["id"] = i + 1
        l["deleted"] = False
        l["reason"] = ""
    return lines


def flag_problems(lines, remove_fillers=True):
    """标出语气词和疑似重复（口误重说）的行，默认删除，用户可在校对页恢复。"""
    for l in lines:
        if remove_fillers and l["text"] and all(ch in FILLERS for ch in l["text"]):
            l["deleted"], l["reason"] = True, "语气词"
    live = [l for l in lines if not l["deleted"]]
    for a, b in zip(live, live[1:]):
        ta, tb = a["text"], b["text"]
        if len(ta) < 3 or b["start"] - a["end"] > 6:
            continue
        head = tb[:len(ta) + 2]
        ratio = difflib.SequenceMatcher(None, ta, head).ratio()
        if ratio >= 0.75 and len(tb) >= len(ta) - 1:
            a["deleted"], a["reason"] = True, "疑似重说"
    return lines


# ---------------------------------------------------------------- 关键词高亮
HL_POS = ("ns", "nr", "nrt", "nt", "nz", "n", "vn", "m", "eng")
GROUP_POS = ("ns", "nr", "nrt", "nt", "nz", "n", "m", "q", "eng")


def auto_highlight(lines, ratio=0.75):
    full = "".join(l["text"] for l in lines if not l["deleted"])
    if not full:
        return lines
    k = max(6, len(lines))
    weights = dict(jieba.analyse.extract_tags(full, topK=k, withWeight=True, allowPOS=HL_POS))
    cands, recent = [], []
    for l in lines:
        if l.get("deleted"):
            cands.append((0.0, None))
            continue
        words = [(w.word, w.flag) for w in pseg.cut(l["text"])]
        best, bi = 0.0, -1
        for i, (w, f) in enumerate(words):
            sc = weights.get(w, 0.0)
            if f in ("ns", "nt", "nrt", "nz") and len(w) >= 2:
                sc += 0.3  # 地名、机构名优先
            if w in recent:
                sc *= 0.4  # 避免连续几行高亮同一个词
            if re.search(r"[0-9]", w) or (f == "m" and len(w) >= 2 and re.fullmatch(r"[零一二三四五六七八九十百千万亿两半几%]+", w)):
                sc = max(sc, 1.2)  # 数字很适合高亮
            if w in ("一起", "一下", "一些", "一个", "一点", "一直", "一定", "一样", "一般", "大家", "我们", "你们"):
                sc = 0
            if len(w) < 2 and f != "m":
                sc = 0
            if sc > best:
                best, bi = sc, i
        span = None
        if bi >= 0:
            a = b = bi
            while a - 1 >= 0 and words[a - 1][1] in GROUP_POS and len(words[a - 1][0]) >= 1:
                a -= 1
            while b + 1 < len(words) and words[b + 1][1] in GROUP_POS:
                b += 1
            if re.search(r"[0-9]$", words[b][0]) and b + 1 < len(words) and len(words[b + 1][0]) == 1:
                b += 1  # 300 + 位 / 个 / 人
            pre = "".join(w for w, _ in words[:a])
            mid = "".join(w for w, _ in words[a:b + 1])
            if len(mid) > 8:  # 过长就只高亮核心词
                pre = "".join(w for w, _ in words[:bi])
                mid = words[bi][0]
            span = (len(pre), len(pre) + len(mid))
        cands.append((best, span))
        recent = ([words[bi][0]] if bi >= 0 else []) + recent[:2]
    scores = sorted([c[0] for c in cands if c[0] > 0], reverse=True)
    cutoff = scores[int(len(scores) * ratio)] if len(scores) > 2 and int(len(scores) * ratio) < len(scores) else 0
    for l, (sc, span) in zip(lines, cands):
        if span and sc > 0 and sc >= cutoff and span[1] - span[0] < len(l["text"]):
            a, b = span
            l["text"] = l["text"][:a] + "[" + l["text"][a:b] + "]" + l["text"][b:]
    return lines


def parse_markup(text):
    """'将在[杭州阿里总部]' → [('将在', False), ('杭州阿里总部', True)]，兼容【】"""
    text = text.replace("【", "[").replace("】", "]")
    runs, buf, hl = [], "", False
    for ch in text:
        if ch in "[]":
            if buf:
                runs.append((buf, hl))
            buf, hl = "", ch == "["
        else:
            buf += ch
    if buf:
        runs.append((buf, hl))
    return runs


def plain(text):
    return "".join(t for t, _ in parse_markup(text))


# ---------------------------------------------------------------- 剪辑方案
def cut_plan(lines, duration, pace="normal", fps=30.0):
    """根据保留的字幕行计算要保留的时间段（对齐到帧，保证音画同步）。
    返回 (intervals, subtitle_timeline)。"""
    pre, post = PACES.get(pace, PACES["normal"])
    keep = [l for l in lines if not l["deleted"] and plain(l["text"]).strip()]
    ordered = sorted(lines, key=lambda x: x["start"])
    pos = {id(l): i for i, l in enumerate(ordered)}
    raw = []
    for l in keep:
        a, b = l["start"] - pre, l["end"] + post
        i = pos[id(l)]
        prv = ordered[i - 1] if i > 0 else None
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        # 紧挨着被删掉的话（重说、卡顿）时，剪辑点贴紧字边界，不留前一遍的尾音
        if prv and prv["deleted"] and l["start"] - prv["end"] < pre + 0.15:
            a = max(a, l["start"])
        if nxt and nxt["deleted"] and nxt["start"] - l["end"] < post + 0.15:
            b = min(b, nxt["start"] + 0.02)
        raw.append([max(0.0, a), min(duration, max(b, a + 0.05))])
    raw.sort()
    merged = []
    for a, b in raw:
        if merged and a <= merged[-1][1] + 0.04:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    # 对齐到帧网格
    iv = []
    for a, b in merged:
        a2 = np.ceil(a * fps - 1e-6) / fps
        b2 = np.ceil(b * fps - 1e-6) / fps
        if b2 - a2 >= 1.0 / fps:
            iv.append((round(float(a2), 4), round(float(b2), 4)))

    def to_out(t):
        acc = 0.0
        for a, b in iv:
            if t < a:
                return acc
            if t <= b:
                return acc + (t - a)
            acc += b - a
        return acc

    total = sum(b - a for a, b in iv)
    timeline = []
    for l in keep:
        timeline.append({"id": l["id"], "text": l["text"],
                         "start": to_out(l["start"]), "end": to_out(l["end"])})
    # 字幕连续显示：间隙很短时延长上一句，避免闪烁
    for i, t in enumerate(timeline):
        nxt = timeline[i + 1]["start"] if i + 1 < len(timeline) else total
        if nxt - t["end"] < 0.6:
            t["end"] = nxt
        t["end"] = min(t["end"], total)
    timeline = [t for t in timeline if t["end"] - t["start"] > 0.05]
    return iv, timeline, total


def split_timeline(timeline, limit=14):
    """导出前的保险：超过 limit 字的字幕按字数比例拆成几句，时间也按比例分配。"""
    out = []
    for t in timeline:
        runs = parse_markup(t["text"])
        chars = [(ch, hl) for txt, hl in runs for ch in txt]
        n = len(chars)
        if n <= limit + 2:
            out.append(t)
            continue
        dur = t["end"] - t["start"]
        pieces = _split_long([(ch, 0, 0) for ch, _ in chars], limit)  # 按词边界断开
        bounds, pos = [], 0
        for pc in pieces:
            bounds.append((pos, pos + len(pc)))
            pos += len(pc)
        k = len(bounds)
        for i, (s0, s1) in enumerate(bounds):
            part = chars[s0:s1]
            if not part:
                continue
            txt, cur = "", None
            for ch, hl in part:
                if hl != cur:
                    if cur:
                        txt += "]"
                    if hl:
                        txt += "["
                    cur = hl
                txt += ch
            if cur:
                txt += "]"
            a = t["start"] + dur * s0 / n
            b = t["start"] + dur * s1 / n
            out.append(dict(t, text=txt, start=a, end=b, punct=t.get("punct", "") if i == k - 1 else ""))
    return out


def refine_lines(audio, lines, sr=SR):
    """气口处理：按音量找到每句真正开口和收声的位置，把句首的吸气声、句尾的拖音空白剪掉。
    只会往里收、不会切进说话内容。"""
    hop = int(sr * 0.01)
    n = len(audio) // hop
    if n < 10:
        return lines
    fr = audio[:n * hop].reshape(n, hop)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-7)
    keep = [l for l in lines if not l["deleted"]]
    idx = np.concatenate([np.arange(int(l["start"] * 100), int(l["end"] * 100)) for l in keep]) if keep else []
    idx = [i for i in idx if 0 <= i < n]
    if not len(idx):
        return lines
    level = float(np.percentile(db[idx], 70))   # 说话时的典型音量
    thr = level - 18                             # 吸气声、底噪一般比说话低 20dB 以上
    for l in keep:
        a, b = int(l["start"] * 100), int(l["end"] * 100)
        a0, b0 = max(0, a), min(n, b)
        if b0 - a0 < 15:
            continue
        seg = db[a0:min(b0, a0 + 30)]               # 句首 0.3 秒内找开口点
        on = np.argmax(seg > thr) if (seg > thr).any() else None
        seg2 = db[max(a0, b0 - 35):b0]              # 句尾 0.35 秒内找收声点
        off = (len(seg2) - 1 - np.argmax((seg2 > thr)[::-1])) if (seg2 > thr).any() else None
        ns = l["start"] if on is None else max(l["start"], (a0 + on) / 100 - 0.03)
        ne = l["end"] if off is None else min(l["end"], (max(a0, b0 - 35) + off) / 100 + 0.08)
        if ne - ns >= 0.2:
            l.setdefault("start0", l["start"])
            l.setdefault("end0", l["end"])
            l["start"], l["end"] = round(float(ns), 3), round(float(ne), 3)
    return lines
