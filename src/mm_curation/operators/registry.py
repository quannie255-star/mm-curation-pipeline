"""算子注册表：YAML 里的算子名 -> 类。

新增算子只需在模块内用 @register("name") 装饰并在 operators/__init__.py
导入，YAML 即可直接引用，无需改动执行器。
"""

from __future__ import annotations

from typing import Any, Type

from .base import BatchOperator, Operator

_REGISTRY: dict[str, Type[Operator]] = {}


def register(name: str):
    def decorator(cls: Type[Operator]) -> Type[Operator]:
        if name in _REGISTRY:
            raise ValueError(f"算子名冲突: {name} 已注册为 {_REGISTRY[name].__name__}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def build_operator(spec: dict[str, Any]) -> Operator:
    op_name = spec.get("op")
    if op_name not in _REGISTRY:
        raise KeyError(f"未注册的算子: {op_name!r}。可用算子: {sorted(_REGISTRY)}")
    return _REGISTRY[op_name](**spec.get("params", {}))


def available_operators() -> list[str]:
    return sorted(_REGISTRY)


def is_batch(op: Operator) -> bool:
    return isinstance(op, BatchOperator)
