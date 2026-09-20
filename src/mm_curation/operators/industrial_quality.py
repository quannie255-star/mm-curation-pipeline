"""工业传感器模态质量算子（V5 α：sensor_stuck / sensor_range / sensor_drift /
unit_consistency / fault_vs_maintenance）。

输入约定：样本全部经 SensorSample 适配器展平（text = 窗口 payload canonical
JSON），解析失败计 None——无法计分保留并记录，与协议语义一致。批量算子假设
输入已按模态过滤（漏斗经 run_batch_mixed_modality 预过滤），与 text_minhash /
fhir_quality 同约定。

时序确定性约定：批量算子内部按 `(window_start, id)` 规范化排序（id 排序约定的
领域版）——窗口顺序只依赖 payload 字段与样本 id、不依赖输入序/分块序。判决则
一律按**样本 id** 记账，不用 payload 派生键（同一时间戳可能有多条样本）。

核心命题（与金融「停牌 vs 采集失败」同构）：形态相同、结论相反的事实区分——
idle 工况的平坦窗（合法）vs 卡死平坦窗（故障）；changeover 工况的均值偏移
（合法）vs 校准漂移（故障）；计划检修窗缺席/哨兵（合法）vs 计划外静默（故障）。
工况标签与检修计划事件由语料/适配层提供，算子不猜测业务。

P2 真实分布适用性修正（2026-09-20，详见 docs/design_tables.md「P2 补表」）：
判据原先照着**合成注入形态**写（完美平坦 / 哨兵 / 表内量程），迁到真实数据后
出现三类失效——空转、同形态多含义、把「没评」伪装成「通过」。本模块的修法：
- `sensor_stuck`：单样本 → 批量，判据加**通道尺度参考**与**无信息通道豁免**；
  R3 再补**设备级停机豁免**（多通道同步冻结 ≠ 单通道卡死）
- `sensor_range`：表外通道记 `None`（无法计分），并提供外接量程表
- `fault_vs_maintenance`：R3 认**三种静默形态**（全哨兵 / 设备停机 / 跨度缺口），
  用并集而非替换 —— 真实数据里一个哨兵窗都没有，而合成的 `unplanned_silence`
  必须保持 100% 召回
三条不变式（改判据时不可破坏）：①「没评」与「评过且通过」必须可区分
（`None` vs `1.0`）；②合法形态（idle 平坦 / changeover 偏移 / 计划检修静默 /
设备停机 / 跨时段窗）不得因判据变敏感而变成误杀；③合成轨门禁数字不得回退。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml
from curation_eval import CostClass, SensorSample, register_operator

from ..data.sensor_synth import RANGE_TABLE, SENTINEL
from .base import BatchOperator, Operator, Sample

_BASELINE = 5  # drift 基线窗数（每组最早 N 个同工况窗）
_DRIFT_Z = 4.0  # 均值偏移判定阈（以窗均值的有效 σ 为单位；4σ 下平稳语料误杀≈0）
_STUCK_ALPHA = 0.2  # 尺度塌陷比：σ_win < α × 通道尺度参考（健康窗内 σ 的 p75）
_STUCK_RUN = 10  # 疑似塌陷需连续 N 窗才判卡死（单窗低方差是噪声，冻结是持续现象）
_GAP_RATIO = 5.0  # 窗内「实际时间跨度 / 应有跨度」判阈，超过即认定窗跨接了两个运行时段
_DEVICE_MIN_CHANNELS = 2  # 设备级停机的前提：至少 2 个通道同时恒定才算「设备没在跑」


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


def _span_ratio(payload: dict) -> float | None:
    """窗内**实际时间跨度 / 应有跨度**（`(n-1)/sampling_hz`）。

    `> 1` = 记录仪在窗内停摆过（有槽位缺席，读数与时间戳不再一一对应）；
    `≈ 1` = 等间隔连续采样。返回 `None` 表示字段缺失/不可算 —— **不是 0**，
    「算不出」与「正常」必须可区分（同 `None` vs `1.0` 的约定）。
    """
    hz = payload.get("sampling_hz") or 0.0
    readings = payload.get("readings") or []
    if hz <= 0 or len(readings) < 2:
        return None
    try:
        start = datetime.fromisoformat(payload["window_start"])
        end = datetime.fromisoformat(payload["window_end"])
    except (KeyError, ValueError):
        return None
    expected = (len(readings) - 1) / hz
    if expected <= 0:
        return None
    return (end - start).total_seconds() / expected


def _device_stop_keys(parsed: list[tuple[Sample, dict | None]]) -> dict[tuple[str, str], int]:
    """设备级停机窗 → `{(device_id, window_start): 同步恒定的通道数}`。

    判据只有一条：同一 `(device_id, window_start)` 下**全部通道的读数都恒定**，
    且至少 `_DEVICE_MIN_CHANNELS` 个通道（单通道恒定是传感器卡死的形态，不算）。

    为什么要这一档（真实数据逼出来的）：
    - `operating_mode == "idle"` 是**标签**，真实数据集基本不给（MetroPT-3 就没有）；
      但设备停机在数据里留下的印记是「同一时刻所有通道一起冻住」——**跨通道同步性**
      就是它与单通道卡死的唯一区别，也是本判据的全部依据。
    - 实测 MetroPT-3：191 个窗 7 通道同时恒定，聚成 7 段连续块（5.1h ~ 61.4h），
      带故障标签 **0/191**（基准故障率 2.0%），落在检修计划内 **0/191**。
      即：它既不是故障、也不在计划里 —— 是**第三态「设备没在跑」**。
      把它判成 `sensor_stuck`（传感器故障）是误诊，判成 `fault_vs_maintenance`
      的计划外静默是误杀（1337 条记录，0 命中）。

    **全哨兵形态不在此列**：合成的 `sensor_unplanned_silence` 把单通道读数全填成
    -999，也是「恒定」，但那是链路静默的靶子，必须留给 `fault_vs_maintenance` 判，
    不能被这里豁免掉（否则合成门禁召回直接掉）。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for _s, p in parsed:
        if p is None or p.get("record_type") != "reading_window":
            continue
        groups.setdefault((p["device_id"], p["window_start"]), []).append(p)
    stops: dict[tuple[str, str], int] = {}
    for key, members in groups.items():
        if len(members) < _DEVICE_MIN_CHANNELS:
            continue
        if any(all(v == SENTINEL for v in m["readings"]) for m in members):
            continue  # 链路静默（哨兵）形态：不在停机档处理
        if all(max(m["readings"]) - min(m["readings"]) == 0.0 for m in members):
            stops[key] = len(members)
    return stops


@register_operator(
    name="sensor_stuck",
    modalities=frozenset({"industrial_sensor"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
    shardable=False,  # 需同通道全量视角（尺度参考 + 无信息通道豁免）
)
class SensorStuckOp(BatchOperator):
    """传感器卡死：**通道内持续尺度塌陷**，且该通道不是「无信息通道」。

    四级判据（由真实数据逼出来的，见 docs/REAL_DATA_REPORT.md §三）：

    0. **设备级停机豁免**：同一 `(device_id, window_start)` 下**全部通道同时恒定**
       （≥2 通道）→ 该窗记 `None`（本判据不适用）。这是 R3 补的一档，也是**误杀
       降幅最大的一档**：真实设备停机留下的是「多通道同步冻结」的印记，与
       「某个传感器卡死」形态相同、含义相反（本模块的核心命题）。MetroPT-3 实测
       191 窗 × 7 通道 = 1337 条记录，故障标签 0/191、计划内 0/191，聚成 7 段连续块
       （5.1h~61.4h）——是「设备没在跑」，不是「传感器坏了」。
    1. **无信息通道豁免**：该通道**一个「健康窗」都没有**（健康窗 = 窗内极差
       不为 0），即通道全程恒定 —— 「平坦」是它的常态、不含任何信息 → 该通道
       所有窗记 `None`（无法计分）。这一条直接消掉 C-MAPSS 92.4% 的 stuck 误杀
       （7817 次误杀里 7220 次落在 s1/s5/s10/s16/s18/s19 这 6 个全程恒定通道）。
    2. **严格平坦**：窗内 `极差 == 0`（严格相等）且非 idle 工况 → 立即判卡死。
       真实冻结就是严格平坦（MetroPT-3 实测 7 通道同步恒定约 163 小时），
       *不是*「接近平坦」——所以这一档不需要连续性。
       注意判据必须写「极差 == 0」而不是「σ == 0」：`[1.2]*256` 的 σ 不是 0，
       而是 `1.2 != sum/len` 带来的 ~1e-15 浮点残差。
    3. **持续塌陷**：`σ_win < α × ref_sigma` 且**连续 ≥ k 窗** → 判卡死。
       抖动的冻结传感器窗内仍有微小噪声，单窗看不出来；但冻结是**持续**现象，
       而合法低方差工况（idle / 低负荷）不会连续 k 窗都塌到 1/20。

    与旧判据（`极差 ≤1e-9` 单样本）的差别不在灵敏度，在**上下文**：
    「完美平坦」既可能是传感器卡死，也可能是该传感器本来就不测量任何东西——
    第 1 档就是为区分这两种相反含义加的（同形态多含义，P2 档核心问题）。

    参考尺度分组粒度 = `(device_id, channel)`：卡死是**单台传感器**的行为，
    按设备类型合并会被没坏的那台掩盖。

    默认参数 `alpha=0.2 / min_run=10` 的来源（不是拍脑袋，过程可复现）：
    在 MetroPT-3 上跑 4×2 参数网格（`eval_real_sensor.py --grid sensor_stuck
    --grid-axis alpha=0.01,0.05,0.2,0.5 --grid-axis min_run=3,10`），挑选准则为
    「扣除人工抽检已确认的未标注真实缺陷（1337 条全通道冻结，占干净 3.29%）后，
    **附加误杀 ≤1.1% 的档位里取最大召回**」。结果 α=0.2/k=10 → 附加误杀 1.07%、
    召回 36.6%；α=0.5/k=10 附加误杀 2.57% 超线被排除，α=0.01/k=10 虽更保守
    （附加 0.23% ）但召回只有 10.3%。同一组参数在 C-MAPSS 上丢弃数与 α=0.05/k=3
    **完全相同**（747）——即它没有在第二个数据集上引入新误杀。
    """

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("sensor_stuck 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        alpha = float(self.params.get("alpha", _STUCK_ALPHA))
        min_run = int(self.params.get("min_run", _STUCK_RUN))
        parsed = [(s, _parse(s)) for s in samples]

        # 分组：**(device_id, channel)** —— 卡死是单台传感器的行为。
        # 组内排序用 `(window_start, id)` 全序：窗口顺序只依赖 payload 字段，
        # 但同一时间戳可能有多条样本（污染器复制的供体窗与原窗同 window_start），
        # 只按时间排就不是全序，会退化成依赖输入序。
        groups: dict[tuple, list[tuple[datetime, str, dict]]] = {}
        for s, p in parsed:
            if p is None or p.get("record_type") != "reading_window":
                continue
            key = (p["device_id"], p["channel"])
            groups.setdefault(key, []).append((datetime.fromisoformat(p["window_start"]), s.id, p))

        # 通道尺度参考 = **健康窗**内 σ 的 p75，健康窗 = 窗内极差不为 0 的窗。
        # 两个必须踩过的坑：
        # ① 平坦窗的 σ 不是 0 而是 ~1e-15——`1.2 == 1.2` 但 `sum/len != 1.2` 的
        #    浮点残差。所以「平坦」只能判 `max-min == 0`（严格相等），不能判 σ。
        #    早期版本直接取 σ 中位数，参考尺度被拉到 1e-15 量级，整组判不出来。
        # ② 取 p75 而非中位数：若某通道 90% 的窗已冻结（σ 塌到 1e-3）、只有 10%
        #    健康（σ≈1），中位数会把参考尺度定在 1e-3 上，于是塌陷反而「正常」。
        #    参考尺度要回答的是「这个通道健康时多大」，所以取高分位。
        # 「健康窗一个都没有」= 该通道全程平坦 = 无信息通道 → 整通道不判（None）。
        ref: dict[tuple, float | None] = {}
        for key, wins in groups.items():
            wins.sort(key=lambda t: (t[0], t[1]))
            healthy = sorted(
                _std(p["readings"], _mean(p["readings"]))
                for _t, _i, p in wins
                if max(p["readings"]) - min(p["readings"]) != 0.0
            )
            ref[key] = healthy[int(0.75 * (len(healthy) - 1))] if healthy else None

        # 设备级停机（全部通道同时恒定）：**不是单通道卡死**，因此本判据对它
        # 不适用 → 记 None（保留、不计分）。这一步把 MetroPT-3 的 1337 次
        # 「判卡死却 0 命中」从误杀里摘出来（详见 _device_stop_keys）。
        stops = _device_stop_keys(parsed)

        # 判决键必须是**样本 id**，不能是 payload 派生的 (device, channel, 时间)：
        # 污染器注入的窗是从供体复制的、window_start 与原窗相同，payload 派生键
        # 会把「注入窗被判卡死」写到**原干净窗**头上 → 合成门禁误杀 1.23% 暴涨到
        # 11.65%（实测踩过）。样本 id 才是框架口径里的单条身份（丢弃集就是按 id 算的）。
        flags: dict[str, float | None] = {}
        evidence: dict[str, dict] = {}
        for key, wins in groups.items():
            r = ref[key]
            if r is None:
                for _t, sid, _p in wins:
                    flags[sid] = None
                continue
            run = 0
            for _t, sid, p in wins:
                readings = p["readings"]
                if p.get("operating_mode") == "idle":
                    run = 0
                    flags[sid] = 1.0
                    continue
                stop_key = (p["device_id"], p["window_start"])
                if stop_key in stops:
                    run = 0
                    flags[sid] = None
                    evidence[sid] = {
                        "rule": "machine_stop",
                        "frozen_channels": stops[stop_key],
                    }
                    continue
                if max(readings) - min(readings) == 0.0:
                    run = 0
                    flags[sid] = 0.0
                    evidence[sid] = {"rule": "exact_flat", "ref_sigma": r}
                    continue
                s_win = _std(readings, _mean(readings))
                if s_win < alpha * r:
                    run += 1
                    if run >= min_run:
                        flags[sid] = 0.0
                        evidence[sid] = {
                            "rule": "scale_collapse",
                            "sigma_win": s_win,
                            "ref_sigma": r,
                            "run_length": run,
                        }
                    else:
                        flags[sid] = 1.0
                else:
                    run = 0
                    flags[sid] = 1.0

        survivors = []
        for s, p in parsed:
            if p is None or p.get("record_type") != "reading_window":
                s.meta[f"score:{self.name}"] = None if p is None else 1.0
                survivors.append(s)
                continue
            score = flags.get(s.id, 1.0)
            s.meta[f"score:{self.name}"] = score
            if score != 1.0 and s.id in evidence:
                # 证据只挂在**非通过**的窗上（丢弃 + 无法计分）：干净语料零开销，
                # 而抽检要看的正是它们
                s.meta["evidence:sensor_stuck"] = evidence[s.id]
            if self.keep(score):
                survivors.append(s)
        return survivors


def _load_ranges(path: str | Path) -> dict[tuple[str, str], tuple[float, float]]:
    """读外接量程表：`{device_type: {channel: [lo, hi]}}`（YAML 或 JSON）。

    宽松解析、严格报错：形状不对直接抛，不做「猜一个」的兜底——量程填错会静默
    产生假的「超量程」判决，比不填更危险。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"量程表不存在: {p}")
    text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) if p.suffix.lower() in (".yaml", ".yml") else json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError(f"量程表 {p} 应为映射 {{device_type: {{channel: [lo, hi]}}}}")
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for device_type, channels in raw.items():
        if not isinstance(channels, dict):
            raise ValueError(f"量程表 {p} 的 {device_type!r} 应为 {{channel: [lo, hi]}}")
        for channel, bounds in channels.items():
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise ValueError(f"量程表 {p} 的 {device_type}/{channel} 应为 [lo, hi]")
            lo, hi = float(bounds[0]), float(bounds[1])
            if lo > hi:
                raise ValueError(f"量程表 {p} 的 {device_type}/{channel} 下界大于上界: {bounds}")
            out[(str(device_type), str(channel))] = (lo, hi)
    return out


@register_operator(
    name="sensor_range",
    modalities=frozenset({"industrial_sensor"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class SensorRangeOp(Operator):
    """量程检查：超量程/物理不可能值占比（按设备类型×通道查量程表）。
    score = 1 - 越界读数占比。

    **表外通道记 `None`（无法计分），不再记 1.0。** 旧行为的危害是「伪装成通过」：
    真实数据集的 `(device_type, channel)` 基本都落在内嵌表外，一律记 1.0 会让报告
    读起来像「量程全部通过」，实际这三个真实数据集上**一次都没有评过**
    （`docs/REAL_DATA_REPORT.md` §一 的三数据集读数可直接复核）。
    与 `sensor_stuck` 的无信息通道豁免同源：**「没评」和「评过且通过」必须可区分**。

    外接量程表：`params.ranges_path` 指向 YAML/JSON，形状
    `{device_type: {channel: [lo, hi]}}`，**覆盖**内嵌表的同名键、其余键沿用内嵌表。
    真实量程必须有可追溯来源（数据集文档 / 传感器手册）；查不到的通道**留空**
    ——留空 = None = 未评，绝不用编造的量程换来一个好看的通过率。
    """

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self._ranges = dict(RANGE_TABLE)
        extra = params.get("ranges_path")
        if extra:
            self._ranges.update(_load_ranges(extra))

    def score(self, sample: Sample) -> float | None:
        payload = _parse(sample)
        if payload is None:
            return None
        if payload.get("record_type") != "reading_window":
            return 1.0
        bounds = self._ranges.get((payload.get("device_type"), payload.get("channel")))
        if bounds is None:
            return None  # 表外通道：无权评判，且不伪装成通过
        lo, hi = bounds
        readings = payload["readings"]
        violations = sum(1 for v in readings if v < lo or v > hi)
        if violations == 0:
            return 1.0
        return 1.0 - violations / len(readings)

    def explain(self, sample: Sample, score: float | None) -> dict:
        payload = _parse(sample)
        if payload is None or payload.get("record_type") != "reading_window":
            return {"non_reading_window": True}
        bounds = self._ranges.get((payload.get("device_type"), payload.get("channel")))
        if bounds is None:
            return {
                "channel_in_range_table": False,
                "reason": "no_range_entry",
                "device_type": payload.get("device_type"),
                "channel": payload.get("channel"),
            }
        lo, hi = bounds
        readings = payload["readings"]
        return {
            "channel_in_range_table": True,
            "range": [lo, hi],
            "n_violations": sum(1 for v in readings if v < lo or v > hi),
            "n_readings": len(readings),
        }


@register_operator(
    name="sensor_drift",
    modalities=frozenset({"industrial_sensor"}),
    required_fields={"text"},
    cost_class=CostClass.RULE,
    shardable=False,  # 需同组窗口全量视角（基线统计）
)
class SensorDriftOp(BatchOperator):
    """漂移检测：同设备×同通道×**同工况**分组内，基线窗（最早 N=5）之后
    的窗口均值偏移超基线 σ 的 4 倍（`params.z`，默认 `_DRIFT_Z`）判漂移。
    跨工况不比较——changeover 的均值偏移是合法业务差异（工况标签防误杀的关键）。
    校准漂移是慢变量，单窗内检不出，必须批量跨窗看。

    **R4：尺度有两种，`params.scale` 选择，默认 `pooled`（既有行为一字不动）**

    - `pooled`（默认）：尺度 = 窗内 pooled σ × AR(1) 有限样本因子。它回答的是
      「**窗内噪声**能推出多大的窗均值抖动」。合成语料上这是对的（合法波动就是
      窗内噪声），真实数据上却是**模型假设错**：真实合法波动里还有负载变化、
      生产节拍、环境温度这些**窗间**因素，它们让窗均值天然散布在「窗内噪声外推」
      的范围之外 —— 此时**任何阈值都救不回来**，只能换尺度的**来源**。
    - `mad`：尺度 = `1.4826 × MAD(同组全部窗均值)`，即「**这个通道自己观察到的**
      窗间波动有多大」。取 MAD 而非标准差是为了抗污染（30% 的窗被判漂移也不动摇）。
      并取 `max(mad_scale, pooled_scale)` 作地板：**MAD 只会让判据更宽松、绝不会
      更敏感**，所以它不可能引入新的误杀来源。

    两种尺度的中心都仍是**基线窗均值的中位数**（口径不变，只换尺度来源），
    所以两者的差异可以被干净地归因到「尺度怎么定的」这一件事上。

    分母口径提醒：`sigma_mean` 若为 0（同组窗均值完全相同），`z` 会变成 inf；
    池化档用 `max(..., 1e-12)` 兜底，MAD 档的 `max(mad, pooled)` 也保证不会更小。
    """

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("sensor_drift 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        scale_mode = str(self.params.get("scale", "pooled"))
        z_thr = float(self.params.get("z", _DRIFT_Z))
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
            pooled = s_w * ((1 + rho) / (n * (1 - rho))) ** 0.5
            base_means = sorted(_mean(w["readings"]) for w in base)
            center = base_means[len(base_means) // 2]
            sigma_mean = pooled
            if scale_mode == "mad":
                all_means = sorted(_mean(w["readings"]) for w in windows)
                med = all_means[len(all_means) // 2]
                devs = sorted(abs(m - med) for m in all_means)
                mad = devs[len(devs) // 2]
                sigma_mean = max(1.4826 * mad, pooled)
            baselines[key] = (center, sigma_mean)

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
                score = 0.0 if abs(z) > z_thr else 1.0
                if score == 0.0:
                    s.meta["evidence:sensor_drift"] = {
                        "scale": scale_mode,
                        "window_mean": _mean(p["readings"]),
                        "baseline_center": bm,
                        "sigma": sigma_mean,
                        "z": z,
                        "z_threshold": z_thr,
                    }
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
    """故障 vs 计划维护判别（本包的归因架构落点）：静默形态窗若落在该设备的
    检修计划窗内 → 合法放行；计划外静默 → 真实数据链路故障。计划索引由
    maintenance_event 样本在同模态样本流内构建——业务事件源是数据的一部分，
    不算子猜测。

    **R3：真实数据上的「静默」不是哨兵，所以判据必须认三种形态**（实点见
    docs/design_tables.md「P2 补表」R3）。三者用**并集**而非替换——把哨兵判据
    换掉，合成门禁的 `sensor_unplanned_silence` 立刻失效，这是本算子的一条红线。

    - **① 全哨兵**：窗内读数全为 `-999` → 计划内 `1.0` / 计划外 `0.0`。
      真实数据实点：**0 个窗**（三个数据集都没有哨兵）。该档在真实轨上无适用性，
      必须显式报出，而不是被读成「静默全部通过」。
    - **② 设备级停机**：同 `(device_id, window_start)` 全通道恒定（≥2 通道）
      → `1.0` + 证据 `machine_stop`。MetroPT-3 实点 191 窗；故障标签 0/191、
      计划内 0/191 —— 判 `0.0` 会白丢 1337 条合法记录。
    - **③ 跨度缺口**：`span_ratio > gap_ratio`（默认 5.0）→ `None` + 证据
      `span_gap`。MetroPT-3 实点 101 窗/通道，且与 ② 的交集为 **0**
      （两种现象正交，不是同一件事的两种说法）。

    形态 ③ 单独成档的理由：窗内 256 个读数横跨了 5~69 倍的应有时间长度，说明
    **该窗跨接了两个运行时段**（记录仪停摆），读数不构成一段连续观测 ——
    「静默是不是故障」这个问题在该窗上**没有定义**，所以是 `None` 而不是 `1.0`。
    实测它与故障标签也统计独立（101 窗里 2 个带标签 = 2.0%，与基准率 2.0% 相同），
    判 `0.0` 同样是误杀。

    形态 ② 与 `sensor_stuck` 的 0 档共用 `_device_stop_keys`（同一真相源）：
    同一个事实在两个问题下给不同答案是对的 —— 「这窗是静默故障吗」答「不是，是停机」
    所以放行；「这通道卡死了吗」答「本判据不适用」所以记 `None`。
    """

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("fault_vs_maintenance 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        gap_ratio = float(self.params.get("gap_ratio", _GAP_RATIO))
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
        stops = _device_stop_keys(parsed)

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
            evidence: dict | None = None
            if all(v == SENTINEL for v in readings):
                # 形态 ①：合成靶子形态，原逻辑一字不动（红线）
                start = datetime.fromisoformat(p["window_start"])
                in_plan = any(
                    device == p["device_id"] and win_start <= start <= win_end
                    for device, win_start, win_end in plan
                )
                score = 1.0 if in_plan else 0.0
                evidence = {"rule": "planned_silence" if in_plan else "unplanned_silence"}
            elif (p["device_id"], p["window_start"]) in stops:
                # 形态 ②：多通道同步冻结 = 设备停机，不是链路故障
                score = 1.0
                evidence = {
                    "rule": "machine_stop",
                    "frozen_channels": stops[(p["device_id"], p["window_start"])],
                }
            else:
                ratio = _span_ratio(p)
                if ratio is not None and ratio > gap_ratio:
                    # 形态 ③：窗跨接两个运行时段 → 本判据无法评判
                    score = None
                    # 估计缺席槽位数（以「应有读数数」为基准）
                    missing = max(0, round((ratio - 1.0) * (len(readings) - 1)))
                    evidence = {
                        "rule": "span_gap",
                        "span_ratio": round(ratio, 3),
                        "missing_slots": missing,
                    }
                else:
                    score = 1.0
            s.meta[f"score:{self.name}"] = score
            if evidence is not None:
                # 本算子的**每一档都是枚举形态**，所以证据一律挂上：它回答的正是
                # 「这个窗凭什么被判成这样」。与 sensor_stuck 相反（那边的 idle
                # 通过是绝大多数窗的常态，挂证据只增噪声）。
                s.meta["evidence:sensor_fault_vs_maintenance"] = evidence
            if self.keep(score):
                survivors.append(s)
        return survivors
