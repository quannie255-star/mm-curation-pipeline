"""偏好闭环数据构造（V3 η-a）：persona-oracle、偏好对、DPO 三元组。

产品语义：偏好是「用户协议」——文本化的选择规则（persona），训练目标是
让判官的裁决跟随协议而非客观质量。v1 的 persona 是规则 oracle（非真人
标注），manifest 与报告如实声明；真人 A/B 标注是后续工作。

独立性（沿用 ζ 的纪律）：
- seed 新族 31（训练 seed 23 / judge benchmark 9000 之外）
- 源文档结构性排除 judge_news_v1 源与 judge SFT 已用文档（调用方负责）
- benchmark 与训练数据同 seed 构造但文档不相交（held-out 切分）
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime
from pathlib import Path

# ---- persona 协议（文本即协议：换 persona = 换这段文本，进 manifest）----

PERSONAS: dict[str, dict[str, str]] = {
    "PA": {
        "name": "精炼派",
        "protocol": "只保留核心事实：时间、地点、主体、结果。冗余细节（数字罗列、"
        "引语、背景展开）应删尽删，篇幅越精炼越好。",
    },
    "PB": {
        "name": "求全派",
        "protocol": "必须保留全部信息：数字、引语、背景与细节一个都不能少，完整性优先于篇幅。",
    },
}

PREF_PROMPT = (
    "你是数据质量的偏好裁决员。下面是同一篇文档的两个版本（甲/乙）。\n"
    "用户的偏好协议：\n{protocol}\n\n"
    "【候选甲】\n{a}\n\n【候选乙】\n{b}\n\n"
    "按用户偏好裁决哪个版本更适合作为该用户专属模型的训练数据，"
    '只输出 JSON：{{"choice": "甲", "reason": "<=30字"}}'
)

_DETAIL_RE = re.compile(r"[\d%「」“”]")
_BOILERPLATE = [
    "扫码关注公众号，回复关键词领取福利",
    "点击链接 www.example-promo.cn 立即抢购",
    "阅读原文，下载 APP 查看更多精彩内容",
]


def _fingerprint(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def _split_doc(text: str) -> tuple[str, str, list[str]] | None:
    """新闻行结构 = 标题 + 空行 + 段落们。返回 (标题, 导语, 细节段)；不合格返回 None。"""
    parts = [p for p in text.split("\n") if p.strip()]
    if len(parts) < 3:
        return None
    title, lead, details = parts[0], parts[1], parts[2:]
    if not any(_DETAIL_RE.search(p) for p in details):
        return None
    return title, lead, details


def completion_for(choice: str, reason: str) -> str:
    return json.dumps({"choice": choice, "reason": reason}, ensure_ascii=False)


CANDIDATE_MAX_CHARS = 350  # 候选截断：保 prompt 完整含指令尾（#59 家族教训）


def _place(gold_text: str, other_text: str, rng: random.Random):
    """50/50 决定金标在甲位还是乙位；返回 (prompt 槽位文本, 金标位)。候选截到 350 字。"""
    gold_text = gold_text[:CANDIDATE_MAX_CHARS]
    other_text = other_text[:CANDIDATE_MAX_CHARS]
    if rng.randrange(2) == 0:
        return {"甲": gold_text, "乙": other_text}, "甲"
    return {"甲": other_text, "乙": gold_text}, "乙"


def build_pref_items(
    corpus_texts: list[dict],
    *,
    n_train_docs: int = 400,
    n_eval_docs: int = 60,
    n_control_docs: int = 15,
    seed: int = 31,
) -> tuple[list[dict], list[dict]]:
    """构造 DPO 三元组（训练）与冻结评测题（benchmark）。

    corpus_texts: [{"id","title","text"}]——须与 judge SFT/judge benchmark 源不相交
    （结构性排除由调用方完成）。返回 (train_triples, eval_items)。
    """
    rng = random.Random(seed)
    docs = []
    for d in corpus_texts:
        sp = _split_doc(d["text"])
        if sp is None:
            continue
        title, lead, _ = sp
        docs.append(
            {
                "id": d["id"],
                "s": f"{title}\n\n{lead}",
                "f": d["text"],
            }
        )
    need = n_train_docs + n_eval_docs + n_control_docs
    if len(docs) < need:
        raise ValueError(f"可切分文档不足：需 {need}，只有 {len(docs)}")
    rng.shuffle(docs)
    train_docs = docs[:n_train_docs]
    eval_docs = docs[n_train_docs : n_train_docs + n_eval_docs]
    control_docs = docs[-n_control_docs:]

    triples: list[dict] = []
    for persona in PERSONAS:
        for d in train_docs:
            gold = "S" if persona == "PA" else "F"
            slots, gold_pos = _place(d[gold.lower()], d["f" if gold == "S" else "s"], rng)
            wrong_pos = "乙" if gold_pos == "甲" else "甲"
            # 最小对：chosen/rejected 唯一差异是字母（reason 固定）——否则 DPO
            # 梯度被 reason 模板 token 吃掉，字母判别学不到（η-a 首训学崩实测）
            triples.append(
                {
                    "persona": persona,
                    "kind": "main",
                    "prompt": PREF_PROMPT.format(
                        protocol=PERSONAS[persona]["protocol"], a=slots["甲"], b=slots["乙"]
                    ),
                    "chosen": completion_for(gold_pos, "符合用户偏好协议"),
                    "rejected": completion_for(wrong_pos, "符合用户偏好协议"),
                    "gold": gold_pos,
                    "gold_variant": gold,
                    "source_id": d["id"],
                }
            )
        # 对照对：带广告损伤的 F vs 干净 S——两个 persona 都必须否决损伤候选
        # （偏好裁决的前提是候选质量合格；这条教会判官「先看污染，再谈偏好」）
        for d in train_docs[:40]:
            damaged = _BOILERPLATE[rng.randrange(len(_BOILERPLATE))] + "\n" + d["f"]
            slots, gold_pos = _place(d["s"], damaged, rng)
            wrong_pos = "乙" if gold_pos == "甲" else "甲"
            triples.append(
                {
                    "persona": persona,
                    "kind": "control",
                    "prompt": PREF_PROMPT.format(
                        protocol=PERSONAS[persona]["protocol"], a=slots["甲"], b=slots["乙"]
                    ),
                    "chosen": completion_for(gold_pos, "符合用户偏好协议"),
                    "rejected": completion_for(wrong_pos, "符合用户偏好协议"),
                    "gold": gold_pos,
                    "gold_variant": "S",
                    "source_id": d["id"],
                }
            )
    rng.shuffle(triples)

    items: list[dict] = []
    for d in eval_docs:
        for persona in PERSONAS:
            gold = "S" if persona == "PA" else "F"
            slots, gold_pos = _place(d[gold.lower()], d["f" if gold == "S" else "s"], rng)
            items.append(
                {
                    "id": "pref-" + _fingerprint(persona + slots["甲"] + slots["乙"]),
                    "persona": persona,
                    "kind": "main",
                    "prompt": PREF_PROMPT.format(
                        protocol=PERSONAS[persona]["protocol"], a=slots["甲"], b=slots["乙"]
                    ),
                    "gold": gold_pos,
                    "gold_variant": gold,
                    "variant_map": {
                        gold_pos: gold,
                        ("乙" if gold_pos == "甲" else "甲"): "F" if gold == "S" else "S",
                    },
                    "source_id": d["id"],
                }
            )
    for d in control_docs:
        damaged = _BOILERPLATE[rng.randrange(len(_BOILERPLATE))] + "\n" + d["f"]
        for persona in PERSONAS:
            slots, gold_pos = _place(d["s"], damaged, rng)
            items.append(
                {
                    "id": "pref-" + _fingerprint(persona + slots["甲"] + slots["乙"]),
                    "persona": persona,
                    "kind": "control",
                    "prompt": PREF_PROMPT.format(
                        protocol=PERSONAS[persona]["protocol"], a=slots["甲"], b=slots["乙"]
                    ),
                    "gold": gold_pos,
                    "gold_variant": "S",
                    "variant_map": {gold_pos: "S", ("乙" if gold_pos == "甲" else "甲"): "F"},
                    "source_id": d["id"],
                }
            )
    return triples, items


def write_benchmark(items: list[dict], out_dir: Path, *, train_jsonl: Path | None) -> dict:
    """冻结偏好 benchmark（复用 ζ 的泄漏检查纪律）。"""
    from mm_curation.benchmarks.builder import _leak_check

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "items.jsonl").write_text(
        "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
        encoding="utf-8",
    )
    if train_jsonl and Path(train_jsonl).exists():
        leak = _leak_check(items, Path(train_jsonl))
    else:
        leak = {
            "train_file": str(train_jsonl) if train_jsonl else None,
            "md5_leaks": [],
            "minhash_leaks": [],
            "note": "训练文件未产出；结构性隔离由源文档排除保证，泄漏检查随训练文件补跑",
        }
    manifest = {
        "benchmark": "pref_news_v1",
        "version": "v1",
        "domain": "中文新闻正文的详略偏好裁决（PA 精炼派 / PB 求全派）",
        "n_items": len(items),
        "balance": {
            key: sum(1 for it in items if f"{it['persona']}/{it['kind']}" == key)
            for key in sorted({f"{it['persona']}/{it['kind']}" for it in items})
        },
        "personas": {k: v["protocol"] for k, v in PERSONAS.items()},
        "seed": 31,
        "leakage_check": leak,
        "label_protocol": "gold=persona-oracle 的选择；对照题（kind=control）中损伤候选"
        "必须被否决——偏好裁决的前提是候选质量合格；v1 标注为"
        " persona-oracle（非真人标注），如实声明",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


# ---- θ：真人标注 v2 → 个人偏好判官（工坊通道，设计表 design_tables.md θ 节）----
#
# 与 η-a oracle 通道的关键差异：标注来源是真人 A/B 点击（或模拟用户的 oracle 点击，
# labeler 字段区分），金标即点击本身。两本账（oracle 流程验收 / 真人验收）分开报告。

USER_PERSONA = "USER"
LABELER_HUMAN = "human"
LABELER_ORACLE = "oracle"
SEED_USER = 53  # seed 新族：训练 23 / judge 9000 / pref 31 / ext 41 / η-b' 47 之外
DEFAULT_PROTOCOL = "只保留核心事实：时间、地点、主体、结果。冗余细节应删尽删。"
MIN_PAIRS = 80  # 最低可训量：holdout 25% 后 train ≥60（含对照）·eval ≥20
CONTROL_TRAIN_FRACTION = 0.1  # 训练对照三元组占比（η-a 为 40/400≈10%）
CONTROL_EVAL_MAX = 15  # 评测对照题上限


def make_label_row(
    *,
    protocol: str,
    source_id: str,
    cand_a: str,
    cand_b: str,
    variant_a: str,
    variant_b: str,
    choice: str,
    labeler: str,
    note: str = "",
    session: str = "studio",
) -> dict:
    """v2 标注行：候选**全文**落盘（v1 只存字数、无法还原 DPO 对的教训）。"""
    if choice not in ("甲", "乙", "REJECT"):
        raise ValueError(f"choice 必须是 甲/乙/REJECT，得到 {choice!r}")
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "session": session,
        "protocol": protocol,
        "source_id": source_id,
        "cand_a": cand_a,
        "cand_b": cand_b,
        "variant_a": variant_a,
        "variant_b": variant_b,
        "choice": choice,
        "labeler": labeler,
        "note": note,
    }


def occupied_source_ids() -> set[str]:
    """全部既有任务的源文档占用（示例语料排除用；文件缺失则跳过该来源）。

    含 judge SFT 选样复现（build_pref_data.py 同款：同 corpus 同 seed → 同一批 500）。
    """
    from mm_curation.tuning.judge_data import TRAIN_SEED

    occ: set[str] = set()
    for p in (
        Path("benchmarks/judge_news_v1/items.jsonl"),
        Path("benchmarks/pref_news_v1/items.jsonl"),
        Path("benchmarks/ext_news_v1/items.jsonl"),
        Path("data/interim/pref_dpo.jsonl"),
        Path("data/interim/ext_dpo.jsonl"),
    ):
        if not p.exists():
            continue
        for ln in p.read_text(encoding="utf-8").split("\n"):
            if ln.strip():
                sid = json.loads(ln).get("source_id")
                if sid:
                    occ.add(sid)
    news = Path("data/raw/news_corpus.jsonl")
    if news.exists():
        rows = [json.loads(ln) for ln in news.read_text(encoding="utf-8").split("\n") if ln.strip()]
        pool = sorted({r["id"] for r in rows if len(r["text"]) >= 200} - occ)
        random.Random(TRAIN_SEED).shuffle(pool)
        occ |= set(pool[:500])
    return occ


def load_news_corpus_excluded() -> list[dict]:
    """示例语料：新闻爬取产物（≥200 字），结构性排除全部既有任务占用。"""
    news = Path("data/raw/news_corpus.jsonl")
    if not news.exists():
        return []
    occ = occupied_source_ids()
    out = []
    for ln in news.read_text(encoding="utf-8").split("\n"):
        if not ln.strip():
            continue
        r = json.loads(ln)
        if len(r["text"]) >= 200 and r["id"] not in occ:
            out.append({"id": r["id"], "title": r["meta"]["title"], "text": r["text"]})
    return out


def make_variant_pairs(corpus_texts: list[dict]) -> list[dict]:
    """文档 → S/F 变体对（待标注队列项）。切分失败的文档直接跳过（调用方报可用率）。"""
    pairs = []
    for d in corpus_texts:
        sp = _split_doc(d["text"])
        if sp is None:
            continue
        title, lead, _ = sp
        pairs.append(
            {
                "source_id": d["id"],
                "s": f"{title}\n\n{lead}",
                "f": d["text"],
            }
        )
    return pairs


def oracle_labels_from_corpus(
    corpus_texts: list[dict],
    *,
    protocol: str = DEFAULT_PROTOCOL,
    prefer: str = "S",
    n_docs: int = 250,
    seed: int = SEED_USER,
) -> list[dict]:
    """模拟用户（流程验收账）：规则 oracle 生成 v2 标注行——精炼派点击 S，求全派点 F。

    走与真人完全相同的 v2 通道（同样的候选呈现、同样的落盘格式），
    训练/评测代码对两种来源不可区分；数字按 labeler 分账报告。
    """
    if prefer not in ("S", "F"):
        raise ValueError(f"prefer 必须是 S/F，得到 {prefer!r}")
    rng = random.Random(seed)
    pairs = make_variant_pairs(corpus_texts)
    if len(pairs) < n_docs:
        raise ValueError(f"可切分文档不足：需 {n_docs}，只有 {len(pairs)}")
    rng.shuffle(pairs)
    rows = []
    for p in pairs[:n_docs]:
        gold, other = prefer, ("F" if prefer == "S" else "S")
        slots, gold_pos = _place(p[gold.lower()], p[other.lower()], rng)
        va = gold if gold_pos == "甲" else other
        vb = other if gold_pos == "甲" else gold
        rows.append(
            make_label_row(
                protocol=protocol,
                source_id=p["source_id"],
                cand_a=slots["甲"],
                cand_b=slots["乙"],
                variant_a=va,
                variant_b=vb,
                choice=gold_pos,
                labeler=LABELER_ORACLE,
                note=f"oracle prefer={prefer}",
            )
        )
    return rows


def _row_gold_variant(r: dict) -> str:
    return r["variant_a"] if r["choice"] == "甲" else r["variant_b"]


def _payload(text: str) -> str:
    """prompt → 候选正文投影：跳过常量模板头（指令+协议），截掉尾部输出指令。

    指纹必须打在有效载荷上——共享模板前后缀会支配 MinHash 极小值，造成
    大面积假阳性（pref_news_v1 冻结 manifest 的 150/150 教训，笔记 #63）。
    """
    marker = "【候选甲】"
    t = text.split(marker, 1)[1] if marker in text else text
    tail = "按用户偏好裁决"
    return t.split(tail, 1)[0] if tail in t else t


def build_user_pref_data(
    labels: list[dict],
    *,
    holdout_ratio: float = 0.25,
    seed: int = SEED_USER,
    limit: int = 0,
    min_pairs: int = MIN_PAIRS,
    eval_source_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """v2 标注 → (DPO 三元组, 冻结评测题, 统计)。

    纪律（设计表 θ 决策点 2）：
    - holdout 以**偏好对**为单位（同一对文本不得同时进训练与评测）
    - chosen/rejected 最小对：只差 甲/乙 字母（#60 红线内建）
    - REJECT（两个都不合格）无法构造 chosen，剔除进统计
    - 训练对照（干净 vs 广告损伤）教「先看污染再谈偏好」；评测对照同构出题
    - limit：学习曲线实验通道（取前 N 个有效对再切分，配 min_pairs 放宽下限）
    - eval_source_ids：「加量不换考卷」通道——这些来源的对强制进评测（冻结
      benchmark 不因追加标注而改版），其余全进训练；None 时按 holdout_ratio 切分
    """
    protocols = {r["protocol"] for r in labels}
    if len(protocols) != 1:
        raise ValueError(f"标注中出现 {len(protocols)} 种协议文本——同一判官只能有一个协议")
    protocol = protocols.pop()
    rows = [r for r in labels if r["choice"] in ("甲", "乙")]
    n_reject = len(labels) - len(rows)
    if limit and limit > 0:
        rows = rows[:limit]
    if len(rows) < min_pairs:
        raise ValueError(f"有效标注不足：需 ≥{min_pairs}（甲/乙），只有 {len(rows)}")

    rng = random.Random(seed)
    if eval_source_ids is not None:
        eval_idx = {i for i, r in enumerate(rows) if r["source_id"] in eval_source_ids}
    else:
        order = list(range(len(rows)))
        rng.shuffle(order)
        n_eval = max(1, round(len(rows) * holdout_ratio))
        eval_idx = set(order[:n_eval])
    train_rows = [r for i, r in enumerate(rows) if i not in eval_idx]
    eval_rows = [r for i, r in enumerate(rows) if i in eval_idx]

    def _prompt(r: dict) -> str:
        return PREF_PROMPT.format(
            protocol=protocol,
            a=r["cand_a"][:CANDIDATE_MAX_CHARS],
            b=r["cand_b"][:CANDIDATE_MAX_CHARS],
        )

    triples: list[dict] = []
    items: list[dict] = []
    for r in train_rows:
        gold, other = r["choice"], ("乙" if r["choice"] == "甲" else "甲")
        triples.append(
            {
                "persona": USER_PERSONA,
                "kind": "main",
                "prompt": _prompt(r),
                "chosen": completion_for(gold, "符合用户偏好协议"),
                "rejected": completion_for(other, "符合用户偏好协议"),
                "gold": gold,
                "gold_variant": _row_gold_variant(r),
                "source_id": r["source_id"],
            }
        )
    for r in eval_rows:
        gold, other = r["choice"], ("乙" if r["choice"] == "甲" else "甲")
        items.append(
            {
                "id": "prefu-" + _fingerprint(r["choice"] + r["cand_a"] + r["cand_b"]),
                "persona": USER_PERSONA,
                "kind": "main",
                "prompt": _prompt(r),
                "gold": gold,
                "gold_variant": _row_gold_variant(r),
                "variant_map": {"甲": r["variant_a"], "乙": r["variant_b"]},
                "source_id": r["source_id"],
            }
        )

    # 对照（训练）：用户选中的干净候选 vs 其广告损伤版——金标恒为干净版
    n_ctrl = max(1, int(len(train_rows) * CONTROL_TRAIN_FRACTION))
    for r in train_rows[:n_ctrl]:
        clean = r["cand_a"] if r["choice"] == "甲" else r["cand_b"]
        damaged = _BOILERPLATE[rng.randrange(len(_BOILERPLATE))] + "\n" + clean
        slots, gold_pos = _place(clean, damaged, rng)
        wrong_pos = "乙" if gold_pos == "甲" else "甲"
        triples.append(
            {
                "persona": USER_PERSONA,
                "kind": "control",
                "prompt": PREF_PROMPT.format(protocol=protocol, a=slots["甲"], b=slots["乙"]),
                "chosen": completion_for(gold_pos, "符合用户偏好协议"),
                "rejected": completion_for(wrong_pos, "符合用户偏好协议"),
                "gold": gold_pos,
                "gold_variant": "clean",
                "source_id": r["source_id"],
            }
        )
    for r in eval_rows[:CONTROL_EVAL_MAX]:
        clean = r["cand_a"] if r["choice"] == "甲" else r["cand_b"]
        damaged = _BOILERPLATE[rng.randrange(len(_BOILERPLATE))] + "\n" + clean
        slots, gold_pos = _place(clean, damaged, rng)
        other_pos = "乙" if gold_pos == "甲" else "甲"
        items.append(
            {
                "id": "prefu-" + _fingerprint("ctrl" + slots["甲"] + slots["乙"]),
                "persona": USER_PERSONA,
                "kind": "control",
                "prompt": PREF_PROMPT.format(protocol=protocol, a=slots["甲"], b=slots["乙"]),
                "gold": gold_pos,
                "gold_variant": "clean",
                "variant_map": {gold_pos: "clean", other_pos: "damaged"},
                "source_id": r["source_id"],
            }
        )
    rng.shuffle(triples)
    rng.shuffle(items)

    stats = {
        "protocol": protocol,
        "seed": seed,
        "holdout_ratio": holdout_ratio,
        "n_labels": len(labels),
        "n_valid": len(rows),
        "n_reject": n_reject,
        "n_train_main": len(train_rows),
        "n_train_control": min(n_ctrl, len(train_rows)),
        "n_eval_main": len(eval_rows),
        "n_eval_control": min(CONTROL_EVAL_MAX, len(eval_rows)),
        "labelers": {
            k: sum(1 for r in labels if r["labeler"] == k)
            for k in {r["labeler"] for r in labels}
        },
    }
    return triples, items, stats


def write_user_benchmark(
    items: list[dict], out_dir: Path, *, train_jsonl: Path | None, stats: dict
) -> dict:
    """冻结个人偏好 benchmark（pref_user_v1）：真人标注声明 + 泄漏检查进 manifest。

    真人标注属个人数据，产物默认留在本地不入库（oracle 验收账由 seed 完全可复现）。
    """
    from mm_curation.benchmarks.builder import _leak_check

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "items.jsonl").write_text(
        "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
        encoding="utf-8",
    )
    if train_jsonl and Path(train_jsonl).exists():
        # 指纹投影到候选正文（_payload），训练侧同步投影——见 _payload 注释
        train_raw = [
            json.loads(ln)
            for ln in Path(train_jsonl).read_text(encoding="utf-8").split("\n")
            if ln.strip()
        ]
        leak = _leak_check(
            [{"id": it["id"], "text": _payload(it["prompt"])} for it in items],
            Path(train_jsonl),
            train_rows=[
                {"id": r.get("id", f"train{i}"), "text": _payload(r.get("prompt", ""))}
                for i, r in enumerate(train_raw)
            ],
        )
    else:
        leak = {
            "train_file": None,
            "md5_leaks": [],
            "minhash_leaks": [],
            "note": "训练文件未产出",
        }
    manifest = {
        "benchmark": "pref_user_v1",
        "version": "v1",
        "domain": "用户个人偏好裁决（wizard 标注通道）",
        "n_items": len(items),
        "balance": {
            key: sum(1 for it in items if f"{it['persona']}/{it['kind']}" == key)
            for key in sorted({f"{it['persona']}/{it['kind']}" for it in items})
        },
        "personas": {USER_PERSONA: stats["protocol"]},
        "seed": stats["seed"],
        "labeler": stats["labelers"],
        "stats": {k: v for k, v in stats.items() if k != "protocol"},
        "label_protocol": "gold=标注者 A/B 点击（labeler 见 labeler 字段；oracle=模拟用户，"
        "human=真人）；对照题（kind=control）中损伤候选必须被否决——偏好裁决的前提是"
        "候选质量合格；产品语义 = 判官复现标注者的显性一致性（held-out 点击未进训练）",
        "leakage_check": {
            **leak,
            "note": "md5 对投影后候选正文结算；minhash 为字节级 4-gram，在 CJK 上因 "
            "UTF-8 首字节取值稀少而灵敏度受限（计数偏高不必然是真泄漏）——结构性隔离 "
            "由按偏好对的 holdout 切分硬保证（同对文本不得同时进训练与评测）",
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
