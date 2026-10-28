"""画面渲染：版式、常驻标题、字幕条、封面、最终合成导出。样式全部来自模板 JSON。

预览图（compose_frame）和导出（export_video）用同一套几何计算，保证所见即所得。"""
import json
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import media
from engine import parse_markup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(ROOT, "fonts")
TPL_DIR = os.path.join(ROOT, "templates")

_font_cache = {}


def font(name, size):
    key = (name, max(8, int(size)))
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(os.path.join(FONT_DIR, name), key[1])
    return _font_cache[key]


def hex_rgba(h, alpha=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(round(alpha * 255)))


# ---------------------------------------------------------------- 模板
def list_templates():
    out = []
    for d in sorted(os.listdir(TPL_DIR)):
        p = os.path.join(TPL_DIR, d, "template.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = json.load(f)
            t["id"] = d
            t["has_preview"] = os.path.exists(os.path.join(TPL_DIR, d, "preview.jpg"))
            out.append(t)
    return out


def get_template(tid):
    tpls = list_templates()
    for t in tpls:
        if t["id"] == tid:
            return t
    return tpls[0]


def output_size(w, h, aspect="original", tpl=None):
    if tpl and tpl.get("layout", {}).get("type") == "letterbox" and aspect == "original":
        aspect = "9:16"  # 上下色带版式固定输出竖屏
    if aspect == "9:16":
        return 1080, 1920
    if aspect == "3:4":
        return 1080, 1440
    if aspect == "16:9":
        return 1920, 1080
    if aspect == "1:1":
        return 1080, 1080
    if h >= w:
        ow = 1080
        oh = int(round(ow * h / w / 2)) * 2
    else:
        ow = 1920
        oh = int(round(ow * h / w / 2)) * 2
    return ow, oh


def video_region(tpl, W, H, src_w, src_h):
    """视频画面在成片里的位置 (x, y, w, h)。全屏版式就是整个画面。"""
    lay = tpl.get("layout", {})
    if lay.get("type") != "letterbox" or W >= H:
        return 0, 0, W, H
    vw = W
    max_h = H * lay.get("video_h", 0.42)
    if src_w > src_h:  # 横屏素材：完整放进去
        vh = min(max_h * 1.25, vw * src_h / src_w)
    else:
        vh = max_h
    vh = int(vh) // 2 * 2
    vy = int(H * lay.get("video_cy", 0.5) - vh / 2) // 2 * 2
    return 0, max(0, vy), vw, vh


def _focus(tpl):
    return tpl.get("layout", {}).get("focus_y", 0.38)


# ---------------------------------------------------------------- 文字工具
HEAVIER = {"NotoSansSC-Medium.otf": "NotoSansSC-Black.otf", "NotoSansSC-Bold.otf": "NotoSansSC-Black.otf",
           "NotoSansSC-Black.otf": "NotoSansSC-Black.otf", "NotoSerifSC-Bold.otf": "NotoSerifSC-Bold.otf"}


def _run_fonts(runs, st, size):
    """每段文字用的字体：开启强调时，关键词用更粗的字、放大一号（默认 1.18 倍）。"""
    emph = st.get("emph", False) and st.get("highlight_style") != "box"
    out = []
    for t, hl in runs:
        if hl and emph:
            sz = size * st.get("emph_scale", 1.18)
            out.append((t, hl, font(st.get("emph_font", HEAVIER.get(st["font"], st["font"])), sz), sz))
        else:
            out.append((t, hl, font(st["font"], size), size))
    return out


def _runs_width(runs, f, st=None, size=0):
    if st is not None and size:
        w = sum(ff.getlength(t) for t, _, ff, _ in _run_fonts(runs, st, size))
    else:
        w = sum(f.getlength(t) for t, _ in runs)
    if st and st.get("highlight_style") == "box":
        w += sum(size * 0.24 for _, hl in runs if hl)  # 色块左右留白
    return w


def _runs_metrics(runs, st, size):
    """整行的上沿和下沿（以基础字号的基线为准），关键词放大后行高也跟着变。"""
    base_asc, _ = font(st["font"], size).getmetrics()
    top, bot = 0, 0
    for _, _, ff, _ in _run_fonts(runs, st, size):
        a, d = ff.getmetrics()
        top = max(top, a - base_asc)
        bot = max(bot, base_asc + d)
    return top, bot


def draw_runs(img, runs, f, x, y, st, size):
    """在 img 上从 (x, y) 开始画一行带高亮的文字（y 为基础字号的顶部）。支持描边、投影、高亮色块、关键词加粗放大。"""
    stroke_r = st.get("stroke", 0)
    sc = hex_rgba(st.get("stroke_color", "#000000"))
    rf = _run_fonts(runs, st, size)
    base_asc, _ = font(st["font"], size).getmetrics()
    if st.get("shadow"):
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(sh)
        xx = x
        for t, _, ff, sz in rf:
            yy = y + base_asc - ff.getmetrics()[0]
            sw = int(round(sz * stroke_r))
            d.text((xx + sz * 0.04, yy + sz * 0.06), t, font=ff, fill=(0, 0, 0, 160), stroke_width=sw,
                   stroke_fill=(0, 0, 0, 160))
            xx += ff.getlength(t)
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(max(1, size * 0.06))))
    d = ImageDraw.Draw(img)
    asc, desc = f.getmetrics()
    xx = x
    for t, hl, ff, sz in rf:
        w = ff.getlength(t)
        yy = y + base_asc - ff.getmetrics()[0]
        col = st["color"]
        if hl and st.get("highlight_style") == "box" and xx > x:
            xx += size * 0.12
        if hl:
            if st.get("highlight_style") == "box":
                p = size * 0.12
                d.rounded_rectangle([xx - p, y - p * 0.4, xx + w + p, y + asc + desc + p * 0.2],
                                    radius=size * 0.1, fill=hex_rgba(st.get("highlight_bg", "#FFE000")))
                col = st.get("highlight_text", "#000000")
                d.text((xx, yy), t, font=ff, fill=hex_rgba(col))
                xx += w + p
                continue
            col = st.get("highlight", col)
        d.text((xx, yy), t, font=ff, fill=hex_rgba(col), stroke_width=int(round(sz * stroke_r)), stroke_fill=sc)
        xx += w


# ---------------------------------------------------------------- 字幕
def _split_runs(runs):
    """把一行字（带高亮）从中间断成两行。"""
    chars = [(ch, hl) for t, hl in runs for ch in t]
    mid = (len(chars) + 1) // 2

    def pack(cs):
        out = []
        for ch, hl in cs:
            if out and out[-1][1] == hl:
                out[-1] = (out[-1][0] + ch, hl)
            else:
                out.append((ch, hl))
        return out
    return pack(chars[:mid]), pack(chars[mid:])


def _en_lines(en, W, H, st):
    """英文翻译行：白色细字，过长时折成两行，再长就缩小。返回 [(文字, 字体)]。"""
    size = st.get("en_size", 0.032) * min(W, H)
    fn = st.get("en_font", "NotoSansSC-Medium.otf")
    f = font(fn, size)
    max_w = W * 0.88
    lines = [en]
    if f.getlength(en) > max_w:
        words = en.split()
        best, cut = None, 1
        for k in range(1, len(words)):
            a, b = " ".join(words[:k]), " ".join(words[k:])
            m = max(f.getlength(a), f.getlength(b))
            if best is None or m < best:
                best, cut = m, k
        if len(words) > 1:
            lines = [" ".join(words[:cut]), " ".join(words[cut:])]
        widest = max(f.getlength(l) for l in lines)
        if widest > max_w:
            f = font(fn, size * max_w / widest)
    return [(l, f) for l in lines]


def subtitle_band(text, W, H, st, punct="", _runs=None, en=""):
    """渲染一行字幕到透明长条图上，返回 (图, 放置的 y)。太长会自动折成两行，而不是把字缩得很小。
    双语模板（bilingual）传入 en 时，在中文下方加一到两行英文。"""
    if en and st.get("bilingual"):
        zh, _ = subtitle_band(text, W, H, st, punct, _runs)
        lines = _en_lines(en, W, H, st)
        f0 = lines[0][1]
        asc, desc = f0.getmetrics()
        lh = int((asc + desc) * 1.08)
        top_gap = int(-zh.height * 0.12)
        img = Image.new("RGBA", (W, zh.height + top_gap + lh * len(lines) + int(lh * 0.3)), (0, 0, 0, 0))
        img.alpha_composite(zh, (0, 0))
        col = hex_rgba(st.get("en_color", "#FFFFFF"), st.get("en_opacity", 0.95))
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ds, d = ImageDraw.Draw(sh), ImageDraw.Draw(img)
        y = zh.height + top_gap
        pos = []
        for l, f in lines:
            x = (W - f.getlength(l)) / 2
            pos.append((x, y, l, f))
            ds.text((x + 2, y + 3), l, font=f, fill=(0, 0, 0, 170))
            y += lh
        img = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(max(1, asc * 0.12))), img)
        d = ImageDraw.Draw(img)
        for x, y, l, f in pos:
            d.text((x, y), l, font=f, fill=col)
        y0 = max(0, min(H - img.height, int(H * st["pos_y"] - zh.height / 2)))
        return img, y0
    base = min(W, H)
    size = st["size"] * base
    f = font(st["font"], size)
    runs = _runs if _runs is not None else parse_markup(text)
    if _runs is None and st.get("keep_punct") and punct and punct in "？！?!":
        runs.append((punct, False))
    total = _runs_width(runs, f, st, size)
    max_w = W * 0.9
    if _runs is None and total > max_w * 1.15 and sum(len(t) for t, _ in runs) >= 6:
        r1, r2 = _split_runs(runs)
        a, _ = subtitle_band("", W, H, st, "", r1)
        b, _ = subtitle_band("", W, H, st, "", r2)
        gap = int(size * 0.05)
        img = Image.new("RGBA", (W, a.height + b.height - int(size * 0.5) + gap), (0, 0, 0, 0))
        img.alpha_composite(a, (0, 0))
        img.alpha_composite(b, (0, a.height - int(size * 0.5) + gap))
        y = max(0, min(H - img.height, int(H * st["pos_y"] - img.height / 2)))
        return img, y
    if total > max_w:  # 太长就自动缩小
        size = size * max_w / total
        f = font(st["font"], size)
        total = _runs_width(runs, f, st, size)
    top, bot = _runs_metrics(runs, st, size)
    th = top + bot
    padx, pady = size * st.get("pad_x", 0.35), size * st.get("pad_y", 0.16)
    band_h = int(th + pady * 2 + size * 0.6)
    img = Image.new("RGBA", (W, band_h), (0, 0, 0, 0))
    bw, bh = total + padx * 2, th + pady * 2
    x0 = (W - bw) / 2
    y0 = (band_h - bh) / 2
    if st.get("box_opacity", 0) > 0:
        ImageDraw.Draw(img).rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=size * st.get("box_radius", 0.15),
                                              fill=hex_rgba(st.get("box_color", "#000000"), st["box_opacity"]))
    draw_runs(img, runs, f, x0 + padx, y0 + pady + top, st, size)
    y = int(H * st["pos_y"] - band_h / 2)
    y = max(0, min(H - band_h, y))
    return img, y


def _scaled(im, sc, alpha=1.0):
    """把整条图以中心为基准缩放后放回原尺寸（用于弹出动画），可同时调透明度。"""
    W, H = im.size
    if abs(sc - 1) > 1e-3:
        nw, nh = max(1, int(round(W * sc))), max(1, int(round(H * sc)))
        sm = im.resize((nw, nh), Image.BICUBIC)
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        # 放大时裁掉超出部分，缩小时居中贴回
        x, y = (W - nw) // 2, (H - nh) // 2
        cx, cy = max(0, -x), max(0, -y)
        part = sm.crop((cx, cy, cx + min(nw, W), cy + min(nh, H)))
        out.alpha_composite(part, (max(0, x), max(0, y)))
        im = out
    if alpha < 1:
        im = im.copy()
        im.putalpha(im.getchannel("A").point(lambda v: int(v * alpha)))
    return im


def _write_concat(entries, workdir, name):
    lst = os.path.join(workdir, name)
    with open(lst, "w", encoding="utf-8") as fp:
        fp.write("ffconcat version 1.0\n")
        for p, d in entries:
            fp.write(f"file '{os.path.basename(p)}'\nduration {max(d, 0.001):.4f}\n")
        fp.write(f"file '{os.path.basename(entries[-1][0])}'\n")
    return lst


POP = [(0.86, 0.6), (1.05, 1.0), (1.0, 1.0)]  # 字幕出现时的弹出：缩放、透明度（每帧 1/30 秒）


def render_subtitle_track(timeline, W, H, st, workdir, total, anim=False, fps=30):
    """把每句字幕渲染成 PNG，并写出 ffmpeg concat 列表。所有长条图同尺寸、同位置。anim=True 时每句带弹出动画。"""
    os.makedirs(workdir, exist_ok=True)
    imgs = [subtitle_band(t["text"], W, H, st, t.get("punct", ""), en=t.get("en", ""))[0] for t in timeline]
    if not imgs:
        return None, 0
    band_h = max(im.height for im in imgs)
    band_y = max(0, min(H - band_h, int(H * st["pos_y"] - band_h / 2)))
    top_align = bool(st.get("bilingual"))  # 双语：中文行位置固定，英文向下延伸
    if top_align:
        zh_h = subtitle_band("示例", W, H, st)[0].height
        band_y = max(0, min(H - band_h, int(H * st["pos_y"] - zh_h / 2)))
    blank = os.path.join(workdir, "blank.png")
    Image.new("RGBA", (W, band_h), (0, 0, 0, 0)).save(blank)
    entries, cur = [], 0.0
    fd = 1.0 / fps
    for i, (t, im) in enumerate(zip(timeline, imgs)):
        if im.height != band_h:
            pad = Image.new("RGBA", (W, band_h), (0, 0, 0, 0))
            pad.paste(im, (0, 0 if top_align else (band_h - im.height) // 2))
            im = pad
        p = os.path.join(workdir, f"s{i:04d}.png")
        im.save(p)
        if t["start"] > cur + 0.001:
            entries.append((blank, t["start"] - cur))
        dur = t["end"] - max(cur, t["start"])
        # 紧接上一句（没有空白）时也弹，但更轻；太短的句子不加动画
        if anim and dur > fd * (len(POP) + 3):
            for k, (sc, al) in enumerate(POP):
                fp = os.path.join(workdir, f"s{i:04d}_{k}.png")
                _scaled(im, sc, al).save(fp)
                entries.append((fp, fd))
            dur -= fd * len(POP)
        entries.append((p, dur))
        cur = t["end"]
    if total > cur:
        entries.append((blank, total - cur))
    return _write_concat(entries, workdir, "subs.txt"), band_y


DEFAULT_BIGWORD = {"font": "NotoSansSC-Black.otf", "size": 0.13, "min_size": 0.08, "color": "#FFE14A",
                   "stroke": 0.1, "stroke_color": "#1D1B19", "shadow": True, "max_width": 0.86, "max_lines": 1,
                   "skew": 0.1, "center_y": 0.62}
BIG_POP = [(0.35, 0.5), (0.8, 1.0), (1.14, 1.0), (1.04, 1.0), (1.0, 1.0)]
BIG_OUT = [(1.03, 0.75), (1.06, 0.45), (1.1, 0.2)]


def render_bigword_track(events, W, H, style, workdir, total, fps=30):
    """重点大字：在画面中间弹出（放大→回弹→停留→淡出）。返回 (concat 列表, 放置的 y)。"""
    if not events:
        return None, 0
    os.makedirs(workdir, exist_ok=True)
    st = dict(DEFAULT_BIGWORD, **(style or {}))
    blocks = [_text_block(e["text"], W, st) for e in events]
    bh = max(b.height for b in blocks)
    band_h = int(bh * 1.3)
    by = max(0, min(H - band_h, int(H * st["center_y"] - band_h / 2)))
    blank = os.path.join(workdir, "bw_blank.png")
    Image.new("RGBA", (W, band_h), (0, 0, 0, 0)).save(blank)
    fd = 1.0 / fps
    entries, cur = [], 0.0
    for i, (e, blk) in enumerate(zip(events, blocks)):
        band = Image.new("RGBA", (W, band_h), (0, 0, 0, 0))
        band.alpha_composite(blk, ((W - blk.width) // 2, (band_h - blk.height) // 2))
        a = max(cur, e["start"])
        if a > cur + 0.001:
            entries.append((blank, a - cur))
        frames = len(BIG_POP) + len(BIG_OUT)
        hold = e["dur"] - frames * fd
        if hold < fd:
            continue
        for k, (sc, al) in enumerate(BIG_POP):
            fp = os.path.join(workdir, f"bw{i:02d}_in{k}.png")
            _scaled(band, sc, al).save(fp)
            entries.append((fp, fd))
        hp = os.path.join(workdir, f"bw{i:02d}.png")
        band.save(hp)
        entries.append((hp, hold))
        for k, (sc, al) in enumerate(BIG_OUT):
            fp = os.path.join(workdir, f"bw{i:02d}_out{k}.png")
            _scaled(band, sc, al).save(fp)
            entries.append((fp, fd))
        cur = a + e["dur"]
    if not entries:
        return None, 0
    if total > cur:
        entries.append((blank, total - cur))
    return _write_concat(entries, workdir, "bigwords.txt"), by


# ---------------------------------------------------------------- 主讲人介绍卡
DEFAULT_SPEAKER = {"x": 0.07, "y": 0.63, "name_size": 0.064, "line_size": 0.034,
                   "name_font": "NotoSansSC-Black.otf", "line_font": "NotoSansSC-Bold.otf",
                   "name_bg": "#FFD43B", "name_color": "#151515",
                   "line_bg": "#000000", "line_bg_opacity": 0.6, "line_color": "#FFFFFF", "start": 0.3, "dur": 4.2}


def speaker_card(name, titles, W, H, style=None):
    """开头的主讲人介绍：黄底黑字的姓名 + 2~3 行深色底白字头衔，左对齐。返回 (图, (x, y))。"""
    st = dict(DEFAULT_SPEAKER, **(style or {}))
    titles = [t.strip() for t in (titles or []) if t and t.strip()][:3]
    name = (name or "").strip()
    if not name and not titles:
        return None, (0, 0)
    base = min(W, H)
    max_w = W * (1 - st["x"] * 2)
    blocks = []

    def block(text, fn, size, fg, bg, op, padx, pady):
        f = font(fn, size)
        w = f.getlength(text)
        if w + padx * 2 > max_w:
            size *= (max_w - padx * 2) / w
            f = font(fn, size)
            w = f.getlength(text)
        bb = f.getbbox(text)
        th = bb[3] - bb[1]
        im = Image.new("RGBA", (int(w + padx * 2), int(th + pady * 2)), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, im.width, im.height], fill=hex_rgba(bg, op))
        d.text((padx, pady - bb[1]), text, font=f, fill=hex_rgba(fg))
        return im

    if name:
        ns = st["name_size"] * base
        blocks.append(block(name, st["name_font"], ns, st["name_color"], st["name_bg"], 1.0, ns * 0.38, ns * 0.24))
    for t in titles:
        ls = st["line_size"] * base
        blocks.append(block(t, st["line_font"], ls, st["line_color"], st["line_bg"], st["line_bg_opacity"],
                            ls * 0.45, ls * 0.32))
    gap = int(base * 0.008)
    cw = max(b.width for b in blocks)
    ch = sum(b.height for b in blocks) + gap * (len(blocks) - 1)
    pad = int(base * 0.02)
    card = Image.new("RGBA", (cw + pad * 2, ch + pad * 2), (0, 0, 0, 0))
    y = pad
    for i, b in enumerate(blocks):
        if i == 1 and name:
            y += gap  # 姓名和头衔之间多留一点
        card.alpha_composite(b, (pad, y))
        y += b.height + gap
    sh = Image.new("RGBA", card.size, (0, 0, 0, 0))
    sh.putalpha(card.getchannel("A").point(lambda v: int(v * 0.35)))
    sh = sh.filter(ImageFilter.GaussianBlur(pad * 0.5))
    out = Image.new("RGBA", card.size, (0, 0, 0, 0))
    out.alpha_composite(sh, (0, 0))
    out.alpha_composite(card)
    x = int(W * st["x"]) - pad
    y = int(H * st["y"] - out.height / 2)
    return out, (max(0, x), max(0, min(H - out.height, y)))


def render_speaker_track(name, titles, W, H, style, workdir, total, fps=30):
    """主讲人介绍卡：开场 0.3 秒从左侧滑入，停留约 4 秒后淡出。返回 (concat 列表, x, y)。"""
    st = dict(DEFAULT_SPEAKER, **(style or {}))
    card, (x, y) = speaker_card(name, titles, W, H, st)
    if card is None:
        return None, 0, 0
    os.makedirs(workdir, exist_ok=True)
    start = st["start"]
    dur = min(st["dur"], max(0.0, total - start - 0.3))
    if dur < 1.5:
        return None, 0, 0
    cw, chh = card.size
    pad_l = x  # 画布从左边缘开始，方便做滑入
    band = (pad_l + cw, chh)
    fd = 1.0 / fps
    blank = os.path.join(workdir, "sp_blank.png")
    Image.new("RGBA", band, (0, 0, 0, 0)).save(blank)
    entries = [(blank, start)]
    n_in, n_out = 9, 7
    for k in range(n_in):  # 先慢后快地滑入 + 渐显
        p = (k + 1) / n_in
        e = 1 - (1 - p) ** 3
        fr = Image.new("RGBA", band, (0, 0, 0, 0))
        im = _scaled(card, 1.0, min(1.0, 0.2 + e))
        xx = int(pad_l - (1 - e) * cw * 0.35)
        if xx < 0:
            im, xx = im.crop((-xx, 0, cw, chh)), 0
        fr.alpha_composite(im, (xx, 0))
        fp = os.path.join(workdir, f"sp_in{k}.png")
        fr.save(fp)
        entries.append((fp, fd))
    hold = Image.new("RGBA", band, (0, 0, 0, 0))
    hold.alpha_composite(card, (pad_l, 0))
    hp = os.path.join(workdir, "sp_hold.png")
    hold.save(hp)
    entries.append((hp, dur - (n_in + n_out) * fd))
    for k in range(n_out):
        fp = os.path.join(workdir, f"sp_out{k}.png")
        _scaled(hold, 1.0, 1 - (k + 1) / (n_out + 1)).save(fp)
        entries.append((fp, fd))
    rest = total - start - dur
    if rest > 0:
        entries.append((blank, rest))
    return _write_concat(entries, workdir, "speaker.txt"), 0, y


# ---------------------------------------------------------------- 常驻层（色带、渐变、顶部标题、IP 名牌）
def _gradient(W, H, spec):
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    h = int(H * spec.get("height", 0.3))
    col = hex_rgba(spec.get("color", "#000000"))
    op = spec.get("opacity", 0.5)
    d = ImageDraw.Draw(g)
    for i in range(h):
        a = op * (1 - i / h) ** 1.6
        y = H - 1 - i if spec.get("pos", "bottom") == "bottom" else i
        d.line([(0, y), (W, y)], fill=col[:3] + (int(255 * a),))
    return g


def _text_lines_block(lines, W, H, st):
    """常驻标题：最多两行，每行可单独设色；可带底色块。返回 (图层, y)。"""
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    size0 = st["size"] * W
    rendered = []
    for i, text in enumerate(lines):
        if not text:
            continue
        size = size0 * (st.get("line2_scale", 0.85) if i == 1 else 1.0)
        f = font(st["font"], size)
        runs = parse_markup(text)
        if i == 1 and not any(hl for _, hl in runs):
            runs = [(t, True) for t, _ in runs]  # 第二行整行用强调色
        w = _runs_width(runs, f, st, size)
        max_w = W * st.get("max_width", 0.9)
        if w > max_w:
            size *= max_w / w
            f = font(st["font"], size)
            w = _runs_width(runs, f, st, size)
        asc, desc = f.getmetrics()
        rendered.append((runs, f, size, w, asc + desc))
    if not rendered:
        return out, 0
    gap = size0 * 0.18
    total_h = sum(r[4] for r in rendered) + gap * (len(rendered) - 1)
    cy = H * st.get("center_y", 0.1)
    y = cy - total_h / 2
    if st.get("box_opacity", 0) > 0:
        bw = max(r[3] for r in rendered) + size0 * 0.8
        d = ImageDraw.Draw(out)
        d.rounded_rectangle([(W - bw) / 2, y - size0 * 0.25, (W + bw) / 2, y + total_h + size0 * 0.25],
                            radius=size0 * st.get("box_radius", 0.12),
                            fill=hex_rgba(st.get("box_color", "#000000"), st["box_opacity"]))
    for runs, f, size, w, lh in rendered:
        draw_runs(out, runs, f, (W - w) / 2, y, st, size)
        y += lh + gap
    return out, 0


def _ip_tag(text, W, H, st):
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not text:
        return out
    size = st.get("size", 0.032) * min(W, H)
    f = font(st.get("font", "NotoSansSC-Medium.otf"), size)
    w = f.getlength(text)
    asc, desc = f.getmetrics()
    padx, pady = size * 0.6, size * 0.3
    bw, bh = w + padx * 2, asc + desc + pady * 2
    pos = st.get("pos", "top_left")
    x = {"top_left": W * 0.05, "bottom_left": W * 0.05, "top_right": W * 0.95 - bw}.get(pos, (W - bw) / 2)
    y = H * st.get("y", 0.05)
    d = ImageDraw.Draw(out)
    if st.get("bg_opacity", 0) > 0:
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=bh / 2,
                            fill=hex_rgba(st.get("bg", "#000000"), st["bg_opacity"]))
    d.text((x + padx, y + pady), text, font=f, fill=hex_rgba(st.get("color", "#FFFFFF"), st.get("opacity", 0.95)))
    return out


def static_overlay(tpl, W, H, src_w, src_h, title="", subtitle="", ip_name=""):
    """整条视频都显示的一层：色带背景、渐变、顶部常驻标题、IP 名牌。没有内容时返回 None。"""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    used = False
    lay = tpl.get("layout", {})
    vx, vy, vw, vh = video_region(tpl, W, H, src_w, src_h)
    if (vw, vh) != (W, H):
        bg = hex_rgba(lay.get("bg", "#000000"))
        bg2 = hex_rgba(lay.get("bg2", lay.get("bg", "#000000")))
        d = ImageDraw.Draw(img)
        for yy in list(range(0, vy)) + list(range(vy + vh, H)):
            k = yy / max(1, H - 1)
            c = tuple(int(bg[i] * (1 - k) + bg2[i] * k) for i in range(3)) + (255,)
            d.line([(0, yy), (W, yy)], fill=c)
        used = True
    for g in tpl.get("gradients", []):
        img.alpha_composite(_gradient(W, H, g))
        used = True
    tt = tpl.get("top_title")
    if tt and tt.get("enabled") and (title or subtitle):
        layer, _ = _text_lines_block([title.strip(), subtitle.strip()], W, H, tt)
        img.alpha_composite(layer)
        used = True
    tag = tpl.get("ip_tag")
    if tag and ip_name:
        img.alpha_composite(_ip_tag(ip_name, W, H, tag))
        used = True
    return img if used else None


def compose_frame(frame, tpl, W, H, src_w, src_h, sub_text="", punct="", title="", subtitle="", ip_name="",
                  sub_en="", speaker=None):
    """生成某一帧在成片中的样子（预览用），与导出的合成逻辑一致。"""
    vx, vy, vw, vh = video_region(tpl, W, H, src_w, src_h)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    canvas.paste(_fit_cover(frame.convert("RGB"), vw, vh, _focus(tpl)), (vx, vy))
    ov = static_overlay(tpl, W, H, src_w, src_h, title, subtitle, ip_name)
    if ov:
        canvas.alpha_composite(ov)
    if speaker:
        card, (cx, cy) = speaker_card(speaker[0], speaker[1], W, H, tpl.get("speaker_card"))
        if card:
            canvas.alpha_composite(card, (cx, cy))
    if sub_text:
        band, y = subtitle_band(sub_text, W, H, tpl["subtitle"], punct, en=sub_en)
        canvas.alpha_composite(band, (0, y))
    return canvas.convert("RGB")


# ---------------------------------------------------------------- 封面
def _fit_cover(frame, W, H, focus_y=0.4, zoom=1.0):
    fw, fh = frame.size
    s = max(W / fw, H / fh) * max(1.0, zoom)
    nw, nh = int(math.ceil(fw * s)), int(math.ceil(fh * s))
    fr = frame.resize((nw, nh), Image.LANCZOS)
    x = (nw - W) // 2
    y = int(max(0, min(nh - H, (nh - H) * focus_y)))
    return fr.crop((x, y, x + W, y + H))


def _wrap(text, f, max_w, max_lines=2):
    if f.getlength(text) <= max_w or max_lines == 1:
        return [text]
    cut = len(text) // 2 + (len(text) % 2)
    return [text[:cut], text[cut:]]


def _text_block(text, W, st):
    """渲染一个封面文字块（描边、立体厚度、倾斜、投影、底色块）到透明图层。"""
    max_w = W * st.get("max_width", 0.9)
    size = st["size"] * W
    f = font(st["font"], size)
    one = f.getlength(text)
    min_size = st.get("min_size", st["size"] * 0.7) * W
    if one <= max_w or size * max_w / one >= min_size or st.get("max_lines", 2) == 1:
        lines = [text]
    else:
        lines = _wrap(text, f, 0, st.get("max_lines", 2))
    widest = max(f.getlength(l) for l in lines)
    if widest > max_w:
        size *= max_w / widest
        f = font(st["font"], size)
    stroke = int(round(size * st.get("stroke", 0.08)))
    asc, desc = f.getmetrics()
    lh = (asc + desc) * st.get("line_height", 1.08)
    pad = stroke + int(size * 0.3)
    bw = int(max(f.getlength(l) for l in lines) + pad * 2 + size * abs(st.get("skew", 0)) + 4)
    bh = int(lh * len(lines) + pad * 2)
    layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))

    def draw(dr, dx, dy, fill, sw, sf):
        for i, l in enumerate(lines):
            lw = f.getlength(l)
            dr.text(((bw - lw) / 2 + dx, pad + i * lh + dy), l, font=f, fill=fill, stroke_width=sw, stroke_fill=sf)

    if st.get("box"):
        bx = st["box"]
        ImageDraw.Draw(layer).rounded_rectangle([pad * 0.3, pad * 0.45, bw - pad * 0.3, bh - pad * 0.25],
                                                radius=size * bx.get("radius", 0.12),
                                                fill=hex_rgba(bx.get("color", "#000000"), bx.get("opacity", 0.9)))
    if st.get("shadow", True):
        sh = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        draw(ImageDraw.Draw(sh), size * 0.05, size * 0.07, (0, 0, 0, 140), stroke, (0, 0, 0, 140))
        layer = Image.alpha_composite(layer, sh.filter(ImageFilter.GaussianBlur(size * 0.06)))
    d = ImageDraw.Draw(layer)
    ex = st.get("extrude")
    if ex:
        exc = hex_rgba(ex.get("color", st.get("stroke_color", "#000000")))
        steps = max(1, int(size * ex.get("depth", 0.06)))
        for k in range(steps, 0, -1):
            draw(d, k * ex.get("dx", 0.6), k * ex.get("dy", 1.0), exc, stroke, exc)
    draw(d, 0, 0, hex_rgba(st["color"]), stroke, hex_rgba(st.get("stroke_color", "#FFFFFF")))
    skew = st.get("skew", 0)
    if skew:
        layer = layer.transform(layer.size, Image.AFFINE, (1, skew, -skew * bh / 2, 0, 1, 0), resample=Image.BICUBIC)
    return layer


def render_cover(frame_path, title, subtitle, tpl, W, H, out_path, zoom=1.0, ip_name="", src_size=None):
    cv = tpl["cover"]
    frame = Image.open(frame_path).convert("RGB")
    sw, sh = src_size or frame.size
    if cv.get("use_layout"):  # 封面直接沿用视频版式（如上下色带 + 顶部标题）
        vx, vy, vw, vh = video_region(tpl, W, H, sw, sh)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        img.paste(_fit_cover(frame, vw, vh, _focus(tpl), zoom), (vx, vy))
        ov = static_overlay(tpl, W, H, sw, sh, title, subtitle, ip_name if cv.get("show_ip", True) else "")
        if ov:
            img.alpha_composite(ov)
        img.convert("RGB").save(out_path, quality=92)
        return out_path
    img = _fit_cover(frame, W, H, cv.get("focus_y", 0.4), zoom=zoom).convert("RGBA")
    if cv.get("darken", 0) > 0:
        img = Image.alpha_composite(img, Image.new("RGBA", img.size, (0, 0, 0, int(255 * cv["darken"]))))
    for g in cv.get("gradients", []):
        img.alpha_composite(_gradient(W, H, g))
    for key, text in (("title", title), ("subtitle", subtitle)):
        if not text or not text.strip():
            continue
        st = cv[key]
        blk = _text_block(text.strip(), W, st)
        x = (W - blk.width) // 2
        if "top" in st:
            y = int(H * st["top"] - blk.height * 0.18)
        else:
            y = int(H * st["center_y"] - blk.height / 2)
        y = max(-int(blk.height * 0.15), min(H - blk.height, y))
        img.alpha_composite(blk, (x, max(0, y)))
    if cv.get("show_ip") and ip_name and tpl.get("ip_tag"):
        img.alpha_composite(_ip_tag(ip_name, W, H, tpl["ip_tag"]))
    img.convert("RGB").save(out_path, quality=92)
    return out_path


# ---------------------------------------------------------------- 导出
def _fmt_srt(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(timeline, path):
    from engine import plain
    with open(path, "w", encoding="utf-8") as f:
        for i, t in enumerate(timeline, 1):
            en = ("\n" + t["en"]) if t.get("en") else ""
            f.write(f"{i}\n{_fmt_srt(t['start'])} --> {_fmt_srt(t['end'])}\n{plain(t['text'])}{en}\n\n")


ENHANCE = "hqdn3d=1.2:1.2:5:5,cas=0.5"          # 轻降噪 + 自适应锐化
DENOISE_AUDIO = "highpass=f=70,afftdn=nr=12:nf=-30:tn=1"  # 去低频嗡声 + 跟踪式降噪


STAB = {  # 防抖强度：smoothing 越大越接近三脚架（单位：帧）
    "gentle": "smoothing=12",
    "standard": "smoothing=30",
    "strong": "smoothing=90",
}


STAB_NAMES = {"auto": "自动", "gentle": "轻柔", "standard": "标准", "strong": "强力", "off": "关闭"}


def _stab_mode(settings):
    """返回 None（不防抖）/ "auto"（先判断再决定）/ 具体强度。"""
    v = settings.get("stabilize", "auto")
    if v is True:
        return "auto"
    if not v or v == "off":
        return None
    return v if (v in STAB or v == "auto") else "auto"


def export_video(src, info, intervals, timeline, total, tpl, settings, overlay_text, out_path, workdir, progress,
                 fps=30, log=print, bigwords=None, bgm=None, sfx_path=None, speaker=None):
    """overlay_text = (title, subtitle, ip_name)。settings: aspect / stabilize / enhance / denoise / loudnorm

    有防抖时分三步：① 分析晃动 → ② 单独生成“剪好 + 稳定 + 调色”的画面 → ③ 叠加字幕、标题、声音合成成片。
    防抖与合成分开跑，避免多路输入导致滤镜重建失败；任何一步防抖失败都会自动退回不防抖，保证能出片。"""
    aspect = settings.get("aspect", "original")
    W, H = output_size(info["width"], info["height"], aspect, tpl)
    vx, vy, vw, vh = video_region(tpl, W, H, info["width"], info["height"])
    fx = settings.get("text_fx", True)
    sub_st = dict(tpl["subtitle"], emph=fx)
    lst, band_y = render_subtitle_track(timeline, W, H, sub_st, os.path.join(workdir, "subs"), total, anim=fx)
    bw_lst, bw_y = render_bigword_track(bigwords or [], W, H, tpl.get("bigword"), os.path.join(workdir, "subs"),
                                        total)
    sp_lst, sp_x, sp_y = (render_speaker_track(speaker[0], speaker[1], W, H, tpl.get("speaker_card"),
                                               os.path.join(workdir, "subs"), total) if speaker else (None, 0, 0))
    ov = static_overlay(tpl, W, H, info["width"], info["height"], *overlay_text)
    ov_path = None
    if ov:
        ov_path = os.path.join(workdir, "overlay.png")
        ov.save(ov_path)

    eps = 0.0005
    expr = "+".join(f"between(t,{a - eps:.4f},{b - 1.0 / fps + eps:.4f})" for a, b in intervals)
    aexpr = "+".join(f"between(t,{a:.4f},{b - 0.00002:.5f})" for a, b in intervals)
    fy = _focus(tpl)
    head = (f"[0:v]setpts=PTS-STARTPTS,fps={fps},select='{expr}',setpts=N/{fps}/TB,"
            f"scale={vw}:{vh}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={vw}:{vh}:(iw-ow)/2:(ih-oh)*{fy},setsar=1,format=yuv420p")
    look = ""
    if settings.get("enhance", True):
        look += "," + ENHANCE
    if tpl.get("grade"):
        look += "," + tpl["grade"]
    NR = ["-reinit_filter", "0"]  # 输入参数中途变化时不重建滤镜（防抖失败的根源）

    mode = _stab_mode(settings)
    if mode == "auto":
        mode = "standard"
    base_file = None
    p0 = 0.0
    if mode:
        try:
            for f in ("transforms.trf", "base.mp4"):
                if os.path.exists(os.path.join(workdir, f)):
                    os.remove(os.path.join(workdir, f))
            with open(os.path.join(workdir, "detect.txt"), "w", encoding="utf-8") as f:
                f.write(head + ",vidstabdetect=shakiness=8:accuracy=15:stepsize=6:mincontrast=0.12"
                        ":result=transforms.trf[v]")
            media.run_progress([*NR, "-i", src, "-filter_complex_script", "detect.txt", "-map", "[v]",
                                "-f", "null", "-"], total, lambda x: progress(0.25 * x), cwd=workdir)
            with open(os.path.join(workdir, "stab.txt"), "w", encoding="utf-8") as f:
                f.write(head + f",vidstabtransform=input=transforms.trf:{STAB[mode]}:optzoom=1:interpol=bicubic"
                        + look + "[v]")
            media.run_progress(["-y", *NR, "-i", src, "-filter_complex_script", "stab.txt", "-map", "[v]",
                                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p",
                                "-r", str(fps), "-t", f"{total:.3f}", "base.mp4"],
                               total, lambda x: progress(0.25 + 0.35 * x), cwd=workdir)
            base_file = "base.mp4"
            p0 = 0.6
        except Exception as e:  # noqa: BLE001
            log("防抖失败，改为不防抖导出：" + str(e)[-300:])
            base_file = None
            p0 = 0.0

    graph, inputs = [], []
    if base_file:
        inputs += [*NR, "-i", base_file, *NR, "-i", src]
        vchain = "[0:v]setsar=1"
        audio_idx = 1
    else:
        inputs += [*NR, "-i", src]
        vchain = head + look
        audio_idx = 0
    if (vw, vh) != (W, H):
        vchain += f",pad={W}:{H}:{vx}:{vy}:black"
    graph.append(vchain + "[base]")
    cur, n = "base", inputs.count("-i")
    if ov_path:
        inputs += ["-loop", "1", "-framerate", str(fps), "-i", "overlay.png"]
        graph.append(f"[{n}:v]format=rgba[ov]")
        graph.append(f"[{cur}][ov]overlay=0:0:shortest=1:format=auto[b1]")
        cur, n = "b1", n + 1
    if lst:
        inputs += [*NR, "-f", "concat", "-safe", "0", "-i", os.path.relpath(lst, workdir)]
        graph.append(f"[{n}:v]fps={fps},format=rgba[sub]")
        graph.append(f"[{cur}][sub]overlay=0:{band_y}:eof_action=pass:format=auto[b2]")
        cur, n = "b2", n + 1
    if bw_lst:
        inputs += [*NR, "-f", "concat", "-safe", "0", "-i", os.path.relpath(bw_lst, workdir)]
        graph.append(f"[{n}:v]fps={fps},format=rgba[bw]")
        graph.append(f"[{cur}][bw]overlay=0:{bw_y}:eof_action=pass:format=auto[b3]")
        cur, n = "b3", n + 1
    if sp_lst:
        inputs += [*NR, "-f", "concat", "-safe", "0", "-i", os.path.relpath(sp_lst, workdir)]
        graph.append(f"[{n}:v]fps={fps},format=rgba[sp]")
        graph.append(f"[{cur}][sp]overlay={sp_x}:{sp_y}:eof_action=pass:format=auto[b4]")
        cur, n = "b4", n + 1
    graph.append(f"[{cur}]format=yuv420p[vout]")
    has_audio = info.get("has_audio", True)
    if has_audio:
        af = f"[{audio_idx}:a]asetpts=PTS-STARTPTS,aselect='{aexpr}',asetpts=N/SR/TB"
        if settings.get("denoise", True):
            af += "," + DENOISE_AUDIO
        if settings.get("loudnorm", True):
            af += ",loudnorm=I=-16:TP=-1.5:LRA=11"
        af += ",aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        mix = []
        if bgm:
            # 背景音乐：循环铺满全片，统一响度，开头淡入、结尾淡出；有人说话时自动压低（侧链压缩）
            inputs += ["-stream_loop", "-1", "-i", bgm["path"]]
            vol = max(0.0, min(1.0, settings.get("bgm_volume", 40) / 100)) * 0.55
            fade_out = max(0.0, total - 1.8)
            graph.append(af + ",asplit=2[voice][vsc]")
            graph.append(f"[{n}:a]atrim=start={bgm.get('start', 0):.3f},asetpts=PTS-STARTPTS,"
                         f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"loudnorm=I=-16:TP=-2:LRA=11,aresample=48000,volume={vol:.3f},"
                         f"atrim=duration={total + 0.5:.3f},afade=t=in:d=0.6,afade=t=out:st={fade_out:.3f}:d=1.8[bgm0]")
            graph.append("[bgm0][vsc]sidechaincompress=threshold=0.04:ratio=5:attack=15:release=400:makeup=1[bgm]")
            mix.append("[bgm]")
            n += 1
        else:
            graph.append(af + "[voice]")
        if sfx_path:
            inputs += ["-i", sfx_path]
            sv = max(0.0, min(1.0, settings.get("sfx_volume", 60) / 100))
            graph.append(f"[{n}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume={sv:.2f}[sfx]")
            mix.append("[sfx]")
            n += 1
        if mix:
            graph.append("[voice]" + "".join(mix) + f"amix=inputs={1 + len(mix)}:normalize=0:duration=first:dropout_transition=0,"
                         "alimiter=limit=0.94:level=disabled,aresample=48000[aout]")
        else:
            graph.append("[voice]anull[aout]")
    with open(os.path.join(workdir, "graph.txt"), "w", encoding="utf-8") as f:
        f.write(";\n".join(graph))
    args = ["-y", *inputs, "-filter_complex_script", "graph.txt", "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    tail = ["-pix_fmt", "yuv420p", "-r", str(fps), "-t", f"{total:.3f}", "-movflags", "+faststart", out_path]
    hq = settings.get("enhance", True)
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18" if hq else "20", "-profile:v", "high"]

    def cb(x):
        progress(p0 + (1 - p0) * x)
    if sys.platform == "darwin":
        # Mac 用硬件编码，速度快好几倍；不支持时自动退回软件编码
        try:
            media.run_progress(args + ["-c:v", "h264_videotoolbox", "-b:v", "16M" if hq else "12M",
                                       "-profile:v", "high"] + tail, total, cb, cwd=workdir)
            return W, H
        except Exception as e:  # noqa: BLE001
            log("硬件编码失败，改用软件编码：" + str(e)[-300:])
    media.run_progress(args + x264 + tail, total, cb, cwd=workdir)
    return W, H


def stabilize_source(src, info, out_path, workdir, mode, progress=lambda f: None):
    """流水线第一步：对整条原片做防抖，输出稳定后的视频（保留原声）。4K 会先缩到 1080p，速度更快。
    之后的识别、剪辑、合成都基于这条稳定后的视频，后面任何一步失败都不用重做防抖。"""
    os.makedirs(workdir, exist_ok=True)
    w, h = info["width"], info["height"]
    long_side = max(w, h)
    scale = ""
    if long_side > 1920:
        scale = "scale='if(gt(iw,ih),1920,-2)':'if(gt(iw,ih),-2,1920)':flags=lanczos,"
    total = info["duration"]
    NR = ["-reinit_filter", "0"]
    import uuid
    tag = uuid.uuid4().hex[:6]          # 每次运行用独立的临时文件，中断后重跑不会互相冲突
    trf_name = f"transforms_{tag}.trf"
    media.run_progress([*NR, "-i", src, "-vf",
                        scale + f"vidstabdetect=shakiness=8:accuracy=15:stepsize=6:mincontrast=0.12:result={trf_name}",
                        "-an", "-f", "null", "-"], total, lambda x: progress(0.4 * x), cwd=workdir)
    tmp = out_path + f".{tag}.part.mp4"
    media.run_progress(["-y", *NR, "-i", src, "-vf",
                        scale + f"vidstabtransform=input={trf_name}:{STAB[mode]}:optzoom=1:interpol=bicubic,"
                        "format=yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-c:a", "copy",
                        "-movflags", "+faststart", tmp], total, lambda x: progress(0.4 + 0.6 * x), cwd=workdir)
    os.replace(tmp, out_path)
    for f in os.listdir(workdir):  # 清理临时文件
        if f.startswith("transforms_"):
            try:
                os.remove(os.path.join(workdir, f))
            except OSError:
                pass
    return out_path
