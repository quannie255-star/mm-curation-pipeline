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


def cohen_kappa(y1: Sequence, y2: Sequence) -> Optional[float]:
    """Cohen's kappa：两个判定者（如 LLM-judge 与 ground truth）的一致性，
    修正了随机一致。κ = (p_o - p_e) / (1 - p_e)。

    - 完全一致 → 1.0；与随机抽签一致 → 0.0；负值 = 比随机还差
    - 无法计算（空输入 / 只有一个类别导致 p_e=1）时返回 None——
      调用方应如实呈现「不可评」而不是拿到假 0 分
    """
    if len(y1) != len(y2):
        raise ValueError(f"两序列长度不一致: {len(y1)} vs {len(y2)}")
    n = len(y1)
    if n == 0:
        return None
    labels = set(y1) | set(y2)
    po = sum(1 for a, b in zip(y1, y2) if a == b) / n
    pe = sum(
        (sum(1 for a in y1 if a == c) / n) * (sum(1 for b in y2 if b == c) / n) for c in labels
    )
    if pe == 1.0:  # 两者都只判了一个类别——一致性无信息
        return None
    return (po - pe) / (1.0 - pe)
