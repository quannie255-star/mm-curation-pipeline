"""判决书（V6 决策点 5）：每条清洗决策一条机器可读的裁决记录。

- `ledger`：`Verdict` schema v1 + 落盘器 + 读/聚合纯函数
- `recording`：算子包装器（不改算子/执行器/协议 地把判决落账）

默认 opt-in（旁路产物）——不传 `verdict_ledger` 时漏斗行为逐位不变。
"""

from .ledger import (
    MANIFEST_FILENAME,
    RULE_BATCH_DROP_NO_SCORE,
    RULE_BATCH_DROP_SCORE_IN_RANGE,
    RULE_NO_SCORE_KEPT,
    RULE_SCORE_ABOVE_MAX,
    RULE_SCORE_BELOW_MIN,
    RULE_WITHIN_THRESHOLD,
    VERDICT_FILENAME,
    VERDICT_SCHEMA_VERSION,
    Verdict,
    VerdictLedger,
    build_verdict,
    derive_rule,
    fingerprint_sample,
    iter_verdicts,
    read_verdicts,
    threshold_of,
    verdict_stats,
)
from .recording import RecordingBatchOperator, RecordingOperator, wrap_for_verdict

__all__ = [
    "VERDICT_SCHEMA_VERSION",
    "VERDICT_FILENAME",
    "MANIFEST_FILENAME",
    "RULE_SCORE_BELOW_MIN",
    "RULE_SCORE_ABOVE_MAX",
    "RULE_WITHIN_THRESHOLD",
    "RULE_NO_SCORE_KEPT",
    "RULE_BATCH_DROP_NO_SCORE",
    "RULE_BATCH_DROP_SCORE_IN_RANGE",
    "Verdict",
    "VerdictLedger",
    "build_verdict",
    "derive_rule",
    "fingerprint_sample",
    "threshold_of",
    "iter_verdicts",
    "read_verdicts",
    "verdict_stats",
    "RecordingOperator",
    "RecordingBatchOperator",
    "wrap_for_verdict",
]
