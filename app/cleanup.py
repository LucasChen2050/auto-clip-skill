"""逐字级清理：在“字 + 时间”序列上找出重说、卡顿和语气词，只保留最后说完整的那一遍，
再把留下的字重新断成句子。删掉的片段单独成行，用户可以一键恢复。"""
import os

import engine
from engine import PUNCT, ROOT, jieba, pseg

SENT_END = set("。！？!?")
SOFT_FILLERS = set("啊哦诶哎嘛呐")
HARD_FILLERS = set("嗯呃额唔")
FUNC_CHARS = set("我你他她它这那就是的在也都还要会能把")

_dict_ready = False


def _is_word(w):
    global _dict_ready
    if not _dict_ready:
        jieba.dt.check_initialized()
        _dict_ready = True
    return jieba.dt.FREQ.get(w, 0) > 0


# ---------------------------------------------------------------- 字序列
def make_stream(segments):
    """把识别结果展开成逐字序列：[{w, s, e, seg, pb(前面有停顿/标点), pa(后面的标点)}]"""
    units = []
    for si, seg in enumerate(segments):
        toks = seg["tokens"]
        prev_punct = True
        for i, (t, ts) in enumerate(toks):
            if t in PUNCT or all(ch in PUNCT for ch in t):
                if units and units[-1]["seg"] == si:
                    units[-1]["pa"] = t
                prev_punct = True
                continue
            nxt = seg["end"]
            for t2, ts2 in toks[i + 1:]:
                nxt = ts2
                break
            end = min(max(nxt, ts + 0.12), ts + 0.5, seg["end"] + 0.1)  # 句末拖音、紧跟的“嗯”不算进字里
            units.append({"w": t, "s": round(ts, 3), "e": round(end, 3), "seg": si,
                          "pb": prev_punct, "pa": "", "del": ""})
            prev_punct = False
    # 段落之间的停顿也算“前面有停顿”
    for a, b in zip(units, units[1:]):
        if b["s"] - a["e"] > 0.35:
            b["pb"] = True
    return units


# ---------------------------------------------------------------- 清理规则
def _mark_fillers(u):
    for i, x in enumerate(u):
        if x["w"] in HARD_FILLERS:
            prev_w = u[i - 1]["w"] if i > 0 and not x["pb"] else ""
            next_w = u[i + 1]["w"] if i + 1 < len(u) and not x["pa"] and not u[i + 1]["pb"] else ""
            if not (_is_word(prev_w + x["w"]) and prev_w) and not (_is_word(x["w"] + next_w) and next_w):
                x["del"] = "语气词"  # “名额”“额外”里的“额”不算
        elif x["w"] in SOFT_FILLERS:
            alone_before = x["pb"]
            alone_after = bool(x["pa"]) or i + 1 >= len(u) or u[i + 1]["pb"]
            if alone_before and alone_after:
                x["del"] = "语气词"


# 单独成句的口头禅：前后都有停顿时整段剪掉（“那个，……”“然后，……”）
TICS = {"那个", "这个", "就是", "就是说", "然后", "然后呢", "那么", "对吧", "是吧", "对不对", "嗯嗯", "呃呃", "啊啊",
        "怎么说呢", "这样子", "那个那个", "就是就是", "然后然后"}


def _mark_tics(u):
    i = 0
    while i < len(u):
        x = u[i]
        if x["del"] or not (x["pb"] or i == 0):
            i += 1
            continue
        done = False
        for L in (4, 3, 2):
            seg = u[i:i + L]
            if len(seg) < L or any(y["del"] for y in seg):
                continue
            t = "".join(y["w"] for y in seg)
            if t not in TICS:
                continue
            last = seg[-1]
            nxt = u[i + L] if i + L < len(u) else None
            inner_ok = all(not y["pa"] for y in seg[:-1])
            after = bool(last["pa"]) or nxt is None or nxt["s"] - last["e"] >= 0.25
            if inner_ok and after:
                for y in seg:
                    y["del"] = "口头禅"
                i += L
                done = True
                break
        if not done:
            i += 1


def _mark_stutter(u):
    """“专专门”“我我们”这类叠字卡顿：删前一个字。正常叠词（看看、天天）不动。"""
    k = [x for x in u if not x["del"]]
    for a, b, c in zip(k, k[1:], k[2:] + [None]):
        if len(a["w"]) != 1 or a["w"] != b["w"] or b["s"] - a["s"] > 1.2:
            continue
        two = a["w"] + b["w"]
        nxt2 = b["w"] + (c["w"] if c else "")
        if _is_word(two):
            continue
        if (c and _is_word(nxt2)) or a["w"] in FUNC_CHARS:
            a["del"] = "卡顿"


def _text(units):
    return "".join(x["w"] for x in units)


def _looks_parallel(k, i, j, m):
    """排比句保护：“我们要做好产品，我们要做好服务”是一口气说完的两句，不是重说。
    条件：前一遍说完了一个完整的词（不是半个字就断掉），后一遍换成了另一个词，且中间没有长停顿。"""
    left = _text(k[i + m:j])
    if len(left) < 2 or len(left) > 8:
        return False
    same_breath = k[j - 1]["seg"] == k[j]["seg"] or k[j - 1]["pa"] in ("，", "、", "；", ",")
    if not same_breath:
        return False
    words = list(jieba.cut(left))
    if not all(_is_word(w) for w in words):
        return False
    cont_words = list(jieba.cut(_text(k[j + m:j + m + len(left) + 4])))
    if not cont_words:
        return False
    return cont_words[0] != words[0] and _is_word(cont_words[0]) and len(cont_words[0]) >= 2


PARTICLES = set("呢啊吧嘛哈呀哦嗯呃额")
# 口语里常被连说两遍的虚词（“可以可以”“就是就是”），两字也剪；“研究研究”“一步一步”不在此列
STUTTER_WORDS = {"可以", "就是", "这个", "那个", "我们", "然后", "因为", "所以", "但是", "其实", "如果", "还是",
                 "已经", "一个", "大家", "你们", "他们", "现在", "怎么", "什么", "这些", "那些", "我们的", "今天"}


def _mark_retakes(u, max_back_sec=15.0, max_back_units=70):
    """重说 / 重复：后面的话和前面某处开头一样 → 删掉前面那一遍（保留最后一遍）。

    两种情况：
    ① 停一下再重来（后一遍前有停顿或标点）：重复≥6 字，或前一遍几乎整句被重说；
    ② 语速快、没停顿就连说两遍（“本次峰会本次峰会有…”）：紧挨着重复≥3 字，中间最多夹一个语气字。
    “一步一步”“研究研究”这类两字叠用不算。"""
    changed = True
    start_j = 1
    while changed:
        changed = False
        k = [x for x in u if not x["del"]]
        for j in range(max(1, start_j), len(k)):
            best = None
            paused = k[j]["pb"] or k[j]["s"] - k[j - 1]["e"] >= 0.2
            lo = max(0, j - max_back_units)
            for i in range(j - 1, lo - 1, -1):
                if k[j]["s"] - k[i]["s"] > max_back_sec:
                    break
                if not paused and j - i > 12:
                    break
                if k[i]["w"] != k[j]["w"]:
                    continue
                m = 0
                while i + m < j and j + m < len(k) and k[i + m]["w"] == k[j + m]["w"]:
                    m += 1
                span = j - i
                left_txt = _text(k[i + m:j])
                left = len([c for c in left_txt if c not in PARTICLES and c not in PUNCT])
                if paused:
                    min_m = 2 if (k[i]["pb"] and k[j]["pb"]) else 3
                    if m < min_m:
                        continue
                    if not (m >= 6 or left <= 2 or (m >= 3 and m / span >= 0.6)):
                        continue
                else:
                    two = m == 2 and span == 2 and _text(k[i:i + 2]) in STUTTER_WORDS
                    if not two and (m < 3 or left > 0 or span - m > 1):
                        continue
                if _looks_parallel(k, i, j, m):
                    continue
                if best is None or m > best[1]:
                    best = (i, m)
            if best:
                i, _ = best
                for x in k[i:j]:
                    x["del"] = "重说" if paused else "重复"
                start_j = i + 1
                changed = True
                break


def clean(units, remove_fillers=True, retakes=True):
    if remove_fillers:
        _mark_fillers(units)
        _mark_tics(units)
    _mark_stutter(units)
    if retakes:
        _mark_retakes(units)
    return units


# ---------------------------------------------------------------- 重新断句
def _phrases(units):
    """把字切成短语：保留的字遇到标点、明显停顿、或被删的字就断开；连续被删的同类片段合成一行。"""
    out, cur = [], []
    for idx, x in enumerate(units):
        cur.append(x)
        nxt = units[idx + 1] if idx + 1 < len(units) else None
        if nxt is None:
            brk = True
        else:
            brk = bool(x["pa"]) or nxt["del"] != x["del"] or nxt["s"] - x["e"] > 0.35
        if brk:
            out.append(cur)
            cur = []
    return out


def build_lines(units, max_chars=13):
    lines = []
    for ph in _phrases(units):
        reason = ph[0]["del"]
        if True:  # 保留的和删掉的都按短句拆分，恢复删掉的句子时也不会出现超长字幕
            tuples = [(x["w"], x["s"], x["e"]) for x in ph]
            parts_t = engine._split_long(tuples, max_chars)
            parts, pos = [], 0
            for p in parts_t:
                parts.append(ph[pos:pos + len(p)])
                pos += len(p)
        for p in parts:
            if not p:
                continue
            lines.append({
                "text": _text(p),
                "punct": p[-1]["pa"],
                "start": round(p[0]["s"] - 0.04, 3),
                "end": round(p[-1]["e"], 3),
                "seg": p[0]["seg"],
                "deleted": bool(reason),
                "reason": reason,
            })
    for i, l in enumerate(lines):
        l["id"] = i + 1
    return lines


# ---------------------------------------------------------------- 纠错词库（“我的词典”，见 lexicon.py）
def load_corrections():
    import lexicon
    return lexicon.parse()[1]


def remember_correction(wrong, right):
    import lexicon
    lexicon.remember(wrong, right)


def apply_corrections(lines, fx=None):
    """按词典纠错：强制替换 + 英文名词写法 + 中文名词拼音模糊匹配。返回改动处数。"""
    import lexicon
    fx = fx or lexicon.fixer()
    n = 0
    for l in lines:
        l["text"], k = fx.fix(l["text"])
        n += k
    return n


def _ai_pieces(units):
    """按停顿和标点把还保留着的字切成小片段，交给 AI 判断。"""
    pieces, cur = [], []
    live = [x for x in units if not x["del"]]
    for idx, x in enumerate(live):
        cur.append(x)
        nxt = live[idx + 1] if idx + 1 < len(live) else None
        if nxt is None or x["pa"] or nxt["s"] - x["e"] >= 0.2 or len(cur) >= 22:
            pieces.append(cur)
            cur = []
    return pieces


def _apply_ai(units, cfg, progress, terms=None):
    import ai
    pieces = _ai_pieces(units)
    rows = []
    for i, pc in enumerate(pieces, 1):
        gap = 0.0 if i == 1 else round(pc[0]["s"] - pieces[i - 2][-1]["e"], 2)
        rows.append({"id": i, "t": _text(pc), "gap": gap})
    res = ai.review(rows, cfg, progress, terms=terms)
    if len(res) < len(rows) * 0.6:
        raise RuntimeError("AI 返回不完整")
    n_del = n_fix = 0
    for i, pc in enumerate(pieces, 1):
        r = res.get(i)
        if not r:
            continue
        if not r["keep"]:
            reason = r["reason"] or "AI删除"
            for x in pc:
                x["del"] = ("AI·" + reason)[:8]
            n_del += 1
            continue
        fix = r["fix"].strip()
        orig = _text(pc)
        if fix and fix != orig and len(fix) == len(orig):  # 只接受等字数的纠错，保证字和时间一一对应
            pos = 0
            for x in pc:
                x["w"] = fix[pos:pos + len(x["w"])]
                pos += len(x["w"])
            n_fix += 1
    return n_del, n_fix


def _proofread(lines, cfg, terms, progress):
    """AI 第二轮校对：把整理好的字幕（只看保留的句子）连同词典再发给 AI，修正错别字和专有名词。
    字幕文字可以自由修改（不影响剪辑点），所以这一轮能把“克劳德扣的”改成“Claude Code”。"""
    import difflib
    import ai
    kept = [l for l in lines if not l["deleted"]]
    if not kept:
        return 0
    res = ai.proofread([{"id": i, "t": l["text"]} for i, l in enumerate(kept)], cfg, terms, progress)
    n = 0
    for i, new in res.items():
        if not (0 <= i < len(kept)):
            continue
        old = kept[i]["text"]
        new = new.strip().rstrip("，。,.!！?？")
        if not new or new == old or len(new) > len(old) + 14 or len(new) < len(old) * 0.5:
            continue
        sm = difflib.SequenceMatcher(None, old, new)
        same = sum(b.size for b in sm.get_matching_blocks())
        if len(old) > 6 and same / len(old) < 0.4:  # 改动太大，可能是 AI 在改写，不采纳
            continue
        kept[i]["text"] = new
        kept[i]["ai_fix"] = old
        n += 1
    return n


def process(segments, max_chars=13, remove_fillers=True, ai_cfg=None, progress=lambda f: None):
    """返回 (lines, note)。配置了 AI 时由 AI 判断重说和错别字，再按词典做第二轮校对；失败自动退回本地规则。"""
    import ai
    import lexicon
    note = ""
    fx = lexicon.fixer()
    terms = lexicon.parse()[0]
    units = make_stream(segments)
    if ai.ready(ai_cfg):
        clean(units, remove_fillers, retakes=False)
        try:
            n_del, n_fix = _apply_ai(units, ai_cfg, lambda f: progress(0.6 * f), terms)
            note = f"AI 已审阅：删除 {n_del} 处重说/口误"
        except Exception as e:  # noqa: BLE001
            for x in units:
                if x["del"].startswith("AI"):
                    x["del"] = ""
            _mark_retakes(units)
            note = f"AI 审阅没成功（{str(e)[:80]}），已改用本地规则"
            n_fix = 0
    else:
        clean(units, remove_fillers)
        n_fix = 0
    lines = build_lines(units, max_chars)
    n_dict = apply_corrections(lines, fx)
    if ai.ready(ai_cfg):
        try:
            n_fix += _proofread(lines, ai_cfg, terms, lambda f: progress(0.6 + 0.4 * f))
            apply_corrections(lines, fx)  # AI 改完再按词典统一一次写法
            note += f"，校对纠正 {n_fix + n_dict} 处错字"
        except Exception as e:  # noqa: BLE001
            note += f"；AI 校对没成功（{str(e)[:60]}）" + (f"，词典纠正 {n_dict} 处" if n_dict else "")
    elif n_dict:
        note = f"按词典纠正 {n_dict} 处"
    return lines, note


# ---------------------------------------------------------------- 内容概括（用于封面标题和文件名）
_OPENERS = ("大家好", "哈喽", "你好", "今天", "我是", "那么", "所以", "然后", "其实", "就是", "我们", "这个", "那个")


def summarize(lines, max_len=16):
    """从字幕里挑一句最能代表主题的话：关键词权重高、地名机构名优先、避开开场白。"""
    import jieba.analyse
    from engine import plain
    kept = [plain(l["text"]) for l in lines if not l.get("deleted") and plain(l["text"]).strip()]
    if not kept:
        return ""
    full = "".join(kept)
    weights = dict(jieba.analyse.extract_tags(full, topK=30, withWeight=True,
                                              allowPOS=("ns", "nr", "nt", "nz", "n", "vn", "m", "eng")))
    cands = list(kept)
    for a, b in zip(kept, kept[1:]):  # 两句短话拼起来往往更完整
        if len(a) + len(b) <= max_len:
            cands.append(a + b)
    best, best_sc = kept[0], -1.0
    for c in cands:
        if len(c) < 4:
            continue
        sc = 0.0
        for w in pseg.cut(c):
            sc += weights.get(w.word, 0)
            if w.flag in ("ns", "nt", "nz") and len(w.word) >= 2:
                sc += 0.25
        if c.startswith(_OPENERS):
            sc *= 0.5
        sc /= 1 + max(0, len(c) - max_len) * 0.2
        if sc > best_sc:
            best, best_sc = c, sc
    for op in ("大家好", "哈喽", "我是"):
        if best.startswith(op) and len(best) > len(op) + 3:
            best = best[len(op):]
    return best[:max_len]
