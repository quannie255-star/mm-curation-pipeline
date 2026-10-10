"""配对符号检验判据的元测试 —— 判据本身不能假绿。

这条判据（`scripts/compare_cleaned_vs_raw.py` 的 `paired_sign_test`）
决定对外结论「清洗更好 / 更差 / 测不出差异」。判据错 = 结论错，
而结论是对外的数字，所以它必须像门禁一样被变异验证。

⚠️ 这条判据历史上真错过一次，方向是**反的**：
`elif k == 0: verdict = "... **清洗更好**"`，其中 `k = min(n_pos, n_neg)`。
**全负时n_pos = 0，k 也是 0** → 「清洗更差」被报成「清洗更好」。
教训：**判据不能由「有没有相反符号」决定结论，必须由符号方向决定。**
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "compare_cleaned_vs_raw.py"


def _load():
    spec = importlib.util.spec_from_file_location("cmp_cleaned_raw", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pst():
    mod = _load()
    assert hasattr(mod, "paired_sign_test"), (
        "compare_cleaned_vs_raw.py 必须把判定抽成 paired_sign_test() 纯函数，"
        "否则判据内嵌在 main() 里既不可测也无法做变异验证"
    )
    return mod.paired_sign_test


# ── 1. 方向：这是最要紧的一条 ──────────────────────────────────────


def test_全正判清洗更好(pst):
    """差为正 = 未清洗 loss 更高 = 清洗更好。"""
    r = pst([0.008, 0.034, 0.027, 0.021, 0.033, 0.012, 0.012, 0.025])
    assert r["n_pos"] == 8 and r["n_neg"] == 0
    assert "清洗更好" in r["verdict"]


def test_全负必须判清洗更差(pst):
    """⚠️ 回归测试：这条在真实实现里栽过（k==0 分支硬写「清洗更好」）。

    差为负 = 未清洗 loss 更低 = **清洗更差**。
    `min(n_pos, n_neg)` 在全负时同样等于 0，所以「k==0」根本不能
    用来推断方向。
    """
    r = pst([-0.008, -0.034, -0.027, -0.021, -0.033, -0.012, -0.012, -0.025])
    assert r["n_pos"] == 0 and r["n_neg"] == 8
    assert "清洗更差" in r["verdict"], (
        f"全负配对差被判成 {r['verdict']!r} —— 方向反了，"
        "这是把阴性结果报成阳性的假绿")
    assert "清洗更好" not in r["verdict"]


def test_符号反转必须翻转结论(pst):
    """同一批数取反 → 结论必须**恰好翻转**。

    这是「判据能区分方向」的最直接检验：
    若实现里任何一处把方向写死，这条必红。
    """
    pos = pst([0.01, 0.02, 0.03, 0.04, 0.05, 0.06])
    neg = pst([-x for x in (0.01, 0.02, 0.03, 0.04, 0.05, 0.06)])
    assert "清洗更好" in pos["verdict"]
    assert "清洗更差" in neg["verdict"]
    # p值与 sd 必须对称（符号检验只看符号，不看幅度）
    assert pos["p_two_sided"] == neg["p_two_sided"]
    assert pos["sd"] == neg["sd"]


# ── 2. 显著性：样本量不足必须「测不出差异」 ────────────────────────


def test_样本量不足不得报阳性(pst):
    """3/3 同号方向一致，但 p=0.25≥0.05 → 只能报「测不出差异」。

    这条直接对应第一版假阳性的教训：看到方向一致就想报「清洗更好」。
    """
    r = pst([0.008, 0.009, 0.010])
    assert r["p_two_sided"] >= 0.05
    assert "测不出差异" in r["verdict"]
    assert "清洗更好" not in r["verdict"]


def test_达显著所需同号数被如实报出(pst):
    """`n_needed_for_p05` 必须与 p 值口径自洽（不能是为了好看而调小的）。

    双尾符号检验全同号时 p = 2 × 0.5^n，要p<0.05 需 **n ≥ 6**
    （n=5 → 2/32 = 0.0625，仍不显著）。
    """
    r = pst([0.01] * 5)
    assert r["p_two_sided"] == pytest.approx(0.0625), "5/5 同号恰好差一点"
    assert r["n_needed_for_p05"] == 6, "need 必须报 6（第 5 个还不足）"
    assert "测不出差异" in r["verdict"]

    r6 = pst([0.01] * 6)
    assert r6["n_needed_for_p05"] <= 6
    assert r6["p_two_sided"] == pytest.approx(2 / 64)
    assert "清洗更好" in r6["verdict"]


def test_混合符号必须报测不出差异(pst):
    """有正有负（k = min(n_pos, n_neg) > 0）→ p 大 → 不能报优劣。"""
    r = pst([0.01, -0.01, 0.02, -0.02, 0.03, -0.03, 0.01, -0.01])
    assert r["k"] == 4
    assert "测不出差异" in r["verdict"]


# ── 3. 退化输入不能崩，也不能假装有结论 ────────────────────────────


def test_全零差报测不出而非显著(pst):
    """两臂逐位相同 → 必须「测不出差异」，不能因为 k==0 就报阳性。"""
    r = pst([0.0] * 8)
    assert r["n_zero"] == 8
    assert "测不出差异" in r["verdict"]
    assert r["t_stat"] is None, "sd=0 → t 无定义，必须报 None 而不是 nan/inf"


def test_单seed不判定(pst):
    r = pst([0.5])
    assert r["n"] == 1
    assert "无法判定" in r["verdict"]


def test_空输入不崩(pst):
    r = pst([])
    assert r["n"] == 0
    assert r["p_two_sided"] == 1.0
    assert "无法判定" in r["verdict"]


# ── 4. 数值口径 ────────────────────────────────────────────────────


def test_p值与符号数一致(pst):
    """p 必须由 n 与 k 现算，不能是写死的常数。

    ⚠️ 注意 `k` 是**少数派**的个数（`min(n_pos, n_neg)`），
    不是同号数——写错这一点就会算出 1.0 然后以为实现坏了
    （本轮第一次跑就是这么误判的）。
    """
    import math

    for n in (3, 5, 6, 8, 10):
        # 全部同号 → 少数派为 0 → p = 2 × (1/2^n)
        expect = 2 * sum(math.comb(n, i) for i in range(1)) / 2 ** n
        r = pst([0.01] * n)
        assert r["k"] == 0
        assert r["p_two_sided"] == pytest.approx(min(1.0, expect), abs=1e-6)

    # 混合符号：少数派为 k
    for n, k in ((4, 1), (6, 2), (8, 3)):
        diffs = [0.01] * (n - k) + [-0.01] * k
        expect = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
        r = pst(diffs)
        assert r["k"] == k
        assert r["p_two_sided"] == pytest.approx(expect, abs=1e-6)


def test_sd是配对差的样本标准差(pst):
    """必须是**配对差**的 sd，不是两臂各自sd 的平均。"""
    import statistics

    diffs = [0.008, 0.034, 0.027, 0.021, 0.033, 0.012, 0.012, 0.025]
    r = pst(diffs)
    assert r["sd"] == pytest.approx(statistics.stdev(diffs), abs=1e-6)


def test_返回值可JSON序列化(pst):
    """结果要塞进 report JSON —— 出现 nan/inf 会写出非法 JSON。"""
    import json

    for diffs in ([0.0] * 8, [0.01] * 8, [-0.01] * 3, [0.01, -0.01]):
        json.dumps(pst(diffs), ensure_ascii=False)  # 不抛异常即通过
