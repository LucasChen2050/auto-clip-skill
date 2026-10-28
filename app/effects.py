"""动效编排：挑出“重点大字”出现的时刻，并生成对应的音效轨。"""
import os
import re
import wave

import numpy as np

from engine import parse_markup, pseg

SFX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sfx")
NUM_RE = re.compile(r"[0-9０-９]|[二三四五六七八九十百千万两][一二三四五六七八九十百千万两]*[个位家场名天点号月年%倍万亿人]")
PREFIX_RE = re.compile(r"^(一场|这场|那场|一次|这次|本次|一个|这个|一家|一位|这些|我们的|我们)")
EVENT_WORDS = ("峰会", "大会", "论坛", "交流会", "分享会", "沙龙", "研讨会", "博览会", "展会", "发布会", "训练营", "对接会")
DATE_RE = re.compile(r"[0-9一二三四五六七八九十]{1,3}月[0-9一二三四五六七八九十]{1,3}[日号]|周[一二三四五六日天]|下周|明天|今晚")


def _score(word):
    if len(word) < 2 or len(word) > 9:
        return 0
    s = 1.0
    if NUM_RE.search(word):
        s += 3
    if DATE_RE.search(word):
        s += 2.5
    if any(w in word for w in EVENT_WORDS):
        s += 2.5
    for w in pseg.cut(word):
        if w.flag in ("ns", "nt", "nz") and len(w.word) >= 2:
            s += 1.2
            break
    return s


def pick_bigwords(timeline, total, enabled=True, avoid_until=0.0):
    """从带高亮的字幕里挑出最值得放大的几个词。频率：大约每 6~8 秒最多一个，全片最多 6 个。"""
    if not enabled or total < 4:
        return []
    cands = []
    for t in timeline:
        for txt, hl in parse_markup(t["text"]):
            txt = txt.strip()
            if hl and txt and len(PREFIX_RE.sub("", txt)) >= 2:
                txt = PREFIX_RE.sub("", txt)
                sc = _score(txt)
                if sc > 0:
                    cands.append((sc, t["start"], t["end"], txt))
                break
    limit = max(1, min(6, int(round(total / 7))))
    chosen = []
    for sc, a, b, txt in sorted(cands, key=lambda c: (-c[0], c[1])):
        if len(chosen) >= limit:
            break
        if a < max(0.6, avoid_until) or a > total - 1.0:  # 开头的主讲人介绍卡显示期间不出大字
            continue
        if any(abs(a - c["start"]) < 4.5 for c in chosen) or any(txt == c["text"] for c in chosen):
            continue
        dur = max(1.0, min(1.6, b - a + 0.3))
        chosen.append({"text": txt, "start": round(a, 3), "dur": round(min(dur, total - a), 3),
                       "sfx": "ding" if NUM_RE.search(txt) or DATE_RE.search(txt) else "pop"})
    return sorted(chosen, key=lambda c: c["start"])


def _load(name):
    p = os.path.join(SFX_DIR, name + ".wav")
    if not os.path.exists(p):
        return None
    with wave.open(p, "rb") as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        x = x.reshape(-1, w.getnchannels())
    return x, sr


def sfx_track(events, total, out_path, intro=True):
    """把音效按时间点铺成一条和成片等长的音轨。没有音效时返回 None。"""
    hits = [(e["sfx"], e["start"]) for e in events]
    if intro:
        hits.append(("whoosh", 0.0))
    if not hits:
        return None
    sr = 44100
    buf = np.zeros((int((total + 1) * sr), 2), np.float32)
    for name, t in hits:
        s = _load(name)
        if not s:
            continue
        x, _ = s
        i = int(max(0, t - 0.03) * sr)
        n = min(len(x), len(buf) - i)
        if n > 0:
            buf[i:i + n] += x[:n, :2] if x.shape[1] >= 2 else np.repeat(x[:n], 2, axis=1)
    peak = np.abs(buf).max()
    if peak < 1e-6:
        return None
    buf = np.clip(buf, -0.98, 0.98)
    with wave.open(out_path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((buf * 32767).astype(np.int16).tobytes())
    return out_path
