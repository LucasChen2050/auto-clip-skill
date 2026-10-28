"""可选的 AI 审阅：把识别出的口播文字交给大模型，判断哪些是说错重说的废弃片段、改同音错别字。

使用任意 OpenAI 兼容接口（DeepSeek、通义千问、Kimi、智谱等），只发送文字，不发送视频和声音。
没配置、网络不通或返回异常时，调用方会自动退回本地规则。"""
import json
import re
import urllib.error
import urllib.request

PRESETS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"id": "qwen", "name": "通义千问（阿里云百炼）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "model": "qwen-plus"},
    {"id": "kimi", "name": "Kimi（月之暗面）", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    {"id": "custom", "name": "自定义（OpenAI 兼容）", "base_url": "", "model": ""},
]

SYSTEM = "你是资深的口播短视频剪辑师，擅长从口播逐字稿里找出说错、重说的废弃片段，并修正语音识别的错别字。只输出 JSON。"

TASK = """下面是一段口播视频的语音识别结果，已按停顿切成很多小片段。每个片段有编号 id、文字 t、与上一段之间的停顿秒数 gap。
标记了 "ctx": true 的片段只是上文，帮你理解语境，不需要判断。

请逐个判断其余片段是否保留：
1. 说话人说错或说到一半停下、又重新说了一遍时，删除前面废弃的那一遍，保留最后说完整、最通顺的一遍。废弃片段后面通常紧跟着内容相同或相近的说法。
2. 删除没有意义的语气词、口头禅（如单独的“嗯”“呃”“那个”“就是说”“然后呢”“对吧”），连说两遍的词（“我们我们”“这个这个”），以及说错又马上纠正的半截话。
3. 正常的强调、排比、并列、递进、承上启下，即使字面相似也必须保留，不要误删。
4. 拿不准的一律保留。删除后剩下的内容连起来读必须通顺、意思完整。
5. 修正明显的同音、近音错别字（语音识别错误），只做等字数替换，例如“外貌”→“外贸”“铲”→“产”。没有错就不要写 fix。

只输出 JSON，格式：
{"items":[{"id":编号,"keep":true或false,"reason":"删除原因（2-4个字，如 重说、口误、语气词），保留时留空","fix":"改正后的文字（字数必须和原文相同，没有修改就省略此字段）"}]}
必须覆盖所有不带 ctx 的 id。

片段如下：
"""


def _post(cfg, messages, json_mode=True, timeout=120):
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": cfg["model"], "messages": messages, "temperature": 0.1}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + cfg["api_key"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")[:300]
        if json_mode and e.code == 400:  # 有的服务不支持 json 模式，去掉再试
            return _post(cfg, messages, json_mode=False, timeout=timeout)
        raise RuntimeError(f"AI 服务返回错误 {e.code}：{msg}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上 AI 服务：{e.reason}") from None
    return data["choices"][0]["message"]["content"]


def _parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("AI 返回的内容不是 JSON")
    return json.loads(m.group(0))


def ready(cfg):
    return bool(cfg and cfg.get("enabled") and cfg.get("api_key") and cfg.get("base_url") and cfg.get("model"))


def test(cfg):
    out = _post(cfg, [{"role": "user", "content": '只回复 JSON：{"ok":true}'}], timeout=30)
    return "ok" in out.lower()


def _glossary(terms, limit=300):
    terms = [t for t in (terms or []) if t][:limit]
    if not terms:
        return ""
    return ("\n说话人的常用词典（专有名词、品牌、人名，识别结果里发音相近的写错的，按词典写法改）：\n"
            + "、".join(terms) + "\n")


def review(pieces, cfg, progress=lambda f: None, chunk=110, ctx=6, terms=None):
    """pieces: [{id, t, gap}] → {id: {"keep": bool, "reason": str, "fix": str}}"""
    result = {}
    task = TASK.replace("片段如下：\n", _glossary(terms) + "\n片段如下：\n")
    total = len(pieces)
    for start in range(0, total, chunk):
        part = pieces[start:start + chunk]
        before = pieces[max(0, start - ctx):start]
        rows = [dict(p, ctx=True) for p in before] + part
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        content = _post(cfg, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task + payload}])
        data = _parse(content)
        for it in data.get("items", []):
            try:
                result[int(it["id"])] = {"keep": bool(it.get("keep", True)), "reason": str(it.get("reason", ""))[:8],
                                         "fix": str(it.get("fix", "") or "")}
            except (KeyError, ValueError, TypeError):
                continue
        progress(min(1.0, (start + chunk) / max(1, total)))
    return result


TRANS_SYS = "You are a professional subtitle translator. Translate Chinese short-video subtitles into natural, concise English. Output JSON only."


def translate(texts, cfg, chunk=60):
    """把中文字幕逐句翻译成英文，返回与输入等长的列表。上下文一起发送，译文更连贯；每句尽量简短。"""
    out = [""] * len(texts)
    for s in range(0, len(texts), chunk):
        part = texts[s:s + chunk]
        items = [{"id": i, "zh": t} for i, t in enumerate(part)]
        prompt = ("Translate each subtitle line into English. Keep it short (ideally under 9 words), natural, "
                  "keep names/brands/numbers accurate, no trailing period. Lines are consecutive parts of one talk.\n"
                  'Output: {"items":[{"id":0,"en":"..."}]} covering every id.\n\n'
                  + json.dumps(items, ensure_ascii=False))
        res = _parse(_post(cfg, [{"role": "system", "content": TRANS_SYS}, {"role": "user", "content": prompt}]))
        for it in res.get("items", []):
            try:
                i = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(part):
                out[s + i] = str(it.get("en", "")).strip().rstrip("。.")
    return out


PROOF_SYS = "你是严谨的中文字幕校对员，熟悉 AI、互联网和商业领域的专有名词。只输出 JSON。"
PROOF_TASK = """下面是一条口播短视频的字幕，由语音识别生成，按顺序排列，每行有编号 id 和文字 t。
请逐行校对，只修正这几类错误：
1. 同音、近音错别字（如“外貌”→“外贸”“机遇”→“机器”要看上下文）；
2. 专有名词、品牌、产品名、人名写错，按下面的词典改成正确写法；英文名词常被识别成发音相近的中文或错的拼写（如“克劳德”→“Claude”“拆GPT”→“ChatGPT”），也要改；
3. 英文名词的大小写（如 chatgpt → ChatGPT）。
不要润色、不要改口语表达、不要增删内容、不要加标点。没有错的行不要输出。
{glossary}
只输出 JSON：{{"items":[{{"id":编号,"t":"改正后的整行文字"}}]}}

字幕如下：
"""


def proofread(rows, cfg, terms=None, progress=lambda f: None, chunk=120):
    """rows: [{id, t}] → {id: 改正后的文字}（只包含需要改的行）"""
    out = {}
    task = PROOF_TASK.format(glossary=_glossary(terms))
    for s in range(0, len(rows), chunk):
        part = rows[s:s + chunk]
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in part)
        data = _parse(_post(cfg, [{"role": "system", "content": PROOF_SYS}, {"role": "user", "content": task + payload}]))
        for it in data.get("items", []):
            try:
                out[int(it["id"])] = str(it.get("t", ""))
            except (KeyError, ValueError, TypeError):
                continue
        progress(min(1.0, (s + chunk) / max(1, len(rows))))
    return out
