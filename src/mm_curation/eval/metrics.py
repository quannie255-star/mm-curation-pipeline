"""检索评测指标（纯函数，无 IO，无模型）。

约定：ranking = 目标样本在检索结果中的名次（1-based），
None = top-k 内未命中。空列表返回 0.0（无查询 = 无结论，不 crash）。
"""

from __future__ import annotations

from typing import Optional


def recall_at_k(rankings: list[Optional[int]], k: int) -> float:
    """目标进入 top-K 的查询占比。"""
    if not rankings or k < 1:
        return 0.0
    hit = sum(1 for r in rankings if r is not None and r <= k)
    return hit / len(rankings)


def mrr(rankings: list[Optional[int]]) -> float:
    """平均倒数排名（Mean Reciprocal Rank）。"""
    if not rankings:
        return 0.0
    return sum(1.0 / r for r in rankings if r is not None) / len(rankings)
