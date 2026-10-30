"""口播稿：写稿、单句改写（网感句式模板）、降重检查。AI 调用都在本机服务里转发，Key 不进浏览器。"""
import json
import os
import re
import time
import uuid

import ctx

TEMPLATES = [
    {"id": "reverse", "name": "反常识", "desc": "先抛出一个和大多数人想法相反的结论，再解释", "ex": "做外贸，询盘越多反而越危险。"},
    {"id": "number", "name": "数字清单", "desc": "用具体数字概括要点，让人知道要讲几件事", "ex": "老板用 AI，记住这 3 句话就够了。"},
    {"id": "suspense", "name": "悬念钩子", "desc": "先讲结果或冲突，留一个让人想听下去的口子", "ex": "那天客户只问了一个问题，我就知道这单成了。"},
    {"id": "pain", "name": "痛点提问", "desc": "直接问出目标观众正在经历的难处", "ex": "你是不是也发了几百封开发信，一个回复都没有？"},
    {"id": "identity", "name": "身份代入", "desc": "点名这条内容是给谁看的，让对的人停下来", "ex": "如果你是做工厂的老板，这条一定要听完。"},
    {"id": "contrast", "name": "前后对比", "desc": "用之前和之后、别人和我们的反差突出价值", "ex": "以前一天回 20 个询盘要一整天，现在两小时搞定。"},
    {"id": "story", "name": "故事开场", "desc": "用一个具体的人、时间、地点开场", "ex": "去年冬天，一个绍兴的老板找到我……"},
    {"id": "scene", "name": "画面感", "desc": "把抽象的道理换成能想象出画面的细节", "ex": "凌晨两点，他还在一封一封地翻询盘。"},
    {"id": "golden", "name": "金句收尾", "desc": "一句短、有节奏、能被记住和转发的总结", "ex": "工具不会淘汰你，会用工具的人才会。"},
    {"id": "interact", "name": "互动引导", "desc": "给出一个低门槛的评论或行动理由", "ex": "你卡在哪一步？评论区告诉我，我挑三个帮你看。"},
    {"id": "plain", "name": "更口语", "desc": "改成跟朋友聊天的说法，去掉书面语和套话", "ex": ""},
    {"id": "short", "name": "再短一点", "desc": "压缩到一口气能说完，信息不丢", "ex": ""},
    {"id": "fresh", "name": "换个说法", "desc": "意思不变，换掉常见套话和重复用词", "ex": ""},
]
TYPES = ["口播", "活动宣传", "干货分享", "观点表达", "直播话术"]
CLICHES = ["今天给大家分享", "今天跟大家聊聊", "大家好我是", "相信很多人", "众所周知", "在这个时代", "赋能", "抓手",
           "闭环", "底层逻辑", "说白了", "话不多说", "干货满满", "建议收藏", "一定要看到最后", "家人们"]
TICS = ["然后", "其实", "就是", "那么", "所以说", "这个", "那个", "我们"]
BANNED = ["最好", "最佳", "最强", "最低价", "唯一", "顶级", "极致", "绝对", "100%", "百分百", "保证", "包过", "稳赚",
          "零风险", "国家级", "全网最", "史上", "根治", "秒杀"]


def _path():
    return os.path.join(ctx.base(), "pad.json")


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"scripts": []}


def save_all(d):
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def upsert(script):
    d = load()
    sid = script.get("id") or uuid.uuid4().hex[:8]
    script = {"id": sid, "topic": str(script.get("topic", ""))[:60], "type": script.get("type", "口播"),
              "len": int(script.get("len", 60) or 60), "lines": [str(x)[:200] for x in script.get("lines", [])][:120],
              "updated": time.time()}
    d["scripts"] = [s for s in d["scripts"] if s["id"] != sid]
    d["scripts"].insert(0, script)
    d["scripts"] = d["scripts"][:200]
    save_all(d)
    return script


def delete(sid):
    d = load()
    d["scripts"] = [s for s in d["scripts"] if s["id"] != sid]
    save_all(d)


# ---------------------------------------------------------------- 降重检查
def _grams(t, n=4):
    t = re.sub(r"[\s，。！？!?、,.；;：:“”\"'（）()【】\[\]]", "", t)
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def history_lines(exclude_id="", project_lines=None, limit=1500):
    out = []
    for s in load()["scripts"]:
        if s["id"] != exclude_id:
            out += [(l, "旧稿「" + (s["topic"] or "未命名")[:10] + "」") for l in s["lines"]]
    for t, src in (project_lines or []):
        out.append((t, src))
    return out[:limit]


def check(lines, exclude_id="", project_lines=None):
    """返回 {lines:[{i, flags:[...]}], tics:{词:次数}, cliches:[...], banned:[...], secs}。"""
    pools = {}
    for l, src in history_lines(exclude_id, project_lines):  # 按来源合成一整段比较，跨句的重复也能查出来
        pools.setdefault(src, []).append(l)
    hist = [(src, _grams("".join(ls))) for src, ls in pools.items()]
    grams = [_grams(l) for l in lines]
    res = []
    for i, (l, g) in enumerate(zip(lines, grams)):
        flags = []
        if len(g) >= 3:
            for j in range(i):
                if len(grams[j]) >= 3 and len(g & grams[j]) / len(g) >= 0.5:
                    flags.append(f"和第 {j + 1} 句意思重复")
                    break
            for src, hg in hist:
                if len(g & hg) / len(g) >= 0.6:
                    flags.append(f"和{src}说法相似")
                    break
        for c in CLICHES:
            if c in l:
                flags.append(f"套话：{c}")
        for b in BANNED:
            if b in l:
                flags.append(f"极限词：{b}")
        res.append({"i": i, "flags": flags})
    text = "".join(lines)
    tics = {t: text.count(t) for t in TICS if text.count(t) >= 3}
    chars = len(re.sub(r"\s", "", text))
    return {"lines": res, "tics": tics, "secs": round(chars / 4.6), "chars": chars}


# ---------------------------------------------------------------- AI
SYS = ("你是资深的短视频口播编导，擅长把内容写得有网感、口语化、节奏好，又真实可信。"
       "不编造数据和经历：需要具体数字但素材里没有时，用【待确认：xxx】标出。不用极限词。只输出 JSON。")


def _persona(info):
    t = ""
    if info.get("name"):
        t += f"说话人：{info['name']}\n"
    if info.get("titles"):
        t += f"身份：{info['titles']}\n"
    if info.get("terms"):
        t += "常用专有名词：" + "、".join(info["terms"][:60]) + "\n"
    return t


def write(cfg, topic, typ, secs, hook, ending, notes, info, avoid):
    import ai
    tpl = {t["id"]: t for t in TEMPLATES}
    h, e = tpl.get(hook), tpl.get(ending)
    prompt = (f"写一条{typ}口播稿，主题：{topic}。念出来约 {secs} 秒（约 {round(secs * 4.6)} 字）。\n"
              f"要求：一句一行，每句不超过 22 个字；前 3 秒要抓人"
              + (f"，开头用「{h['name']}」句式：{h['desc']}（例：{h['ex']}，只参考句式，不要照抄）" if h else "")
              + "；中间有具体的例子或细节，不说空话套话；"
              + (f"结尾用「{e['name']}」：{e['desc']}" if e else "结尾自然收住，可以有一个互动")
              + "。\n避免这些套话：" + "、".join(CLICHES[:10]) + "。\n"
              + (f"不要和以前的稿子说法重复，这些开头和句子已经用过：\n{avoid}\n" if avoid else "")
              + (f"补充素材：\n{notes}\n" if notes else "") + _persona(info)
              + '只输出 JSON：{"lines":["第一句","第二句"],"title":"10 字左右的封面标题"}')
    d = ai._parse(ai._post(cfg, [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}], timeout=90))
    return {"lines": [str(x).strip() for x in d.get("lines", []) if str(x).strip()][:60], "title": str(d.get("title", ""))[:20]}


def rewrite(cfg, lines, i, tid, info, avoid):
    import ai
    t = {x["id"]: x for x in TEMPLATES}.get(tid) or TEMPLATES[-1]
    ctx_lines = "\n".join(f"{k + 1}. {l}" for k, l in enumerate(lines))
    prompt = (f"下面是一条口播稿。只改写第 {i + 1} 句，用「{t['name']}」的方式：{t['desc']}"
              + (f"（参考句式：{t['ex']}，不要照抄例句）" if t["ex"] else "")
              + "。\n其他句子不动；改写后要和上下文衔接自然、意思不跑偏、口语化，不超过 25 字；"
              "不要用套话和极限词；三个候选之间说法要明显不同。\n"
              + (f"避免和这些已有说法重复：\n{avoid}\n" if avoid else "") + _persona(info)
              + f"\n全文：\n{ctx_lines}\n\n" + '只输出 JSON：{"cands":["候选1","候选2","候选3"]}')
    d = ai._parse(ai._post(cfg, [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}], timeout=60))
    return [str(x).strip() for x in d.get("cands", []) if str(x).strip()][:3]
