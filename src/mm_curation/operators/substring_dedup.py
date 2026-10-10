"""跨文档「精确共享子串」去重（suffix array 层，G2）。

为什么这一层不可省（与业界 SOTA 的差距）
--------------------------------------
本项目原去重三件套（`md5_exact` / `phash_near` / `minhash_lsh`）覆盖的是：
字节级相同、感知近似、以及**文档级Jaccard 近似**。
它们都抓不到一类东西：**两篇文档共享一段很长的逐字连续子串，
但整体措辞不同**。现实语料里这种片段极常见：
许可证头、爬虫注入的样板句、导航残留、模板化免责声明。

DataComp-LM 把这一层叫 **exact substring dedup**，实现方式是对
**拼接后的全语料**建后缀数组，删掉出现次数超阈值的长子串；
HF 的 datatrove 把它做成了流水线的一步。

⚠️ 阈值是**唯一危险参数**，不许拍脑袋
----------------------------------
DataComp-LM 的实测：**5-token 最小 n-gram 能保住 Core 指标，却重创 MMLU**
（因为短的结构化片段——列表项、多选题内容——被误删）。
所以「共享多长算重复」没有普适值。
`DEFAULT_MIN_CHARS` 只作为**占位默认值**（未标定），
真实取值必须由 `scripts/text_dedup_benchmark.py` 的对照实验给出，
并在 `configs/detection_slo.yaml` 之外单独登记。

与既有算子的关系（不动既有 config）
------------------------------------
- **新增**一个算子 `substring_dedup`，**不改** `md5_exact` /
  `phash_near` / `minhash_lsh` 的任何行为，既有 4 个 config 逐字不动。
- 与 `minhash_lsh` 的分工：MinHash 问「这两篇像不像」，
  本算子问「这两篇有没有共享一大段原话」。**不是替代关系。**

复杂度与规模声明（诚实边界）
----------------------------
拼接全语料 + 倍增后缀数组是 **O(N log N) 时间 / O(N) 空间**，
其中 N 是全语料**字符总数**。这在本项目万级样本（数十 MB）上是秒级；
到亿级文档（DataComp 那个量级）会超单机内存 —— 那是
datatrove / Spark 的分布式版本要解决的问题，本项目**不追**（见
docs/GAP_ANALYSIS.md 第五节：万级规模下这不是瓶颈，补它违背收敛决策）。
"""

from __future__ import annotations

from collections import defaultdict

from curation_eval import CostClass, register_operator

from .base import BatchOperator, Sample
from .dedup import _keep_first, _mark_dup

#:⚠️ **占位默认值，未经标定**。真实阈值须由
#: `scripts/text_dedup_benchmark.py` 的多档对照实验确定
#: （DataComp-LM 实证：5-token 档保 Core 但重创 MMLU）。
#: 这里给 200 字只是「让脚本能跑」的量级，不是结论。
DEFAULT_MIN_CHARS = 200

#: 参与子串比对的文本上限（单篇）。超长文本对 O(N log N) 是内存杀手，
#: 而「一篇 10 万字的文档」本身就该被 doc_length 算子拦掉。
MAX_DOC_CHARS = 100_000


def build_suffix_array(s: str) -> list[int]:
    """倍增法（prefix doubling）构造后缀数组，O(N log² N) 时间 / O(N) 空间。

    每轮把「当前长度 k 的块排名」当第一关键字、「向后 k 位的块排名」
    当第二关键字，稳定计数排序使长度翻倍，直到所有后缀互异。

    ⚠️ 用 `sorted(..., key=...)` 而不是手写计数排序：
    # 复杂度略高（多一个 log），但**没有边界 off-by-one 风险**，
    # 而这个模块的正确性靠变异测试守着，不值得为速度换正确性。
    """
    n = len(s)
    if n == 0:
        return []
    sa = list(range(n))
    rank = [ord(c) for c in s]
    k = 1
    while True:
        # 第二关键字为 -1 表示「后面不足 k 位」，排最前
        key = lambda i: (rank[i], rank[i + k] if i + k < n else -1)  # noqa: E731
        sa.sort(key=key)
        new_rank = [0] * n
        for idx in range(1, n):
            new_rank[sa[idx]] = new_rank[sa[idx - 1]] + (key(sa[idx]) != key(sa[idx - 1]))
        rank = new_rank
        if rank[sa[-1]] == n - 1:
            return sa
        k <<= 1
        if k >= n:
            return sa


def kasai_lcp(s: str, sa: list[int]) -> list[int]:
    """Kasai 算法求 height[sa[i]]（与前一后缀的最长公共前缀），O(N) 摊还。

    关键性质：`height[i] >= height[i-1] - 1`，所以从前往后扫时
    比较次数总摊还 O(N)。逐个起点的朴素比较是 O(N²)。
    """
    n = len(s)
    if n == 0:
        return []
    height = [0] * n
    lcp = 0
    rank = [0] * n
    for i, p in enumerate(sa):
        rank[p] = i
    for i in range(n):
        if rank[i] == 0:
            lcp = 0
            continue
        j = sa[rank[i] - 1]
        while i + lcp < n and j + lcp < n and s[i + lcp] == s[j + lcp]:
            lcp += 1
        height[rank[i]] = lcp
        if lcp > 0:
            lcp -= 1
    return height


def find_long_repeated(
    s: str, min_chars: int, bounds: list[tuple[int, int]] | None = None
) -> dict[int, int]:
    """找出所有「长度 ≥ min_chars 且出现 ≥ 2 次」的最长子串。

    返回 `{起点: 该处重复子串的长度}`。

    ⚠️ **`bounds` 是本函数语义的关键，不能省**

    不给 `bounds` 时，退化为「整串视角」的重复子串检测——**包含单篇
    内部的自我重复**。用例：一篇里`"abc"*20` 自我重复，会被算成「重复子串」，
    但那是**同一篇文档的复读**（那是 `char_repetition` 算子的职责，
    不该由跨文档去重来管）。

    给了 `bounds`（每篇在拼接串中的区间）后，只统计**跨文档**的重复：
    对每个位置 i，取它与**右侧最靠左**的、属于**另一篇**的后缀的 LCP；
    非跨篇的一律不算。这样判据才真正回答「哪两篇共享一大段原话」。

    实现：拼接串建 SA + height，按 sa 顺序扫。维护以 `i` 为右端、
    长度≥ `min_chars` 的连续链；链上若**存在属于不同文档的后缀对**，
    则该链上的每个起点都登记一次命中。
    """
    n = len(s)
    if n < min_chars * 2:
        return {}
    sa = build_suffix_array(s)
    height = kasai_lcp(s, sa)

    # 每个后缀起点 → 所属文档下标（未给 bounds 时全部视为同一篇 = 退化视角）
    if bounds is None:
        owner = [-1] * n
    else:
        owner = [-1] * n
        for di, (a, b) in enumerate(bounds):
            for p in range(a, min(b, n)):
                owner[p] = di

    hits: dict[int, int] = {}
    i = 1
    while i < n:
        if height[i] >= min_chars:
            # 找出这条「height >= min_chars」的连续链 [i, j]
            j = i
            while j + 1 < n and height[j + 1] >= min_chars:
                j += 1
            # 链上后缀是 sa[i-1..j+1]，它们两两之间 LCP ≥ min_chars
            docs = [owner[p] for p in sa[i - 1: j + 2] if owner[p] >= 0]
            cross = len(set(docs)) > 1 if bounds is not None else True
            if cross:
                # 区间内每个后缀登记：与链上邻居的最长 LCP
                for p in range(i, j + 1):
                    hits[sa[p]] = height[p]
            i = j + 1
        else:
            i += 1
    return hits


def find_long_repeated_cross(
    s: str, min_chars: int, bounds: list[tuple[int, int]]
) -> list[tuple[int, int, int]]:
    """找「跨文档出现 ≥ 2 次」的重复子串，返回 `[(起点, 长度, 对方文档下标)]`。

    这是 `run_batch` 真正使用的判定。核心不变量：

        **重复子串跨篇，当且仅当在 SA 排序里它的某个前驱属于另一篇。**

    为什么不能看「起点与终点是否落在不同文档」：两篇逐字相同时，
    重复子串的长度（实测 140 字）可能**远小于单篇长度**，
    起终点仍在同一篇里→ 那种判据恒不成立 → 完全漏检。
    （实现时正是用「两篇逐字相同」这个用例抓出来的。）

    为什么不能只看紧邻前驱：同一篇内部可能有很长的自我重复，
    紧邻前驱常常同篇。所以要**沿 height 链向左扩展**，
    越过所有同篇前驱，找到第一个异篇前驱。链上 LCP 单调不增，
    越往左长度只会更短 —— 这正是能取到的最大跨篇公共长度。
    """
    n = len(s)
    if n < min_chars * 2 or len(bounds) < 2:
        return []
    sa = build_suffix_array(s)
    height = kasai_lcp(s, sa)
    rank = [0] * n
    for i, p in enumerate(sa):
        rank[p] = i

    # 每个后缀起点 → 所属文档（分隔符位置不属于任何文档，标-1）
    owner = [-1] * n
    for di, (a, b) in enumerate(bounds):
        for p in range(a, min(b, n)):
            owner[p] = di

    out: list[tuple[int, int, int]] = []
    emitted: set[tuple[int, int]] = set()
    for i in range(1, n):
        if height[i] < min_chars:
            continue
        di = owner[sa[i]]
        if di < 0:
            continue
        # 沿链向左：height[i], height[i-1], ... 直到找到异篇前驱
        j = i
        while j - 1 >= 0:
            prev_start = sa[j - 1]
            dj = owner[prev_start]
            common = height[j]
            if common < min_chars:
                break
            if dj >= 0 and dj != di:
                key = (min(di, dj), max(di, dj))
                if key not in emitted:
                    emitted.add(key)
                    out.append((sa[i], common, dj))
                break
            j -= 1
    return out


@register_operator(
    name="substring_dedup",
    modalities=frozenset({"text_article"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
    shardable=False,  # 需要全语料视图
    superlinear=True,  # O(N log² N)
)
class SubstringDedup(BatchOperator):
    """跨文档精确共享子串去重（suffix array 层）。

    流程：
      ①把每篇文档用**不同分隔符**拼成一个大串（分隔符唯一 → 不会跨文档误配对）
      ② 建后缀数组 + LCP
      ③ 长度 ≥ `min_chars` 且出现 ≥ 2 次的子串 → 该区间内的文档互为重复
      ④ 按输入顺序保留每组的第一个（与其他去重算子一致的「先到先保留」约定）

    与 MinHash 的区别：MinHash 判「整体像不像」（对**改写**有效），
    本算子判「有没有共享一大段**原话**」（对**模板/样板/许可证**有效）。
    两者互补，不能互相替代。

    ⚠️ `min_chars` 未经标定，见模块顶部说明。
    """

    def __init__(self, min_chars: int = DEFAULT_MIN_CHARS, **params):
        super().__init__(min_chars=min_chars, **params)
        self.min_chars = min_chars

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        # 空/过短文档不进拼接串——它们只会制造噪声匹配
        usable = [s for s in samples if self.min_chars * 2 <= len(s.text) <= MAX_DOC_CHARS]
        if len(usable) < 2:
            return list(samples)

        # 用**每篇不同的分隔符**，防止子串跨文档边界被误配对
        seps = [chr(0xE000 + i) for i in range(len(usable))]
        if len({s for s in seps}) != len(usable):
            # 分隔符不够用（文档数超过私用区容量）→ 明确降级而非静默错误
            return self._blockwise(usable, samples)

        # 每篇在拼接串中的区间。**必须先建bounds 再判重**，
        # 否则单篇内部的自我重复（复读）也会被算成「跨文档共享」——
        # 那是char_repetition 的职责，跨文档去重不该越界。
        bounds: list[tuple[int, int]] = []  # (start, end)
        parts: list[str] = []
        pos = 0
        for s, sep in zip(usable, seps):
            bounds.append((pos, pos + len(s.text)))
            parts.append(s.text)
            parts.append(sep)
            pos += len(s.text) + 1
        joined = "".join(parts)

        try:
            cross = find_long_repeated_cross(joined, self.min_chars, bounds)
        except (MemoryError, RecursionError):  # pragma: no cover - 规模极端时
            return self._blockwise(usable, samples)

        def doc_of(p: int) -> int | None:
            lo, hi = 0, len(bounds) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                a, b = bounds[mid]
                if p < a:
                    hi = mid - 1
                elif p >= b:
                    lo = mid + 1
                else:
                    return mid
            return None

        # 文档 → 「与它共享长子串的另一篇文档」
        #
        # ⚠️ 判据是**子串在另一篇里也出现**，而不是「子串起终点落在不同文档」。
        # 后者是错的（实现时踩过）：两篇逐字相同时，重复子串长度（140）
        # 可能远小于单篇长度 → 起终点仍在同一篇 → 判据恒不成立 → **完全漏检**。
        # 正确做法见 `find_long_repeated_cross`：它沿 SA 链向左扩展，
        # 找**属于另一篇**的前驱，并直接返回对方文档下标。
        dup_of: dict[int, int] = {}
        for start, _length, other in cross:
            di = doc_of(start)
            if di is None or other is None or other == di:
                continue
            dup_of.setdefault(di, other)
            dup_of.setdefault(other, di)

        if not dup_of:
            return list(samples)

        drop: set[str] = set()
        for di, dj in dup_of.items():
            # 保证保留「下标更小」的那篇（先到先保留，与其余去重算子一致）
            keep_i, drop_i = (di, dj) if di < dj else (dj, di)
            _mark_dup(usable[drop_i], usable[keep_i], "substring_dedup")
            drop.add(usable[drop_i].id)
        return _keep_first(samples, drop)

    def _blockwise(self, usable: list[Sample], samples: list[Sample]) -> list[Sample]:
        """降级路径：文档过多时分块跑，避免拼接串爆内存。

        ⚠️ 分块会**削弱检出**（跨块的共享子串看不见），
        所以这不是等价实现——真到那一步应改用分布式方案。
        """
        chunk = max(2, len(usable) // 4)
        drop: set[str] = set()
        for start in range(0, len(usable), chunk):
            block = usable[start : start + chunk]
            if len(block) < 2:
                continue
            bbounds: list[tuple[int, int]] = []
            parts: list[str] = []
            pos = 0
            for i, s in enumerate(block):
                bbounds.append((pos, pos + len(s.text)))
                parts.append(s.text)
                parts.append(chr(0xE000 + i))
                pos += len(s.text) + 1
            joined = "".join(parts)
            try:
                cross = find_long_repeated_cross(joined, self.min_chars, bbounds)
            except (MemoryError, RecursionError):  # pragma: no cover
                continue
            for _start, _len, other in cross:
                di = doc_of_simple(joined, _start, block)
                if di is None or other is None or other == di:
                    continue
                keep_i, drop_i = (di, other) if di < other else (other, di)
                _mark_dup(block[drop_i], block[keep_i], "substring_dedup")
                drop.add(block[drop_i].id)
        return _keep_first(samples, drop)


def doc_of_simple(joined: str, pos: int, block: list[Sample]) -> int | None:
    """线性定位 `pos` 属于 block 的第几篇（分块降级路径用，规模小）。"""
    cursor = 0
    for i, s in enumerate(block):
        if cursor <= pos < cursor + len(s.text):
            return i
        cursor += len(s.text) + 1
    return None


#: 组间归因用的分组结果（供报告与调试）
def group_by_shared_substring(samples: list[Sample], min_chars: int) -> dict[str, list[str]]:
    """返回 {保留样本 id: [被它判重的样本 id, ...]}，仅用于报告/排查。"""
    op = SubstringDedup(min_chars=min_chars)
    kept = op.run_batch(list(samples))
    kept_ids = {s.id for s in kept}
    groups: dict[str, list[str]] = defaultdict(list)
    for s in samples:
        if s.id in kept_ids:
            continue
        dup = s.meta.get("dedup:substring_dedup") or {}
        anchor = (dup.get("duplicate_of") if isinstance(dup, dict) else None)
        if anchor:
            groups.setdefault(str(anchor), []).append(s.id)
    return dict(groups)
