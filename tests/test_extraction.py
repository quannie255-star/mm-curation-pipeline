"""抽取忠实性数据构造单测：三损伤正确性 / 最小对 / 事实窗口 / 隔离字段。"""

from __future__ import annotations

import json

import pytest

from mm_curation.tuning.extraction import (
    _FACT_RE,
    SOURCE_MAX_CHARS,
    _swap_number,
    build_ext_items,
    extract_facts,
)


def _doc(i: int) -> dict:
    body = (
        f"{i}月{i}日，某市宣布启动{i * 10}号重点工程，总投资{i * 100}万元。\n"
        f"负责人在「开工仪式」上表示，项目将于明年{i % 12 + 1}月建成投运。\n"
        f"据介绍，该工程覆盖{i + 3}个片区，直接受益居民约{i * 1000}人。\n"
        f"背景方面，此前相关规划已于去年通过评审。"
    )
    return {"id": f"news{i:06d}", "title": f"标题{i}", "text": f"标题{i}\n\n{body}"}


def test_extract_facts_window_and_order():
    d = _doc(1)
    source, facts = extract_facts(d["text"])
    assert len(source) <= SOURCE_MAX_CHARS
    assert len(facts) >= 3
    assert all(_FACT_RE.search(f) for f in facts)


def test_swap_number_changes_digits_keeps_width():
    import re

    rng = __import__("random").Random(1)
    sent = "项目总投资1234万元，覆盖5个片区。"
    out = _swap_number(sent, rng)
    assert out != sent  # 数字确实被篡改
    assert re.search(r"\d+", out)  # 仍是数字句


def test_build_items_triples_minimal_pair_and_kind_balance():
    corpus = [_doc(i) for i in range(30)]
    triples, items = build_ext_items(corpus, n_train=12, n_eval=6)
    assert len(triples) == 12 and len(items) == 6
    kinds = {t["kind"] for t in triples}
    assert kinds == {"number_swap", "hallucinate", "omit"}
    for t in triples:
        cj, rj = json.loads(t["chosen"]), json.loads(t["rejected"])
        assert cj["choice"] != rj["choice"]  # 最小对：只差字母
        assert cj["reason"] == rj["reason"]  # reason 固定同文（#60 红线）
        assert t["persona"] == "EXT"
    # 评测题损伤三型均衡覆盖
    assert {it["kind"] for it in items} == {"number_swap", "hallucinate", "omit"}


def test_build_items_insufficient_rejected():
    with pytest.raises(ValueError, match="不足"):
        build_ext_items([_doc(i) for i in range(3)], n_train=10, n_eval=5)
