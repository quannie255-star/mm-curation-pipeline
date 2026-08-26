"""去重算子三件套（BatchOperator：需要全量视角，漏斗中独立成阶段）。

「先到先保留」约定：按输入顺序保留每组的第一个样本（种子集保证原始样本
在前、注入样本在后，与 contamination 模块的约定对齐）。

- md5_exact:  字节级完全相同（对应 exact_duplicate）
- phash_near: 感知哈希，海明距离 <= threshold 视为重复（对应 near_duplicate_image）
- minhash_lsh: caption 字符 3-gram MinHash + LSH，Jaccard 阈值召回
  （对应 near_duplicate_text；MinHash-LSH 是 JD 生态标配，见 docs/JD_RESEARCH.md）
"""

from __future__ import annotations

import hashlib

from .base import BatchOperator, Sample
from .registry import register


def _keep_first(samples: list[Sample], drop: set[str]) -> list[Sample]:
    return [s for s in samples if s.id not in drop]


def _mark_dup(dup: Sample, kept: Sample, method: str) -> None:
    dup.meta[f"dedup:{method}"] = {"duplicate_of": kept.id}


@register("md5_exact")
class Md5ExactDedup(BatchOperator):
    """图像字节 md5 完全一致 -> 去重。O(n) 哈希表，成本最低，放去重组最前。"""

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        seen: dict[str, Sample] = {}
        drop: set[str] = set()
        for s in samples:
            try:
                digest = hashlib.md5(open(s.image_path, "rb").read()).hexdigest()
            except OSError:
                continue
            if digest in seen:
                _mark_dup(s, seen[digest], "md5_exact")
                drop.add(s.id)
            else:
                seen[digest] = s
        return _keep_first(samples, drop)


@register("phash_near")
class PHashNearDedup(BatchOperator):
    """感知哈希（pHash, 64bit）海明距离 <= threshold（默认 16）-> 去重。

    对重编码/轻度裁剪/压缩鲁棒，而 md5 完全失效——这是感知去重存在的意义。
    默认阈值 12 经真实数据扫描校准（ROADMAP 2026-08-20）：真实照片中自然
    相似场景的海明距离从 ~14 起密集出现，12 以下基本只有真实近重复；
    配套污染器 near_duplicate_image 的轻度裁剪(4~10%)，召回可达 ~90%。
    """

    def __init__(self, threshold: int = 12, **params):
        super().__init__(threshold=threshold, **params)
        self.threshold = threshold

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        import imagehash
        from PIL import Image

        hashes: list[tuple[Sample, object]] = []
        drop: set[str] = set()
        for s in samples:  # 读图失败的样本不参与，交给质量算子处理
            try:
                hashes.append((s, imagehash.phash(Image.open(s.image_path))))
            except OSError:
                continue
        kept: list[tuple[Sample, object]] = []
        for s, h in hashes:
            dup_of = next((k for k, kh in kept if h - kh <= self.threshold), None)
            if dup_of is not None:
                _mark_dup(s, dup_of, "phash_near")
                drop.add(s.id)
            else:
                kept.append((s, h))
        return _keep_first(samples, drop)


@register("minhash_lsh")
class MinHashLshDedup(BatchOperator):
    """caption 文本 MinHash + LSH 近似去重。

    字符 3-gram 对中文无分词依赖。默认阈值 0.65 经真实数据扫描校准
    （ROADMAP 2026-08-20）：召回 94% / 误杀 0.7%（拐点）；0.5 时模板句
    自然相似（"拿网球拍的男人/女人"，J~0.5-0.6）会被批量误杀。
    短 caption（<min_len）不参与聚类：几字文本的 3-gram 无区分力，
    聚类只会制造碰撞（截断类低质文本的典型产物）。
    """

    def __init__(self, threshold: float = 0.65, num_perm: int = 128, min_len: int = 8, **params):
        super().__init__(threshold=threshold, num_perm=num_perm, min_len=min_len, **params)
        self.threshold = threshold
        self.num_perm = num_perm
        self.min_len = min_len

    @staticmethod
    def _shingles(text: str) -> set[str]:
        return {text[i : i + 3] for i in range(max(len(text) - 2, 1))}

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        from datasketch import MinHash, MinHashLSH

        lsh = MinHashLSH(threshold=self.threshold, num_perm=self.num_perm)
        drop: set[str] = set()
        by_id = {s.id: s for s in samples}
        for s in samples:
            if len(s.caption) < self.min_len:
                continue
            m = MinHash(num_perm=self.num_perm)
            for gram in self._shingles(s.caption):
                m.update(gram.encode("utf-8"))
            hits = lsh.query(m)
            if hits:
                _mark_dup(s, by_id[hits[0]], "minhash_lsh")
                drop.add(s.id)
            else:
                lsh.insert(s.id, m)
        return _keep_first(samples, drop)
