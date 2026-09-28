"""抽取忠实性判官数据构造（V3 η-b）：客观 oracle + 三损伤 + 最小对 DPO。

任务语义与 η-a 相反（客观对齐计数 vs 主观偏好），接口形态刻意复用
（A/B 双候选 + JSON 裁决）——证明「同一骨架可套多任务」时，变的必须是
任务语义，不变的是协议位。

忠实性协议（文本即协议，进 manifest）：抽取中的每条事实必须能在原文
找到直接依据；原文的关键事实（数字、引语、结论）不得遗漏。

三损伤（均匀分布，防判官学单一模式）：
- number_swap 数字篡改：相近值替换（±1~9 或换 2-3 位数字），防过易
- hallucinate 幻觉注入：混入一条他文事实句（跨文档幻觉，最可判）
- omit 关键遗漏：删 1-2 条事实句（最难，分层报告不设线）

最小对（#60 教训内建）：chosen/rejected 唯一差异是甲/乙字母。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path

EXT_PROMPT = (
    "你是数据抽取的质量审核员。下面是一篇原文和两份从原文抽取的要点清单（甲/乙）。\n"
    "忠实性判定规则：抽取中的每一条事实必须能在原文找到直接依据，"
    "原文的关键事实（数字、引语、结论）不得遗漏。\n\n"
    "【原文】\n{source}\n\n"
    "【抽取甲】\n{a}\n\n【抽取乙】\n{b}\n\n"
    '按忠实性裁决哪份抽取合格，只输出 JSON：{{"choice": "甲", "reason": "<=30字"}}'
)

_FACT_RE = re.compile(r"[\d%]|「[^」]+」|“[^”]+”")
_SENT_SPLIT = re.compile(r"(?<=[。！？])")
NUM_RE = re.compile(r"\d+")

# 原文窗口：事实产量在 800-1500 字窗口近乎平坦（476→509 篇），取 500 字换
# 1.5B 训练的显存/速度可训性（窗口权衡实测见 η-b 开发记录）
SOURCE_MAX_CHARS = 500
CANDIDATE_MAX_CHARS = 320  # 候选截断：保 prompt 完整含指令尾


def _fingerprint(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def extract_facts(text: str) -> tuple[str, list[str]]:
    """原文窗口内的 (source, 事实句列表)：含数字/引语、≥15 字的句子，保序。"""
    source = text[:SOURCE_MAX_CHARS]
    facts = []
    for para in source.split("\n")[1:]:
        for sent in _SENT_SPLIT.split(para):
            sent = sent.strip()
            if len(sent) >= 15 and _FACT_RE.search(sent):
                facts.append(sent)
    return source, facts


def _swap_number(sent: str, rng: random.Random) -> str:
    """相近值数字篡改：±1~9 或数字换位（保证确实变化）。"""

    def repl(m: re.Match) -> str:
        old = m.group()
        for _ in range(10):
            if len(old) == 1:
                new = str((int(old) + rng.randint(1, 9)) % 10)
            else:
                new = str(int(old) + rng.choice([-9, -3, -2, -1, 1, 2, 3, 9]))
                if len(new) != len(old):  # 保持位数，防过易
                    continue
            if new != old:
                return new
        return old + "0" if len(old) < 4 else old[:-1] + ("9" if old[-1] != "9" else "8")

    out, n = NUM_RE.subn(repl, sent, count=1)
    return out if n else sent


def build_ext_items(
    corpus_texts: list[dict],
    *,
    n_train: int = 400,
    n_eval: int = 100,
    seed: int = 41,
    other_pool_size: int = 40,
) -> tuple[list[dict], list[dict]]:
    """构造抽取忠实性的 DPO 三元组与冻结评测题。

    corpus_texts: [{"id","title","text"}]——须与既有占用不相交（调用方排除）。
    返回 (train_triples, eval_items)；kind = 损伤类型（number_swap/hallucinate/omit）。
    """
    rng = random.Random(seed)
    docs = []
    for d in corpus_texts:
        source, facts = extract_facts(d["text"])
        if len(facts) >= 3:
            docs.append({"id": d["id"], "source": source, "facts": facts})
    need = n_train + n_eval
    if len(docs) < need:
        raise ValueError(f"可构造文档不足：需 {need}，只有 {len(docs)}")
    rng.shuffle(docs)
    train_docs = docs[:n_train]
    eval_docs = docs[n_train:need]
    # 幻觉句池：他文事实句（跨文档）
    foreign_pool = [f for d in docs[-other_pool_size:] for f in d["facts"][1:3]]

    def good_ext(facts: list[str], rng: random.Random) -> tuple[str, list[str]]:
        k = rng.randint(3, min(5, len(facts)))
        picked = facts[:k]
        shuffled = picked[:]
        rng.shuffle(shuffled)
        return "\n".join(f"- {s}" for s in shuffled), picked

    def corrupt(
        ext: str, picked: list[str], foreign: list[str], rng: random.Random
    ) -> tuple[str, str]:
        kind = ("number_swap", "hallucinate", "omit")[rng.randrange(3)]
        lines = ext.split("\n")
        if kind == "number_swap":
            i = rng.randrange(len(lines))
            lines[i] = "- " + _swap_number(lines[i][2:], rng)
        elif kind == "hallucinate" and foreign:
            pos = rng.randrange(len(lines) + 1)
            lines.insert(pos, "- " + rng.choice(foreign))
        else:  # omit
            drop = 1 if len(picked) <= 4 else rng.randint(1, 2)
            lines = lines[:-drop] if len(lines) > drop else lines[:1]
        return "\n".join(lines), kind

    triples: list[dict] = []
    for d in train_docs:
        ext, picked = good_ext(d["facts"], rng)
        bad, kind = corrupt(ext, picked, foreign_pool, rng)
        slots, gold_pos = (
            ({"甲": ext, "乙": bad}, "甲")
            if rng.randrange(2) == 0
            else ({"甲": bad, "乙": ext}, "乙")
        )
        wrong = "乙" if gold_pos == "甲" else "甲"
        triples.append(
            {
                "persona": "EXT",
                "kind": kind,
                "prompt": EXT_PROMPT.format(source=d["source"], a=slots["甲"], b=slots["乙"]),
                "chosen": completion_for(gold_pos),
                "rejected": completion_for(wrong),
                "gold": gold_pos,
                "source_id": d["id"],
            }
        )
    rng.shuffle(triples)

    items: list[dict] = []
    for d in eval_docs:
        ext, picked = good_ext(d["facts"], rng)
        bad, kind = corrupt(ext, picked, foreign_pool, rng)
        slots, gold_pos = (
            ({"甲": ext, "乙": bad}, "甲")
            if rng.randrange(2) == 0
            else ({"甲": bad, "乙": ext}, "乙")
        )
        items.append(
            {
                "id": "ext-" + _fingerprint(slots["甲"] + slots["乙"]),
                "persona": "EXT",
                "kind": kind,
                "prompt": EXT_PROMPT.format(source=d["source"], a=slots["甲"], b=slots["乙"]),
                "gold": gold_pos,
                "gold_variant": "faithful",
                "variant_map": {},
                "source_id": d["id"],
            }
        )
    return triples, items


def completion_for(choice: str) -> str:
    return json.dumps({"choice": choice, "reason": "符合忠实性协议"}, ensure_ascii=False)


def write_benchmark(items: list[dict], out_dir: Path, *, train_jsonl: Path | None) -> dict:
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
            "note": "训练文件未产出；结构性隔离由源文档排除保证",
        }
    manifest = {
        "benchmark": "ext_news_v1",
        "version": "v1",
        "domain": "中文新闻原文的要点抽取忠实性判定（客观 oracle：事实-原文对齐）",
        "n_items": len(items),
        "balance": {
            key: sum(1 for it in items if f"EXT/{it['kind']}" == key)
            for key in sorted({f"EXT/{it['kind']}" for it in items})
        },
        "seed": 41,
        "protocol": EXT_PROMPT.split("【原文】")[0].split("。", 1)[1].strip(),
        "leakage_check": leak,
        "label_protocol": "gold=忠实抽取所在槽位；损伤三类均匀分布（数字篡改/幻觉注入/"
        "关键遗漏）；候选与原文窗口均截断（原文 800 字/候选 320 字）",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


# ---- η-b'：分解式逐条忠实性判官（任务重设计，绕整体裁决能力悬崖）----

FACT_PROMPT = (
    "你是数据抽取的忠实性审核员。判断下面的事实陈述是否能从原文中找到直接依据。\n"
    "判定规则：陈述中的每个细节（数字、引语、主体、结论）都必须与原文一致。\n\n"
    "【原文】\n{source}\n\n【事实陈述】\n{fact}\n\n"
    '只输出 JSON：{{"supported": true, "reason": "依据比对"}}'
)


def completion_for_fact(supported: bool) -> str:
    return json.dumps({"supported": supported, "reason": "依据比对"}, ensure_ascii=False)


def build_fact_items(
    corpus_texts: list[dict],
    *,
    n_train_docs: int = 250,
    n_eval_docs: int = 80,
    seed: int = 47,
) -> tuple[list[dict], list[dict]]:
    """逐条忠实性数据：每文档 1 supported 三元组 + 1 corrupted 三元组（训练）；
    评测题 = supported 与 corrupted（number_swap / cross_doc 交替）各一。"""
    rng = random.Random(seed)
    docs = []
    for d in corpus_texts:
        source, facts = extract_facts(d["text"])
        if len(facts) >= 2:
            docs.append({"id": d["id"], "source": source, "facts": facts})
    need = n_train_docs + n_eval_docs
    if len(docs) < need:
        raise ValueError(f"可构造文档不足：需 {need}，只有 {len(docs)}")
    rng.shuffle(docs)
    train_docs = docs[:n_train_docs]
    eval_docs = docs[n_train_docs:need]
    foreign_pool = [f for d in docs[-30:] for f in d["facts"][1:3]]

    triples: list[dict] = []
    for n, d in enumerate(train_docs):
        supported_fact = rng.choice(d["facts"][:2])
        other_fact = rng.choice([f for f in d["facts"] if f != supported_fact])
        # supported 三元组
        triples.append(_fact_triple(d, supported_fact, True, rng, "supported"))
        # unsupported 三元组：偶数序号数字篡改，奇数序号他文幻觉
        bad = _swap_number(other_fact, rng) if n % 2 == 0 else rng.choice(foreign_pool)
        kind = "number_swap" if n % 2 == 0 else "cross_doc"
        triples.append(_fact_triple(d, bad, False, rng, kind))
    rng.shuffle(triples)

    items: list[dict] = []
    for n, d in enumerate(eval_docs):
        supported_fact = rng.choice(d["facts"][:2])
        items.append(_fact_item(d, supported_fact, True, rng, "supported", n))
        other_fact = rng.choice([f for f in d["facts"] if f != supported_fact])
        if n % 2 == 0:
            bad, kind = _swap_number(other_fact, rng), "number_swap"
        else:
            bad, kind = rng.choice(foreign_pool), "cross_doc"
        items.append(_fact_item(d, bad, False, rng, kind, n))
    return triples, items


def _place_pair(chosen: str, rejected: str, rng: random.Random):
    """50/50 决定 chosen 在甲/乙位；返回 (槽位字典, chosen 位)。"""
    if rng.randrange(2) == 0:
        return {"甲": chosen, "乙": rejected}, "甲"
    return {"甲": rejected, "乙": chosen}, "乙"


def _fact_triple(d: dict, fact: str, supported: bool, rng: random.Random, kind: str) -> dict:
    slots, gold_pos = _place_pair(
        completion_for_fact(supported), completion_for_fact(not supported), rng
    )
    wrong = "乙" if gold_pos == "甲" else "甲"
    return {
        "persona": "EXTF",
        "kind": kind,
        "prompt": FACT_PROMPT.format(source=d["source"], fact=fact),
        "chosen": slots[gold_pos],
        "rejected": slots[wrong],
        "gold": str(supported),
        "source_id": d["id"],
    }


def _fact_item(d: dict, fact: str, supported: bool, rng: random.Random, kind: str, n: int) -> dict:
    return {
        "id": f"fact-{_fingerprint(fact + str(n))}",
        "persona": "EXTF",
        "kind": kind,
        "prompt": FACT_PROMPT.format(source=d["source"], fact=fact),
        "gold": str(supported).lower(),
        "source_id": d["id"],
    }


def write_fact_benchmark(items: list[dict], out_dir: Path, *, train_jsonl: Path | None) -> dict:
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
            "note": "训练文件未产出",
        }
    manifest = {
        "benchmark": "fact_ext_v1",
        "version": "v1",
        "domain": "中文新闻原文的单条事实忠实性判定（分解式，客观 oracle）",
        "n_items": len(items),
        "balance": {
            key: sum(1 for it in items if f"EXTF/{it['kind']}" == key)
            for key in sorted({f"EXTF/{it['kind']}" for it in items})
        },
        "seed": 47,
        "leakage_check": leak,
        "label_protocol": "gold=supported(逐字原文) 的真假；number_swap=相近数字篡改；"
        "cross_doc=他文事实（纯幻觉）；源文档复用 ext 任务占用（任务不同"
        "标签独立，不构成泄漏），judge/pref 占用仍全数排除",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
