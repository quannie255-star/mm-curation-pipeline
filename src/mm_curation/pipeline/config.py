"""清洗漏斗 YAML 配置的解析与校验。

配置即契约：所有阈值、算子组合、顺序都外置到 configs/*.yaml，
代码不硬编码业务阈值，保证「换阈值 = 换配置文件」的可复现实验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..operators.base import Operator
from ..operators.registry import available_operators, build_operator


@dataclass
class OperatorSpec:
    op: str
    params: dict[str, Any] = field(default_factory=dict)

    def build(self) -> Operator:
        return build_operator({"op": self.op, "params": self.params})


@dataclass
class PipelineConfig:
    name: str
    raw_jsonl: Path
    output_dir: Path
    operators: list[OperatorSpec]
    description: str = ""

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PipelineConfig":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{path} 应为 YAML 映射")

        missing = {"name", "dataset", "operators", "output"} - raw.keys()
        if missing:
            raise ValueError(f"{path} 缺少字段: {sorted(missing)}")

        known = set(available_operators())
        specs: list[OperatorSpec] = []
        for i, item in enumerate(raw["operators"]):
            if not isinstance(item, dict) or "op" not in item:
                raise ValueError(f"operators[{i}] 格式错误，应为 {{op: ..., params: ...}}")
            if item["op"] not in known:
                raise ValueError(
                    f"operators[{i}] 引用了未注册算子 {item['op']!r}，可用: {sorted(known)}"
                )
            specs.append(OperatorSpec(op=item["op"], params=item.get("params", {})))
        if not specs:
            raise ValueError("operators 不能为空")

        return cls(
            name=raw["name"],
            description=raw.get("description", ""),
            raw_jsonl=Path(raw["dataset"]["raw_jsonl"]),
            output_dir=Path(raw["output"]["dir"]),
            operators=specs,
        )
