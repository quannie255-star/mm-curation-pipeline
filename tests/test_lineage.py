"""血缘图与数据契约的测试。

血缘这边锁的是两条「不报错、只安静给错答案」的坑；
契约这边锁的是 min_len 用在数值列上的语义陷阱（笔记 #79）。
"""

from __future__ import annotations

import pytest

from mm_curation.lineage import contract as C
from mm_curation.lineage import graph as G


def _rows():
    """三条判决书：doc_length(seq=1) 处理 e1/e2，perplexity(seq=2) 处理 e1。"""
    return [
        {
            "run_id": "r",
            "op": "doc_length",
            "seq": 1,
            "decision": "keep",
            "prov": {"used": "e1", "activity": "doc_length"},
        },
        {
            "run_id": "r",
            "op": "doc_length",
            "seq": 1,
            "decision": "drop",
            "prov": {"used": "e2", "activity": "doc_length"},
        },
        {
            "run_id": "r",
            "op": "perplexity",
            "seq": 2,
            "decision": "keep",
            "prov": {"used": "e1", "activity": "perplexity"},
        },
        # 序号更小但共享实体 → 是上游，不是下游
        {
            "run_id": "r",
            "op": "dedup",
            "seq": 0,
            "decision": "keep",
            "prov": {"used": "e1", "activity": "dedup"},
        },
    ]


def test_upstream_entities_returns_entities_not_verdict_ids():
    """两步跳：作业 ← 裁决 → 实体。

    直接找 `to == job` 只能拿到裁决节点 id，返回的条数会恒等于 1，
    看起来像「这个算子只处理了一份数据」——不崩，但完全错。
    """
    g = G.build_lineage(_rows(), run_id="r")
    assert g.upstream_entities("doc_length") == ["e1", "e2"]


def test_downstream_requires_shared_entity_and_later_seq():
    """下游 = 共享实体 **且** 序号更大。

    少了共享实体条件，「配置里排在后面」就会被当成「受它影响」；
    少了序号条件，上游算子会被算成下游。
    """
    g = G.build_lineage(_rows(), run_id="r")
    # perplexity 处理了 e1（doc_length 也处理过）且 seq 更大 → 下游
    assert g.downstream_of("doc_length") == ["perplexity"]
    # dedup 共享 e1 但 seq 更小 → 不是下游
    assert "dedup" not in g.downstream_of("doc_length")


def test_downstream_of_unknown_job_is_empty():
    g = G.build_lineage(_rows(), run_id="r")
    assert g.downstream_of("不存在的算子") == []


def test_mermaid_quotes_labels():
    """标签里有 `#` 和 `sha256:`，裸写会让 Mermaid 静默渲染成空白。"""
    g = G.build_lineage(_rows(), run_id="r")
    out = g.to_mermaid()
    assert out.startswith("graph LR")
    assert 'n0["' in out  # 节点必须走 id["标签"] 形式
    assert "#" in out and "[" in out
    for line in out.splitlines()[1:]:
        assert line.count('["') == 2


def test_openlineage_export_shape():
    g = G.build_lineage(_rows(), run_id="r")
    ol = g.to_openlineage()
    assert ol["eventType"] == "COMPLETE"
    assert ol["run"]["runId"] == "r"
    assert {i["name"] for i in ol["inputs"]} == {"e1", "e2"}


# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------


def test_min_len_vs_min_value_on_numeric_column():
    """笔记 #79：min_len 对数值列比的是**位数**。

    text_len = 1234 时 LENGTH('1234') = 4，写 min_len: 10 会判 FAIL——
    语义完全不同却不会报错，是最难发现的那一类错误。
    """
    c = C.Contract(
        dataset="d",
        version=1,
        owner="o",
        table="t",
        fields={"n": {"min_len": 10}, "m": {"min_value": 10}},
    )
    kinds = {name.split(".")[-1] for name, *_ in C.compile_field_checks(c)}
    assert {"min_len", "min_value"} <= kinds
    for name, sql, _expect, _sev in C.compile_field_checks(c):
        if name == "n.min_len":
            assert "LENGTH" in sql  # 位数
        if name == "m.min_value":
            assert "LENGTH" not in sql  # 数值大小


def test_field_rules_compile_to_zero_expected_counting_assertions():
    """全部编译成「期望为 0 的计数」：能报出坏了多少条，而不是只说 true/false。"""
    c = C.Contract(
        dataset="d",
        version=1,
        owner="o",
        table="t",
        fields={
            "id": {"required": True, "unique": True},
            "modality": {"allowed": ["text"]},
            "s": {"min_len": 2, "max_len": 9},
        },
    )
    checks = C.compile_field_checks(c)
    assert all(expect == 0 for _n, _s, expect, _sv in checks)
    # id.required / id.unique / modality.allowed / s.min_len / s.max_len
    assert [n for n, *_ in checks] == [
        "id.required",
        "id.unique",
        "modality.allowed",
        "s.min_len",
        "s.max_len",
    ]


def test_dataset_is_scoped_into_where_clause():
    """契约必须带 dataset 过滤：否则「某数据集唯一」会被全库唯一偷偷满足。"""
    c = C.Contract(
        dataset="text_funnel",
        version=1,
        owner="o",
        table="stg_samples",
        fields={"id": {"unique": True}},
    )
    _n, sql, _e, _s = C.compile_field_checks(c)[0]
    assert "dataset = 'text_funnel'" in sql


def test_empty_sql_is_error_not_pass():
    """规则没写 sql → ERROR，绝不能因为「没东西可查」就算通过。"""
    r = C._run(None, "x", "   ", 0, "error")
    assert r.status == C.STATUS_ERROR


def test_contract_requires_dataset(tmp_path):
    """没有 dataset 的契约无法限定作用域，直接拒收而不是默认全库。"""
    p = tmp_path / "bad.yaml"
    p.write_text("version: 1\nowner: o\n", encoding="utf-8")
    with pytest.raises(ValueError):
        C.load_contract(p)
