"""清洗算子基类与样本数据结构——协议单一来源：curation-eval（V2 决策②）。

本模块只做 re-export；Sample/Operator/BatchOperator 的定义、注册表机制、
执行器协议都在 curation-eval 包内（0.2.0 起为公共 API）。
score 语义约定（随协议下沉）：统一为「越高越好」；None = 无法计分（保留并
记录缺失）；分数写入 sample.meta["score:<op_name>"] 供漏斗报告与阈值扫描复用。
主仓库提供具体算子实现（经 @register_operator 注册进 curation_eval 注册表）。
"""

from __future__ import annotations

from curation_eval import (
    BatchOperator,
    Executor,
    FunnelResult,
    LocalSequentialExecutor,
    Operator,
    Sample,
    StageStat,
)

__all__ = [
    "BatchOperator",
    "Executor",
    "FunnelResult",
    "LocalSequentialExecutor",
    "Operator",
    "Sample",
    "StageStat",
]
