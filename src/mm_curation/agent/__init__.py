"""Agent 编排层：按样本状态自适应决定「哪些成本档值得跑」。

## 这个模块解决的具体问题

现有漏斗是**固定顺序**：`config.operators` 按 YAML 顺序依次执行。
而算子声明里已有权威的成本分档（`OperatorMeta.cost_class`）：

    rule 23 个 / perceptual 2 个 / model 4 个 / llm 1 个

`llm_judge` 是唯一的一档 LLM 判官，也是最贵的。
让每条样本都去问它，等于**为大部分明显干净的样本付最贵的钱**。

所以本模块做的是：**在结果可比的前提下，省掉那些问不问都一样贵的档**。

## 红线：只省成本，不掉质量

任何「Agent 跑出来的效果」必须与**静态全量基线**可比。
所以本模块的设计有一条硬约束：

- 路由只读**上游已经算好的 score**（`sample.meta['score:*']`），**不重算**
- 判据全部是**显式阈值 + 确定性规则**，无 LLM、无随机
- `shardable=False` 的算子不进逐样本路由（它们需要全量可见性）

## 模块清单与依赖方向

    policy.py    零依赖（纯函数决策）      ← 绝大多数逻辑
    budget.py    零依赖（记账 + 降级）
    memory.py    零依赖（压缩 + 可归因）
    plan.py      零依赖（语料级计划）
    graph.py     惰性依赖 langgraph         ← 只有编排需要它

依赖方向是刻意的：**决策不依赖编排**。
这样主仓 625 条测试能完整覆盖决策链，而不必在主仓装 langgraph
（它只装在独立 venv `mm-agent` 里，而那个 venv 没装本项目的包）。
"""

from .base import AGENT_VERSION, AgentRunResult
from .budget import BudgetExhausted, BudgetLedger
from .graph import LangGraphUnavailable, RunState, build_graph, run_state
from .memory import (
    CompressedMemory,
    DecisionRecord,
    compress,
    estimate_tokens,
)
from .policy import (
    ALL_TIERS,
    COST_WEIGHT,
    SKIPPABLE_TIERS,
    CorpusPlan,
    TierDecision,
    decide_tiers,
    estimate_cost,
    keep_min_table,
    normalized_margin,
    plan_for_corpus,
    tier_table,
)

__all__ = [
    "AGENT_VERSION",
    "ALL_TIERS",
    "COST_WEIGHT",
    "SKIPPABLE_TIERS",
    "AgentRunResult",
    "BudgetExhausted",
    "BudgetLedger",
    "CompressedMemory",
    "CorpusPlan",
    "DecisionRecord",
    "LangGraphUnavailable",
    "RunState",
    "TierDecision",
    "build_graph",
    "compress",
    "decide_tiers",
    "estimate_cost",
    "estimate_tokens",
    "keep_min_table",
    "normalized_margin",
    "tier_table",
    "plan_for_corpus",
    "run_state",
]
