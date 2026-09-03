"""Cohen's kappa 单测：已知 2x2 值、完全一致、随机一致、退化与边界。"""

from __future__ import annotations

import pytest
from curation_eval import cohen_kappa


def test_perfect_agreement():
    assert cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0


def test_known_2x2_value():
    # po=0.8, pe=(0.6*0.5 + 0.4*0.5)=0.5 -> κ=(0.8-0.5)/0.5=0.6
    y1 = [1, 1, 1, 1, 0, 0, 0, 0, 1, 0]
    y2 = [1, 1, 1, 0, 0, 0, 0, 1, 1, 0]
    assert cohen_kappa(y1, y2) == pytest.approx(0.6)


def test_chance_agreement():
    # po = pe = 0.5 -> κ=0（与随机抽签一致）
    assert cohen_kappa([1, 0, 1, 0], [1, 1, 0, 0]) == pytest.approx(0.0)


def test_degenerate_single_class_returns_none():
    # 两边都只判了一个类别——一致性无信息，应返回 None 而非假 1.0
    assert cohen_kappa([1, 1], [1, 1]) is None


def test_empty_and_length_mismatch():
    assert cohen_kappa([], []) is None
    with pytest.raises(ValueError, match="长度"):
        cohen_kappa([1], [1, 0])


def test_worse_than_chance_negative():
    # 完全反判：po=0, pe=0.5 -> κ=-1（比随机还差）
    assert cohen_kappa([1, 1, 0, 0], [0, 0, 1, 1]) == pytest.approx(-1.0)
