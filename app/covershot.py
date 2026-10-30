"""封面选帧：在“值得做封面的时刻”附近抽几帧，挑最清晰、曝光最好、画面最饱满的一帧。

时刻来源：AI 挑出的句子（有场景、有情绪、说出核心价值的地方）> 带高亮关键词或数字的句子 > 均匀分布的句子。
没有 AI 时也能用：只是少了“内容上最合适”的判断，仍会避开模糊、过暗、过曝的帧。"""
import os
import re
import subprocess

import numpy as np

import media
from engine import plain

NUM_RE = re.compile(r"\d|[一二三四五六七八九十百千万]+[个位年天倍%]")


def _gray(path, t, w=320):
    """抽一帧缩小的灰度图 + 彩色图（用于打分）。"""
    r = subprocess.run([media.FFMPEG, "-hide_banner", "-nostdin", "-ss", f"{max(0, t):.3f}", "-i", path,
                        "-frames:v", "1", "-vf", f"scale={w}:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, **media._NOWIN)
    return r.stdout


def score_frame(rgb):
    """清晰度（拉普拉斯方差）+ 曝光 + 对比度 + 色彩，越高越好。"""
    g = rgb.mean(axis=2)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    h, w = g.shape
    center = lap[h // 6: h * 5 // 6, w // 6: w * 5 // 6]  # 人通常在中间，中间清晰更重要
    sharp = np.log1p(center.var()) / 7.0
    mean = g.mean() / 255
    expo = 1 - min(1, abs(mean - 0.5) * 2.2)
    clip = ((g < 8).mean() + (g > 247).mean())
    contrast = min(1, g.std() / 64)
    sat = (rgb.max(axis=2) - rgb.min(axis=2)).mean() / 255
    return sharp * 0.5 + expo * 0.22 + contrast * 0.15 + min(sat * 2.5, 1) * 0.08 - clip * 0.5


def candidate_windows(lines, moments, duration):
    kept = [l for l in lines if not l.get("deleted")]
    wins = []
    for i, m in enumerate(moments or []):
        wins.append((m["start"], m["end"], 0.25 - i * 0.05, m.get("why") or "AI 推荐"))
    for l in kept[1:]:
        t = plain(l["text"])
        if "[" in l["text"] or NUM_RE.search(t):
            wins.append((l["start"], l["end"], 0.06, "关键信息"))
    if kept:
        step = max(1, len(kept) // 5)
        for l in kept[::step]:
            wins.append((l["start"], l["end"], 0.0, ""))
        wins.append((kept[0]["start"], kept[0]["end"], -0.02, "开头"))
    else:
        wins.append((0.5, min(duration, 3.0), 0.0, ""))
    seen, out = set(), []
    for a, b, bonus, why in wins:
        k = round(a, 1)
        if k in seen:
            continue
        seen.add(k)
        out.append((max(0.0, a), min(duration - 0.05, max(a + 0.4, b)), bonus, why))
    return out[:12]


def pick(src, info, lines, moments, workdir, n_keep=6, progress=lambda f: None):
    """返回按得分排序的候选：[{t, score, why, thumb}]，thumb 是保存的小图路径。"""
    os.makedirs(workdir, exist_ok=True)
    for f in os.listdir(workdir):
        if f.startswith("cand_"):
            os.remove(os.path.join(workdir, f))
    W = 320
    H = int(round(W * info["height"] / info["width"] / 2)) * 2
    wins = candidate_windows(lines, moments, info["duration"])
    res = []
    for wi, (a, b, bonus, why) in enumerate(wins):
        best = None
        for k in range(4):  # 每个时刻抽 4 帧，避开眨眼、动作模糊
            t = a + 0.15 + (b - a - 0.3) * k / 3 if b - a > 0.6 else a + (b - a) * k / 4
            raw = _gray(src, t, W)
            if len(raw) < W * H * 3:
                continue
            rgb = np.frombuffer(raw[:W * H * 3], np.uint8).reshape(H, W, 3).astype(np.float32)
            sc = score_frame(rgb) + bonus
            if best is None or sc > best[0]:
                best = (sc, t)
        if best:
            res.append({"t": round(best[1], 2), "score": round(float(best[0]), 3), "why": why})
        progress((wi + 1) / len(wins))
    res.sort(key=lambda x: -x["score"])
    out = []
    for i, c in enumerate(res[:n_keep]):
        p = os.path.join(workdir, f"cand_{i}.jpg")
        media.grab_frame(src, c["t"], p, width=360)
        out.append(dict(c, thumb=os.path.basename(p)))
    return out
