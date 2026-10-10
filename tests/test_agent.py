"""Agent 编排层的测试。

## 为什么这批测试的每一条都来自实跑，而不是「应该测什么」

本文件里的每条断言都对应一个**在真实数据上暴露过的问题**，
而它们**在夹具上不会暴露**——这正是记忆里那条
「夹具造出来的新库不会携带历史包袱」的翻版：

1. **量纲错误**（`normalized_margin` 那一组）
   第一版判据是「所有规则分 >= 0.9」，实跑只省 7.2%。
   而夹具里若只用 `chinese_ratio`（score天然在 0.85 左右），
   这个错判据看起来**完全正常**。
   真因是 `doc_length` 的 score 是长度（p50=181），
   与比例类的 score 放在同一把尺子上比。

2. **往返丢决策**（`test_roundtrip_*`）
   `to_dict` 从不写 `per_sample_plans`，而 `from_dict` 一直读它。
   落盘再读回来，决策静默清空——**不报错，只是内容没了**。

3. **不可判定项拖垮决策**（`test_unjudgeable_does_not_force_full_run`）
   第一版的 `min()` 取全部 score，于是「一个算子不可判定」
   就把整条决策拖成跑满，Agent 永远不省。

所以这里的夹具**必须包含异量纲的算子**（长度型+ 比例型），
否则等于没测。
"""

from __future__ import annotations

import json

import pytest

from mm_curation.agent import (
    ALL_TIERS,
    COST_WEIGHT,
    SKIPPABLE_TIERS,
    AgentRunResult,
    BudgetLedger,
    CompressedMemory,
    DecisionRecord,
    compress,
    decide_tiers,
    estimate_cost,
    keep_min_table,
    normalized_margin,
    plan_for_corpus,
    run_state,
)
from mm_curation.agent import policy as policy_mod
from mm_curation.agent.base import EffectComparison
from mm_curation.operators.base import Sample
from mm_curation.pipeline.config import OperatorSpec, PipelineConfig

# ──────────────────────────── 夹具 ────────────────────────────

#: 量纲对照表（真实配置里这些算子的 min 值）。
#: **刻意包含 doc_length=30** —— 它是非比例型，
#: 正是本轮抓到的那个坑的根源。
KEEP_MIN = {
    "doc_length": 30,
    "chinese_ratio": 0.3,
    "char_repetition": 0.8,
    "line_repetition": 0.8,
    "pii_detect": 0.9,
}


def _text_samples(n: int = 12) -> list[Sample]:
    """真实分布的文本样本 meta（与实跑观测一致）。

    实跑观测（kept 样本）：
      chinese_ratio  p50 = 0.848
      char_repetition p50 = 0.990
      line_repetition  p50 = 1.000
      doc_length      p50 = 181（**长度，不是比例**）
    """
    out = []
    for i in range(n):
        out.append(
            Sample(
                id=f"s{i}",
                text="数据平台建设需要统一指标口径，并且链路可追溯。" * 3,
                modality="text_article",
                meta={
                    "score:chinese_ratio": 0.85,
                    "score:char_repetition": 0.99,
                    "score:line_repetition": 1.0,
                    "score:pii_detect": 1.0,
                    "score:doc_length": 181.0,
                },
            )
        )
    return out


def _config(**overrides) -> PipelineConfig:
    ops = [
        OperatorSpec(op="doc_length", params={"min": 30, "max": 50000}),
        OperatorSpec(op="chinese_ratio", params={"min": 0.3}),
        OperatorSpec(op="char_repetition", params={"min": 0.8}),
        OperatorSpec(op="line_repetition", params={"min": 0.8}),
        OperatorSpec(op="pii_detect", params={"min": 0.9}),
    ]
    kw = {
        "name": "t",
        "raw_jsonl": None,
        "output_dir": None,
        "operators": ops,
    }
    kw.update(overrides)
    return PipelineConfig(**kw)


# ────────────────── normalized_margin：量纲判据 ──────────────────


def test_margin_normalizes_against_own_threshold():
    """同一把尺子：不同 keep_min 下，余量要可比。

    这是「不同量纲不能直接比绝对值」的数学表达。
    """
    # chinese_ratio: (0.85 - 0.3) / 0.7 = 0.786
    assert normalized_margin(0.85, 0.3) == pytest.approx(0.7857, abs=1e-3)
    # char_repetition: (0.99 - 0.8) / 0.2 = 0.95
    assert normalized_margin(0.99, 0.8) == pytest.approx(0.95, abs=1e-3)


def test_margin_is_none_for_non_ratio_threshold():
    """★ keep_min >= 1（如 doc_length 的 30）判为**不可判定**，不是「差」。

    错判的后果很具体：若把它当成 0来算，
    margin = 181/1 = 181 → 看起来「极其干净」，
    于是任何长度都无条件放行——判据完全失效。
    """
    assert normalized_margin(181.0, 30) is None
    assert normalized_margin(0.85, None) is None
    assert normalized_margin(0.85, 1.5) is None
    assert normalized_margin(0.85, -0.1) is None


def test_margin_monotone_in_score():
    """余量必须对分数单调递增——否则「更高分更干净」这个前提就不成立。"""
    vals = [normalized_margin(s, 0.3) for s in (0.31, 0.5, 0.8, 0.95)]
    assert vals == sorted(vals)


def test_keep_min_table_reads_config():
    """量纲表必须现从 config 抽，不允许手抄。

    第一版让调用方自己传表，我手写时把 doc_length 的 min 写成 0.9，
    于是一个长度型 score 被当成「离满分只剩 7%」，
    179/193 条样本全被判成不干净——**省不到任何成本**。
    """
    tbl = keep_min_table(_config())
    assert tbl["doc_length"] == 30
    assert tbl["chinese_ratio"] == 0.3


# ────────────────── decide_tiers：放行判据 ──────────────────


def test_rule_only_when_margins_wide():
    """★ 余量宽裕时**必须真跑到 model 档**，只跳 llm。

    这条断言在 2026-10-05 被实跑数据推翻过一次，
    推翻过程本身是这条测试的价值所在，所以保留在docstring 里。

    ## 被推翻的历史版本
    原文断言 `d.tiers == ("rule",)`、`skipped == ("perceptual","model","llm")`，
    配套实现是「所有非rule 的档一律跳掉」。
    它在干净语料上看着全绿——但实跑注入字符级噪声后发现：

        规则档keep 120 / 120（**一条都没拦住**）
        perplexity（model 档）keep 2 / 120 → **贵档独拦 118 条**

    根因：乱码字符大多仍落在 `chinese_ratio` 认可的汉字区间内，
    `doc_length` 也照样够长 → **规则分对乱码几乎完全失效**。
    于是「规则分干净就跳过 model 档」恰好漏掉
    model 档唯一有增量价值的形态。

    ## 因此正确行为是
    余量宽裕只**允许**跳白名单里的档（当前 = `llm`），
    model 档必须真跑。代价是省得少了
    （实跑 saved_ratio 0.644，而旧口径声称 0.967），
    但省得少好过漏检：漏检是**静默**的，账单是**可见**的。

    ## 这条测试守的是「白名单」不是「余量」
    余量判据本身由 `test_unjudgeable_does_not_force_full_run` 等守着；
    这里守的是「余量宽裕 ≠ 可以乱跳档」。
    """
    d = decide_tiers(_text_samples(1)[0], keep_min_of_op=KEEP_MIN)
    # ★ 精确断言（含skipped），不给「跳多跳少都算过」的余地
    assert d.tiers == ("rule", "perceptual", "model"), d.reason
    assert d.skipped_tiers == ("llm",)
    assert "doc_length" in d.reason, "被排除的算子必须写进理由，否则无法审计"
    # 白名单是唯一跳档依据：跳掉的档必须**逐个**在白名单里
    assert set(d.skipped_tiers) <= SKIPPABLE_TIERS, d.skipped_tiers
    # 而 model 档在任何余量下都不许被跳（它是唯一能拦乱码的档）
    assert "model" in d.tiers, "跳model 档等于放弃乱码检出，实测代价 118/120"


def test_skippable_tiers_is_enforced_not_documented():
    """★ 白名单必须是**代码强制**，不能只是注释。

    上一版 `SKIPPABLE_TIERS` 只是模块级常量 + 一大段注释，
    三处判据各写各的 `t for t in ceiling if t != "rule"`——
    于是 `max_tier="model"` 时**没有任何机制**阻止它跳过 model 档。
    注释写得再详细也不产生约束力。

    ## 变异测试：注入一个「实现退化」的跳档函数
    这里不用「直接调私有 `_decision`」来测守卫，那只能证明
    「那个函数会报错」，不能证明**公开路径**受它保护。
    做法是monkeypatch `_after_skip` 成历史写法
    （硬跳一切非 rule 档），让退化从**公开入口 `decide_tiers`** 走一遍：

        必须抛 AssertionError（而不是静默跳过 model 档）
    若哪天有人把 `_decision` 里的断言删掉，这条会红。

    第二段反向验证：白名单显式含model 时同一退化路径放行——
    证明守卫是**由白名单驱动**的，而不是硬编码了某个固定档名。
    """
    monkey = pytest.MonkeyPatch()

    def _degraded(ceiling: tuple[str, ...]) -> tuple[str, ...]:
        # 历史写法：余量宽裕就只跑 rule 档，其余全跳
        return ("rule",)

    monkey.setattr(policy_mod, "_after_skip", _degraded)
    try:
        with pytest.raises(AssertionError, match="白名单") as ei:
            decide_tiers(_text_samples(1)[0], keep_min_of_op=KEEP_MIN)
        # 报出来的必须是 perceptual 与 model（= 白名单管不着的档）
        assert "model" in str(ei.value), ei.value
    finally:
        monkey.undo()

    # 反向：白名单显式放开 model + perceptual 后，同一退化路径放行——
    # 证明守卫是**由白名单驱动**的，而不是硬编码了某个固定档名
    monkey.setattr(
        policy_mod,
        "SKIPPABLE_TIERS",
        frozenset({"llm", "model", "perceptual"}),
    )
    try:
        monkey.setattr(policy_mod, "_after_skip", _degraded)
        d = decide_tiers(_text_samples(1)[0], keep_min_of_op=KEEP_MIN)
        assert d.tiers == ("rule",)
        # skipped 保持 ceiling 的**档位顺序**（perceptual → model → llm），
        # 不是字母序—— 顺序本身就是信息，排序会把它抹掉
        assert d.skipped_tiers == ("perceptual", "model", "llm")
    finally:
        monkey.undo()

    # 恢复正常后：唯一被跳的档是白名单里的 llm
    d = decide_tiers(_text_samples(1)[0], keep_min_of_op=KEEP_MIN)
    assert d.tiers == ("rule", "perceptual", "model"), d.reason
    assert d.skipped_tiers == ("llm",)


def test_whitelist_guard_also_catches_dropping_rule_tier():
    """★ 守卫不只管「跳贵档」，连 rule 档被丢掉也拦。

    这条是写上面那条变异测试时**意外发现的**：
    我第一版变异体写成 `tuple(t for t in ceiling if t != "rule")`，
    它连 rule 档一起跳了，守卫立刻报 `['rule']`——
    比我要测的「跳 model」更严重，于是顺手把这条能力固定下来。

    上一版 `skipped_tiers` 由调用点手写，
    没有任何机制能拦住「rule 档被算进skipped」这种错误记账：
    记账错了，`saved_ratio` 就会虚高，而虚高是**看不出来**的。
    """
    monkey = pytest.MonkeyPatch()
    # 即使白名单全开，连 rule 档都不许跳
    monkey.setattr(
        policy_mod,
        "SKIPPABLE_TIERS",
        frozenset({"rule", "perceptual", "model", "llm"}),
    )
    try:
        monkey.setattr(policy_mod, "_after_skip", lambda ceiling: ("perceptual",))
        with pytest.raises(AssertionError, match="白名单") as ei:
            decide_tiers(_text_samples(1)[0], keep_min_of_op=KEEP_MIN)
        assert "rule" in str(ei.value), ei.value
    finally:
        monkey.undo()


def test_model_tier_never_skipped_even_with_wide_margins():
    """★ `max_tier="model"` 时余量再宽也必须跑 model。

    这是 `test_rule_only_when_margins_wide` 的加强版：
    那条用默认 `max_tier="llm"`，万一将来实现改成
    「只在 llm 上限时才跳 model」就会蒙混过关。
    这里把上限压到 model 档本身，堵死这个后门。
    """
    d = decide_tiers(_text_samples(1)[0], max_tier="model", keep_min_of_op=KEEP_MIN)
    assert d.tiers == ("rule", "perceptual", "model"), d.reason
    assert d.skipped_tiers == (), "上限就是 model 时，没有任何档可跳"


def test_unjudgeable_does_not_force_full_run():
    """★ 一个不可判定的算子**不能**把整条决策拖成跑满。

    第一版对全部 score 取 min()，doc_length 永远拖后腿→
    Agent 永远不省。这条是「结构化判据」的守门测试。

    「跑满」= 4 档全跑。少跑一档 llm 就算省到了，
    但**不能少跑 model**（见 `test_rule_only_when_margins_wide`）。
    """
    s = _text_samples(1)[0]
    only_ratio = {k: v for k, v in KEEP_MIN.items() if k != "doc_length"}
    d = decide_tiers(s, keep_min_of_op=only_ratio)
    assert d.tiers == ("rule", "perceptual", "model"), d.reason
    assert d.skipped_tiers == ("llm",)


def test_no_threshold_table_means_run_full():
    """没有尺子就不量长度——一律跑满，且理由要说清是「缺表」。"""
    d = decide_tiers(_text_samples(1)[0])
    assert d.tiers == ALL_TIERS
    assert "不可判定" in d.reason or "没有任何" in d.reason


def test_narrow_margin_forces_full_run():
    """余量不够宽→ 必须跑满，且理由要指出最弱项（能追问）。"""
    s = _text_samples(1)[0]
    s.meta["score:chinese_ratio"] = 0.45  # margin = (0.45-0.3)/0.7 = 0.21
    d = decide_tiers(s, keep_min_of_op=KEEP_MIN)
    assert d.tiers == ALL_TIERS
    assert "chinese_ratio" in d.reason


def test_margin_ok_is_validated():
    """margin_ok 越界必须报错，而不是被静默夹紧。"""
    with pytest.raises(ValueError, match="margin_ok"):
        decide_tiers(_text_samples(1)[0], keep_min_of_op=KEEP_MIN, margin_ok=1.0)
    with pytest.raises(ValueError, match="margin_ok"):
        decide_tiers(_text_samples(1)[0], keep_min_of_op=KEEP_MIN, margin_ok=0.0)


def test_bad_max_tier_raises():
    with pytest.raises(ValueError, match="max_tier"):
        decide_tiers(_text_samples(1)[0], max_tier="gpu")


def test_short_text_skips_llm():
    """短文本 LLM 价值低——跳过但保留其它档。"""
    s = Sample(id="x", text="短", modality="text_article", meta={"score:chinese_ratio": 0.5})
    d = decide_tiers(s, keep_min_of_op=KEEP_MIN)
    assert "llm" not in d.tiers
    assert "perceptual" in d.tiers
    assert d.skipped_tiers == ("llm",)


def test_rule_ceiling_always_rule():
    d = decide_tiers(_text_samples(1)[0], max_tier="rule", keep_min_of_op=KEEP_MIN)
    assert d.tiers == ("rule",)


def test_no_score_means_full_run():
    s = Sample(id="x", text="无分数的样本", modality="text_article")
    d = decide_tiers(s, keep_min_of_op=KEEP_MIN)
    assert d.tiers == ALL_TIERS
    assert "没有任何规则 score" in d.reason


def test_decision_is_frozen():
    """决策对象会进 LangGraph 状态，可变会污染版本。"""
    d = decide_tiers(_text_samples(1)[0], keep_min_of_op=KEEP_MIN)
    with pytest.raises(Exception):
        d.tiers = ("llm",)  # type: ignore[misc]


# ────────────────── plan_for_corpus：语料级 ──────────────────


def test_plan_saves_cost_on_realistic_distribution():
    """修复的核心目标：真实分布下要有**实质**节省。"""
    plan = plan_for_corpus(_text_samples(50), config=_config())
    assert plan.saved_ratio > 0.5, f"只省了 {plan.saved_ratio:.1%}，说明判据口径又错了"
    n_llm = sum(1 for d in plan.per_sample.values() if d.runs_expensive)
    assert n_llm == 0, "这批样本余量都宽，不该有任何 LLM 调用"


def test_plan_is_deterministic():
    """同输入两次跑出逐条一致的决策——这是「可复跑」纪律的底线。"""
    a = plan_for_corpus(_text_samples(20), config=_config())
    b = plan_for_corpus(_text_samples(20), config=_config())
    assert {k: v.tiers for k, v in a.per_sample.items()} == {
        k: v.tiers for k, v in b.per_sample.items()
    }
    assert a.planned_cost == b.planned_cost


def test_plan_without_config_is_conservative():
    """不给 config → 无尺子 → 全部跑满，且成本= 基线（省 0）。"""
    plan = plan_for_corpus(_text_samples(10))
    assert plan.saved_ratio == 0.0
    assert all(d.tiers == ALL_TIERS for d in plan.per_sample.values())


def test_explicit_table_overrides_config():
    """落盘的表优先于现读 config——因为事后审计要以落盘为准。"""
    samples = _text_samples(3)
    tight = {"chinese_ratio": 0.3, "char_repetition": 0.8, "line_repetition": 0.8}
    p_cfg = plan_for_corpus(samples, config=_config())
    p_tbl = plan_for_corpus(samples, config=_config(), keep_min_of_op=tight)
    # tight 里缺 pii_detect/doc_length → 可判定项变少但 chinese_ratio 余量仍宽
    assert p_tbl.planned_cost <= p_cfg.planned_cost


def test_cost_estimate_uses_weights():
    assert estimate_cost(("rule",), 10) == 10 * COST_WEIGHT["rule"]
    assert estimate_cost(("rule", "llm"), 1) == COST_WEIGHT["rule"] + COST_WEIGHT["llm"]


# ────────────────── run_state：不依赖 langgraph 的等价流程 ──────────────────


def test_run_state_records_threshold_table_in_manifest():
    """★ 落盘必须带着「当时用的那把尺子」，否则事后无法回答「按什么判的」。"""
    st = run_state(run_id="r1", samples=_text_samples(5), config=_config())
    d = st.to_dict()
    assert d["keep_min_of_op"]["doc_length"] == 30
    assert d["margin_ok"] == 0.5
    assert len(d["plans"]) == 5


def test_run_state_plans_match_pure_policy():
    """编排层不得藏逻辑：纯 Python 版的决策必须与逐条调policy 一致。"""
    samples = _text_samples(8)
    st = run_state(run_id="r2", samples=samples, config=_config())
    ref = plan_for_corpus(samples, config=_config())
    for sid, d in ref.per_sample.items():
        assert tuple(st.plans[sid]["tiers"]) == d.tiers, sid
        assert st.plans[sid]["reason"] == d.reason, sid


def test_run_state_is_json_serializable():
    """RunState 的契约：全部字段可JSON 序列化。"""
    st = run_state(run_id="r3", samples=_text_samples(3), config=_config())
    json.dumps(st.to_dict())


# ──────────────────往返：落盘再读回 ──────────────────


def _result(n: int = 6) -> AgentRunResult:
    plan = plan_for_corpus(_text_samples(n), config=_config())
    mem = compress(_records(n))
    return AgentRunResult(
        run_id="rt",
        plan=plan,
        memory=mem,
        ledger={"cost": 123, "calls": 4},
        effect=EffectComparison(baseline=0.9, agent=0.9, metric="kept_rate"),
        trace=["plan", "route", "memory"],
    )


def test_roundtrip_preserves_decisions():
    """★ to_dict 必须写 per_sample_plans（修复前它从不写 → 读回全空）。"""
    r = _result()
    back = AgentRunResult.from_dict(r.to_dict())
    assert set(back.plan.per_sample) == set(r.plan.per_sample)
    for sid, d in r.plan.per_sample.items():
        assert back.plan.per_sample[sid].tiers == d.tiers
        assert back.plan.per_sample[sid].reason == d.reason
        assert back.plan.per_sample[sid].skipped_tiers == d.skipped_tiers


def test_roundtrip_preserves_cost_and_effect():
    r = _result()
    back = AgentRunResult.from_dict(r.to_dict())
    assert back.plan.baseline_cost == r.plan.baseline_cost
    assert back.plan.planned_cost == r.plan.planned_cost
    assert back.effect is not None and back.effect.preserved
    assert back.ledger == r.ledger


def test_n_llm_calls_reports_baseline_and_routed():
    """★ 这两个数必须一起报——只报一个就看不出「省没省」。"""
    r = _result()
    assert r.n_llm_calls == len(r.plan.per_sample)
    assert r.n_llm_calls_after_routing == sum(
        1 for d in r.plan.per_sample.values() if d.runs_expensive
    )
    # 这批样本余量都宽 → 路由后应为 0，且这就是模块的价值所在
    assert r.n_llm_calls_after_routing == 0
    assert r.cost_saved_ratio > 0.5


def test_effect_preserved_tolerance():
    assert EffectComparison(baseline=0.9, agent=0.9).preserved
    assert not EffectComparison(baseline=0.9, agent=0.8).preserved
    assert EffectComparison(baseline=0.9, agent=0.9 + 1e-12).preserved


# ────────────────── 预算 ──────────────────


def test_budget_afford_and_record():
    led = BudgetLedger(total_budget=60)
    assert led.can_afford(COST_WEIGHT["rule"])
    assert led.can_afford(COST_WEIGHT["rule"] + COST_WEIGHT["llm"])
    led.record("rule", COST_WEIGHT["rule"], detail="跑规则档")
    led.record("llm", COST_WEIGHT["llm"], detail="跑 LLM 判官")
    assert led.spent == COST_WEIGHT["rule"] + COST_WEIGHT["llm"]
    assert led.by_tier == {"rule": COST_WEIGHT["rule"], "llm": COST_WEIGHT["llm"]}
    assert len(led.entries) == 2


def test_budget_rejects_negative_cost():
    """★ 负数代价必须报错——否则 remaining 变成加法，
    「越花越多」的语义错误账本也能通过所有断言。"""
    led = BudgetLedger(total_budget=10)
    with pytest.raises(ValueError, match="不得为负"):
        led.record("rule", -5)


def test_degrade_skips_unaffordable_but_keeps_cheaper():
    """★ 超预算要**跳贵的、留便宜的**，不能全停。

    `affordable_tiers` 按代价从低到高逐档判断，付不起就跳过它，
    但**继续看后面更便宜的档**——全停等于白白丢掉本可拿到的结果。
    """
    led = BudgetLedger(total_budget=COST_WEIGHT["rule"])
    kept = led.affordable_tiers(("rule", "perceptual", "model", "llm"), dict(COST_WEIGHT))
    assert kept == ("rule",), f"应只留得起 rule，实际 {kept}"
    assert "llm" in led.degraded_ops


def test_budget_exhausted_and_remaining():
    led = BudgetLedger(total_budget=10)
    assert not led.exhausted
    led.record("rule", 10)
    assert led.exhausted
    assert led.remaining == 0


def test_budget_ledger_roundtrip():
    led = BudgetLedger(total_budget=100)
    led.record("rule", 30, detail="d")
    back = BudgetLedger.from_dict(led.to_dict())
    assert back.spent == led.spent
    assert back.total_budget == led.total_budget
    assert back.by_tier == led.by_tier


# ────────────────── 记忆压缩 ──────────────────


def _records(n: int) -> list[DecisionRecord]:
    return [
        DecisionRecord(
            sample_id=f"s{i}",
            tiers=("rule",),
            reason=f"5 项规则分余量均>= 0.5（最弱 chinese_ratio=0.{60 + i % 30}）",
            evidence=f"scores={{chinese_ratio: 0.{60 + i % 30}}}",
        )
        for i in range(n)
    ]


def test_memory_compresses_into_summary():
    """压缩的价值：进上下文的只有摘要，**不是全部记录**。"""
    mem = compress(_records(200))
    assert mem.n_records == 200
    summary_only = len(mem.render_summary().splitlines())
    assert summary_only <= 10, "摘要渲染出来只有几行，不能随样本量线性增长"


def test_memory_keeps_traceability():
    """★ 压缩不能把「可追溯」一起压掉——每条决策都能按 id 追回理由。"""
    mem = compress(_records(20))
    assert mem.n_records == 20
    for i in range(20):
        got = mem.explain(f"s{i}")
        assert got is not None, f"s{i} 追不回"
        assert "chinese_ratio" in got
    assert mem.explain("不存在") is None


def test_memory_counts_skipped_tiers():
    """被跳过的档是「省下的成本来源」，必须单独统计——它是本模块的价值证据。"""
    recs = [
        DecisionRecord(sample_id="a", tiers=("rule",), reason="余量宽", evidence=""),
        DecisionRecord(sample_id="b", tiers=("rule", "llm"), reason="余量窄", evidence=""),
    ]
    mem = compress(recs, skipped={"llm": 1})
    assert mem.summary == {"rule": 1, "rule+llm": 1}
    assert mem.skipped == {"llm": 1}
    assert "省下的成本来源" in mem.render_summary()


def test_memory_is_deterministic():
    """★ 压缩必须纯函数——引入随机后就没法回归测试。"""
    a = compress(_records(30))
    b = compress(_records(30))
    assert a.to_dict() == b.to_dict()


def test_memory_within_budget():
    mem = compress(_records(50))
    assert mem.within_budget(mem.summary_tokens())
    assert not mem.within_budget(1)


def test_summary_tokens_scales_sublinearly():
    """★ 常驻上下文的 token 必须与样本量**近似无关**（实跑抓到过这个失效）。

    第一版 `summary_tokens()` 把每条 reason 都算进去：
    293 条样本下`render_summary()` 只要 6 token，
    `summary_tokens()` 却是 5206 token —— **恰好线性爆炸**。
    docstring 却写着「不会随样本量线性爆炸」。
    这类「文档承诺与实现相反」的失效最坏：代码看起来实现了压缩。

    ## 判据为什么不是「严格相等」
    第一版我写的是 `small.summary_tokens() == big.summary_tokens()`，
    实测 5 != 6 —— 而那**不是缺陷**：摘要里有 `rule: 10` 与 `rule: 400`
    这样的计数，计数当然会随样本量变。要求严格相等等于要求
    摘要不许报条数，而报条数恰恰是摘要的价值。

    这与parity 门禁那条教训同源：**先问「正确实现下会不会误判」**。
    正确判据是**增长量级对比**：样本 ×40 时，
    全量口径涨~40 倍而常驻口径基本不动。
    """
    small = compress(_records(10))
    big = compress(_records(400))
    full_growth = big.full_context_tokens() / max(small.full_context_tokens(), 1)
    summary_growth = big.summary_tokens() / max(small.summary_tokens(), 1)
    assert full_growth >= 10, f"全量口径本就该涨，实测只涨 {full_growth:.1f} 倍"
    assert summary_growth <= full_growth / 10, (
        f"常驻口径涨了 {summary_growth:.1f} 倍，而全量涨了 {full_growth:.1f} 倍—— 没实现解耦"
    )


def test_full_context_tokens_is_the_audit_view():
    """全量口径**确实**线性增长——它是审计视图，不该被「优化」掉。"""
    small = compress(_records(10))
    big = compress(_records(400))
    assert big.full_context_tokens() > small.full_context_tokens() * 10
    # 压缩比应该很小（常驻只有分桶摘要）
    assert big.compression_ratio() < 0.1, big.compression_ratio()


def test_within_budget_judges_summary_not_full():
    """★ 预算判定必须用常驻口径，否则同一预算会随样本量忽绿忽红。

    实测：400 条样本时全量口径 3906 token、常驻 6 token。
    若按全量判，预算给 100 token 时小语料通过、大语料就红——
    而变的不是预算，是口径选错了。
    """
    big = compress(_records(400))
    assert big.summary_tokens() < 100 < big.full_context_tokens()
    assert big.within_budget(100), "按常驻口径应通过"
    assert not big.within_budget(big.summary_tokens() - 1), "连常驻都超了才该红"


def test_memory_roundtrip():
    mem = compress(_records(10), skipped={"model": 3})
    back = CompressedMemory.from_dict(mem.to_dict())
    assert back.n_records == mem.n_records
    assert back.summary == mem.summary
    assert back.skipped == mem.skipped
    assert back.explain("s5") == mem.explain("s5")
