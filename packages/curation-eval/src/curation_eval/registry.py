"""算子注册表：带元数据声明的算子 SDK 机制。

框架拥有机制（注册/校验/元数据），实例拥有实现（具体算子在消费方注册）。
注册时 fail-fast 校验（协议矛盾在 import 时死，不在运行时）：
- required_fields 必须被声明的模态蕴含（text_article 蕴含 {text}，image_caption
  蕴含 {text, image_path}）
- 模态必须已在 MODALITY_FIELDS 登记；重名算子拒绝
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Type

from .schema import MODALITY_FIELDS


class CostClass(str, Enum):
    """成本档位（成本-质量前沿分析与逐算子成本表的渲染依据）。"""

    RULE = "rule"  # 纯 CPU 规则（长度/正则/字符统计）
    PERCEPTUAL = "perceptual"  # 感知哈希 / 传统 CV（pHash、Laplacian）
    MODEL = "model"  # 神经网络推理（CLIP、CNN 检测器）
    LLM = "llm"  # LLM-as-judge（远程推理服务）


@dataclass(frozen=True)
class OperatorMeta:
    """算子的框架级声明（注册时一次性校验）。"""

    name: str
    modalities: frozenset[str]
    required_fields: frozenset[str]
    cost_class: CostClass
    shardable: bool = True  # map 语义：输入分片结果不变；全量可见性算子必须 False
    superlinear: bool = False  # 复杂度超线性（规模悬崖标注，成本表渲染）
    input_signal: str | None = None  # 消费的质量信号 token（如 "embedding:image"）
    output_signal: str | None = None  # 产出的信号，默认 "score:<name>"

    def __post_init__(self) -> None:
        if not self.modalities:
            raise ValueError(f"{self.name}: modalities 不得为空")
        unknown_mod = set(self.modalities) - set(MODALITY_FIELDS)
        if unknown_mod:
            raise ValueError(
                f"{self.name}: 未知模态 {sorted(unknown_mod)}，已知: {sorted(MODALITY_FIELDS)}"
            )
        implied = frozenset().union(*(MODALITY_FIELDS[m] for m in self.modalities))
        unknown = set(self.required_fields) - implied
        if unknown:
            raise ValueError(
                f"{self.name}: 依赖字段 {sorted(unknown)} 未被模态 "
                f"{sorted(self.modalities)} 蕴含（蕴含: {sorted(implied)}）"
            )


_REGISTRY: dict[str, tuple[Type, OperatorMeta]] = {}


def _camel_to_snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def _derive_name(cls: Type) -> str:
    """类名派生算子名：WmNsfwCnnOp -> wm_nsfw_cnn（缩写连写保持一组）。"""
    return _camel_to_snake(cls.__name__).removesuffix("_op")


def register_operator(
    *,
    modalities,
    required_fields,
    cost_class,
    shardable: bool = True,
    superlinear: bool = False,
    input_signal: str | None = None,
    output_signal: str | None = None,
    name: str | None = None,
):
    """类装饰器：登记算子元数据。

    name 缺省时由类名派生（建议显式指定以精确匹配既有算子名）。
    """

    def decorator(cls: Type) -> Type:
        op_name = name or _derive_name(cls)
        meta = OperatorMeta(
            name=op_name,
            modalities=frozenset(modalities),
            required_fields=frozenset(required_fields),
            cost_class=CostClass(cost_class),
            shardable=shardable,
            superlinear=superlinear,
            input_signal=input_signal,
            output_signal=output_signal,
        )
        if op_name in _REGISTRY:
            raise ValueError(f"算子名冲突: {op_name} 已注册")
        _REGISTRY[op_name] = (cls, meta)
        cls.name = op_name
        cls.meta = meta
        return cls

    return decorator


def get_operator_meta(name: str) -> OperatorMeta:
    return _REGISTRY[name][1]


def available_operator_metas() -> dict[str, OperatorMeta]:
    return {n: m for n, (_, m) in _REGISTRY.items()}
