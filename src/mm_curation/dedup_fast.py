"""向量化文本近似去重（fast path）：numpy MinHash + banded LSH。

为什么存在：漏斗算子 minhash_lsh（datasketch 参考实现）逐文档计算签名，
长文本（千字级）在 10 万文档量级需要数小时——基准实测结论。本模块把签名
计算完全向量化（字节 4-gram shingle + 60 个通用哈希函数的最小值，numpy 广播），
30 万文档签名耗时从小时级降到 ~30 秒。语义与参考实现一致：字符 shingle 的
Jaccard 估计、先到先保留、候选对经估计 Jaccard 阈值复核。

算法细节：
- shingle = UTF-8 字节 4-gram（前 prefix_chars 字节；截断是网页去重的标准做法）
- 签名 = min((a_i * h + b_i) mod p)，i ∈ 60 个哈希函数（31 位素数域，避免溢出）
- LSH：80 签名分 8 band × 10 row。捕获率 = 1-(1-J^rows)^bands，桶规模
  随 rows 指数收缩：rows=6 时模板家族成员（两两 J≈0.75）共桶率 0.18，
  家族桶反复突破 max_bucket 触发跳桶，桶内真近重复被连带牺牲（β 基准
  实测 near 召回仅 0.5）；rows=10 家族共桶率降到 0.056，桶回到可复核
  规模，再靠加 band（8 个）把 J≈0.9 的真近重复捕获率拉回 ~0.95
- 签名完全相同的样本（精确重复）在分桶前无条件预聚类，不受跳桶影响——
  否则模板化语料（如维基同模板人物条目簇）撑爆 LSH 桶时，桶内真重复会被
  整桶漏掉（β 基准实测教训，见 docs/ENGINEERING_NOTES.md）
- 候选对经估计 Jaccard >= threshold 复核后才合并（union-find 传递闭包）
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

_PRIME = (1 << 31) - 1
_WEIGHTS = np.array([1, 256, 65536, 16777216], dtype=np.uint64)


@dataclass
class FastDedupResult:
    kept: list  # Sample 列表（每簇第一个，顺序保持）
    duplicate_of: dict[str, str] = field(default_factory=dict)
    est_jaccard: dict[str, float] = field(default_factory=dict)  # 被去重样本的估计相似度


def _signature(text: str, prefix_chars: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    data = text.encode("utf-8")[:prefix_chars].ljust(4, b"\x00")
    arr = np.frombuffer(data, dtype=np.uint8)
    win = np.lib.stride_tricks.sliding_window_view(arr, 4).astype(np.uint64)
    h = win @ _WEIGHTS  # (n_shingles,) uint64，值域 < 2^32
    # 31 位素数域：a < 2^31、h < 2^32 → a*h < 2^63，uint64 不溢出
    return ((a[:, None] * h[None, :] + b[:, None]) % np.uint64(_PRIME)).min(axis=1)


def _find(parent: list[int], x: int) -> int:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def dedup_texts(
    samples: list,
    *,
    threshold: float = 0.7,
    num_perm: int = 80,
    bands: int = 8,
    prefix_chars: int = 600,
    seed: int = 42,
    max_bucket: int = 2000,
) -> FastDedupResult:
    """文本近似去重：返回每簇的第一个样本（顺序保持）与重复溯源信息。"""
    if not samples:
        return FastDedupResult(kept=[])
    if num_perm % bands:
        raise ValueError("num_perm 必须能被 bands 整除")

    rng = np.random.default_rng(seed)
    a = rng.integers(1, 1 << 31, size=num_perm, dtype=np.uint64)
    b = rng.integers(0, 1 << 31, size=num_perm, dtype=np.uint64)
    sigs = np.stack([_signature(s.text, prefix_chars, a, b) for s in samples])
    rows = num_perm // bands

    parent = list(range(len(samples)))
    est: dict[int, float] = {}

    # 精确签名预聚类：60 个哈希函数全同 ≈ 文本前缀全同，无条件合并，
    # 且不占用 LSH 桶的两两复核名额（跳桶不再牺牲真重复）
    first_seen: dict[bytes, int] = {}
    for i in range(len(samples)):
        key = sigs[i].tobytes()
        if key in first_seen:
            parent[_find(parent, i)] = _find(parent, first_seen[key])
            est[i] = 1.0
        else:
            first_seen[key] = i

    buckets: dict[tuple, list[int]] = defaultdict(list)
    for i in range(len(samples)):
        sig = sigs[i]
        for band in range(bands):
            key = sig[band * rows : (band + 1) * rows].tobytes()
            buckets[(band, key)].append(i)

    for members in buckets.values():
        if len(members) < 2 or len(members) > max_bucket:
            continue  # 超大桶 = 高频 shingle 意外碰撞，跳过（宁漏勿错）
        for x, y in combinations(members, 2):
            if _find(parent, x) == _find(parent, y):
                continue  # 预聚类已合并
            j = float(np.mean(sigs[x] == sigs[y]))
            if j < threshold:
                continue
            rx, ry = _find(parent, x), _find(parent, y)
            if rx == ry:
                continue
            # 固定「小索引做根」：先到先保留必须由合并方向保证。若让 y 做
            # 根，注入样本（列表尾部的较大索引）会反过来当簇代表被保留，
            # 真源文档被丢——先到先保留退化为随机保留（β 基准实测教训）
            parent[max(rx, ry)] = min(rx, ry)
            est[max(x, y)] = max(est.get(max(x, y), 0.0), j)

    kept, duplicate_of, est_jaccard = [], {}, {}
    for i, s in enumerate(samples):
        root = _find(parent, i)
        if root == i:
            kept.append(s)
        else:
            duplicate_of[s.id] = samples[root].id
            if i in est:
                est_jaccard[s.id] = round(est[i], 4)
    return FastDedupResult(kept=kept, duplicate_of=duplicate_of, est_jaccard=est_jaccard)


def exact_text_duplicates(samples: list) -> dict[str, str]:
    """文本 md5 精确去重（text_article 版的 md5_exact 语义）。"""
    import hashlib

    seen: dict[str, str] = {}
    out: dict[str, str] = {}
    for s in samples:
        digest = hashlib.md5(s.text.encode("utf-8")).hexdigest()
        if digest in seen:
            out[s.id] = seen[digest]
        else:
            seen[digest] = s.id
    return out
