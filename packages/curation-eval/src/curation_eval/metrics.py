"""评测指标：丢弃语义的 P/R + 检索指标（协议单一来源，主仓库消费）。"""

from __future__ import annotations

from typing import Optional, Sequence

from .schema import Sample


def pr_from_drops(dropped_ids: Sequence[str], mixed: list[Sample]) -> dict:
    """从"你的系统丢弃的 id 列表"算 precision / recall。

    mixed 为污染后的全量样本（注入样本 labels["dirty"] 存在，干净样本为空）。
    丢弃 = positive：precision=丢得准，recall=抓得全。
    """
    dirty_ids = {s.id for s in mixed if s.labels}
    clean_ids = {s.id for s in mixed if not s.labels}
    dropped = set(dropped_ids)
    tp = len(dropped & dirty_ids)
    fp = len(dropped & clean_ids)
    fn = len(dirty_ids - dropped)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if dirty_ids else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "n_dirty": len(dirty_ids),
        "clean_killed": fp,
        "dirty_missed": fn,
    }


def recall_at_k(rankings: list[Optional[int]], k: int) -> float:
    """目标进入 top-K 的查询占比；ranking=目标名次(1-based)，None=未命中。"""
    if not rankings or k < 1:
        return 0.0
    return sum(1 for r in rankings if r is not None and r <= k) / len(rankings)


def mrr(rankings: list[Optional[int]]) -> float:
    if not rankings:
        return 0.0
    return sum(1.0 / r for r in rankings if r is not None) / len(rankings)
