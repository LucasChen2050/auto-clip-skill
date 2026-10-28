"""为每个模板生成预览图（成片画面 + 封面），新加的模板启动时自动补上。"""
import os

from PIL import Image, ImageDraw, ImageFilter

import render

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "sample.jpg")
DEMO = {"sub": "将在[杭州阿里总部]举办", "title": "外贸人必看", "subtitle": "3个获客新渠道", "ip": "@你的账号名",
        "en": "It will be held at Alibaba HQ in Hangzhou", "name": "张老师", "titles": ["外贸增长顾问", "服务 300+ 出海企业"]}


def sample_frame():
    """一张中性的示意画面：办公室背景 + 人物剪影（不含任何真人肖像）。"""
    if os.path.exists(SAMPLE):
        return Image.open(SAMPLE).convert("RGB")
    W, H = 1080, 1920
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        k = y / H
        d.line([(0, y), (W, y)], fill=(int(176 - 60 * k), int(170 - 62 * k), int(160 - 64 * k)))
    d.rectangle([90, 380, 520, 900], fill=(214, 222, 228))       # 窗
    d.rectangle([640, 300, 1000, 1150], fill=(120, 96, 78))       # 书架
    for i in range(5):
        d.rectangle([660, 330 + i * 160, 980, 345 + i * 160], fill=(92, 72, 58))
    d.rectangle([0, 1300, W, H], fill=(96, 92, 88))               # 桌面
    img = img.filter(ImageFilter.GaussianBlur(18))                 # 背景虚化
    d = ImageDraw.Draw(img)
    d.ellipse([370, 640, 710, 1040], fill=(64, 56, 54))           # 头
    d.rounded_rectangle([190, 1000, 890, 1920], radius=260, fill=(38, 40, 48))  # 身体
    d.rectangle([500, 1000, 580, 1140], fill=(64, 56, 54))        # 脖子
    img.save(SAMPLE, quality=88)
    return img


def make(tpl, force=False):
    out = os.path.join(render.TPL_DIR, tpl["id"], "preview.jpg")
    if os.path.exists(out) and not force:
        return out
    fr = sample_frame()
    W, H = 1080, 1920
    a = render.compose_frame(fr, tpl, W, H, fr.width, fr.height, DEMO["sub"], "", DEMO["title"], DEMO["subtitle"],
                             DEMO["ip"], sub_en=DEMO["en"],
                             speaker=(DEMO["name"], DEMO["titles"]) if tpl.get("speaker_card") else None)
    tmp = out + ".cover.jpg"
    render.render_cover(SAMPLE, DEMO["title"], DEMO["subtitle"], tpl, W, H, tmp, ip_name=DEMO["ip"])
    b = Image.open(tmp)
    sw, sh = 300, 534
    pv = Image.new("RGB", (sw * 2 + 12, sh), (255, 255, 255))
    pv.paste(a.resize((sw, sh), Image.LANCZOS), (0, 0))
    pv.paste(b.resize((sw, sh), Image.LANCZOS), (sw + 12, 0))
    pv.save(out, quality=86)
    os.remove(tmp)
    return out


def ensure_all(force=False):
    for t in render.list_templates():
        try:
            make(t, force)
        except Exception as e:  # noqa: BLE001
            print("模板预览生成失败：", t["id"], e)
