"""记忆压缩：把批量的样本级信息压成**可归因**的摘要。

## 要解决的问题

一次 Agent 跑批会产生几十上百条决策记录。
把全部原始记录塞进上下文既超token 预算，又让人看不出「到底为什么」。
所以需要压缩**但压缩最容易犯的错是把「可追溯」一起压掉了**——
变成一段读起来很顺、但无法追问细节的摘要。

## 本模块的取舍：可归因优先于压缩率

压缩后必须能回答两类问题：

1. **单条**决策为什么这么定？（→ 每条决策留一个 `evidence` 短串）
2. **某个样本**被哪个环节判了什么？（→ `by_sample` 索引）

而**不可回答**的问题：「第 37 条决策的完整输入是什么」。
那个靠 `entries` 全量落盘解决，**不靠压缩后的内存态**。

换句话说：**压缩的是上下文，不是可审计性**。
这与本项目「判决书逐条落盘」的既有设计是一致的——
Agent 的记忆压缩是同一件事的延续，不是新机制。

## token 预算是怎么算的

用「字符数 / 4」作为 token 的粗估（CJK 会高估、英文会低估，
对预算控制来说宁可保守）。**明确写出来是为了避免被当成精确值**——
把它当精确 token 数用，就会在真实调用时超预算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def estimate_tokens(text: str) -> int:
    """粗估 token 数。**是估计，不是精确值**。

    用 `len // 4` 对纯英文相当准，对中文会**高估约1.5~2 倍**。
    对预算控制来说高估是安全方向（宁可少装也不要超预算），
    但**不能**用它来做「精确控制输出长度」这类事。
    """
    return max(1, len(text) // 4)


@dataclass
class DecisionRecord:
    """一条决策的**落盘形态**：短、可归因、不含原始大对象。

    为什么不是直接存 `TierDecision`：
    后者含 `reason` 长文本与 tier 列表，直接 dump 会随版本变动而变，
    于是历史账本无法回放。落盘用固定字段才稳定。
    """

    sample_id: str
    tiers: tuple[str, ...]
    reason: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "tiers": list(self.tiers),
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass
class CompressedMemory:
    """压缩后的记忆：三层结构。

    1. `summary`   —— 分桶计数，一眼看出决策分布
    2. `by_sample` —— 每条决策的短理由，可逐条追问
    3. `records`   —— 全量落盘记录（供重放核对，不进上下文）
    """

    #: 摘要：决策档位组合 → 出现次数
    summary: dict[str, int] = field(default_factory=dict)
    #: sample_id → 决策（短形式）
    by_sample: dict[str, DecisionRecord] = field(default_factory=dict)
    #: 全量记录（追加式）
    records: list[DecisionRecord] = field(default_factory=list)
    #: 被跳过的档：档 → 次数（用来验证「省了多少钱」）
    skipped: dict[str, int] = field(default_factory=dict)

    def add(self, rec: DecisionRecord, *, skipped: tuple[str, ...] = ()) -> None:
        key = "+".join(rec.tiers) if rec.tiers else "direct"
        self.summary[key] = self.summary.get(key, 0) + 1
        self.by_sample[rec.sample_id] = rec
        self.records.append(rec)
        for t in skipped:
            self.skipped[t] = self.skipped.get(t, 0) + 1

    # ── 视图 ──
    @property
    def n_records(self) -> int:
        return len(self.records)

    def render_summary(self) -> str:
        """把摘要渲染成给人看的一行行文本。"""
        lines = [f"决策分布（{self.n_records} 条）："]
        for key, n in sorted(self.summary.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {key}: {n}")
        if self.skipped:
            lines.append("被跳过的档（= 省下的成本来源）：")
            for t, n in sorted(self.skipped.items()):
                lines.append(f"  {t}: {n}")
        return "\n".join(lines)

    def explain(self, sample_id: str) -> str | None:
        """单条决策的理由 —— 这是「压缩不丢可归因性」的落点。"""
        rec = self.by_sample.get(sample_id)
        if rec is None:
            return None
        tail = f"（证据：{rec.evidence}）" if rec.evidence else ""
        return f"{rec.tiers or '直接放行'}：{rec.reason}{tail}"

    # ── 预算 ──
    def summary_tokens(self) -> int:
        """把**分桶摘要**放进上下文需要多少 token。

        这个数**不随样本量线性增长** —— 它只与「有几种决策形态」有关，
        与「有多少条样本」无关。这正是压缩的意义。

        ## 第一版把这个方法写错了（2026-10-05 实跑抓到）
        docstring 写「不含 records，所以不会线性爆炸」，
        而实现却是：

            parts = [self.render_summary()]
            parts.extend(r.reason for r in self.by_sample.values())

        即**每条样本的理由都被算进上下文**。
        实跑 293 条样本：`render_summary()` 只要 6 token，
        `summary_tokens()` 却是 **5206 token** —— 恰好线性爆炸。
        docstring 与实现互相矛盾，而这类矛盾最坏：
        **代码看起来实现了压缩，实际没有。**

        修法是把两个口径彻底分开：
        - `summary_tokens()`：常驻上下文用，只算分桶摘要（O(形态数)）
        - `full_context_tokens()`：要把全部理由都带上时用（O(样本数)）

        两者都必须存在，因为**「可归因性」与「上下文预算」是真实的张力**：
        全带上可归因但贵，只带摘要便宜但要靠 `explain(id)` 回查。
        把它们混成一个数，就等于假装这个张力不存在。
        """
        return estimate_tokens(self.render_summary())

    def full_context_tokens(self) -> int:
        """把摘要**与每条理由**都放进上下文需要的 token。

        用途：事后审计 / 小语料全量回顾。
        **它确实随样本量线性增长** —— 这是这个方法存在的意义，
        而不是缺陷：审计时本来就要看到全部依据。

        拿它与 `summary_tokens()`对比就是压缩比：

            ratio = summary_tokens() / full_context_tokens()
        """
        parts = [self.render_summary()]
        parts.extend(r.reason for r in self.by_sample.values())
        return estimate_tokens("\n".join(parts))

    def compression_ratio(self) -> float:
        """压缩比：常驻上下文 / 全量上下文。越小压得越狠。"""
        full = self.full_context_tokens()
        if full <= 0:
            return 0.0
        return self.summary_tokens() / full

    def within_budget(self, max_tokens: int) -> bool:
        """常驻摘要是否在预算内。

        注意判的是 `summary_tokens()` 而非全量——
        否则「超预算」的判定会随样本量变化，
        同一个预算在 10 条时通过、300 条时就红，
        而那不是预算的问题，是口径选错了。
        """
        return self.summary_tokens() <= max_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": dict(sorted(self.summary.items())),
            "skipped": dict(sorted(self.skipped.items())),
            "n_records": self.n_records,
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CompressedMemory:
        mem = cls()
        mem.summary = dict(raw.get("summary", {}))
        mem.skipped = dict(raw.get("skipped", {}))
        for item in raw.get("records", []):
            rec = DecisionRecord(
                sample_id=item["sample_id"],
                tiers=tuple(item["tiers"]),
                reason=item["reason"],
                evidence=item.get("evidence", ""),
            )
            mem.by_sample[rec.sample_id] = rec
            mem.records.append(rec)
        return mem

    def dump(self, path: Any) -> None:
        """全量落盘。**记忆压缩不牺牲可审计性**——细节在这里，不在上下文里。"""
        from pathlib import Path

        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def compress(
    records: list[DecisionRecord], *, skipped: dict[str, int] | None = None
) -> CompressedMemory:
    """把决策记录压成 `CompressedMemory`。

    这是**纯函数**：同样的输入必然得到同样的输出。
    之所以强调这条，是因为记忆压缩一旦引入随机性
    （比如按时间窗口切分、或采样保留），就无法回归测试。
    """
    mem = CompressedMemory()
    for rec in records:
        mem.add(rec)
    if skipped:
        mem.skipped.update(skipped)
    return mem
