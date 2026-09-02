"""实时质量门（Phase2 P2）：单条数据到达即评分，不阻塞写入。

与漏斗共享同一套算子与阈值（从 PipelineConfig 构建单样本算子集），
保证「在线判定」与「离线漏斗」口径一致——这是能对接真实数据管道的前提
（爬虫 → 质量门 → 入库），也是批处理管道产品化的关键一步。
"""

from __future__ import annotations

import logging
from typing import Optional

from ..operators.base import Operator, Sample
from ..operators.registry import is_batch

logger = logging.getLogger(__name__)


class QualityGate:
    def __init__(self, ops: list[Operator], detector_op: Optional[Operator] = None):
        self.ops = ops  # 单样本算子（含阈值）
        self.detector_op = detector_op  # wm_nsfw_cnn（权重存在时启用）

    @classmethod
    def from_config(cls, config, include_detector: bool = True) -> "QualityGate":
        ops = []
        for spec in config.operators:
            op = spec.build()
            if not is_batch(op):
                ops.append(op)
        detector_op = None
        if include_detector:
            try:
                from ..detector.model import DEFAULT_MODEL_PATH
                from ..operators import build_operator

                if DEFAULT_MODEL_PATH.exists():  # 权重缺失 → 质量门降级（无 wm_nsfw 分数）
                    detector_op = build_operator({"op": "wm_nsfw_cnn", "params": {"min": 0.30}})
                else:
                    logger.info("检测器权重缺失，质量门降级运行（无 wm_nsfw_cnn 分数）")
            except Exception as e:
                logger.info("检测器算子不可用，质量门降级: %s", e)
        return cls(ops, detector_op)

    def assess(self, image_path: str, text: str) -> dict:
        """逐算子打分并按阈值判 flags。样本缺 text 时文本算子给 None 分。"""
        sample = Sample(id="ingest", image_path=image_path, text=text if text is not None else "")
        scores: dict[str, Optional[float]] = {}
        flags: list[str] = []
        for op in self.ops:
            score = op.score(sample)
            sample.meta[f"score:{op.name}"] = score
            scores[op.name] = None if score is None else round(float(score), 4)
            if not op.keep(score):
                flags.append(op.name)
        if self.detector_op is not None:
            kept = self.detector_op.run_batch([sample])
            scores["wm_nsfw_cnn"] = round(float(sample.meta.get("score:wm_nsfw_cnn", 0.0)), 4)
            if not kept:
                flags.append("wm_nsfw_cnn")
        return {"scores": scores, "flags": flags, "passed": not flags}
