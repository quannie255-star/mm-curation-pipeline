"""工业传感器模态质量算子（V5 α：sensor_stuck / sensor_range / sensor_drift /
unit_consistency / fault_vs_maintenance）。

输入约定：样本全部经 SensorSample 适配器展平（text = 窗口 payload canonical
JSON），解析失败计 None——无法计分保留并记录，与协议语义一致。批量算子假设
输入已按模态过滤（漏斗经 run_batch_mixed_modality 预过滤），与 text_minhash /
fhir_quality 同约定。

时序确定性约定：批量算子内部按 (device_id, channel, window_start) 规范化排序
（id 排序约定的领域版）——窗口顺序只依赖 payload 字段、不依赖输入序/分块序。

核心命题（与金融「停牌 vs 采集失败」同构）：形态相同、结论相反的事实区分——
idle 工况的平坦窗（合法）vs 卡死平坦窗（故障）；changeover 工况的均值偏移
（合法）vs 校准漂移（故障）；计划检修窗缺席/哨兵（合法）vs 计划外静默（故障）。
工况标签与检修计划事件由语料/适配层提供，算子不猜测业务。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from curation_eval import CostClass, SensorSample, register_operator

from ..data.sensor_synth import RANGE_TABLE, SENTINEL
from .base import BatchOperator, Operator, Sample

_BASELINE = 5  # drift 基线窗数（每组最早 N 个同工况窗）
_DRIFT_Z = 4.0  # 均值偏移判定阈（以窗均值的有效 σ 为单位；4σ 下平稳语料误杀≈0）


def _lag1_autocorr(values: list[float]) -> float:
    mean = sum(values) / len(values)
    num = sum((values[i] - mean) * (values[i + 1] - mean) for i in range(len(values) - 1))
    den = sum((v - mean) ** 2 for v in values)
    if den <= 0:
        return 0.0
    return max(0.0, min(0.99, num / den))


def _parse(sample: Sample) -> dict | None:
    try:
        return SensorSample.parse(sample)
    except ValueError:
        return None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float], mean: float) -> float:
    return max(0.0, sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


@register_operator(
    name="sensor_stuck",
    modalities=frozenset({"industrial_sensor"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class SensorStuckOp(Operator):
    """传感器卡死：窗口极差≈0 且工况非 idle → 卡死；idle 平坦窗是合法停机。
    score 二值（1.0 / 0.0）；非读数窗（事件样本）放行。"""

    def score(self, sample: Sample) -> float | None:
        payload = _parse(sample)
        if payload is None:
            return None
        if payload.get("record_type") != "reading_window":
            return 1.0
        readings = payload["readings"]
        flat = max(readings) - min(readings) <= 1e-9
        if flat and payload.get("operating_mode") != "idle":
            return 0.0
        return 1.0


@register_operator(
    name="sensor_range",
    modalities=frozenset({"industrial_sensor"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class SensorRangeOp(Operator):
    """量程检查：超量程/物理不可能值占比（按设备类型×通道查内嵌量程表）。
    表外通道不越权评判（=1.0）。score = 1 - 越界读数占比。"""

    def score(self, sample: Sample) -> float | None:
        payload = _parse(sample)
        if payload is None:
            return None
        if payload.get("record_type") != "reading_window":
            return 1.0
        bounds = RANGE_TABLE.get((payload.get("device_type"), payload.get("channel")))
        if bounds is None:
            return 1.0
        lo, hi = bounds
        readings = payload["readings"]
        violations = sum(1 for v in readings if v < lo or v > hi)
        if violations == 0:
            return 1.0
        return 1.0 - violations / len(readings)


@register_operator(
    name="sensor_drift",
    modalities=frozenset({"industrial_sensor"}),
    required_fields={"text"},
    cost_class=CostClass.RULE,
    shardable=False,  # 需同组窗口全量视角（基线统计）
)
class SensorDriftOp(BatchOperator):
    """漂移检测：同设备×同通道×**同工况**分组内，基线窗（最早 N=5）之后
    的窗口均值偏移超基线 σ 的 3 倍判漂移。跨工况不比较——changeover 的均值
    偏移是合法业务差异（工况标签防误杀的关键）。校准漂移是慢变量，单窗内
    检不出，必须批量跨窗看。"""

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("sensor_drift 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        groups: dict[tuple, list[dict]] = {}
        parsed: list[tuple[Sample, dict | None]] = []
        for s in samples:
            p = _parse(s)
            parsed.append((s, p))
            if p is None or p.get("record_type") != "reading_window":
                continue
            key = (p["device_id"], p["channel"], p["operating_mode"])
            groups.setdefault(key, []).append(p)
        baselines: dict[tuple, tuple[float, float]] = {}
        for key, windows in groups.items():
            windows.sort(key=lambda p: datetime.fromisoformat(p["window_start"]))
            base = windows[:_BASELINE]
            if len(windows) <= _BASELINE:
                continue
            # 窗均值的有效 σ：窗内 pooled σ × AR(1) 有限样本因子（由 lag-1
            # 自相关自适应估计，不硬编码过程参数）。基线中心与尺度都用中位数：
            # 独立评测时全集可能含其他类型的灾难注入（如 -999 静默窗），均值/μ
            # 型统计会被拖垮，中位数对 ≤2/5 基线被污染仍稳健。
            base_stds = sorted(_std(w["readings"], _mean(w["readings"])) for w in base)
            s_w = base_stds[len(base_stds) // 2]
            rho = _mean([_lag1_autocorr(w["readings"]) for w in base])
            n = len(base[0]["readings"])
            sigma_mean = s_w * ((1 + rho) / (n * (1 - rho))) ** 0.5
            base_means = sorted(_mean(w["readings"]) for w in base)
            baselines[key] = (base_means[len(base_means) // 2], sigma_mean)

        survivors = []
        for s, p in parsed:
            if p is None:
                s.meta[f"score:{self.name}"] = None
                survivors.append(s)
                continue
            if p.get("record_type") != "reading_window":
                s.meta[f"score:{self.name}"] = 1.0
                survivors.append(s)
                continue
            key = (p["device_id"], p["channel"], p["operating_mode"])
            base = baselines.get(key)
            if base is None:
                score = 1.0  # 基线不足（小规模语料），无从判漂移不误杀
            else:
                bm, sigma_mean = base
                z = (_mean(p["readings"]) - bm) / max(sigma_mean, 1e-12)
                score = 0.0 if abs(z) > _DRIFT_Z else 1.0
            s.meta[f"score:{self.name}"] = score
            if self.keep(score):
                survivors.append(s)
        return survivors


@register_operator(
    name="unit_consistency",
    modalities=frozenset({"industrial_sensor"}),
    required_fields={"text"},
    cost_class=CostClass.RULE,
    shardable=False,  # 需同测点全量视角（单位众数）
)
class UnitConsistencyOp(BatchOperator):
    """单位一致性：同设备类型×同通道的窗口单位应一致（MPa vs bar 混源是
    典型集成事故）。组内单位众数为准（并列取字典序，确定性），偏离者判违规。
    只看标签不改写数值——换算执行属预处理阶段，不进漏斗。"""

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("unit_consistency 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        groups: dict[tuple, Counter] = {}
        parsed: list[tuple[Sample, dict | None]] = []
        for s in samples:
            p = _parse(s)
            parsed.append((s, p))
            if p is None or p.get("record_type") != "reading_window":
                continue
            key = (p["device_type"], p["channel"])
            groups.setdefault(key, Counter())[p["unit"]] += 1
        dominant: dict[tuple, str] = {}
        for key, counts in groups.items():
            dominant[key] = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        survivors = []
        for s, p in parsed:
            if p is None:
                s.meta[f"score:{self.name}"] = None
                survivors.append(s)
                continue
            if p.get("record_type") != "reading_window":
                s.meta[f"score:{self.name}"] = 1.0
                survivors.append(s)
                continue
            key = (p["device_type"], p["channel"])
            score = 1.0 if p["unit"] == dominant[key] else 0.0
            s.meta[f"score:{self.name}"] = score
            if self.keep(score):
                survivors.append(s)
        return survivors


@register_operator(
    name="fault_vs_maintenance",
    modalities=frozenset({"industrial_sensor"}),
    required_fields={"text"},
    cost_class=CostClass.RULE,
    shardable=False,  # 需检修计划事件全量视角
)
class FaultVsMaintenanceOp(BatchOperator):
    """故障 vs 计划维护判别（本包的归因架构落点）：静默形态窗（全哨兵值
    -999）若落在该设备的检修计划窗内 → 合法放行；计划外静默 → 真实数据
    链路故障。计划索引由 maintenance_event 样本在同模态样本流内构建——
    业务事件源是数据的一部分，不算子不猜测。"""

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("fault_vs_maintenance 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        plan: list[tuple[str, datetime, datetime]] = []
        parsed: list[tuple[Sample, dict | None]] = []
        for s in samples:
            p = _parse(s)
            parsed.append((s, p))
            if p is not None and p.get("record_type") == "maintenance_event":
                plan.append(
                    (
                        p["device_id"],
                        datetime.fromisoformat(p["window_start"]),
                        datetime.fromisoformat(p["window_end"]),
                    )
                )

        survivors = []
        for s, p in parsed:
            if p is None:
                s.meta[f"score:{self.name}"] = None
                survivors.append(s)
                continue
            if p.get("record_type") != "reading_window":
                s.meta[f"score:{self.name}"] = 1.0
                survivors.append(s)
                continue
            readings = p["readings"]
            silent = all(v == SENTINEL for v in readings)
            if not silent:
                score = 1.0
            else:
                start = datetime.fromisoformat(p["window_start"])
                in_plan = any(
                    device == p["device_id"] and win_start <= start <= win_end
                    for device, win_start, win_end in plan
                )
                score = 1.0 if in_plan else 0.0
            s.meta[f"score:{self.name}"] = score
            if self.keep(score):
                survivors.append(s)
        return survivors
