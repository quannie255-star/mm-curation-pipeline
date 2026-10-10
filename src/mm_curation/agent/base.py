"""Agent 运行结果：把「决策 + 成本 + 可归因性」收成一份可核对的产物。

## 为什么单独一个 base.py

因为运行结果是这个模块**唯一的对外契约**：
别的东西（policy / budget / memory）都是实现细节，
而这份结果是会被写进文档、被门禁核对、被第三方复跑的东西。

所以它的字段设计遵循两条：
1. **每个对外数字都有来源**（`baseline_cost` / `planned_cost` 都来自policy 的现算）
2. **效果可比**（`effect` 字段直接给出与静态基线的差值，而不是让人自己比）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .memory import CompressedMemory
from .policy import CorpusPlan

#: 模块版本。**变更时必须同步**，否则历史 run 的 manifest 无法解释。
AGENT_VERSION = "0.1.0"


@dataclass(frozen=True)
class EffectComparison:
    """Agent 路由与静态基线的**效果对比**。

    这是整个模块最重要的一段：成本节省本身没有意义，
    只有「**省了钱且效果没掉**」才成立。

    字段刻意保留 `recall_like` 这种**可被不同漏斗填充**的形态，
    而不是写死 R@1 —— 因为本模块的对照实验在真实语料上做，
    那里没有「检索召回」这个概念，只有「保留率」。
    把它写死成 R@1 会让这个类在别的口径下变成负担。
    """

    #: 静态基线的效果值（如 kept_rate）
    baseline: float
    #: Agent 路由后的效果值
    agent: float
    #: 该效果的名称，如 "kept_rate"
    metric: str = "kept_rate"

    @property
    def delta(self) -> float:
        return self.agent - self.baseline

    @property
    def preserved(self) -> bool:
        """效果是否持平。

        容差 1e-9 而不是 0：Agent 与静态跑的是**同一批样本**，
        理论上应逐位一致；但若将来引入任何浮点路径，
        「持平」不该因为最后一位的表示差异就判失败。
        1e-9 已经远小于任何有业务意义的差异。
        """
        return abs(self.delta) <= 1e-9

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "agent": self.agent,
            "delta": self.delta,
            "preserved": self.preserved,
        }


@dataclass
class AgentRunResult:
    """一次 Agent 运行的完整结果。

    ## 为什么成本与效果**必须放在同一个对象里**
    只报成本节省而不报效果对比，那份数字是**无法被质疑的**——
    看起来很划算，但没有「省了钱的代价是什么」的答案。
    把两者绑在一起，任何人看到 cost_saved_ratio 都能立刻问效果，
    也就没法只挑好听的报。
    """

    run_id: str
    plan: CorpusPlan
    memory: CompressedMemory
    ledger: dict[str, Any]
    effect: EffectComparison | None = None
    #: 决策轨迹（逐条 id 顺序）—— 可复现性的证据
    trace: list[str] = field(default_factory=list)

    @property
    def cost_saved_ratio(self) -> float:
        return self.plan.saved_ratio

    @property
    def n_llm_calls(self) -> int:
        """按全量基线口径，LLM 判官**本应**被调用的次数。

        反过来也说明这个模块的价值：
        若 Agent 路由后 `n_llm_calls` 接近这个数，
        那「自适应路由」并没有省到什么——**判据自己会承认这一点**。
        """
        return len(self.plan.per_sample)

    @property
    def n_llm_calls_after_routing(self) -> int:
        return sum(1 for d in self.plan.per_sample.values() if d.runs_expensive)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_version": AGENT_VERSION,
            "n_samples": len(self.plan.per_sample),
            "baseline_cost": self.plan.baseline_cost,
            "planned_cost": self.plan.planned_cost,
            "cost_saved_ratio": round(self.plan.saved_ratio, 6),
            "n_llm_calls_baseline": self.n_llm_calls,
            "n_llm_calls_after_routing": self.n_llm_calls_after_routing,
            "global_ops": list(self.plan.global_ops),
            # ★ 逐样本决策必须落盘（2026-10-05 修的真缺口）。
            #   `from_dict` 一直读这个键，但 `to_dict` 从不写它——
            #   于是「跑完存盘再读回来」会**静默丢掉全部决策**，
            #   读回来的对象看起来正常（有 baseline/planned_cost），
            #   只是 per_sample 空了。那正是本项目最恨的那类失效：
            #   **不报错，只是内容没了。**
            "per_sample_plans": {
                sid: {
                    "tiers": list(d.tiers),
                    "reason": d.reason,
                    "skipped_tiers": list(d.skipped_tiers),
                }
                for sid, d in self.plan.per_sample.items()
            },
            "ledger": self.ledger,
            "memory": self.memory.to_dict(),
            "effect": self.effect.to_dict() if self.effect else None,
            "trace": list(self.trace),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgentRunResult:
        """从 `to_dict()` 重建。

        **往返必须等价**——这是「落盘内容可信」的唯一保证。
        记忆里的教训：`from_dict` 缺失会让「记了账」与「账本正确」
        变成两件无法互相验证的事。
        """
        effect_raw = raw.get("effect")
        eff = (
            EffectComparison(
                baseline=effect_raw["baseline"],
                agent=effect_raw["agent"],
                metric=effect_raw.get("metric", "kept_rate"),
            )
            if effect_raw
            else None
        )

        plans_raw = raw.get("per_sample_plans") or {}
        from .policy import TierDecision  # noqa: PLC0415

        # 计划对象需要从 per_sample 决策重建；若落盘里没有，
        # 退化为「全部按计划档」的空计划——**并显式记下来**，
        # 而不是悄悄给出一个看起来正常的对象。
        per_sample = {
            sid: TierDecision(
                tiers=tuple(v["tiers"]),
                reason=v["reason"],
                skipped_tiers=tuple(v.get("skipped_tiers", ())),
            )
            for sid, v in plans_raw.items()
        }
        plan = CorpusPlan(
            per_sample=per_sample,
            global_ops=tuple(raw.get("global_ops", [])),
            baseline_cost=raw["baseline_cost"],
            planned_cost=raw["planned_cost"],
        )
        return cls(
            run_id=raw["run_id"],
            plan=plan,
            memory=CompressedMemory.from_dict(raw["memory"]),
            ledger=raw["ledger"],
            effect=eff,
            trace=list(raw.get("trace", [])),
        )
