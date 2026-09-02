"""脏/净索引检索对比评测（Week3 D3，docs/design_tables.md 1.1/1.2）。

公平性设计（design 3.2）：查询集只含净索引样本——它们在脏索引中同样存在
（脏索引 = 净索引 + 注入的超集），两侧 ground truth 均可达，
指标差异只能来自污染数据对排名的挤压。
held_out / self 两类查询分开统计：自检索（查询=入库 caption）机制上偏乐观。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..index.searcher import IndexSearcher
from ..operators.base import Sample
from .metrics import mrr, recall_at_k

K_LIST = (1, 5, 10)


@dataclass
class QuerySpec:
    query_id: str
    text: str
    origin: str  # held_out（未参与索引构建的 caption）/ self（入库 caption 本身）
    target_id: str


@dataclass
class EvalResult:
    index: str
    n_queries: int
    recall_at_k: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    per_origin: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "n_queries": self.n_queries,
            "recall_at_k": {str(k): v for k, v in self.recall_at_k.items()},
            "mrr": round(self.mrr, 4),
            "per_origin": {
                o: {
                    "n": d["n"],
                    "recall_at_k": {str(k): v for k, v in d["recall_at_k"].items()},
                    "mrr": round(d["mrr"], 4),
                }
                for o, d in self.per_origin.items()
            },
        }


def build_queries(samples: list[Sample]) -> list[QuerySpec]:
    """一图一查询：有 held-out caption（meta.extra_captions[0]）用之，否则自检索。"""
    out = []
    for s in samples:
        extra = s.meta.get("extra_captions") or []
        if extra:
            out.append(QuerySpec(s.id, extra[0], "held_out", s.id))
        else:
            out.append(QuerySpec(s.id, s.text, "self", s.id))
    return out


def target_rank(hits, target_id: str) -> Optional[int]:
    """目标在命中列表中的名次（1-based）；未命中返回 None。

    并列分数取保守口径：同分块内取最差位置（design 3.2 #4）。
    """
    idx = next((i for i, h in enumerate(hits) if h.id == target_id), None)
    if idx is None:
        return None
    score = hits[idx].score
    worst = idx
    while worst + 1 < len(hits) and hits[worst + 1].score == score:
        worst += 1
    return worst + 1


def evaluate_index(
    searcher: IndexSearcher, queries: list[QuerySpec], query_vecs, k_list: tuple[int, ...] = K_LIST
) -> EvalResult:
    """单索引评测：批量向量检索 -> 逐查询名次 -> 指标聚合（总览 + 分 origin）。"""
    hits_batch = searcher.search_many_by_vectors(query_vecs, max(k_list))
    rankings = [target_rank(hits, q.target_id) for q, hits in zip(queries, hits_batch)]
    by_origin: dict[str, list[Optional[int]]] = {}
    for q, r in zip(queries, rankings):
        by_origin.setdefault(q.origin, []).append(r)

    def _block(rs: list[Optional[int]]) -> dict:
        return {
            "n": len(rs),
            "recall_at_k": {k: recall_at_k(rs, k) for k in k_list},
            "mrr": mrr(rs),
        }

    return EvalResult(
        index=searcher.name,
        n_queries=len(queries),
        recall_at_k={k: recall_at_k(rankings, k) for k in k_list},
        mrr=mrr(rankings),
        per_origin={o: _block(rs) for o, rs in sorted(by_origin.items())},
    )


def compare(results: list[EvalResult]) -> dict:
    """对比汇总：以第一个索引为基准，输出差值与相对提升。"""
    base = results[0]
    out = {"base": base.to_dict(), "others": []}
    for r in results[1:]:
        delta = {
            f"recall@{k}": round(r.recall_at_k[k] - base.recall_at_k[k], 4)
            for k in base.recall_at_k
        }
        delta["mrr"] = round(r.mrr - base.mrr, 4)
        out["others"].append({"result": r.to_dict(), "delta_vs_base": delta})
    return out
