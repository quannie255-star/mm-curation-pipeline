"""清洗算子基类与样本数据结构。

核心约定（整个漏斗的一致性都建立在这上面）：
- score 语义统一为「越高越好」的质量分；原本"越高越坏"的指标（如 NSFW 概率、
  模糊度）由算子内部转换为质量分（如 1 - p）。
- 算子必须无状态：参数经 __init__ 注入，不在样本间携带状态，保证可任意
  组合、可并行、可独立单测。
- score 写入 sample.meta["score:<op_name>"]，漏斗报告与阈值扫描直接复用，
  换阈值重跑无需重新计算。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Sample:
    """管道最小处理单元：一条图文对。

    labels 存放污染器注入的 ground truth（如 {"dirty": "watermark"}），
    干净样本为空 dict。评测模块据此计算每个算子的 precision/recall。
    """

    id: str
    image_path: str
    caption: str
    meta: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Sample":
        return cls(
            id=d["id"],
            image_path=d["image_path"],
            caption=d["caption"],
            meta=d.get("meta", {}),
            labels=d.get("labels", {}),
        )


class Operator(ABC):
    """单样本过滤算子基类。"""

    name: str = "operator"

    def __init__(self, **params: Any):
        self.params = params

    @abstractmethod
    def score(self, sample: Sample) -> Optional[float]:
        """返回该维度的质量分；None 表示无法计算（视为保留并记录为缺失）。"""

    def keep(self, score: Optional[float]) -> bool:
        if score is None:
            return True
        lo = self.params.get("min")
        hi = self.params.get("max")
        if lo is not None and score < lo:
            return False
        if hi is not None and score > hi:
            return False
        return True

    def __call__(self, sample: Sample) -> Optional[Sample]:
        s = self.score(sample)
        sample.meta[f"score:{self.name}"] = s
        return sample if self.keep(s) else None


class BatchOperator(Operator):
    """跨样本算子基类（去重等需要全量视角的阶段）。

    score() 不适用（判定依赖样本间关系），子类实现 run_batch()：
    输入全量样本，返回保留的样本子集，并把判定依据写入 meta。
    """

    def __call__(self, sample: Sample) -> Optional[Sample]:  # pragma: no cover
        raise TypeError(f"{type(self).__name__} 是批量算子，请通过 run_batch() 调用")

    def score(self, sample: Sample) -> Optional[float]:  # pragma: no cover
        raise TypeError(f"{type(self).__name__} 是批量算子，无单样本 score")

    @abstractmethod
    def run_batch(self, samples: list[Sample]) -> list[Sample]: ...
