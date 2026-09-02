"""D3 评测模块测试：查询构造、名次判定（含并列保守口径）、指标聚合。"""

from __future__ import annotations

import pytest

from mm_curation.eval.retrieval import (
    QuerySpec,
    build_queries,
    evaluate_index,
    target_rank,
)
from mm_curation.index.searcher import SearchHit
from mm_curation.operators.base import Sample


def _sample(sid, caption, extra=None):
    return Sample(
        id=sid, image_path="unused.jpg", text=caption, meta={"extra_captions": extra or []}
    )


def test_build_queries_origin_split():
    samples = [
        _sample("a", "入库句子甲", ["held-out 句子甲"]),
        _sample("b", "入库句子乙"),  # 无 extra -> self
    ]
    qs = build_queries(samples)
    assert [(q.text, q.origin, q.target_id) for q in qs] == [
        ("held-out 句子甲", "held_out", "a"),
        ("入库句子乙", "self", "b"),
    ]


def _hit(i, score, sid=None):
    return SearchHit(row=i, id=sid or f"s{i}", score=score, image_path="p", text="c", labels={})


def test_target_rank_basic_and_miss():
    hits = [_hit(0, 0.9, "x"), _hit(1, 0.8, "target"), _hit(2, 0.7, "y")]
    assert target_rank(hits, "target") == 2
    assert target_rank(hits, "absent") is None


def test_target_rank_tie_takes_worst_position():
    hits = [
        _hit(0, 0.9, "x"),
        _hit(1, 0.8, "target"),
        _hit(2, 0.8, "y"),
        _hit(3, 0.8, "z"),
        _hit(4, 0.1, "w"),
    ]
    assert target_rank(hits, "target") == 4  # 并列 0.8 三条，保守取最差


class FakeSearcher:
    """可控命中：按查询 id 返回预置的命中列表。"""

    def __init__(self, name, hits_by_query):
        self.name = name
        self._hits = hits_by_query

    def search_many_by_vectors(self, vectors, top_k):
        return [self._hits[i][:top_k] for i in range(len(vectors))]


def test_evaluate_index_aggregates_with_origin():
    queries = [
        QuerySpec("q1", "t1", "held_out", "a"),
        QuerySpec("q2", "t2", "held_out", "b"),
        QuerySpec("q3", "t3", "self", "c"),
        QuerySpec("q4", "t4", "self", "zz"),  # 未命中
    ]
    hits = [
        [_hit(0, 0.9, "a")],  # q1: rank 1
        [_hit(0, 0.9, "x"), _hit(1, 0.8, "b"), _hit(2, 0.1)],  # q2: rank 2
        [_hit(0, 0.9, "x"), _hit(1, 0.5, "c"), _hit(2, 0.4)],  # q3: rank 2
        [_hit(0, 0.9, "x"), _hit(1, 0.8, "y"), _hit(2, 0.7)],  # q4: miss
    ]
    r = evaluate_index(FakeSearcher("fake", hits), queries, query_vecs=[0, 1, 2, 3])
    assert r.index == "fake" and r.n_queries == 4
    assert r.recall_at_k[1] == pytest.approx(0.25)  # 只 q1
    assert r.recall_at_k[5] == pytest.approx(0.75)  # q1 q2 q3
    assert r.mrr == pytest.approx((1 + 0.5 + 0.5) / 4)
    assert r.per_origin["held_out"]["n"] == 2
    assert r.per_origin["held_out"]["recall_at_k"][1] == pytest.approx(0.5)
    assert r.per_origin["self"]["recall_at_k"][10] == pytest.approx(0.5)  # q3 命中 q4 未中
