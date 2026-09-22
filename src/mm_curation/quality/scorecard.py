"""数据质量记分卡（DQ Scorecard）+ SLO 比对。

为什么要这一层：分数、覆盖率、漂移这些数字散在各份报告里，
没人能一眼回答「这个数据集现在能不能用」。记分卡把它们收成
**一张 health 表**，每条都能归因到具体算子。

## 与行业工具的区别（这是本模块的立身之本）

Great Expectations / Soda 这类断言型工具的输出是 **pass / fail**。
它们**不回答两件事**：

1. **这条规则今天评过没有**（覆盖率）。覆盖率为 0 的规则报 pass，
   是因为它一条都没看——把「没评」当成「通过」，是质量看板最常见的自欺方式。
2. **这条规则本身灵不灵**（召回/误杀）。规则通过了不等于规则有效。

所以本记分卡把 `coverage` 提升为与 `score` 并列的一等列：
**覆盖率不足时分数记 `None` 并标 `NOT_EVALUATED`，不用 1.0 伪装。**

## 质量四维 → 算子的映射

映射写在 `configs/quality_slo.yaml` 里，不在代码里硬编码——
新增算子 = 改配置，不改代码。维度沿用数据治理行业通行的四维：

| 维度 | 问的问题 | 本项目对应算子（示例） |
|---|---|---|
| completeness 完整性 | 该有的字段/内容有没有 | doc_length、has_text |
| accuracy 准确性 | 内容与真值/期望是否一致 | 注入评测 recall、detector |
| consistency 一致性 | 口径/单位/格式是否统一 | chinese_ratio、unit_consistency |
| timeliness 时效性 | 数据是否够新、是否按时产出 | 窗口时间跨度、window_start |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

DEFAULT_SLO_PATH = "configs/quality_slo.yaml"

# 状态值：与报告口径一致（NOT_EVALUATED 不是 0，也不是通过）
STATUS_OK = "OK"
STATUS_BREACH = "BREACH"
STATUS_NOT_EVALUATED = "NOT_EVALUATED"
STATUS_NO_SLO = "NO_SLO"
# 有动作但**没有通过率语义**：批量去重算子只丢不打留分数，
# 而「丢」正是它的期望行为——用通过率会给它打低分，等于惩罚它正常工作。
STATUS_OBSERVED = "OBSERVED"

# 五维（行业通行四维 + 唯一性）：去重是本项目 R@1 提升的主因，
# 不纳入记分卡等于把最强的那一级排除在质量视图之外。
DIMENSIONS = ("completeness", "accuracy", "consistency", "uniqueness", "timeliness")


@dataclass
class SloSpec:
    """单个数据集 × 维度的质量目标。"""

    min_score: float | None = None
    min_coverage: float = 0.0


@dataclass
class ScorecardConfig:
    version: int
    dimensions: dict[str, dict[str, Any]] = field(default_factory=dict)  # dim -> {ops: [...]}
    slo: dict[str, dict[str, SloSpec]] = field(default_factory=dict)  # dataset -> dim -> SloSpec

    def ops_for(self, dim: str) -> list[str]:
        return list((self.dimensions.get(dim) or {}).get("ops") or [])

    def slo_for(self, dataset: str, dim: str) -> SloSpec | None:
        return self.slo.get(dataset, {}).get(dim)


def load_slo(path: str | Path = DEFAULT_SLO_PATH) -> ScorecardConfig:
    if yaml is None:  # pragma: no cover
        raise RuntimeError("需要 pyyaml：`pip install pyyaml`")
    p = Path(path)
    if not p.exists():
        return ScorecardConfig(version=0)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    dims = {k: v for k, v in (data.get("dimensions") or {}).items()}
    slo: dict[str, dict[str, SloSpec]] = {}
    for ds, per in (data.get("slo") or {}).items():
        slo[ds] = {}
        for dim, spec in (per or {}).items():
            spec = spec or {}
            slo[ds][dim] = SloSpec(
                min_score=None if spec.get("min_score") is None else float(spec["min_score"]),
                min_coverage=float(spec.get("min_coverage", 0.0)),
            )
    return ScorecardConfig(version=int(data.get("version", 0)), dimensions=dims, slo=slo)


@dataclass
class DimensionScore:
    dataset: str
    dimension: str
    ops_total: int
    ops_evaluated: int
    coverage: float | None  # 已评单元 / 理论单元；无法计算时为 None
    score: float | None  # 已评样本上的通过率；无分母时为 None
    status: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "dimension": self.dimension,
            "ops_total": self.ops_total,
            "ops_evaluated": self.ops_evaluated,
            "coverage": self.coverage,
            "score": self.score,
            "status": self.status,
            "detail": self.detail,
        }


def _pass_rate(n_pass: int, n_scored: int) -> float | None:
    """通过率。**分母为 0 时返回 None 而不是 0**（笔记：0/0 不是零，是没有分母）。"""
    if n_scored <= 0:
        return None
    return n_pass / n_scored


def score_dimension(
    *,
    dataset: str,
    dimension: str,
    ops: list[str],
    per_op: dict[str, dict[str, int]],  # op -> {n_scored, n_drop}
    n_samples: int,
    slo: SloSpec | None,
) -> DimensionScore:
    """给一个数据集的一个质量维度打分。

    `per_op` 是被评到的算子及其「评了多少 / 扔了多少」；
    不在这张表里的算子 = **一次都没评过**，计入 `ops_total` 但不进分子。
    """
    # 「评过」有两种证据：留了分数（打分算子），或做了丢弃（批量去重算子只丢不打分）。
    # 只认分数会把 md5_exact / phash_near / minhash_lsh / text_minhash 误判成空转——
    # 它们在 image_funnel 上实丢 342 条，却一条分数都没留（笔记 #78）。
    evaluated = [
        op
        for op in ops
        if op in per_op and (per_op[op].get("n_scored", 0) > 0 or per_op[op].get("n_drop", 0) > 0)
    ]
    n_scored_total = sum(per_op[op]["n_scored"] for op in evaluated)
    n_drop_total = sum(per_op[op].get("n_drop", 0) for op in evaluated)
    # 只丢不打分的算子（批量去重）会让 n_scored=0 而 n_drop>0，直接相减得负数；
    # 夹到 0 —— 负的「通过数」没有意义，且此时 score 本就是 None。
    n_pass_total = max(0, n_scored_total - n_drop_total)

    # 覆盖率用**算子级**而不是单元级：批量去重算子只丢不打分，
    # 用单元级分数覆盖率会让它恒为 0（明明丢了 342 条却显示「未评」），见笔记 #78。
    # 算子级回答的是治理真正关心的问题：「这个维度下有几条规则真的在跑」。
    coverage = (len(evaluated) / len(ops)) if ops else None

    score = _pass_rate(n_pass_total, n_scored_total)

    # 状态判定：先判能不能评，再判达不达标——顺序不能反
    if not ops or not evaluated:
        status = STATUS_NOT_EVALUATED
    elif slo is None:
        status = STATUS_NO_SLO
    elif score is None:
        # 有动作、但没有通过率语义（批量去重算子）：只承诺「规则在跑」，
        # 不承诺通过率——否则等于惩罚它正常工作。
        status = (
            STATUS_OBSERVED
            if coverage is not None and coverage >= slo.min_coverage
            else STATUS_BREACH
        )
    else:
        ok = True
        if slo.min_score is not None:
            ok = ok and score >= slo.min_score
        ok = ok and coverage is not None and coverage >= slo.min_coverage
        status = STATUS_OK if ok else STATUS_BREACH

    return DimensionScore(
        dataset=dataset,
        dimension=dimension,
        ops_total=len(ops),
        ops_evaluated=len(evaluated),
        coverage=coverage,
        score=score,
        status=status,
        detail={
            "ops_evaluated_names": sorted(evaluated),
            "ops_never_evaluated": sorted(set(ops) - set(evaluated)),
            "n_scored": n_scored_total,
            "n_pass": n_pass_total,
            "n_drop": n_drop_total,
            "slo": None
            if slo is None
            else {
                "min_score": slo.min_score,
                "min_coverage": slo.min_coverage,
            },
        },
    )


def build_scorecard(
    *,
    profile: dict[str, dict[str, Any]],  # dataset -> {n_total, ...}
    stages: dict[str, dict[str, dict[str, int]]],  # dataset -> op -> {n_scored, n_drop}
    cfg: ScorecardConfig,
) -> list[DimensionScore]:
    """对整个数仓打分（纯函数，不连数据库——便于测试与复算）。"""
    out: list[DimensionScore] = []
    for dataset in sorted(profile):
        n_samples = int(profile[dataset].get("n_total", 0) or 0)
        per_op = stages.get(dataset, {})
        for dim in DIMENSIONS:
            ops = cfg.ops_for(dim)
            out.append(
                score_dimension(
                    dataset=dataset,
                    dimension=dim,
                    ops=ops,
                    per_op=per_op,
                    n_samples=n_samples,
                    slo=cfg.slo_for(dataset, dim),
                )
            )
    return out


def summarize(cards: list[DimensionScore]) -> dict[str, Any]:
    """汇总：健康度 + 破线项清单（对标 OpenMetadata 的 data health score）。

    ⚠️ health_score 是**覆盖率加权**的，不是简单平均。
    原因：finance_funnel 的 accuracy 只有 1/6 条规则在跑（coverage 0.167）却拿了 1.0 分——
    简单平均会让这个「几乎没评」的维度和 coverage 0.75 的维度平起平坐，
    把 0.976 这种虚高数字端出去。加权后它只按 0.167 的话语权计入。
    配套把 `mean_coverage` 一起报出来：**没有覆盖率的健康度是不能单独引用的**。
    """
    scored = [c for c in cards if c.score is not None]
    w = [c.coverage or 0.0 for c in scored]
    wsum = sum(w)
    health = sum(c.score * (c.coverage or 0.0) for c in scored) / wsum if wsum > 0 else None
    covs = [c.coverage for c in cards if c.coverage is not None]
    by_status: dict[str, int] = {}
    for c in cards:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    return {
        "n_dimensions": len(cards),
        "n_evaluated": len(scored),
        "health_score": health,
        "health_score_weighted_by": "coverage",
        "mean_coverage": (sum(covs) / len(covs)) if covs else None,
        "by_status": by_status,
        "breaches": [
            {
                "dataset": c.dataset,
                "dimension": c.dimension,
                "score": c.score,
                "coverage": c.coverage,
            }
            for c in cards
            if c.status == STATUS_BREACH
        ],
        "not_evaluated": [
            {
                "dataset": c.dataset,
                "dimension": c.dimension,
                "ops_never_evaluated": c.detail.get("ops_never_evaluated", []),
            }
            for c in cards
            if c.status == STATUS_NOT_EVALUATED
        ],
    }
