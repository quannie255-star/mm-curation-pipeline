"""L2 模型算子：Chinese-CLIP 图文对齐 + 语义去重（批量编码，GPU/CPU 自适应）。

为什么是 BatchOperator：逐样本调用会导致每个样本一次前向（batch_size=1），
GPU 利用率极低；批量编码是这类模型算子的唯一合理形态。它们在漏斗中位于
L1 规则与哈希去重之后——便宜算子先缩小规模，昂贵算子最后跑（成本分级）。

分数语义（与全项目统一：越高越好）：
- clip_alignment: 图文余弦相似度 [-1, 1]，错配对显著低于正常对
- semantic_dedup: 无单样本分数（判定依赖样本间关系），阈值是"图像向量
  余弦相似度超过多少视为同一张图"
"""

from __future__ import annotations

import numpy as np
from curation_eval import CostClass, register_operator

from ..embedding import clip_encoder
from .base import BatchOperator, Sample


def _mark_dup(dup: Sample, kept: Sample, method: str) -> None:
    dup.meta[f"dedup:{method}"] = {"duplicate_of": kept.id}


@register_operator(
    name="clip_alignment",
    modalities=frozenset({"image_caption"}),
    required_fields=frozenset({"text", "image_path"}),
    cost_class=CostClass.MODEL,
    shardable=True,  # 逐样本独立打分（批量仅为 GPU 效率）
)
class ClipAlignmentOp(BatchOperator):
    """Chinese-CLIP 图文对齐分：cos(image, caption) < min 的样本判为错配。

    靶子：mismatched_pair（caption 来自别的图）。真实业务里这来自
    网页图文错配（alt 文本与主图不符），是图文对语料最伤模型质量的污染。
    阈值经真实数据校准后写入 configs/（余弦相似度量级 ~0.2-0.35）。
    """

    def __init__(self, min: float = 0.2, **params):
        super().__init__(min=min, **params)
        self.min = min

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        encoder = clip_encoder.get_encoder()
        img = encoder.encode_images([s.image_path for s in samples])
        txt = encoder.encode_texts([s.text for s in samples])
        sims = np.sum(img * txt, axis=1)
        kept = []
        for s, sim in zip(samples, sims):
            s.meta["score:clip_alignment"] = float(sim)
            if sim >= self.min:
                kept.append(s)
        return kept


@register_operator(
    name="semantic_dedup",
    modalities=frozenset({"image_caption"}),
    required_fields=frozenset({"image_path"}),
    cost_class=CostClass.MODEL,
    shardable=False,
    superlinear=True,  # O(n²) 点积
    input_signal="embedding:image",  # 复用 clip_alignment 已编码的图像向量
)
class SemanticDedupOp(BatchOperator):
    """图像向量 kNN 语义去重：与已保留样本余弦相似度 > threshold 视为重复。

    靶子：semantic_duplicate（轻微裁剪 + caption 同义改写——md5/pHash/
    MinHash 全部失效，只有 embedding 能抓）。O(n²) 点积在万级样本内可接受
    （归一化向量矩阵相乘），更大规模换 FAISS（Week3 的索引直接复用本编码器）。
    """

    def __init__(self, threshold: float = 0.93, **params):
        super().__init__(threshold=threshold, **params)
        self.threshold = threshold

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        encoder = clip_encoder.get_encoder()
        emb = encoder.encode_images([s.image_path for s in samples])
        kept: list[Sample] = []
        kept_vecs: list[np.ndarray] = []
        for s, v in zip(samples, emb):
            if kept_vecs:
                sims = np.stack(kept_vecs) @ v
                if sims.max() > self.threshold:
                    j = int(sims.argmax())
                    _mark_dup(s, kept[j], "semantic_dedup")
                    continue
            kept.append(s)
            kept_vecs.append(v)
        return kept
