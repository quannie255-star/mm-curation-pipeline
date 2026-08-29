"""跨样本集去污染（decontamination）：训练语料 vs 评测集的重叠检测。

生产背景：所有大模型实验室在评测前都要做"预训练语料 × 基准测试集"的
去污染——重叠会虚高评测分。本项目把同一协议用在管道内部：
- 评测前：检索评测的目标图像不得（近似）存在于被测索引之外的正份
- 训练前：种子集之间的交叉泄漏检测

检测双层（与漏斗去重组同一套组件语义）：
1. 图像 pHash：海明距离 <= threshold 视为同一张图
2. 文本 MinHash-LSH：caption 近似重复（字符 3-gram Jaccard）

输入两个样本集 A（语料）与 B（评测/新集），返回 B 中与 A 重叠的样本
及判定依据。P/R 可用 ground truth labels 验证（注入的重复即"已知污染"）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..operators.base import Sample


@dataclass
class OverlapHit:
    sample_id: str
    method: str  # phash_image / minhash_text
    overlap_with: str
    distance: float  # 海明距离或 Jaccard 相似度（按 method 解释）


def _phash_int(path: str) -> int:
    import imagehash
    from PIL import Image

    return int.from_bytes(imagehash.phash(Image.open(path)).hash.tobytes(), "big")


def _minhash(caption: str, num_perm: int = 128):
    from datasketch import MinHash

    m = MinHash(num_perm=num_perm)
    for g in {caption[i : i + 3] for i in range(max(len(caption) - 2, 1))}:
        m.update(g.encode("utf-8"))
    return m


def detect_overlap(
    corpus: list[Sample],
    incoming: list[Sample],
    phash_threshold: int = 12,
    text_threshold: float = 0.65,
    min_caption_len: int = 8,
) -> list[OverlapHit]:
    """找出 incoming 中与 corpus 重叠的样本（先图像后文本，命中即记录）。

    corpus 侧建立 pHash 线性表 + MinHash-LSH 索引；规模到十万级时
    pHash 换分桶、LSH 原生可扩展（与 dedup_incremental 同一升级路径）。
    """
    from datasketch import MinHashLSH

    corpus_phash = [(s.id, _phash_int(s.image_path)) for s in corpus]
    lsh = MinHashLSH(threshold=text_threshold, num_perm=128)
    corpus_sigs: dict[str, object] = {}
    for s in corpus:
        if len(s.caption) >= min_caption_len:
            m = _minhash(s.caption)
            lsh.insert(s.id, m)
            corpus_sigs[s.id] = m

    hits: list[OverlapHit] = []
    for s in incoming:
        h = _phash_int(s.image_path)
        match = next(
            (
                (cid, bin(h ^ ph).count("1"))
                for cid, ph in corpus_phash
                if bin(h ^ ph).count("1") <= phash_threshold
            ),
            None,
        )
        if match:
            hits.append(OverlapHit(s.id, "phash_image", match[0], float(match[1])))
            continue
        if len(s.caption) >= min_caption_len:
            m = _minhash(s.caption)
            found = lsh.query(m)
            if found:
                j = max(_jaccard(m, corpus_sigs[fid]) for fid in found)
                hits.append(OverlapHit(s.id, "minhash_text", found[0], j))
    return hits


def _jaccard(a, b) -> float:
    return a.jaccard(b)


def evaluate_decontam(hits: list[OverlapHit], incoming: list[Sample]) -> dict:
    """对照 ground truth（labels.dirty 非空 = 已知污染）算 P/R。"""
    flagged = {h.sample_id for h in hits}
    dirty = {s.id for s in incoming if s.labels}
    tp = len(flagged & dirty)
    fp = len(flagged - dirty)
    fn = len(dirty - flagged)
    return {
        "n_incoming": len(incoming),
        "n_flagged": len(flagged),
        "precision": round(tp / (tp + fp), 4) if flagged else None,
        "recall": round(tp / (tp + fn), 4) if dirty else None,
        "clean_flagged": fp,
        "method_breakdown": _count_by_method(hits),
    }


def _count_by_method(hits: list[OverlapHit]) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in hits:
        out[h.method] = out.get(h.method, 0) + 1
    return dict(sorted(out.items()))
