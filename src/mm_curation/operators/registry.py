"""算子注册表——机制在 curation-eval，本模块只提供主仓库便捷封装。

V2 起：注册机制（元数据声明 + 注册时 fail-fast 校验）来自 curation_eval.registry；
本模块保留 build_operator / is_batch / available_operators 供管道配置与脚本使用。
"""

from __future__ import annotations

from typing import Any

from curation_eval import BatchOperator, Operator
from curation_eval.registry import available_operator_metas, get_operator_class


def build_operator(spec: dict[str, Any]) -> Operator:
    """按配置 spec 构造算子实例：{"op": name, "params": {...}}。"""
    op_name = spec.get("op")
    try:
        cls = get_operator_class(op_name)
    except KeyError:
        raise KeyError(f"未注册的算子: {op_name!r}。可用算子: {available_operators()}") from None
    return cls(**spec.get("params", {}))


def available_operators() -> list[str]:
    return sorted(available_operator_metas())


def is_batch(op: Operator) -> bool:
    return isinstance(op, BatchOperator)
