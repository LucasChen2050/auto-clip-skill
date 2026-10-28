"""我的词典：常用词、专有名词（尤其 AI 领域的英文名词）和“错词=正确词”的强制替换。

识别完成后按三层纠错：
① 强制替换（错词=正确词）；
② 英文名词统一写法（chatgpt / chat gpt → ChatGPT）；
③ 中文名词按拼音模糊匹配（“只能体”→“智能体”，三个字及以上才自动改，避免误伤）。
接入 AI 时，词典还会发给 AI 做第二轮校对，处理发音相近但写法完全不同的情况（如“克劳德”→“Claude”）。"""
import os
import re

import ctx

MARK = "#@词典v1"
DEFAULT_TERMS = """\
# —— AI 名词 ——
AI
AIGC
AGI
Agent
智能体
大模型
大语言模型
LLM
提示词
Prompt
Token
API
MCP
RAG
多模态
工作流
知识库
向量数据库
微调
算力
推理模型
Skill
Claude
Claude Code
Anthropic
ChatGPT
OpenAI
GPT
Gemini
DeepSeek
Kimi
通义千问
豆包
文心一言
智谱
Cursor
Copilot
Midjourney
Sora
Coze
扣子
Manus
Dify
n8n
Notion
飞书
# —— 业务常用 ——
私域
公域
获客
转化率
复购率
GMV
ROI
跨境电商
阿里巴巴国际站
数字人
短视频
直播间
"""


def _header():
    return (f"{MARK}\n# 我的词典：每行写一个你常说的词、专有名词、品牌名或人名，识别结果里发音相近的写法会被改成这里的写法。\n"
            "# 也可以写“识别错的词=正确的词”，以后的视频会直接替换。以 # 开头的是说明，可以删。\n")


def path():
    return ctx.corr_path()


def load_text():
    """读取词典原文；第一次使用时写入常用名词（保留以前记住的纠错）。"""
    p = path()
    old = ""
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            old = f.read()
        if MARK in old:
            return old
    pairs = [l for l in old.splitlines() if "=" in l and not l.strip().startswith("#")]
    text = _header() + DEFAULT_TERMS + ("# —— 记住的纠错 ——\n" + "\n".join(pairs) + "\n" if pairs else "")
    save_text(text)
    return text


def save_text(text):
    text = text.replace("\r\n", "\n")
    if MARK not in text:
        text = MARK + "\n" + text
    with open(path(), "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def parse(text=None):
    """返回 (词条列表, [(错词, 正确词)])。"""
    text = load_text() if text is None else text
    terms, pairs = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            a, b = line.split("=", 1)
            if a.strip():
                pairs.append((a.strip(), b.strip()))
                if b.strip():
                    terms.append(b.strip())
        else:
            terms.append(line)
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out, pairs


def remember(wrong, right):
    """“全部替换”时勾选“记住”：把纠错写进词典（同一个错词只保留最新的）。"""
    text = load_text()
    lines = [l for l in text.splitlines() if not (l.split("=", 1)[0].strip() == wrong and "=" in l)]
    lines.append(f"{wrong}={right}")
    save_text("\n".join(lines))


# ---------------------------------------------------------------- 纠错
_HAN = re.compile(r"^[一-鿿]+$")
_FUZZY = [("zh", "z"), ("ch", "c"), ("sh", "s")]


def _norm(py):
    for a, b in _FUZZY:
        if py.startswith(a):
            py = b + py[len(a):]
    if py.startswith("l"):
        py = "n" + py[1:]
    for a, b in (("ang", "an"), ("eng", "en"), ("ing", "in")):
        if py.endswith(a):
            py = py[:-len(a)] + b
    return py


def _pys(text):
    from pypinyin import lazy_pinyin
    return [_norm(x) for x in lazy_pinyin(text, errors=lambda s: ["?"] * len(s))]


def _eng_pattern(term):
    """英文名词：忽略大小写，字母之间允许被识别成空格（chat gpt → ChatGPT）。"""
    chars = [c for c in term if c != " "]
    body = r"\s?".join(re.escape(c) for c in chars)
    return re.compile(r"(?<![A-Za-z])" + body + r"(?![A-Za-z])", re.I)


class Fixer:
    def __init__(self, terms, pairs):
        self.pairs = sorted(pairs, key=lambda p: -len(p[0]))
        self.eng = [(t, _eng_pattern(t)) for t in sorted(terms, key=len, reverse=True)
                    if re.search(r"[A-Za-z]", t) and len(t.replace(" ", "")) >= 2]
        self.han = []
        for t in terms:
            if _HAN.match(t) and 3 <= len(t) <= 8:
                self.han.append((t, _pys(t)))
        self.han.sort(key=lambda x: -len(x[0]))

    def fix(self, text):
        n = 0
        for a, b in self.pairs:
            if a in text:
                n += text.count(a)
                text = text.replace(a, b)
        for t, pat in self.eng:
            new = pat.sub(t, text)
            if new != text:
                n += 1
                text = new
        if self.han:
            chars = list(text)
            pys = _pys(text) if all(len(c) == 1 for c in chars) else None
            if pys and len(pys) == len(chars):
                i = 0
                while i < len(chars):
                    hit = None
                    for t, tp in self.han:
                        L = len(t)
                        if i + L <= len(chars) and pys[i:i + L] == tp and "".join(chars[i:i + L]) != t:
                            hit = t
                            break
                    if hit:
                        chars[i:i + len(hit)] = list(hit)
                        n += 1
                        i += len(hit)
                    else:
                        i += 1
                text = "".join(chars)
        return text, n


def fixer():
    terms, pairs = parse()
    try:
        from engine import jieba
        for t in terms:
            jieba.add_word(t, freq=20000)
    except Exception:  # noqa: BLE001
        pass
    return Fixer(terms, pairs)
