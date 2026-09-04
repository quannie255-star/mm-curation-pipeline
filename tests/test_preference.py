"""偏好数据构造单测：persona-oracle / 顺序随机化 / 对照题 / 结构完整性。"""

from __future__ import annotations

import json

import pytest

from mm_curation.tuning.preference import (
    PERSONAS,
    PREF_PROMPT,
    _split_doc,
    build_pref_items,
)


def _doc(i: int) -> dict:
    body = (
        f"{i}月{i}日，某地发生了一件值得记录的事，主体已确认结果已公布。\n"
        f"据介绍，该项目投资{i * 10}%的资金，负责人表示「进展顺利」。\n"
        f"背景方面，这件事可以追溯到多年前的另一项计划，具体细节从略。"
    )
    return {"id": f"news{i:06d}", "title": f"标题{i}", "text": f"标题{i}\n\n{body}"}


def test_split_doc_variants():
    d = _doc(1)
    sp = _split_doc(d["text"])
    assert sp is not None
    title, lead, details = sp
    assert title == "标题1" and "月1日" in lead
    assert len(details) == 2


def test_split_doc_rejects_short():
    assert _split_doc("只有标题\n\n一段") is None


def test_build_items_structure_and_balance():
    corpus = [_doc(i) for i in range(30)]
    triples, items = build_pref_items(corpus, n_train_docs=12, n_eval_docs=6, n_control_docs=3)
    # 每 persona 12 主对 + 12 对照对（对照取训练文档前 40）= 24 三元组，共 48
    assert len(triples) == 48
    for t in triples:
        assert t["persona"] in PERSONAS
        obj_c, obj_r = json.loads(t["chosen"]), json.loads(t["rejected"])
        assert obj_c["choice"] in ("甲", "乙") and obj_r["choice"] in ("甲", "乙")
        assert obj_c["choice"] != obj_r["choice"]
        assert t["prompt"].startswith("你是数据质量的偏好裁决员")
        assert "用户的偏好协议" in t["prompt"]
    # 评测题：6 文档 × 2 persona + 3 对照 × 2 persona = 18
    assert len(items) == 18
    kinds = {(it["persona"], it["kind"]) for it in items}
    assert ("PA", "main") in kinds and ("PA", "control") in kinds
    # 金标一致性：main 题中 PA 金标变体必为 S，PB 必为 F
    for it in items:
        if it["kind"] == "main":
            expect = "S" if it["persona"] == "PA" else "F"
            assert it["gold_variant"] == expect
        else:
            assert it["gold_variant"] == "S"  # 对照题：干净 S 必为金标
    # 顺序随机化：甲位金标不应全占（18 题里两种位置都要出现）
    assert len({it["gold"] for it in items}) == 2


def test_corpus_insufficient_rejected():
    with pytest.raises(ValueError, match="不足"):
        build_pref_items([_doc(i) for i in range(5)], n_train_docs=10)


def test_prompt_contains_both_candidates():
    corpus = [_doc(i) for i in range(10)]
    _, items = build_pref_items(corpus, n_train_docs=4, n_eval_docs=2, n_control_docs=1)
    for it in items:
        assert "【候选甲】" in it["prompt"] and "【候选乙】" in it["prompt"]
        assert PREF_PROMPT.split("{protocol}")[0] in it["prompt"]
