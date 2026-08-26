"""指标纯函数测试：手算对照 + 边界。"""

from __future__ import annotations

import pytest

from mm_curation.eval.metrics import mrr, recall_at_k


def test_perfect_rankings():
    rs = [1, 1, 1, 1]
    assert recall_at_k(rs, 1) == 1.0
    assert mrr(rs) == 1.0


def test_mixed_rankings_hand_computed():
    rs = [1, None, 3, 7]  # 命中名次 1、3、7，一条未命中
    assert recall_at_k(rs, 1) == pytest.approx(0.25)
    assert recall_at_k(rs, 3) == pytest.approx(0.50)
    assert recall_at_k(rs, 5) == pytest.approx(0.50)
    assert recall_at_k(rs, 10) == pytest.approx(0.75)
    assert mrr(rs) == pytest.approx((1 + 1 / 3 + 1 / 7) / 4)  # 所有命中名次都计入


def test_all_miss_and_empty():
    assert recall_at_k([None, None], 10) == 0.0
    assert mrr([None, None]) == 0.0
    assert recall_at_k([], 10) == 0.0  # 无查询 = 无结论，不 crash
    assert mrr([]) == 0.0
    assert recall_at_k([2], 0) == 0.0  # k<1 非法
