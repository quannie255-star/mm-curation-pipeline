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
