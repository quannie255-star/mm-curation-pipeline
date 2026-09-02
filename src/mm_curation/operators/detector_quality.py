"""L2 检测器算子：合成数据自训的水印/NSFW CNN（Phase2 P1-T3）。

score = 1 - max(P(watermark), P(ad))，与全项目"越高越好"语义统一。
BatchOperator 形态：批量推理（逐样本前向会把 GPU 利用率打到地板）。
"""

from __future__ import annotations

import numpy as np
from curation_eval import CostClass, register_operator

from ..detector import model as detector_model
from .base import BatchOperator, Sample


@register_operator(
    name="wm_nsfw_cnn",
    modalities=frozenset({"image_caption"}),
    required_fields=frozenset({"image_path"}),
    cost_class=CostClass.MODEL,
    shardable=True,  # 逐样本独立推理
)
class WmNsfwCnnOp(BatchOperator):
    """水印/NSFW 检测：max P(脏类) > 1-min 视为脏。

    默认 min=0.30（任一脏类概率超过 0.70 即丢弃）——经全量脏集阈值扫描
    校准：主靶召回 100% / 干净误杀 1.0%。此前默认 0.90 时误杀 16.9%
    （softmax 分数分布长尾所致，见 ENGINEERING_NOTES #36）。"""

    def __init__(self, min: float = 0.30, batch_size: int = 64, **params):
        super().__init__(min=min, batch_size=batch_size, **params)
        self.min = min
        self.batch_size = batch_size

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        import torch
        from PIL import Image
        from torchvision import transforms

        model = detector_model.load_detector()
        device = next(model.parameters()).device
        tf = transforms.Compose(
            [
                transforms.Resize((224, 224)),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ]
        )
        probs_all = []
        with torch.no_grad():
            for start in range(0, len(samples), self.batch_size):
                chunk = samples[start : start + self.batch_size]
                x = torch.stack([tf(Image.open(s.image_path).convert("RGB")) for s in chunk]).to(
                    device
                )
                probs_all.append(torch.softmax(model(x), dim=1).cpu().numpy())
        probs = np.concatenate(probs_all) if probs_all else np.zeros((0, 3))
        kept = []
        for s, p in zip(samples, probs):
            score = float(1.0 - max(p[1], p[2]))  # 1 - max(P(wm), P(ad))
            s.meta["score:wm_nsfw_cnn"] = score
            if score >= self.min:
                kept.append(s)
        return kept
