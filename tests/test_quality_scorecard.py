"""数据质量记分卡测试。

这里锁的是三类**容易自欺**的行为：
1. 没评过 → 不能记成通过（NOT_EVALUATED ≠ 0 也 ≠ OK）
2. 只丢不打分的算子 → 不能算空转（笔记 #78）
3. 低覆盖率的满分 → 不能和健康度混为一谈
"""

from __future__ import annotations

from mm_curation.quality import scorecard as SC


def _card(**kw):
    kw.setdefault("dataset", "d")
    kw.setdefault("dimension", "completeness")
    kw.setdefault("ops", ["a", "b"])
    kw.setdefault("per_op", {})
    kw.setdefault("n_samples", 10)
    kw.setdefault("slo", SC.SloSpec(min_score=0.9, min_coverage=0.5))
    return SC.score_dimension(**kw)


def test_zero_denominator_is_not_zero_score():
    """0/0 是「没有分母」，不是「零分」。记分卡必须返回 None。"""
    c = _card(per_op={})
    assert c.score is None
    assert c.status == SC.STATUS_NOT_EVALUATED
    assert c.coverage == 0.0


def test_dropping_without_scoring_counts_as_evaluated():
    """笔记 #78：n_scored=0 但 n_drop>0 的算子确实在工作。

    只认分数会把它判成「一次都没评过」，进而让整个 uniqueness 维度
    恒为 NOT_EVALUATED——明明它实丢了 342 条。
    """
    c = _card(ops=["md5_exact"], per_op={"md5_exact": {"n_scored": 0, "n_drop": 342}})
    assert c.ops_evaluated == 1
    assert c.coverage == 1.0
    # 但它没有通过率语义：不能拿 (0-342)/0 去算分
    assert c.score is None
    assert c.status == SC.STATUS_OBSERVED


def test_coverage_is_operator_level_not_cell_level():
    """覆盖率问的是「有几条规则在跑」，不是「有几个单元格有分数」。

    用单元级口径时，只丢不打分的算子会让它恒为 0——明明在丢数据却显示未评。
    """
    c = _card(ops=["a", "b", "c", "d"], per_op={"a": {"n_scored": 3, "n_drop": 0}})
    assert c.coverage == 0.25


def test_no_slo_is_not_ok():
    """不设目标就不算达标：没有 SLO 的维度标 NO_SLO，不能默认 OK。"""
    c = _card(slo=None, per_op={"a": {"n_scored": 10, "n_drop": 0}})
    assert c.score == 1.0
    assert c.status == SC.STATUS_NO_SLO


def test_breach_when_coverage_below_slo_even_with_perfect_score():
    """覆盖率不达标即破线，哪怕分数是 1.0——「没评过」的满分没有意义。"""
    c = _card(
        ops=["a", "b", "c", "d"],
        per_op={"a": {"n_scored": 10, "n_drop": 0}},
        slo=SC.SloSpec(min_score=0.9, min_coverage=0.5),
    )
    assert c.score == 1.0
    assert c.status == SC.STATUS_BREACH


def test_health_score_is_coverage_weighted():
    """低覆盖率的维度不能和高覆盖率的维度平起平坐。

    简单平均会让 coverage 0.1 的满分维度和 coverage 0.9 的维度同权，
    端出去一个虚高的健康度。加权后话语权按实际跑过的规则数分配。
    """
    hi = SC.DimensionScore("d", "x", 10, 9, 0.9, 0.5, SC.STATUS_OK)
    lo = SC.DimensionScore("d", "y", 10, 1, 0.1, 1.0, SC.STATUS_OK)
    s = SC.summarize([hi, lo])
    assert s["health_score_weighted_by"] == "coverage"
    # (0.5*0.9 + 1.0*0.1) / (0.9+0.1) = 0.55
    assert abs(s["health_score"] - 0.55) < 1e-9
    # 简单平均会给出 0.75 —— 那个数字不该被单独引用
    assert abs((0.5 + 1.0) / 2 - 0.75) < 1e-9


def test_summarize_reports_mean_coverage_alongside_health():
    """健康度必须和覆盖率一起报：分开看任何一个都会误导。"""
    cards = [
        SC.DimensionScore("d", "x", 10, 9, 0.9, 0.5, SC.STATUS_OK),
        SC.DimensionScore("d", "y", 10, 1, 0.1, 1.0, SC.STATUS_OK),
    ]
    s = SC.summarize(cards)
    assert abs(s["mean_coverage"] - 0.5) < 1e-9
    assert s["by_status"]["OK"] == 2


def test_negative_pass_count_is_clamped():
    """只丢不打分的算子会让 n_scored - n_drop 变负；负的「通过数」没有意义。"""
    c = _card(ops=["md5"], per_op={"md5": {"n_scored": 0, "n_drop": 5}})
    assert c.detail["n_pass"] == 0
    assert c.detail["n_drop"] == 5
