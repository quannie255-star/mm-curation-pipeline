"""混比实验的「可归因臂」判定。

## 为什么这组测试存在

`eval_source_mixing.py` 的报告里有一句硬结论：
「干净子集内wiki 占比越低 ppl 越高」。这句话成立的前提是
**「干净子集」这个划分本身是对的**。

第一版 md 是我**手写**的，把四个臂标成 ✅。但那个判断的依据是
「当时那批数据的长度中位恰好接近」。换一批数据（窗口放宽、
某个源长度分布变了），可比性就悄悄不成立，而**手写的报告会继续声称可比**——
一份不会腐烂的报告就是假绿。

所以判定必须**现算**，且必须有测试盯着。现算之后立刻抓到一个真缺陷：

**`mono_finance`（丢弃率 22.5%）被误判为可比。**
原判据是「丢弃率在中位 ±0.15 内」，而这批数据的中位恰好是 11%，
带子 [−4%, 26%] 刚好罩住 22.5%。绝对带宽的问题在于
**它随这批数据的量级漂移**：中位被离群值拉高，带子跟着变宽，
于是「离群值自己把判据放松了」。

改成**相对倍数**（不超过中位 × DROP_TOL）后正确排除。
这个缺陷只有靠「构造一个更高丢弃率的输入，看判据是否响应」才能发现 ——
因为在原始这一批上，DROP_TOL 取 1.8 或 2.0 结果**完全一样**
（22.5% 恰好卡在 11% × 2.0 = 22% 旁边），只看原始数据会以为判据不敏感。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "eval_source_mixing.py"
REPORT = REPO_ROOT / "data" / "reports" / "source_mixing.json"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("mix_eval", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def payload():
    if not REPORT.exists():
        pytest.skip(f"缺 {REPORT.name}（需先跑 eval_source_mixing.py）")
    return json.loads(REPORT.read_text(encoding="utf-8"))


def test_长度离群的臂必须被排除(mod, payload):
    """news 的长度中位比其余臂大约 4 倍 → 不能归因到「源质量」。"""
    good, bad = mod.comparable_arms(payload)
    assert "mono_news" in bad, f"长度离群臂必须在「有混淆」里：good={good} bad={bad}"


def test_丢弃率离群的臂必须被排除(mod, payload):
    """finance 丢弃率 22.5%，其余臂 9–11%。

    ⚠️ 这条正是第一版的漏网处：绝对带宽 ±0.15 在这批数据上
    把22.5% 判成了可比（中位 11% → 带子到 26%）。
    """
    good, bad = mod.comparable_arms(payload)
    assert "mono_finance" in bad, f"丢弃率离群臂必须被排除：good={good} bad={bad}"


def test_四个混比梯度臂必须全部可比(mod, payload):
    """它们才是那条单调性结论的依据，少一个结论就不成立。"""
    good, _ = mod.comparable_arms(payload)
    for k in ("mono_wiki", "mix_80_10_10", "mix_50_25_25", "mix_33_33_33"):
        assert k in good, f"{k} 应判为可比，实际 good={good}"


def test_判据对丢弃率必须敏感(mod, payload):
    """**反向验证**：阈值放松后，离群臂必须回到「可比」。

    ⚠️ 没有这条就会得出「判据不敏感」的**错误结论** ——
    原始这批上 DROP_TOL 取 1.8 / 2.0 / 2.4 结果**完全一样**
    （finance 的 22.5% ÷ 簇内基准 9.0% = 2.5 倍，恰好卡在 2.4 与 2.6 之间），
    看起来像阈值随便取都行。必须跨过那个边界才看得出差别。

    顺带钉住基准的取法：**簇内最小值**（9.0%）而不是最大值。
    若基准取簇内最大值，离群臂自己就是基准 →
    「离群 ≤ 离群 ×倍数」恒成立 → 判据恒真（这正是修掉的那个病）。
    """
    good_18, _ = mod.comparable_arms(payload)
    orig = mod.DROP_TOL
    try:
        mod.DROP_TOL = 1.8
        g_tight, _ = mod.comparable_arms(payload)
        mod.DROP_TOL = 3.0
        g_loose, _ = mod.comparable_arms(payload)
    finally:
        mod.DROP_TOL = orig

    assert "mono_finance" not in g_tight, "严格阈值下22.5% 应被排除"
    assert "mono_finance" in g_loose, "放松阈值后 22.5% 应被放回 —— 否则阈值没起作用"
    # 判据必须真的在动，而不是两组结果碰巧相同
    assert set(g_tight) != set(g_loose), "阈值变化必须改变判定结果"


def test_判据对长度必须敏感(mod, payload):
    """反向验证：把长度带子放大，news 必须回到「可比」。"""
    _, bad_default = mod.comparable_arms(payload)
    good_loose, _ = mod.comparable_arms(payload, tol=100.0)
    assert "mono_news" in bad_default
    assert "mono_news" in good_loose, "长度阈值放大后news 应回到可比"


def test_可比臂不足时不得硬凑结论(mod, payload):
    """干净子集不足 2 个时，报告必须说「无法比较」而不是硬给结论。

    这条防的是「为了有话可说而制造结论」。

    ⚠️ 造这个输入时我连着两次把期望写错，值得记下来：
    第一次把 4 个臂长度都设成 9999，以为「它们会互相可比 = 判据失效」——
    其实那**正是设计的正确行为**（最大簇 = 主流），反而是唯一正常长度的
    `mono_wiki` 被判成离群。
    第二次改成 news 设 99999、其余 9999，结果 3 个混比臂成了最大簇，
    依然有 3 个可比臂 —— 因为「最大簇」这个设计**本来就允许离群臂成主流**。

    结论：想让可比臂只剩 1 个，不能靠改长度（会重新聚类），
    得靠**收紧阈值**。这里直接把 tol 调到极小，使每个簇只剩一个元素。
    """
    good, _ = mod.comparable_arms(payload, tol=1.0000001)
    assert len(good) < 2, f"极小tol 下可比臂应不足 2 个：{good}"
    md = mod.render_markdown(payload)
    # 原始数据下可比臂充足 → 报告必须给逐段显著性
    assert "干净子集内的逐段显著性" in md
    assert "无法在子集内做显著性比较" not in md


def test_可比臂不足时md必须明说无法比较(mod, payload):
    """直接构造「只有一个可比臂」的 payload，验证报告不硬凑结论。"""
    only = {
        "protocol": dict(payload["protocol"]),
        "ppl": {"mono_wiki": 1.5, "other": 2.0},
        "arms": {
            "mono_wiki": {"len_p50": 150, "actual_ratios": {"wiki": 1.0}},
            "other": {"len_p50": 500, "actual_ratios": {"news": 1.0}},
        },
        "funnel": {"mono_wiki": {"drop_rate": 0.1}, "other": {"drop_rate": 0.1}},
        "conclusion": dict(payload["conclusion"]),
    }
    good, bad = mod.comparable_arms(only)
    assert len(good) < 2, f"应只剩不到 2 个可比臂：{good}"
    md = mod.render_markdown(only)
    assert "无法在子集内做显著性比较" in md, "可比臂不足时必须明说，不能硬给结论"


def test_md必须由payload现算(mod, payload):
    """md 里的数字必须来自 payload，不是写死的字符串。"""
    md = mod.render_markdown(payload)
    ppl = payload["ppl"]
    assert f"{ppl['mono_wiki']:.4f}" in md, "md 里的 mono_wiki 值必须与 payload 一致"
    assert str(payload["protocol"]["n_total_per_arm"]) in md, "样本量必须来自 protocol"
    # 混淆臂必须在 md 里被点名，而不是静默混进结论
    _, bad = mod.comparable_arms(payload)
    for name in bad:
        assert name in md, f"有混淆的臂 {name} 必须在 md 里被点名"


def test_显著性表必须按梯度排序而非入库顺序(mod, payload):
    """第一版踩的坑： 是 dict 顺序，跨档跳着比。

    正确顺序 = 按 wiki 占比从高到低，因为唯一变量就是 wiki 比例。
    """
    md = mod.render_markdown(payload)
    assert "按 wiki 占比从高到低排列" in md
    good, _ = mod.comparable_arms(payload)
    arms = payload["arms"]
    ordered = sorted(
        good,
        key=lambda n: -float(arms[n]["actual_ratios"].get("wiki", 0.0)),
    )
    pos = [md.index(f"| {a} → {b} |") for a, b in zip(ordered, ordered[1:])]
    assert pos == sorted(pos), f"显著性表未按梯度排列：{ordered}"


def test_判定方向必须看符号而不是绝对值(mod, payload):
    """第一版踩的坑：用 abs(d) 定「变好/变差」，符号信息丢失。

    Δ=−1.13（ppl 下降 = 变**好**）被标成「显著变差」。
    """
    md = mod.render_markdown(payload)
    import re

    for line in md.splitlines():
        m = re.match(r"\| \S+ → \S+ \| ([+-][\d.]+) \| [\d.]+× \| (\S+)", line)
        if not m:
            continue
        delta, verdict = float(m.group(1)), m.group(2)
        if "测不出" in verdict:
            continue
        if delta > 0:
            assert "变差" in verdict, f"Δ>0（ppl 升高）应判变差：{line}"
        else:
            assert "变好" in verdict, f"Δ<0（ppl 降低）应判变好：{line}"


def test_显著阈值必须显式成常量(mod):
    """阈值写成内联魔法数就没法被变异测试打到。"""
    assert hasattr(mod, "SIG_RATIO") and mod.SIG_RATIO > 1
    import inspect

    assert "SIG_RATIO" in inspect.getsource(mod.render_markdown)
