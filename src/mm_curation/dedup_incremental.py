"""增量去重器（Phase2 P3）：批处理漏斗 → 持续服务的关键升级。

场景：数据持续流入（爬虫/Kafka），新样本到达时立刻判定是否重复，
命中即丢、未命中即入索引——不需要攒批重跑全量去重。

三层判定（与漏斗去重组同一套阈值语义，PATH 已在 ROADMAP 校准）：
1. md5 精确：字节级相同
2. pHash 感知：与已有哈希海明距离 <= threshold（线性扫，万级内可接受；
   十万级换分桶/LSH——扩展点已注明）
3. MinHash-LSH 文本：caption 近似重复（datasketch 原生增量 insert/query）

语义去重（图像向量）需 GPU 编码，默认关闭；真实部署接 FAISS IVF 常驻
（design 表 P3 的扩展路径），本模块先把"查-判-插"的协议定型。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass
class DedupVerdict:
    is_duplicate: bool
    method: Optional[str] = None  # md5_exact / phash_near / minhash_lsh
    duplicate_of: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "is_duplicate": self.is_duplicate,
            "method": self.method,
            "duplicate_of": self.duplicate_of,
        }


class IncrementalDeduper:
    def __init__(
        self,
        phash_threshold: int = 12,
        minhash_threshold: float = 0.65,
        min_caption_len: int = 8,
        num_perm: int = 128,
    ):
        self.phash_threshold = phash_threshold
        self.minhash_threshold = minhash_threshold
        self.min_caption_len = min_caption_len
        self.num_perm = num_perm
        self._md5: dict[str, str] = {}  # digest -> sample_id（反查 duplicate_of）
        self._phash: list[tuple[str, int]] = []  # (id, 64bit 整数哈希)
        from datasketch import MinHashLSH

        self._lsh = MinHashLSH(threshold=minhash_threshold, num_perm=num_perm)
        self._sigs: dict[str, object] = {}

    def check_and_add(self, sample_id: str, image_path: str, caption: str) -> DedupVerdict:
        """查-判-插一体：返回判定；未命中时把该样本登记进三层索引。"""
        verdict = self.check(sample_id, image_path, caption)
        if verdict.is_duplicate:
            return verdict
        self._add(sample_id, image_path, caption)
        return verdict

    def check(self, sample_id: str, image_path: str, caption: str) -> DedupVerdict:
        # 1) md5
        digest = hashlib.md5(open(image_path, "rb").read()).hexdigest()
        if digest in self._md5:
            return DedupVerdict(True, "md5_exact", self._md5[digest])
        # 2) pHash（海明距离 = 两 64bit 整数异或的置位数）
        if self._phash:
            import imagehash
            from PIL import Image

            h = int.from_bytes(imagehash.phash(Image.open(image_path)).hash.tobytes(), "big")
            for sid, stored in self._phash:
                if bin(h ^ stored).count("1") <= self.phash_threshold:
                    return DedupVerdict(True, "phash_near", sid)
        # 3) MinHash-LSH
        if len(caption) >= self.min_caption_len:
            m = self._minhash(caption)
            hits = self._lsh.query(m)
            if hits:
                return DedupVerdict(True, "minhash_lsh", hits[0])
        return DedupVerdict(False)

    def _add(self, sample_id: str, image_path: str, caption: str) -> None:
        digest = hashlib.md5(open(image_path, "rb").read()).hexdigest()
        self._md5[digest] = sample_id
        import imagehash
        from PIL import Image

        h = int.from_bytes(imagehash.phash(Image.open(image_path)).hash.tobytes(), "big")
        self._phash.append((sample_id, h))
        if len(caption) >= self.min_caption_len:
            m = self._minhash(caption)
            self._lsh.insert(sample_id, m)
            self._sigs[sample_id] = m

    def _minhash(self, caption: str):
        from datasketch import MinHash

        m = MinHash(num_perm=self.num_perm)
        for g in {caption[i : i + 3] for i in range(max(len(caption) - 2, 1))}:
            m.update(g.encode("utf-8"))
        return m

    def __len__(self) -> int:
        return len(self._phash)
