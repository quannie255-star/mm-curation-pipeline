"""把算子包装成「判决书生产者」：不改算子、不改执行器、不改协议。

## 为什么用包装器而不是改执行器

γ 阶段的等价性验收（2106 条四口径全等）依赖 `LocalSequentialExecutor` 与
`RayDistributedExecutor` 的行为契约。把落账逻辑塞进执行器会同时改动两个后端的
语义面。包装器让判决书成为**算子层面的可选外包层**：

- 单样本算子 → `RecordingOperator`：委托 `__call__`，看返回是否 None 定 drop/keep
- 批量算子 → `RecordingBatchOperator`：委托 `run_batch`，按 id 差集定 drop/keep
  （执行器已经把模态不匹配的样本预过滤掉，故这里看到的正是「被评判的」那批）

`isinstance(op, BatchOperator)` 的分派由 `wrap_for_verdict` 保证——包装后类型不变，
执行器的分派逻辑照旧。
"""

from __future__ import annotations

from typing import Any

from curation_eval import BatchOperator, Operator

from .ledger import VerdictLedger, build_verdict


def _explain_of(inner: Any, sample, score: float | None) -> dict[str, Any]:
    """取算子的可选领域判据（`explain` 钩子）；未实现或返回值不合规 → 空。"""
    fn = getattr(inner, "explain", None)
    if not callable(fn):
        return {}
    out = fn(sample, score)
    return out if isinstance(out, dict) else {}


class _RecordingBase:
    """共享的元数据透传 + 落账（`meta`/`params` 必须透传，执行器要读 meta.modalities）。"""

    def __init__(self, inner, seq: int, ledger: VerdictLedger):
        self.inner = inner
        self.seq = seq
        self.ledger = ledger
        self.name = getattr(inner, "name", type(inner).__name__)
        self.meta = getattr(inner, "meta", None)
        self.params = getattr(inner, "params", {}) or {}

    def _record(self, sample, *, dropped: bool) -> None:
        score = sample.meta.get(f"score:{self.name}")
        self.ledger.record(
            build_verdict(
                run_id=self.ledger.run_id,
                seq=self.seq,
                op=self.name,
                sample=sample,
                dropped=dropped,
                params=self.params,
                extra_evidence=_explain_of(self.inner, sample, score),
            )
        )


class RecordingOperator(_RecordingBase, Operator):
    """单样本算子的判决书包装（阈值判决语义与内层完全一致）。"""

    def score(self, sample):
        return self.inner.score(sample)

    def keep(self, score):
        return self.inner.keep(score)

    def __call__(self, sample):
        out = self.inner(sample)
        self._record(sample, dropped=out is None)
        return out


class RecordingBatchOperator(_RecordingBase, BatchOperator):
    """批量算子的判决书包装（全量视角；drop = 未进入存活集）。"""

    def run_batch(self, samples):
        survivors = self.inner.run_batch(samples)
        survived = {s.id for s in survivors}
        for s in samples:
            self._record(s, dropped=s.id not in survived)
        return survivors


def wrap_for_verdict(op, *, seq: int, ledger: VerdictLedger):
    """按算子类型分派包装（包装后 isinstance 语义不变）。"""
    if isinstance(op, BatchOperator):
        return RecordingBatchOperator(op, seq, ledger)
    return RecordingOperator(op, seq, ledger)
