"""算子分数分布漂移监控（PSI）：阈值腐烂的告警器。

生产背景：清洗阈值是在某一批数据的分数分布上校准的；上游换源/改版后
分布静默漂移，旧阈值会突然大面积误杀或漏放（本项目实测过 softmax 阈值
陷阱，ENGINEERING_NOTES #36）。本模块用业界通行的 PSI
（Population Stability Index）对每个算子的分数分布做参考/当前对比：

- PSI < 0.10  稳定
- 0.10 ~ 0.25 轻微漂移（关注）
- PSI > 0.25  显著漂移（告警：阈值需重校准）

分位数分箱在参考分布上取（业界惯例），避免空箱。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

EPS = 1e-6  # 避免零箱导致 ln(0)/除零


@dataclass
class DriftCheck:
    op: str
    psi: float
    level: str  # stable / moderate / significant

    def to_dict(self) -> dict:
        return {"op": self.op, "psi": round(self.psi, 4), "level": self.level}


def psi(expected: list[float], actual: list[float], bins: int = 10) -> float:
    """参考分布 vs 当前分布的 PSI。分箱边界取自参考分布的分位数。"""
    if not expected or not actual:
        return 0.0
    srt = sorted(expected)
    qs = [srt[min(int(len(srt) * i / bins), len(srt) - 1)] for i in range(bins + 1)]
    qs[0], qs[-1] = -math.inf, math.inf
    boundaries = sorted(set(qs))  # 参考分布长尾压扁时箱数会少于 bins
    n_e = _hist(expected, boundaries)
    n_a = _hist(actual, boundaries)
    total = 0.0
    for e, a in zip(n_e, n_a):
        pe, pa = e / len(expected), a / len(actual)
        pe, pa = max(pe, EPS), max(pa, EPS)
        total += (pa - pe) * math.log(pa / pe)
    return total


def _hist(values: list[float], boundaries: list[float]) -> list[int]:
    out = [0] * (len(boundaries) - 1)
    for v in values:
        for i in range(len(out)):
            if boundaries[i] <= v < boundaries[i + 1]:
                out[i] += 1
                break
    return out


def scores_of(samples: list[dict], op: str) -> list[float]:
    """从样本 meta 提取某算子的分数（漏斗已写入；None 跳过）。"""
    return [
        float(s["meta"][f"score:{op}"])
        for s in samples
        if s.get("meta", {}).get(f"score:{op}") is not None
    ]


def drift_report(
    reference: list[dict], current: list[dict], ops: Optional[list[str]] = None
) -> dict:
    """对一批算子分数做 PSI 对比。ops 缺省时取两批样本共有的 score 键。"""
    if ops is None:
        ref_ops = {k[6:] for s in reference for k in s.get("meta", {}) if k.startswith("score:")}
        cur_ops = {k[6:] for s in current for k in s.get("meta", {}) if k.startswith("score:")}
        ops = sorted(ref_ops & cur_ops)
    checks = []
    for op in ops:
        value = psi(scores_of(reference, op), scores_of(current, op))
        level = "significant" if value > 0.25 else "moderate" if value > 0.10 else "stable"
        checks.append(DriftCheck(op, value, level))
    return {
        "n_ref": len(reference),
        "n_cur": len(current),
        "checks": [c.to_dict() for c in checks],
        "alert": any(c.level == "significant" for c in checks),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# 分数分布漂移报告（PSI）",
        "",
        f"- 参考 {report['n_ref']} 条 / 当前 {report['n_cur']} 条"
        f" | **{'⚠ 显著漂移，阈值需重校准' if report['alert'] else '✓ 分布稳定'}**",
        "",
        "| 算子 | PSI | 级别 |",
        "|---|---|---|",
    ]
    for c in report["checks"]:
        lines.append(f"| {c['op']} | {c['psi']:.3f} | {c['level']} |")
    lines += ["", "级别口径：PSI<0.10 稳定；0.10~0.25 轻微；>0.25 显著（业界通行）。"]
    return "\n".join(lines)
