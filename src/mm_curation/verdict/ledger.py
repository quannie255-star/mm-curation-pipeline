"""判决书（Verdict Ledger）：每条清洗决策一条机器可读的裁决记录。

## 定位

判决书**不是「一个层」，是一条数据记录**，被四处消费：
漏斗（写入）→ 人审层（读队列）→ 血缘层（导出 Croissant 记录级血缘）→ UI（展示/统计）。

## 为什么这是生态位（V6 §五/§六）

Data-Juicer 的 `tracer` 能告诉你「哪些样本被过滤了」，但那是**运行期内存态**：
不落盘、无判据依据、无输入指纹。Croissant 的 PROV-O 只做到**数据集/文件级**。
「单条样本为什么被删、依据是什么、能不能被推翻」这一层是空的。

## 三格是「判决书」与「一行日志」的分界

- `rule`：**机器可读判据代号**（如 `score_below_min`），可聚合可统计
- `evidence`：**判据真正用到的量**（如 `{chars_han: 430, chars_total: 3001}`）
- `input_fingerprint`：该级输入的 sha256，判决可回溯到具体输入

## 旁路产物（零破坏）

默认不落盘；只有显式构造 `VerdictLedger` 并传给 `run_funnel` 才写。
既有 config 的行为逐位不变（V4「既有数字逐项相等」红线）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

VERDICT_SCHEMA_VERSION = 1
VERDICT_FILENAME = "verdict.jsonl"
MANIFEST_FILENAME = "manifest.json"

# 判据代号（机器可读，UI/统计按此分组）
RULE_SCORE_BELOW_MIN = "score_below_min"
RULE_SCORE_ABOVE_MAX = "score_above_max"
RULE_WITHIN_THRESHOLD = "within_threshold"
RULE_NO_SCORE_KEPT = "no_score_kept"
RULE_BATCH_DROP_NO_SCORE = "batch_drop_no_score"
RULE_BATCH_DROP_SCORE_IN_RANGE = "batch_drop_score_in_range"

_THRESHOLD_KEYS = ("min", "max")


def fingerprint_sample(sample) -> str:
    """内容指纹（不含 id）：同一份内容在不同运行中指纹相同，可跨运行追踪。

    id 刻意不入指纹——换 id 不改变「这条内容被谁删了」的事实。
    """
    payload = "\x00".join(
        [
            str(getattr(sample, "modality", "")),
            str(getattr(sample, "text", "")),
            str(getattr(sample, "image_path", "") or ""),
        ]
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def threshold_of(params: dict[str, Any]) -> dict[str, float]:
    """从算子 params 提取阈值（只取 min/max；其余参数是算子内部调参，不是判决门限）。"""
    return {k: params[k] for k in _THRESHOLD_KEYS if k in params}


def derive_rule(score: float | None, threshold: dict[str, float], dropped: bool) -> str:
    """判据代号推导（机械版：阈值比较）。

    算子可用可选的 `explain()` 钩子在 `evidence` 里补充领域判据；
    但 `rule` 的分类保持机械可聚合——领域细节进 evidence 不进 rule。
    """
    if score is None:
        return RULE_BATCH_DROP_NO_SCORE if dropped else RULE_NO_SCORE_KEPT
    lo, hi = threshold.get("min"), threshold.get("max")
    if lo is not None and score < lo:
        return RULE_SCORE_BELOW_MIN
    if hi is not None and score > hi:
        return RULE_SCORE_ABOVE_MAX
    return RULE_BATCH_DROP_SCORE_IN_RANGE if dropped else RULE_WITHIN_THRESHOLD


@dataclass
class Verdict:
    """一条裁决记录（schema v1）。

    `created_at` 刻意不入行——运行时间会破坏逐字节可复现性；时间戳进 manifest。
    """

    v: int
    run_id: str
    seq: int
    op: str
    decision: str  # drop | keep
    score: float | None
    threshold: dict[str, float]
    rule: str
    evidence: dict[str, Any]
    input_fingerprint: str
    transform: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    prov: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Verdict":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def build_verdict(
    *,
    run_id: str,
    seq: int,
    op: str,
    sample,
    dropped: bool,
    params: dict[str, Any] | None = None,
    extra_evidence: dict[str, Any] | None = None,
) -> Verdict:
    """由「算子判决结果」构造裁决记录（漏斗集成与测试共用的单一构造路径）。"""
    params = params or {}
    score = sample.meta.get(f"score:{op}")
    threshold = threshold_of(params)
    rule = derive_rule(score, threshold, dropped)
    fp = fingerprint_sample(sample)

    evidence: dict[str, Any] = {"score": score, **threshold}
    if extra_evidence:
        evidence.update(extra_evidence)

    transform = {k: v for k, v in sample.meta.items() if k.startswith("transform:")} or None

    return Verdict(
        v=VERDICT_SCHEMA_VERSION,
        run_id=run_id,
        seq=seq,
        op=op,
        decision="drop" if dropped else "keep",
        score=score,
        threshold=threshold,
        rule=rule,
        evidence=evidence,
        input_fingerprint=fp,
        transform=transform,
        review=None,
        # PROV-O 映射：activity=漏斗运行；used=输入实体；entity=本裁决
        prov={
            "activity": "curation_funnel",
            "used": fp,
            "wasGeneratedBy": f"op:{op}",
            "seq": seq,
        },
    )


class VerdictLedger:
    """判决书落盘器（追加式 JSONL + 一次性 manifest）。

    用法：
        ledger = VerdictLedger("data/verdicts", run_id="zh_wiki_20260920")
        ledger.record(verdict)      # 逐条追加
        ledger.close()              # 写 manifest（含计数与 config 指纹）
    """

    def __init__(self, out_dir: str | Path, *, run_id: str, config_name: str = ""):
        if not run_id:
            raise ValueError("run_id 不得为空（判决书必须可归属到一次运行）")
        self.out_dir = Path(out_dir)
        self.run_id = run_id
        self.config_name = config_name
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / VERDICT_FILENAME
        self.manifest_path = self.out_dir / MANIFEST_FILENAME
        self._n_written = 0
        self._closed = False

    @property
    def n_written(self) -> int:
        return self._n_written

    def record(self, verdict: Verdict) -> None:
        if self._closed:
            raise RuntimeError("判决书已 close，不能继续写入")
        if verdict.run_id != self.run_id:
            raise ValueError(
                f"verdict.run_id={verdict.run_id!r} 与 ledger.run_id={self.run_id!r} 不一致"
            )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(verdict.to_dict(), ensure_ascii=False) + "\n")
        self._n_written += 1

    def close(self, *, extra: dict[str, Any] | None = None) -> None:
        from datetime import datetime

        if self._closed:
            return
        manifest = {
            "v": VERDICT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "config_name": self.config_name,
            "n_verdicts": self._n_written,
            "verdict_file": VERDICT_FILENAME,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if extra:
            manifest.update(extra)
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._closed = True


def iter_verdicts(path: str | Path) -> Iterator[dict[str, Any]]:
    """读判决书（禁用 splitlines——U+2028 陷阱，笔记 #44）。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            yield json.loads(line)


def read_verdicts(path: str | Path) -> list[dict[str, Any]]:
    return list(iter_verdicts(path))


def verdict_stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """按算子 / 判据代号聚合（UI 与报告共用的纯函数）。"""
    rows = list(rows)
    by_op: dict[str, dict[str, int]] = {}
    by_rule: dict[str, int] = {}
    for r in rows:
        op = r.get("op", "?")
        bucket = by_op.setdefault(op, {"n": 0, "drop": 0, "keep": 0, "seq": r.get("seq", 0)})
        bucket["n"] += 1
        decision = r.get("decision", "?")
        bucket[decision] = bucket.get(decision, 0) + 1
        by_rule[r.get("rule", "?")] = by_rule.get(r.get("rule", "?"), 0) + 1
    return {
        "n_total": len(rows),
        "n_drop": sum(1 for r in rows if r.get("decision") == "drop"),
        "n_keep": sum(1 for r in rows if r.get("decision") == "keep"),
        "by_op": {k: v for k, v in sorted(by_op.items(), key=lambda kv: kv[1]["seq"])},
        "by_rule": dict(sorted(by_rule.items())),
    }
