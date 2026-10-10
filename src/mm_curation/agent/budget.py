"""成本记账与预算控制。

## 为什么预算控制必须是硬约束而不是提示

Agent 编排层的失败模式与模型不同：
模型的失败是「答错」，编排层的失败是「**钱烧完了才发现**」。
所以这里的设计取向是**默认拒绝**：

- 超预算不是「停止」，而是**降级**（跳掉最贵那档）
- 降级会**留痕**（记进`degraded_ops`），不允许静默降级
- 任何一次记账都必须能重放：`to_dict()` 出来的账与 `from_dict()` 回来的账一致

最后一条尤其重要：成本账本如果不能重放，
「Agent 省了 80% 成本」这句话就无法被第三方核对——
而这正是本项目所有对外数字的纪律要求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class BudgetExhausted(RuntimeError):
    """预算已耗尽且**不允许降级**时抛出。

    单独一个异常类型而不是复用 `RuntimeError`，
    是为了让调用方能精确区分「预算问题」与「其他 bug」——
    前者该降级重试，后者该炸。
    """


@dataclass
class BudgetLedger:
    """成本账本：记账 + 预算判定 + 降级建议。

    ## 为什么用相对代价而不是美元
    真实调用要按token / 图片数 / 模型单价算钱，依机器与模型而变。
    而本模块要回答的问题是「Agent 省了多少」，那是**相对量**。
    用绝对金额会让结论随环境漂移，而相对代价永远可比。
    需要绝对成本时（比如真接LLM API），在 `record` 时把绝对值一并写进 `detail`。
    """

    total_budget: int
    spent: int = 0
    #: 按成本档累计的代价
    by_tier: dict[str, int] = field(default_factory=dict)
    #: 实际被跳过的档（降级留痕）
    degraded_ops: list[str] = field(default_factory=list)
    #: 记账流水（用于重放核对）
    entries: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.total_budget <= 0:
            raise ValueError(f"total_budget 必须为正，得到 {self.total_budget}")

    @property
    def remaining(self) -> int:
        return self.total_budget - self.spent

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.total_budget

    def can_afford(self, cost: int) -> bool:
        return self.spent + cost <= self.total_budget

    def record(self, tier: str, cost: int, *, detail: str = "") -> None:
        """记一笔账。**不允许负数代价**——那会让`remaining` 变成加法，
        于是「越花越多」这种语义错误的账本也能通过所有断言。"""
        if cost < 0:
            raise ValueError(f"代价不得为负：tier={tier!r} cost={cost}")
        self.spent += cost
        self.by_tier[tier] = self.by_tier.get(tier, 0) + cost
        self.entries.append({"tier": tier, "cost": cost, "detail": detail})

    def affordable_tiers(self, tiers: tuple[str, ...], costs: dict[str, int]) -> tuple[str, ...]:
        """在预算内能付得起的档位（保持原顺序）。

        用于降级：**按代价从低到高**逐档判断付不付得起，
        付不起就跳过它，但**继续看后面更便宜的档**——
        直接「付不起就全停」会把后面免费的档也砍掉，
        那是错误的降级（白白丢失本可以拿到的结果）。
        """
        out: list[str] = []
        for t in tiers:
            c = costs.get(t, 1)
            if self.can_afford(c):
                out.append(t)
            else:
                self.degraded_ops.append(t)
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_budget": self.total_budget,
            "spent": self.spent,
            "remaining": self.remaining,
            "by_tier": dict(sorted(self.by_tier.items())),
            "degraded_ops": list(self.degraded_ops),
            "entries": list(self.entries),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BudgetLedger:
        """从 `to_dict()` 的结果重建。

        存在的理由：**账本必须可重放**。
        只有 `to_dict` 没有 `from_dict`，那么「记了账」和「账本正确」
        是两件无法互相验证的事——而本项目不允许出现无法验证的数字。
        """
        led = cls(total_budget=raw["total_budget"], spent=raw["spent"])
        led.by_tier = dict(raw.get("by_tier", {}))
        led.degraded_ops = list(raw.get("degraded_ops", []))
        led.entries = list(raw.get("entries", []))
        return led
