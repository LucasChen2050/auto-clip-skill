"""标题提炼：先用代码从文稿里找“活动名称”，找不到再交给 AI，最后才用关键句兜底。

主标题：如“杭州阿里外贸峰会”（城市 + 机构 + 活动）
副标题：如“10月19日 杭州见”（日期 + 地点），没有就留空"""
import json
import re

from engine import jieba, plain, pseg  # noqa: F401  (jieba 已在 engine 中初始化)

EVENT_WORDS = ("峰会", "大会", "论坛", "交流会", "分享会", "沙龙", "研讨会", "博览会", "展会", "发布会", "训练营",
               "培训会", "对接会", "招商会", "见面会", "私董会", "年会", "座谈会", "说明会", "推介会", "答谢会",
               "嘉年华", "节", "会销", "公开课", "大讲堂", "游学", "考察")
EXTEND_POS = ("ns", "nt", "nz", "n", "nr", "nrt", "j", "vn", "eng", "l")
STOP = {"一场", "这场", "这次", "本次", "一次", "我们", "大家", "这个", "一个", "今天", "明天", "现场", "活动", "那场"}
ORG_HINT = ("阿里巴巴", "阿里", "国际站", "商会", "协会", "集团", "公司", "大学", "学院", "政府", "银行")
TOPIC_WORDS = ("外贸", "跨境", "电商", "出海", "品牌", "营销", "直播", "创业", "数字化", "供应链", "私域", "短视频",
               "制造", "工厂", "招商", "投资", "财税", "管理", "AI")
NUM = "0-9零一二三四五六七八九十"
DATE_RE = re.compile(rf"([{NUM}]{{1,3}}月[{NUM}]{{1,3}}[日号])")


def _clean(t):
    return re.sub(r"[\s，。！？!?、,.；;：:“”\"'‘’（）()【】\[\]]+", "", t)


def _candidates(text):
    words = [(w.word, w.flag) for w in pseg.cut(text)]
    out = []
    for i, (w, f) in enumerate(words):
        if not any(w.endswith(ev) for ev in EVENT_WORDS) or len(w) < 2:
            continue
        if w == "节":
            continue
        name, j = w, i - 1
        while j >= 0:
            pw, pf = words[j]
            if pw in STOP or len(name) + len(pw) > 12:
                break
            if pf in EXTEND_POS or (pf == "m" and re.search(r"[0-9]|月|届", pw)):
                name = pw + name
                j -= 1
                continue
            break
        out.append(name)
    return out


def extract_by_code(lines):
    """返回 (title, subtitle) 或 (None, None)。"""
    kept = [plain(l["text"]) for l in lines if not l.get("deleted")]
    text = "，".join(kept)
    if not text:
        return None, None
    cands = _candidates(text)
    if not cands:
        return None, None
    # 出现次数多、更具体（更长、带机构/地名）的优先
    score = {}
    for c in cands:
        score[c] = score.get(c, 0) + 1 + len(c) * 0.15 + (0.5 if any(o in c for o in ORG_HINT) else 0)
    title = max(score, key=score.get)
    # 补上城市 / 机构：如“外贸峰会” → “杭州阿里外贸峰会”
    places = [w.word for w in pseg.cut(text) if w.flag == "ns" and len(w.word) >= 2]
    orgs = [o for o in ORG_HINT if o in text]
    org = orgs[0] if orgs else ""
    if org == "阿里" and "阿里巴巴" in text:
        org = "阿里巴巴"
    city = max(set(places), key=places.count) if places else ""
    if title in EVENT_WORDS:  # 只有“峰会”两个字：补上文稿里最核心的主题词，如“外贸峰会”
        import jieba.analyse
        topics = [w for w in TOPIC_WORDS if w in text]
        if topics:
            title = max(topics, key=text.count) + title
        else:
            for kw in jieba.analyse.extract_tags(text, topK=8, allowPOS=("n", "vn", "nz", "ns", "nt")):
                if (kw not in STOP and len(kw) >= 2 and len(kw + title) <= 8 and "会" not in kw
                        and not any(kw.endswith(ev) for ev in EVENT_WORDS) and kw not in ("现场", "专家", "老板")):
                    title = kw + title
                    break
    if org and org not in title and len(org + title) <= 12:
        title = org + title
    if city and city not in title and len(city + title) <= 12:
        title = city + title
    # 副标题：日期 + 地点
    date = DATE_RE.search(text)
    sub = ""
    if date:
        sub = date.group(1).replace("号", "日")
        if city:
            sub += f" {city}见"
    elif city and city not in title:
        sub = f"{city}站"
    return title, sub


AI_PROMPT = """下面是一条口播短视频的字幕文稿。请提炼：
1. title：视频的主标题，≤12个字。文稿里有活动名称（如“杭州阿里外贸峰会”）就用活动名称，否则用最能吸引目标观众的核心主题。
2. subtitle：副标题，≤14个字，优先写时间地点（如“10月19日 杭州见”），没有就写一个核心卖点；实在没有就留空。
只输出 JSON：{"title":"...","subtitle":"..."}

文稿：
"""


def extract_by_ai(lines, cfg):
    import ai
    text = "，".join(plain(l["text"]) for l in lines if not l.get("deleted"))[:3000]
    content = ai._post(cfg, [{"role": "system", "content": "你是短视频运营编辑，只输出 JSON。"},
                             {"role": "user", "content": AI_PROMPT + text}], timeout=60)
    data = ai._parse(content)
    return _clean(str(data.get("title", "")))[:14], str(data.get("subtitle", "")).strip()[:16]


def extract(lines, ai_cfg=None):
    """返回 (title, subtitle, 来源说明)。"""
    import ai
    import cleanup
    t, s = extract_by_code(lines)
    if t:
        return t, s, "从文稿中找到活动名称"
    if ai.ready(ai_cfg):
        try:
            t, s = extract_by_ai(lines, ai_cfg)
            if t:
                return t, s, "AI 提炼"
        except Exception as e:  # noqa: BLE001
            return cleanup.summarize(lines, 12), "", f"AI 提炼失败（{str(e)[:40]}），用关键句代替"
    return cleanup.summarize(lines, 12), "", "未找到活动名称，用关键句代替"


if __name__ == "__main__":
    tests = ["下周一我们将在杭州阿里总部举办一场外贸峰会，欢迎各位积极参与",
             "10月19号下午2点，阿里巴巴国际站在杭州有一场外贸交流会，现场会讲一个人怎么做外贸",
             "今天跟大家聊聊做外贸怎么找客户"]
    for t in tests:
        print(t, "→", extract_by_code([{"text": t}]))
    print(json.dumps(EVENT_WORDS, ensure_ascii=False)[:0])
