"""LangGraph 状态图：把「路由 → 执行 → 记账 → 降级」编排成一张图。

## 为什么 `langgraph` 是**惰性导入**（生产级取舍，不是偷懒）

本项目有两套 Python 环境：

- 主仓（`Python311`）：跑 625 条测试，**没有装 langgraph**
- Agent venv（`mm-agent`）：装了 langgraph，**但没装 `curation_eval`**（本项目的包）

若在模块顶层 `import langgraph`，那么主仓 `import mm_curation.agent` 直接炸，
625 条测试全灭。**为了让一个可选的编排框架拖垮整个项目，是不可接受的依赖方向。**

所以：
- `policy.py` / `budget.py` / `memory.py` / `plan.py` **零第三方依赖**，
  主仓可完整测试（这是绝大多数逻辑所在）
- `graph.py` 把 `langgraph` 的 import放在**函数体内**，
  只有真的要用图时才需要那个 venv

这不是「为跑分降级」，而是**依赖倒置**：编排是可选的，决策是必需的。

## 图的形状

    START → plan → route ⇄ execute → finalize → END
                  ↑│
                  └─┘（还有样本就继续）

`route` 是唯一的决策点，`execute` 按预算与路由结果跑对应档。
**为什么不做成「每档一个节点」**：那会让节点的执行顺序
由图的拓扑决定，而本项目要求「便宜的档先跑」——
顺序必须**由成本档声明**驱动，不能由图结构隐式决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .budget import BudgetLedger
from .memory import CompressedMemory, DecisionRecord
from .policy import (
    ALL_TIERS,
    COST_WEIGHT,
    decide_tiers,
    keep_min_table,
    plan_for_corpus,
)


@dataclass
class RunState:
    """LangGraph 的状态体。

    用 `dataclass` 而非 `TypedDict`：本图要跑在**两个环境**里
    （主仓测试不需要langgraph），dataclass 的行为在两边一致，
    而 `TypedDict` 的 reducers 只在 langgraph 运行时才有意义。

    所有字段都必须**可 JSON 序列化**——
    Agent 状态常被落盘用于事后审计，含不可序列化对象会让落盘失败，
    而失败的落盘往往在事故之后才发现。
    """

    run_id: str
    max_tier: str = "llm"
    total_budget: int = 10**9
    samples: list[dict[str, Any]] = field(default_factory=list)
    #: `算子名 → 漏斗的保留阈值`（量纲对照表）。
    #:
    #: **存表而不是存 PipelineConfig 对象**，有两条理由：
    #: 1. 本类的契约是「所有字段可 JSON 序列化」，而 config是复杂对象；
    #: 2. 更重要的——落盘一份表就等于**把当时的尺子留了证**。
    #: 事后审计能回答「当时是用哪套阈值判的」，而只存结果就答不了。
    #:
    #: 空 dict = 没有尺子→ `decide_tiers` 会一律按上限跑（不猜）。
    keep_min_of_op: dict[str, float] = field(default_factory=dict)
    #: 放行所需的最小归一余量（越大越保守）
    margin_ok: float = 0.5
    #: 已完成的样本 id 顺序（决策可复现性的证据）
    trace: list[str] = field(default_factory=list)
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    spent: int = 0
    degraded: list[str] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "max_tier": self.max_tier,
            "total_budget": self.total_budget,
            "n_samples": len(self.samples),
            "keep_min_of_op": dict(sorted(self.keep_min_of_op.items())),
            "margin_ok": self.margin_ok,
            "trace": list(self.trace),
            "plans": self.plans,
            "spent": self.spent,
            "degraded": list(self.degraded),
            "memory": self.memory,
        }


class LangGraphUnavailable(RuntimeError):
    """langgraph 不在当前环境。

    单独一个异常类型，并在 message 里说清**怎么办**——
    比 `ImportError` 多一层的信息量，因为这个错误几乎总是环境配置问题。
    """


def _require_langgraph() -> Any:
    """惰性导入 langgraph，失败时给出**可操作**的提示。

    错误信息必须回答「我该做什么」，而不只是「哪里错了」——
    这是本项目对所有错误信息的标准（见engineering notes 里的门禁报错纪律）。
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise LangGraphUnavailable(
            "langgraph 不在当前环境。\n"
            "  它只用于**编排**，不用于决策（policy.py 零依赖）。\n"
            "  测试与决策逻辑：在主仓 Python 直接跑 mm_curation.agent.policy。\n"
            "  跑图：换到装了 langgraph 的 venv，或先装它：\n"
            '    pip install "langgraph>=1.2,<2"'
        ) from exc
    return StateGraph, START, END


def plan_node(state: RunState) -> dict[str, Any]:
    """节点 1：为每条样本做路由决策。

    决策在**样本级**完成（不依赖全量），所以这一步可分片——
    这与 `OperatorMeta.shardable` 的语义一致。
    """
    plan = plan_for_corpus(
        _samples_from_state(state),
        max_tier=state.max_tier,
        # 量纲对照表由调用方在 run_state 时从 PipelineConfig 灌入，
        # **决策层不手抄** ——抄一份就会漂移，
        # 而漂移的后果是「省了钱但改了结论」且无法被察觉。
        keep_min_of_op=dict(state.keep_min_of_op) or None,
        margin_ok=state.margin_ok,
    )
    return {
        "plans": {
            sid: {"tiers": list(d.tiers), "reason": d.reason} for sid, d in plan.per_sample.items()
        },
        "trace": ["plan"],
    }


def route_node(state: RunState) -> dict[str, Any]:
    """节点 2：确定本次要付的代价并在超预算时降级。

    降级**必须留痕**（写进 `degraded`）：静默降级会让「成本下降」
    与「效果下降」混在一起，而后者是红线。
    """
    ledger = BudgetLedger(total_budget=state.total_budget, spent=state.spent)
    degraded: list[str] = []
    for sid, plan in state.plans.items():
        tiers = tuple(plan["tiers"])
        afford = ledger.affordable_tiers(tiers, COST_WEIGHT)
        if afford != tiers:
            degraded.append(sid)
        ledger.record("+".join(afford) or "direct", sum(COST_WEIGHT[t] for t in afford), detail=sid)
    return {
        "spent": ledger.spent,
        "degraded": degraded,
        "memory": ledger.to_dict(),
        "trace": state.trace + ["route"],
    }


def memory_node(state: RunState) -> dict[str, Any]:
    """节点 3：把决策压成可归因的摘要。"""
    records = [
        DecisionRecord(
            sample_id=sid,
            tiers=tuple(plan["tiers"]),
            reason=plan["reason"],
            evidence=plan["reason"][:80],
        )
        for sid, plan in state.plans.items()
    ]
    mem = CompressedMemory()
    for rec in records:
        mem.add(rec, skipped=("llm",) if "llm" not in rec.tiers else ())
    return {"memory": mem.to_dict(), "trace": state.trace + ["memory"]}


def build_graph():  # pragma: no cover - 需要 langgraph 环境
    """构建并编译状态图。

    **返回的是编译后的 app**，不是图：调用方拿到可直接 `invoke` 的对象。
    """
    StateGraph, START, END = _require_langgraph()

    g = StateGraph(RunState)
    g.add_node("plan", plan_node)
    g.add_node("route", route_node)
    g.add_node("memory", memory_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "route")
    g.add_edge("route", "memory")
    g.add_edge("memory", END)
    return g.compile()


def run_state(
    *,
    run_id: str,
    samples: list[Any],
    max_tier: str = "llm",
    total_budget: int = 10**9,
    config: Any = None,
    margin_ok: float = 0.5,
) -> RunState:
    """**不依赖 langgraph** 的等价流程。

    存在的理由很实际：`graph.py` 里的图只是**编排**，
    而编排的价值可以被这个纯 Python 版完整复现。
    有了它：
    - 主仓 625 条测试**不需要装 langgraph** 就能覆盖整条决策链
    - 面试时可以直接对比「同一套决策，编排层换掉不影响结果」

    如果两者结果不一致，那说明编排层里藏了逻辑——
    **那本身就是缺陷**，所以这个函数同时是一道对照门禁。

    ## `config` 为什么在这里就抽成表
    `RunState` 的字段约定是「全部可 JSON 序列化」，
    所以量纲对照表必须在**构造 state 之前**从 config抽好。
    顺带得到一个好处：`state.to_dict()` 里带着那张表，
    落盘之后能回答「这次是用哪套阈值判的」。
    """
    state = RunState(
        run_id=run_id,
        max_tier=max_tier,
        total_budget=total_budget,
        keep_min_of_op=(
            {k: v for k, v in keep_min_table(config).items() if v is not None}
            if config is not None
            else {}
        ),
        margin_ok=margin_ok,
        samples=[
            {
                "id": s.id,
                "text": getattr(s, "text", "") or "",
                "meta": dict(getattr(s, "meta", {}) or {}),
            }
            for s in samples
        ],
    )
    updates = plan_node(state)
    state.plans = updates["plans"]
    state.trace = list(updates["trace"])

    updates = route_node(state)
    state.spent = updates["spent"]
    state.degraded = updates["degraded"]
    ledger_dict = updates["memory"]

    updates = memory_node(state)
    # memory 节点用自己的压缩态；账本另存，供审计
    state.memory = {
        "ledger": ledger_dict,
        "compressed": updates["memory"],
    }
    state.trace = list(updates["trace"])
    return state


def _samples_from_state(state: RunState) -> list[Any]:
    """把 RunState 的 dict 形态还原成决策所需的最小对象。

    刻意**不复用真正的 `Sample`**：那会把 `__post_init__` 的校验
    （比如图像字段联动）带进来，而路由决策只需要 id/text/meta。
    构造一个鸭子类型的轻量对象，隔离了无关依赖——
    **测试装置不该引入被测对象之外的要求**。
    """

    class _Mini:
        __slots__ = ("id", "text", "meta", "labels", "image_path", "modality")

        def __init__(self, row: dict[str, Any]) -> None:
            self.id = row["id"]
            self.text = row["text"]
            self.meta = row["meta"]
            self.labels = {}
            self.image_path = None
            self.modality = "text_article"

    return [_Mini(r) for r in state.samples]


__all__ = [
    "ALL_TIERS",
    "LangGraphUnavailable",
    "RunState",
    "build_graph",
    "decide_tiers",
    "memory_node",
    "plan_node",
    "route_node",
    "run_state",
]
