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
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml
from curation_eval import CostClass, SensorSample, register_operator

from ..data.sensor_synth import RANGE_TABLE, SENTINEL
from .base import BatchOperator, Operator, Sample
from .robust import MAD_TO_SIGMA
from .robust import mad_scale as _mad_scale
from .robust import median as _median
from .robust import robust_limit as _robust_limit

_BASELINE = 5  # drift 基线窗数（每组最早 N 个同工况窗）
_DRIFT_Z = 4.0  # 均值偏移判定阈（以窗均值的有效 σ 为单位；4σ 下平稳语料误杀≈0）
_STUCK_ALPHA = 0.2  # 尺度塌陷比：σ_win < α × 通道尺度参考（健康窗内 σ 的 p75）
_STUCK_RUN = 10  # 疑似塌陷需连续 N 窗才判卡死（单窗低方差是噪声，冻结是持续现象）
_GAP_RATIO = 5.0  # 窗内「实际时间跨度 / 应有跨度」判阈，超过即认定窗跨接了两个运行时段
_DEVICE_MIN_CHANNELS = 2  # 设备级停机的前提：至少 2 个通道同时恒定才算「设备没在跑」
# R8：数据包络的**参考窗数下限**。包络由 MAD 定，而小样本下 MAD 抖动过大
# （效率低、且对「巧合地聚集」毫无抵抗力）→ 与其给一个不可信的边界，
# 不如老实记未评。这是**工程下限**，不是从数据里标定出来的，故不随数据集变化。
_ENV_MIN_REF = 15

# --- R9（2026-09-22）多变量层（MSPC）默认参数 ---
_MSPC_REF_FRAC = 0.3  # 参考集 = 每台设备**寿命最早**这个比例的窗（工控「黄金批次」做法）
_MSPC_MIN_REF = 20  # 参考行数下限；不足 → 记 None（模型不可靠时不猜）
_MSPC_MIN_CHANNELS = 3  # 参与建模的通道数下限（<3 个通道谈不上「相互关系」）
_MSPC_VAR_FRAC = 0.9  # 保留主元的累计解释方差比例
_MSPC_SENSOR_SHARE = 0.6  # 单通道贡献占比超过此值 → 传感器可疑（数据质量问题）
# 超限倍数：控制限取「中位数 + margin × 1.4826×MAD」（`.robust.robust_limit`），
# **不是** MSPC 教科书的「参考集 99 分位」——那个在小参考集上会退化成静默空转
# （`n=50` 时 99 分位即最大值，乘 margin 后无点能超），实测踩过，见 #75。
# margin 的语义是「超出稳健尺度几倍才算**粗大误差**」：噪声级偏离不算。
_MSPC_MARGIN = 3.0
# 单通道主导的**持续性**要求：默认 1 = 关闭（单窗粗大偏离即算）。
# 真实设备的长故障用 `sensor_run=5` 更稳；评测注入（单窗）下必须为 1，
# 否则判据结构上抓不到孤立缺陷 —— 这也是「参数是未声明的数据形状假设」又一例。
_MSPC_SENSOR_RUN = 1


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


def _load_envelopes(path: str | Path) -> dict[tuple[str, str], tuple[float, float, int]]:
    """读**数据包络**表（R8）：`{"<device_type>/<channel>": {"lo":..,"hi":..,"n_ref":..}}`。

    存在理由（实测）：真实数据集的通道基本都落在手册量程表外，`sensor_range` 于是
    **一次都没评过**（记 `None`）——判据在真实数据上 100% 静默空转。补上「量程从哪来」
    这件事，是补**缺失的输入**，不是调阈值。

    `n_ref` 是**必填**字段，它记「这个边界由多少个参考窗支撑」。缺了它就无法判断
    边界可不可信，只能记未评；所以这里与 `_load_ranges` 同规矩：**宽松解析、严格报错**
    ——包络填错会静默产生假的「超量程」判决，比不填更危险。

    边界该从哪来（生产者见 `scripts/build_envelopes.py`）：**只能**来自判据当时
    看得到的数据（每组最早的参考段），**不能**用整份数据的事后分布——后者会拿
    「已经包含缺陷的分布」去判缺陷，构成同义反复。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"包络表不存在: {p}")
    text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) if p.suffix.lower() in (".yaml", ".yml") else json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError(f"包络表 {p} 应为映射 {{'<device_type>/<channel>': {{lo, hi, n_ref}}}}")
    out: dict[tuple[str, str], tuple[float, float, int]] = {}
    for key, entry in raw.items():
        if str(key).startswith("_"):  # `_meta` 等说明块不是通道
            continue
        if not isinstance(entry, dict) or "lo" not in entry or "hi" not in entry:
            raise ValueError(f"包络表 {p} 的 {key!r} 应为 {{lo, hi, n_ref}}")
        if "n_ref" not in entry:
            raise ValueError(f"包络表 {p} 的 {key!r} 缺 n_ref（边界可不可信无法判断）")
        lo, hi = float(entry["lo"]), float(entry["hi"])
        if lo > hi:
            raise ValueError(f"包络表 {p} 的 {key!r} 下界大于上界: {[lo, hi]}")
        n_ref = int(entry["n_ref"])
        if n_ref < 0:
            raise ValueError(f"包络表 {p} 的 {key!r} 的 n_ref 为负: {n_ref}")
        device_type, _, channel = str(key).rpartition("/")
        if not device_type or not channel:
            raise ValueError(f"包络表 {p} 的键应为 '<device_type>/<channel>'，收到 {key!r}")
        out[(device_type, channel)] = (lo, hi, n_ref)
    return out


@register_operator(
    name="sensor_range",
    modalities=frozenset({"industrial_sensor"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class SensorRangeOp(Operator):
    """量程检查：超量程/物理不可能值占比（按设备类型×通道查边界表）。
    score = 1 - 越界读数占比。

    **表外通道记 `None`（无法计分），不再记 1.0。** 旧行为的危害是「伪装成通过」：
    真实数据集的 `(device_type, channel)` 基本都落在内嵌表外，一律记 1.0 会让报告
    读起来像「量程全部通过」，实际这三个真实数据集上**一次都没有评过**
    （`docs/REAL_DATA_REPORT.md` §一 的三数据集读数可直接复核）。
    与 `sensor_stuck` 的无信息通道豁免同源：**「没评」和「评过且通过」必须可区分**。

    ---

    **边界来源有两条，按优先级（R8，2026-09-22 补第二条）**：

    1. **手册量程**（`params.ranges_path` 外接，或内嵌 `RANGE_TABLE`）——
       形状 `{device_type: {channel: [lo, hi]}}`，外接**覆盖**内嵌表的同名键。
       真实量程必须有可追溯来源（数据集文档 / 传感器手册）；查不到的通道**留空**。
    2. **数据包络**（`params.envelope_path`）——形状
       `{"<device_type>/<channel>": {"lo":..,"hi":..,"n_ref":..}}`，由
       `scripts/build_envelopes.py` 从**参考段**（每组最早的 `ref_frac` 比例窗）
       按稳健限生成。

    为什么需要第二条（这是实测逼出来的，不是设计偏好）：真实数据集的通道基本
    全在手册表外 → 判据在真实数据上**100% 静默空转**，注入 1888 条已知越界缺陷
    的召回是 **0.0%**，而它的职责恰恰就是抓越界。所以问题不在阈值、在**缺输入**。

    ⚠️ 包络的诚实性条件（三条，缺一不可）：
    - **只能**来自参考段（判据当时看得到的数据），不能用整份数据的事后分布——
      后者拿「已含缺陷的分布」去判缺陷，是同义反复；
    - `n_ref` 必须记在表里且 ≥ `params.min_ref`（默认 `_ENV_MIN_REF`），
      否则**仍记未评**（边界可不可信无法判断时不许猜）；
    - 包络是**代理边界**，不是物理量程：它只能发现「偏离自己历史形态」的值，
      发现不了「一直都在量程外但稳定」的值——那种要靠手册量程。表里 `_meta`
      会记下它是怎么算的，谁用谁可复核。
    """

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self._ranges = dict(RANGE_TABLE)
        extra = params.get("ranges_path")
        if extra:
            self._ranges.update(_load_ranges(extra))
        self._envelopes: dict[tuple[str, str], tuple[float, float, int]] = {}
        env = params.get("envelope_path")
        if env:
            self._envelopes = _load_envelopes(env)
        self._min_ref = int(params.get("min_ref", _ENV_MIN_REF))

    def _bounds_for(self, payload: dict) -> tuple[float, float, str] | None:
        """定这条记录的判定边界。两条来源按优先级；都不可用 → `None`（未评）。

        抽成一个方法是为了 `score` 与 `explain` 共用同一份优先级逻辑——
        两处各写一遍必然漂移（判决与证据不一致是审计的噩梦）。
        """
        key = (payload.get("device_type"), payload.get("channel"))
        if key in self._ranges:
            lo, hi = self._ranges[key]
            return (lo, hi, "manual_range")
        env = self._envelopes.get(key)
        if env is not None and env[2] >= self._min_ref:
            return (env[0], env[1], "data_envelope")
        return None

    def score(self, sample: Sample) -> float | None:
        payload = _parse(sample)
        if payload is None:
            return None
        if payload.get("record_type") != "reading_window":
            return 1.0
        found = self._bounds_for(payload)
        if found is None:
            return None  # 两条来源都不可用：无权评判，且不伪装成通过
        lo, hi, _source = found
        readings = payload["readings"]
        violations = sum(1 for v in readings if v < lo or v > hi)
        if violations == 0:
            return 1.0
        return 1.0 - violations / len(readings)

    def explain(self, sample: Sample, score: float | None) -> dict:
        payload = _parse(sample)
        if payload is None or payload.get("record_type") != "reading_window":
            return {"non_reading_window": True}
        key = (payload.get("device_type"), payload.get("channel"))
        env = self._envelopes.get(key)
        found = self._bounds_for(payload)
        if found is None:
            # 未评的两种原因必须可区分（「表外」vs「有包络但参考不足」）
            return {
                "channel_in_range_table": False,
                "channel_in_envelope_table": env is not None,
                "envelope_n_ref": env[2] if env is not None else None,
                "min_ref": self._min_ref,
                "reason": "insufficient_reference" if env is not None else "no_range_entry",
                "device_type": payload.get("device_type"),
                "channel": payload.get("channel"),
            }
        lo, hi, source = found
        readings = payload["readings"]
        return {
            "channel_in_range_table": source == "manual_range",
            "bounds_source": source,
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
                sigma_mean = max(MAD_TO_SIGMA * mad, pooled)
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


# ---------------------------------------------------------------------------
# R9（2026-09-22）：**多变量层**（MSPC）——补的是「维度」，不是「更好的阈值」。
#
# 稳健统计三件套（中位数 / MAD 尺度 / 稳健限）已抽到 `.robust`，理由是它现在有
# **两个**消费者：本层的 T²/SPE 控制限，和 `sensor_range` 的数据包络（R8）。
# 上面按旧名（`_median` / `_mad_scale` / `_robust_limit`）导入，调用点一字未动。
# ---------------------------------------------------------------------------


def _mspc_fit(
    rows: list[tuple],
    vectors: dict[tuple, dict[str, float]],
    channels: list[str],
    ref_frac: float,
    var_frac: float,
    margin: float,
) -> dict | None:
    """拟合 MSPC 模型：稳健标准化 → PCA → 参考集上的 T²/SPE 控制限。

    参考集 = 每台设备**寿命最早** `ref_frac` 比例的窗（工控里的 golden batch 做法）。

    返回 `None` = **本组模型不可靠，一律记「未评」**（不猜）。四种情形：
    通道数不足、参考行数不足、某通道在参考集里完全没有离散度（尺度为 0，
    标准化会除零 —— 那是「无信息通道」，不是「正常通道」）、或 T²/SPE 在
    参考集上零离散度（定不出控制限）。
    """
    if len(channels) < _MSPC_MIN_CHANNELS:
        return None

    by_device: dict[str, list[tuple]] = {}
    for row in rows:
        by_device.setdefault(row[2], []).append(row)
    picked: list[tuple] = []
    for device_rows in by_device.values():
        device_rows.sort(key=lambda r: r[3])
        picked.extend(device_rows[: max(1, math.ceil(ref_frac * len(device_rows)))])
    # 参考行必须**通道齐全**（缺通道的行进不了矩阵；它不是异常，是数据缺口）
    ref_rows = [r for r in picked if all(c in vectors[r] for c in channels)]

    if len(ref_rows) < max(_MSPC_MIN_REF, len(channels) + 2):
        return None

    ref = np.array([[vectors[r][c] for c in channels] for r in ref_rows], dtype=float)
    center = np.array([_median(ref[:, j].tolist()) for j in range(len(channels))])
    scale = np.array([_mad_scale(ref[:, j].tolist(), center[j]) for j in range(len(channels))])
    alive = scale > 0
    if int(alive.sum()) < _MSPC_MIN_CHANNELS:
        return None

    kept = [c for c, ok in zip(channels, alive) if ok]
    center, scale = center[alive], scale[alive]
    z_ref = (ref[:, alive] - center) / scale

    _u, sv, vt = np.linalg.svd(z_ref, full_matrices=False)
    eigvals = (sv**2) / max(1, z_ref.shape[0] - 1)
    total = float(eigvals.sum())
    if total <= 0:
        return None
    n_comp = int(np.argmax(np.cumsum(eigvals) / total >= var_frac)) + 1
    loadings = vt[:n_comp].T

    t2_ref, spe_ref, _ = _mspc_stats(z_ref, loadings, eigvals[:n_comp])
    t2_limit = _robust_limit(t2_ref.tolist(), margin)
    spe_limit = _robust_limit(spe_ref.tolist(), margin)
    if t2_limit is None or spe_limit is None:
        return None
    return {
        "channels": kept,
        "center": center,
        "scale": scale,
        "loadings": loadings,
        "eigvals": eigvals[:n_comp],
        "n_comp": n_comp,
        "t2_limit": t2_limit,
        "spe_limit": spe_limit,
    }


def _mspc_stats(z: np.ndarray, loadings: np.ndarray, eigvals: np.ndarray):
    """MSPC 的两个统计量：T²（模型内偏离 `Σt²/λ`）与 SPE（模型外残差 `Σe²`）。

    顺带返回残差矩阵——SPE 的**逐通道贡献**就是它的平方，贡献图要用。
    """
    t = z @ loadings
    t2 = ((t**2) / np.maximum(eigvals, 1e-12)).sum(axis=1)
    resid = z - t @ loadings.T
    spe = (resid**2).sum(axis=1)
    return t2, spe, resid


def _mspc_row(model: dict, values: list[float]) -> dict:
    """单行的 T²/SPE + **贡献分解**（贡献图是区分故障归属的依据）。

    - SPE 贡献：残差平方 `e_j²`（各分量之和 = SPE）
    - T² 贡献：标准部分分解 `Σ_a t_a·V_aj·z_j/λ_a`（各分量之和 = T²）

    两者各自归一后**按超限倍数加权**再合成：哪个统计量超得更多，归属就听谁的。
    归一化保证「占比」这个数在 0~1 之间，可以拿一个固定门槛（`sensor_share`）判归属。
    """
    z = (values - model["center"]) / model["scale"]
    loadings = model["loadings"]
    lam = np.maximum(model["eigvals"], 1e-12)
    t = z @ loadings
    t2 = float(((t**2) / lam).sum())
    resid = z - t @ loadings.T
    spe = float((resid**2).sum())

    t2_c = np.abs(z * (loadings @ (t / lam)))
    weight = max(spe / max(model["spe_limit"], 1e-12), 0.0) + max(
        t2 / max(model["t2_limit"], 1e-12), 0.0
    )
    combined = (spe / max(model["spe_limit"], 1e-12)) * (resid**2) + (
        t2 / max(model["t2_limit"], 1e-12)
    ) * t2_c
    total = float(combined.sum())
    share = (combined / total) if total > 0 else np.zeros_like(combined)
    top = int(np.argmax(share)) if total > 0 else -1
    return {
        "t2": t2,
        "spe": spe,
        "top_channel": model["channels"][top] if top >= 0 else "",
        "top_share": float(share[top]) if top >= 0 else 1.0,
        "_weight": weight,
    }


@register_operator(
    name="sensor_multivariate",
    modalities=frozenset({"industrial_sensor"}),
    required_fields={"text"},
    cost_class=CostClass.RULE,
    shardable=False,  # 需同设备**全通道**全量视角（PCA 模型 + 控制限）
)
class SensorMultivariateOp(BatchOperator):
    """多变量一致性（MSPC）：把一个设备**同一时刻的全部通道**当成一个向量看，
    用 PCA 学「正常情况下它们怎么一起动」，再看每个时刻偏离了没有。

    **为什么需要这一档（实点，见 ENGINEERING_NOTES #73）**：`sensor_stuck` /
    `sensor_drift` / `sensor_range` 都是**单通道**判据——一个通道一个阈值。
    这在真实设备上有硬上限：设备退化常是**多个通道一起动**，每个通道单独看都
    还在范围内，联合起来才异常。C-MAPSS 上实测单变量统计量的 AUC：
    现判据 0.6272 / 趋势斜率 0.6333 / 两者取大 0.6254 —— 三个都接近 0.5，
    **即单变量层面几乎没有信息**。所以本算子补的是**维度**，不是更好的阈值。
    行业解法同此：MSPC（多变量统计过程控制）自 1991 年沿用至今，
    文献对 SPE 的描述正是本项目的场景——「即使单变量都在历史范围内，
    缓慢漂移的传感器也会让 SPE 升高」。

    两个统计量（标准形式）：
    - **T²（Hotelling）**：在模型能解释的方向上偏离了多远（`Σt²/λ`）；
    - **SPE / Q 残差**：模型**没见过**的模式有多大（`Σe²`）。

    **关键设计：用贡献图把「传感器坏了」和「设备变了」分开。**
    这是 MSPC 的标准用法，也正好命中真实数据上最要命的那个混淆
    （三个真实数据集的标签全是**过程**异常，见设计表 §8.10 根因 A）。

    触发条件是 `T² > margin × 控制限` 或 `SPE > margin × 控制限`。控制限取自
    参考集的 99 分位，**按构造**参考集自己就有约 1% 会「超限」——所以「超限」
    本身不算数（那是噪声级偏离），要超到 `margin` 倍才算**粗大误差**。
    实测：不加这个倍数时 C-MAPSS 上有 651 个窗被判单通道可疑（其中 630 个是
    数据集的干净窗），加 `margin=3` 后降到约 1/4 —— 倍数比任何阈值微调都管用，
    因为它改变的是「什么算异常」的定义，不是「门槛高低」。

    （另有 `sensor_run` 持续档可选：要求单通道主导**连续 N 个时刻**才算。
    默认 1 = 关闭。真实设备的长故障用 `sensor_run=5` 更稳，但评测注入是单窗，
    开了就结构上抓不到 —— 参数本身就是未声明的数据形状假设，见笔记 #72。）
    - 偏离**由单个通道主导**（占比 > `sensor_share`）→ `sensor_suspect`
      → `0.0` **丢弃**：该通道与其余通道的相互关系断了，是**数据质量**问题。
    - 偏离由**多个通道共同贡献** → `process_suspect` → `1.0` **保留** + 证据：
      设备状态真的变了，读数本身是合法的。**过程异常不是脏数据**，
      删掉它等于把最有价值的那段数据扔了 —— 这也是本档同时充当
      「误杀抑制器」的原因：凡是多通道共同变化的时刻，一律放行。

    **不需要量程表、不需要单位、不需要工况标签**：建模前按通道做稳健标准化
    （中位数 + 1.4826×MAD），只看**结构**不看量纲。这正是它能在真实数据上
    真正跑起来的原因（`sensor_range` 在 C-MAPSS 上 100% 未评就是卡在量程表上）。

    模型不可靠时记 `None`（参考行不足 / 通道不足 / 无信息通道 / 该行缺通道），
    与「评过且通过」严格区分。
    """

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("sensor_multivariate 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        ref_frac = float(self.params.get("ref_frac", _MSPC_REF_FRAC))
        var_frac = float(self.params.get("var_frac", _MSPC_VAR_FRAC))
        sensor_share = float(self.params.get("sensor_share", _MSPC_SENSOR_SHARE))
        margin = float(self.params.get("margin", _MSPC_MARGIN))
        if "quantile" in self.params:
            # 旧的「分位限」档已废弃（小参考集上退化成最大值 → 判据静默空转）。
            # 显式报错而不是静默忽略：静默忽略会让配置文件里的参数变成摆设。
            raise ValueError("sensor_multivariate: 参数 quantile 已废弃，控制限改用 margin")

        # 1) 聚成「同设备同刻的通道向量」：样本粒度是 (设备, 通道, 窗)，向量粒度是 (设备, 窗)
        parsed: list[tuple[Sample, dict | None]] = []
        vectors: dict[tuple, dict[str, float]] = {}
        for s in samples:
            p = _parse(s)
            parsed.append((s, p))
            if p is None or p.get("record_type") != "reading_window":
                continue
            row = (p["device_type"], p.get("operating_mode"), p["device_id"], p["window_start"])
            vectors.setdefault(row, {})[p["channel"]] = _mean(p["readings"])

        # 2) 按 (设备类型, 工况) 建模 —— 工况不同的时刻天然不能进同一个相关结构
        groups: dict[tuple, list[tuple]] = {}
        for row in vectors:
            groups.setdefault((row[0], row[1]), []).append(row)

        flags: dict[tuple, float | None] = {}
        evidence: dict[tuple, dict] = {}
        for rows in groups.values():
            present = sorted({c for row in rows for c in vectors[row]})
            # 只在 ≥90% 的行里都出现的通道才参与建模；缺席是数据缺口，不是判据的事
            channels = [
                c for c in present if sum(1 for row in rows if c in vectors[row]) >= 0.9 * len(rows)
            ]
            model = _mspc_fit(rows, vectors, channels, ref_frac, var_frac, margin)
            if model is None:
                for row in rows:
                    flags[row] = None
                    evidence[row] = {"rule": "no_reference_model"}
                continue
            kept = model["channels"]
            by_device: dict[str, list[tuple]] = {}
            for row in rows:
                if any(c not in vectors[row] for c in kept):
                    flags[row] = None  # 该时刻缺通道 → 向量不完整，本判据不适用
                    continue
                stat = _mspc_row(model, np.array([vectors[row][c] for c in kept], dtype=float))
                flagged = (
                    stat["t2"] > margin * model["t2_limit"]
                    or stat["spe"] > margin * model["spe_limit"]
                )
                if not flagged:
                    flags[row] = 1.0
                    continue
                sensor_side = stat["top_share"] > sensor_share
                by_device.setdefault(row[2], []).append(
                    (
                        row,
                        sensor_side,
                        stat,
                        {
                            "rule": "sensor_suspect" if sensor_side else "process_suspect",
                            "t2": round(stat["t2"], 4),
                            "t2_limit": round(model["t2_limit"], 4),
                            "spe": round(stat["spe"], 4),
                            "spe_limit": round(model["spe_limit"], 4),
                            "top_channel": stat["top_channel"],
                            "top_share": round(stat["top_share"], 4),
                            "n_channels": len(kept),
                            "n_components": model["n_comp"],
                        },
                    )
                )

            # 持续性：单通道主导必须**连续 run_len 个时刻且指向同一通道**才算传感器故障。
            # 单次尖峰是噪声，不是故障——与 sensor_stuck 的 min_run 同一条道理。
            # 这一步是本算子的「误杀抑制器」：不连续的偏离一律降级为瞬时偏离并放行。
            run_len = int(self.params.get("sensor_run", _MSPC_SENSOR_RUN))
            for device_rows in by_device.values():
                device_rows.sort(key=lambda item: item[0][3])  # 按时刻（window_start）
                i = 0
                while i < len(device_rows):
                    row, sensor_side, stat, detail = device_rows[i]
                    if not sensor_side:
                        flags[row] = 1.0
                        detail["attribution"] = (
                            "多通道共同变化：设备状态变了，读数本身合法（保留）"
                        )
                        evidence[row] = detail
                        i += 1
                        continue
                    j = i
                    while (
                        j + 1 < len(device_rows)
                        and device_rows[j + 1][1]
                        and device_rows[j + 1][2]["top_channel"] == stat["top_channel"]
                    ):
                        j += 1
                    seg_len = j - i + 1
                    for k in range(i, j + 1):
                        krow, _side, _stat, kdetail = device_rows[k]
                        kdetail["run_length"] = seg_len
                        if seg_len >= run_len:
                            flags[krow] = 0.0
                            kdetail["attribution"] = (
                                f"单通道主导且连续 {seg_len} 个时刻：该通道与其余通道的"
                                "相互关系断了（数据质量缺陷）"
                            )
                        else:
                            flags[krow] = 1.0
                            kdetail["rule"] = "sensor_transient"
                            kdetail["attribution"] = (
                                f"单通道主导但不连续（连续 {seg_len} < {run_len} 个时刻）："
                                "瞬时偏离，按噪声放行"
                            )
                        evidence[krow] = kdetail
                    i = j + 1

        # 3) 记账一律按样本 id（同框架口径），不用 payload 派生键。
        # 关键：**判决在「时刻」级，丢弃在「通道」级**。
        # 向量的一致性断裂说明「这一时刻不可信」，但**偏离归因于哪个通道**由贡献图给出；
        # 把同一时刻其余 20 个通道一起丢掉，是拿 20 条好记录换 1 条坏记录
        # —— 实测过：整行丢弃让附带误丢从上百条涨到 758 条（评测单位错配）。
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
            row = (p["device_type"], p.get("operating_mode"), p["device_id"], p["window_start"])
            score = flags.get(row, None)
            detail = evidence.get(row)
            if (
                score == 0.0
                and detail is not None
                and detail.get("rule") == "sensor_suspect"
                and p["channel"] != detail.get("top_channel")
            ):
                # 同刻的其他通道：跨通道关系已断，但它们本身没有可归因的偏离 → 保留
                score = 1.0
            s.meta[f"score:{self.name}"] = score
            if detail is not None:
                # 与 fault_vs_maintenance 同规矩：**每一档都是枚举形态**，
                # `process_suspect` 更是「保留但必须可审计」——它回答的正是
                # 「为什么不丢」。所以证据一律挂上，不管丢没丢。
                s.meta["evidence:sensor_multivariate"] = detail
            if self.keep(score):
                survivors.append(s)
        return survivors
