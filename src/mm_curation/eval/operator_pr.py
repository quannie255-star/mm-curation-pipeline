"""算子级 P/R 独立评测（Week3 D4，design 2.3）。

与漏斗评测的关键区别：漏斗里算子串联，上游扔掉的样本不会进入下游，
导致「只改 caption 不动图」的注入（near_duplicate_text）会被先到的
md5 阶段以「同图」误判保留、却永远进不到 minhash 阶段——漏斗口径下
minhash 的 recall 被系统性低估。本模块对每个算子**独立**在全量脏集上
跑一次，丢弃集合互不影响，得到算子真实的能力边界。

ground truth 约定：sample.labels["dirty"] 为污染类型（干净样本为空 dict）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ..operators.base import Operator, Sample
from ..operators.registry import is_batch
from ..pipeline.config import OperatorSpec

# 算子 -> 设计上的主靶脏数据类型（污染器 impl.py 文档约定）。
# recall 对全部脏类型都会算，这里只用于高亮「该算子本来就该抓的那类」。
OPERATOR_TARGETS: dict[str, list[str]] = {
    "md5_exact": ["exact_duplicate"],
    "phash_near": ["near_duplicate_image"],
    "minhash_lsh": ["near_duplicate_text"],
    "semantic_dedup": ["semantic_duplicate"],
    "resolution": ["low_resolution"],
    "blur": ["blur"],
    # clip_alignment 的主靶是图文错配；校准中发现它顺带压住 nsfw 占位与
    # 部分 low_quality_text（刷字/截断文本与图天然不对齐）——这正是
    # 「算子级 P/R」比漏斗串联更该独立看的原因。
    "clip_alignment": ["mismatched_pair"],
    "text_length": ["low_quality_text"],
    "chinese_ratio": ["low_quality_text"],
    "char_repetition": ["low_quality_text"],
    # aspect_ratio 无直接主靶（抓极端横幅/拼接图），留空表示「无主靶」。
    "aspect_ratio": [],
}


@dataclass
class OperatorPR:
    """单个算子在全量脏集上独立评测的结果。"""

    op: str
    n_in: int
    n_dropped: int
    clean_killed: int  # 被扔的干净样本数（误杀）
    dirty_caught: Counter = field(default_factory=Counter)  # 被扔样本的脏类型构成
    primary_target: list[str] = field(default_factory=list)

    @property
    def precision(self) -> Optional[float]:
        """被扔样本中确实是脏数据的占比。None = 该算子没扔任何样本。"""
        if self.n_dropped == 0:
            return None
        return (self.n_dropped - self.clean_killed) / self.n_dropped

    @property
    def clean_kill_rate(self) -> Optional[float]:
        if self.n_in == 0:
            return None
        return self.clean_killed / self.n_in

    def recall_of(self, dirty_type: str, total: int) -> Optional[float]:
        """对某脏类型的召回率。None = 该类型在全集中不存在（分母 0）。"""
        if total == 0:
            return None
        return self.dirty_caught.get(dirty_type, 0) / total

    def to_dict(self, dirty_totals: dict[str, int], n_clean: int) -> dict:
        recall_by_type = {
            k: round(v, 4) if v is not None else None
            for k, v in ((t, self.recall_of(t, dirty_totals.get(t, 0))) for t in dirty_totals)
        }
        return {
            "op": self.op,
            "primary_target": self.primary_target,
            "n_in": self.n_in,
            "n_dropped": self.n_dropped,
            "clean_killed": self.clean_killed,
            "dirty_caught": dict(self.dirty_caught),
            "precision": round(self.precision, 4) if self.precision is not None else None,
            "clean_kill_rate": round(self.clean_kill_rate, 4)
            if self.clean_kill_rate is not None
            else None,
            "recall_by_type": recall_by_type,
            "primary_recall": {t: recall_by_type.get(t) for t in self.primary_target},
        }


def run_operator(op: Operator, samples: list[Sample]) -> tuple[list[Sample], list[Sample]]:
    """单个算子独立跑一次全量，返回 (存活, 丢弃)。

    单样本算子：逐个 __call__，返回 None 即丢弃；
    批量算子：run_batch 后按 id 差集找出被扔样本（与漏斗执行器同口径）。
    """
    if is_batch(op):
        survivors = op.run_batch(list(samples))
        kept_ids = {s.id for s in survivors}
        dropped = [s for s in samples if s.id not in kept_ids]
        return survivors, dropped
    kept, dropped = [], []
    for s in samples:
        (kept if op(s) is not None else dropped).append(s)
    return kept, dropped


def evaluate_operator(spec: OperatorSpec, samples: list[Sample]) -> OperatorPR:
    """对单个算子在全量脏集上独立评测。"""
    op = spec.build()
    _kept, dropped = run_operator(op, list(samples))
    clean_killed = sum(1 for s in dropped if not s.labels)
    dirty_caught: Counter = Counter(
        s.labels.get("dirty") or "clean/未标注" for s in dropped if s.labels
    )
    return OperatorPR(
        op=spec.op,
        n_in=len(samples),
        n_dropped=len(dropped),
        clean_killed=clean_killed,
        dirty_caught=dirty_caught,
        primary_target=OPERATOR_TARGETS.get(spec.op, []),
    )


def evaluate_all(
    specs: list[OperatorSpec], samples: list[Sample]
) -> tuple[list[OperatorPR], dict[str, int], int]:
    """对一组算子逐个独立评测。

    返回 (结果列表, 各脏类型在全集中的总数, 干净样本总数)——
    分母由调用方算好传入，避免每个算子重复统计。
    """
    dirty_totals: Counter = Counter(s.labels["dirty"] for s in samples if s.labels)
    n_clean = sum(1 for s in samples if not s.labels)
    results = [evaluate_operator(spec, samples) for spec in specs]
    return results, dict(dirty_totals), n_clean


def render_pr_markdown(
    results: list[OperatorPR],
    dirty_totals: dict[str, int],
    n_clean: int,
    pipeline_name: str = "",
) -> str:
    """算子级 P/R 报告（人读 Markdown）。"""
    title = f"算子级 P/R 报告: {pipeline_name}" if pipeline_name else "算子级 P/R 报告"
    lines = [
        f"# {title}",
        "",
        "- 评测口径：每个算子**独立**在全量脏集上跑一次，丢弃集合互不影响",
        "  （与漏斗串联不同——串联会因上游拦截而低估下游算子的真实召回）。",
        f"- 全集 {sum(dirty_totals.values()) + n_clean} 条"
        f"（干净 {n_clean} / 脏 {sum(dirty_totals.values())}）。",
        "",
        "| 算子 | 主靶 | 扔 | 误杀 | precision | 主靶 recall | 干净误杀率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        prec = "—" if r.precision is None else f"{r.precision:.1%}"
        kill = "—" if r.clean_kill_rate is None else f"{r.clean_kill_rate:.2%}"
        if r.primary_target:
            recalls = [
                f"{t} {r.recall_of(t, dirty_totals.get(t, 0)) or 0:.0%}" for t in r.primary_target
            ]
            prim = "、".join(recalls)
        else:
            prim = "—"
        lines.append(
            f"| {r.op} | {', '.join(r.primary_target) or '—'} | {r.n_dropped} "
            f"| {r.clean_killed} | {prec} | {prim} | {kill} |"
        )

    # 完整 recall 矩阵：哪个算子顺带抓了别的类型的脏数据
    lines += ["", "## 完整召回矩阵（行=算子，列=脏类型）", ""]
    types = sorted(dirty_totals)
    header = "| 算子 | " + " | ".join(types) + " |"
    sep = "|---|" + "|".join("---" for _ in types) + "|"
    lines += [header, sep]
    for r in results:
        cells = [f"{r.recall_of(t, dirty_totals.get(t, 0)) or 0:.0%}" for t in types]
        lines.append(f"| {r.op} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 各脏类型全集数量",
        "",
        "| 脏类型 | 数量 |",
        "|---|---|",
    ]
    for t, n in sorted(dirty_totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {t} | {n} |")
    lines.append("")
    return "\n".join(lines)
