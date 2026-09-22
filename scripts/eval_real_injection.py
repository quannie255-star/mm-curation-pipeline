"""真实背景半合成评测（R11，2026-09-22）：把**已知**的数据质量缺陷注入真实窗，
在真实背景上量出**有效**的召回与误杀。

为什么需要这个脚本（实测逼出来的，见 `docs/ENGINEERING_NOTES.md` #71）：
三个真实数据集的真值标签（C-MAPSS `degraded` / MetroPT-3 `fault_window_air_leak` /
SKAB `sensor_anomaly`）**全是过程/设备异常**，没有一个提供「数据质量缺陷」的真值。
拿它们当召回分母，等于用数据质量检测器去考故障诊断 —— 所以真实轨召回低
（C-MAPSS 21.9%）**不是能力指标**。要真的知道「洗得怎么样」，必须自己造真值。

做法（业界标准：把已知故障注入真实录波）：
1. **基线跑**：真实语料原样跑一遍 → 得到「不注入任何东西时，算子在这份真实数据上
   自然会丢掉哪些窗」。这批**不是**误杀，是这份数据自带的异常（退化/故障）。
2. **注入跑**：向真实窗注入已知形态的数据质量缺陷 → 再跑一遍。
3. **差分口径**：
   - `recall_injected` = 注入窗被丢 / 注入窗总数 —— **有效真值，这个数可以对外讲**；
   - `collateral_fp` = 注入跑里新丢的**非注入**窗（= 注入造成的附带损伤）—— 应当 ≈ 0；
   - `background_flag_rate` = 基线跑丢的非注入窗占比 —— 这是「这份真实数据自带的
     异常率」，**不是**误杀率（上界），必须与上一行分开报，不许混。

一句话：**召回用注入集（真值已知），误杀用差分（基线有对照）。**

⚠️ 为什么不用 `curation_eval.sensor_contamination` 里现成的污染器（实测结论）：
那批污染器**隐式依赖合成语料的形状**，在真实窗上供体池直接为空：
- `_beyond_baseline` 要求同组早于它的窗 ≥ `_BASELINE`(5) 个 ——
  C-MAPSS 每台设备每通道只有 4~17 个窗，多数不满足；
- `sensor_out_of_range` 要求 `(device_type, channel)` 在**内嵌量程表**里 ——
  真实通道是 `cmapss_fd001/s1`、`metropt3/...`，全在表外；
- `sensor_unit_swap` 要求单位 ∈ {`MPa`} —— 真实单位是 `au`/`bar`/`°C`。
这与 `docs/design_tables.md` §8.10 根因 A/D 是同一件事：**靶子是按合成形态写的**。
所以本脚本按**真实载荷形状**重写五个注入形态，且**不需要量程表**——
越界形态的边界由语料自身**剖面**得到（这既是 R8 的落点，也是「真实数据没有手册量程」
这个现实下的唯一诚实做法）。
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "curation-eval" / "src"))

from mm_curation.eval.operator_pr import run_operator  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline import PipelineConfig, run_funnel  # noqa: E402

REAL = REPO / "data" / "raw" / "real"
SENTINEL = -999.0
# R10：覆盖率闸门。评过的样本不足这个比例时，该算子的召回**不可引用**。
# 为什么是 50% 而不是 90%：真实数据的通道本来就会有一部分落在两类边界来源之外
# （工况罕见、参考段不足），「评了一多半」和「几乎没评」是两件事；
# 这道闸门的职责只是拦住**后者**（例如全 0 的 `sensor_range`），
# 更严的比例会把正常的部分覆盖误伤成 INVALID，闸门就成了噪声。
_COVERAGE_MIN = 0.5
# 注入形态 = **数据质量**维度的五类（与五个工业算子一一对应）。
# 刻意不注入过程异常：那不在本项目职责内，注进去才是自己骗自己。
KINDS = ("flatline", "cal_offset", "out_of_envelope", "unit_mismatch", "silence")


def load(source: str, windows: str | None = None) -> list[Sample]:
    path = REAL / source / (windows or "windows.jsonl")
    if not path.exists():
        raise SystemExit(f"未找到 {path}")
    return [
        Sample.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    ]


def _reading(samples: list[Sample]) -> list[Sample]:
    return [s for s in samples if s.meta.get("sensor_record_type") == "reading_window"]


def _limit_devices(samples: list[Sample], max_devices: int | None) -> list[Sample]:
    """按**设备**整体截断，保组结构（批量算子的分组不能被切断，否则量的是另一件事）。"""
    if not max_devices:
        return samples
    seen: list[str] = []
    for s in samples:
        try:
            dev = json.loads(s.text).get("device_id", "")
        except (json.JSONDecodeError, TypeError):
            continue
        if dev not in seen:
            if len(seen) >= max_devices:
                break
            seen.append(dev)
    keep = set(seen)
    out = []
    for s in samples:
        try:
            dev = json.loads(s.text).get("device_id")
        except (json.JSONDecodeError, TypeError):
            dev = None
        if dev in keep:
            out.append(s)
    return out


def _pctl(sorted_values: list[float], q: float) -> float:
    n = len(sorted_values)
    if n == 0:
        return 0.0
    idx = min(n - 1, max(0, int(q * n)))
    return sorted_values[idx]


def profile_envelopes(readings: list[Sample]) -> dict[str, tuple[float, float, float]]:
    """**数据剖面**出的通道包络：`channel -> (p99, iqr, 中位数)`。

    这是本脚本在没有手册量程时唯一诚实的越界依据：边界来自这份数据自己的分布，
    而不是编一个量程。剖面的用途是**造测试靶子**（注入），不是给生产判据用
    ——判据若用自己剖出来的边界判越界，就成了同义反复。
    """
    by_channel: dict[str, list[float]] = defaultdict(list)
    for s in readings:
        try:
            p = json.loads(s.text)
        except (json.JSONDecodeError, TypeError):
            continue
        by_channel[p["channel"]].extend(p["readings"])
    out: dict[str, tuple[float, float, float]] = {}
    for channel, values in by_channel.items():
        values.sort()
        lo25, hi75 = _pctl(values, 0.25), _pctl(values, 0.75)
        out[channel] = (_pctl(values, 0.99), max(hi75 - lo25, 1e-9), _pctl(values, 0.5))
    return out


def _store(sample: Sample, payload: dict) -> None:
    """写回载荷并同步 meta —— 与 `curation_eval.sensor_contamination._store` 同约定。"""
    sample.text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    m = sample.meta
    m["sensor_record_type"] = payload.get("record_type")
    m["device_id"] = payload.get("device_id")
    m["device_type"] = payload.get("device_type", "")
    m["window_start"] = payload.get("window_start")
    m["window_end"] = payload.get("window_end")
    if payload.get("record_type") == "reading_window":
        m["channel"] = payload["channel"]
        m["unit"] = payload["unit"]
        m["sampling_hz"] = payload["sampling_hz"]
        m["operating_mode"] = payload["operating_mode"]


def eligible_windows(readings: list[Sample], ref_frac: float) -> list[Sample]:
    """可注入窗 = 不在其 (设备, 通道, 工况) 组**最早 `ref_frac` 比例**里的窗。

    两条理由（都是实测逼出来的）：
    1. **注入必须替换原记录，不能追加**。污染器是从供体复制的窗，`device_id` 与
       `window_start` 与原窗相同 —— 追加会让同一 (设备, 时刻, 通道) 出现两条记录，
       任何「按载荷聚成时刻向量」的判据（如 MSPC）都会读到不确定的向量，
       且**参考集会被注入值污染**。实测：追加式注入让 MSPC 的附带误丢从 0 涨到 924。
    2. **不注入参考段**。批量判据的基线/参考集取自每组最早那批窗；
       把缺陷注进参考段等于同时改模型，量到的是「模型被污染后多差」，
       不是「判据能不能抓到脏」。这也正是包内 `_beyond_baseline` 的初衷
       （它要求同组有 ≥`_BASELINE` 个更早的窗，保证注入窗一定落在判决区）
       —— 只是那个常数在真实数据上不成立，这里改成按**比例**取。
    """
    groups: dict[tuple, list[Sample]] = defaultdict(list)
    for s in readings:
        p = json.loads(s.text)
        groups[(p["device_id"], p["channel"], p.get("operating_mode"))].append(s)
    out: list[Sample] = []
    for members in groups.values():
        members.sort(key=lambda s: json.loads(s.text)["window_start"])
        out.extend(members[max(1, math.ceil(ref_frac * len(members))) :])
    return out


def inject(
    readings: list[Sample],
    k: int,
    seed: int,
    envelopes: dict[str, tuple[float, float, float]],
    mode_unit: dict[tuple[str, str], str],
) -> list[Sample]:
    """向真实窗注入 k 条已知缺陷，返回注入样本（id 加后缀、labels 记形态）。"""
    rng = random.Random(seed)
    injected: list[Sample] = []
    for i in range(k):
        kind = KINDS[i % len(KINDS)]
        src = readings[rng.randrange(len(readings))]
        payload = json.loads(src.text)
        values = list(payload["readings"])
        if kind == "flatline":
            # 卡死：窗内严格平坦（非 idle 才有意义）—— `sensor_stuck` 的靶子
            if payload.get("operating_mode") == "idle":
                continue
            values = [values[0]] * len(values)
        elif kind == "cal_offset":
            # 校准偏移：整体加「5 倍窗内 σ + 1」（远超 drift 判定阈）—— `sensor_drift` 的靶子
            mean = sum(values) / len(values)
            std = max(1e-9, (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5)
            values = [round(v + 5.0 * std + 1.0, 4) for v in values]
        elif kind == "out_of_envelope":
            # 越出剖面包络：头两个读数推到 p99 之外（用该数据自己的分布，不编量程）
            p99, iqr, _med = envelopes[payload["channel"]]
            values[0] = round(p99 + 10.0 * iqr + 1.0, 4)
            if len(values) > 1:
                values[1] = round(p99 + 11.0 * iqr + 1.0, 4)
        elif kind == "unit_mismatch":
            # 单位混用：标签换成同组非众数单位（数值不换算）—— `unit_consistency` 的靶子
            key = (payload["device_type"], payload["channel"])
            dominant = mode_unit.get(key, payload["unit"])
            payload["unit"] = "bar" if dominant != "bar" else "MPa"
        else:  # silence
            # 链路静默：全哨兵 —— `fault_vs_maintenance` 的靶子
            values = [SENTINEL] * len(values)

        payload["readings"] = values
        dirty = copy.deepcopy(src)
        dirty.id = f"{src.id}::inj{i}::{kind}"
        dirty.labels = {"dirty": kind}
        _store(dirty, payload)
        injected.append(dirty)
    return injected


def run_operators(
    specs, samples: list[Sample], restrict: set[str] | None = None
) -> dict[str, dict]:
    """每个算子独立跑一遍：**同时**给出「丢了谁」和「评了多少」。

    R10（2026-09-22）要的就是后一半。`score=None` 在协议里的含义是「无法计分
    （保留并记录缺失）」，但所有报告历来只印召回/误杀 —— 一个 **100% 未评** 的算子，
    召回必然印成 `0.0%`，读起来和「它判过、但这条数据是干净的」**长得一模一样**。
    实测：`sensor_range` 在三个真实数据集上未经包络时**一次都没评过**，
    报告里那行 `0.0%` 不是「零召回」，是**没有分母**。

    所以把覆盖率与召回印在一起：**没有分母的 0% 不配叫结论**。

    `restrict` 限定统计覆盖率的样本 id 子集 —— 传「读数窗」的 id，覆盖率才对应
    「清洗工业读数」这件事的分母（非窗记录不是被判的对象）。
    """
    out: dict[str, dict] = {}
    for spec in specs:
        op = spec.build()
        _kept, dropped = run_operator(op, list(samples))
        # 评分统一落在 meta["score:<op>"]（协议约定，见 curation_eval/sdk.py `Operator.__call__`）
        pool = samples if restrict is None else [s for s in samples if s.id in restrict]
        scored = sum(1 for s in pool if s.meta.get(f"score:{spec.op}") is not None)
        out[spec.op] = {
            "drops": {s.id for s in dropped},
            "scored": scored,
            "total": len(pool),
        }
    return out


def _coverage_verdict(scored: int, total: int, min_rate: float) -> str:
    """覆盖率判定。`ALL_UNSCORED` / `LOW_COVERAGE` 时该算子的召回**不可引用**。"""
    if total == 0:
        return "NO_DATA"
    if scored == 0:
        return "ALL_UNSCORED"
    if scored / total < min_rate:
        return "LOW_COVERAGE"
    return "OK"


def drop_ids(specs, samples: list[Sample]) -> dict[str, set[str]]:
    """每个算子独立跑一遍，返回 {算子: 被丢样本 id 集合}（薄封装，兼容旧调用点）。"""
    return {op: r["drops"] for op, r in run_operators(specs, samples).items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=["skab", "metropt3", "cmapss"])
    parser.add_argument("--windows", default=None)
    parser.add_argument("--config", default="configs/funnel_industrial_real.yaml")
    parser.add_argument("--inject-rate", type=float, default=0.1)
    parser.add_argument(
        "--ref-frac", type=float, default=0.3,
        help="每组最早这个比例的窗视为参考段，不注入（避免同时改模型；与 MSPC 口径一致）",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-devices", type=int, default=None)
    parser.add_argument(
        "--envelope",
        default=None,
        help="数据包络表路径（R8）：运行时挂到 sensor_range 的 params.envelope_path。"
        "挂上后 sensor_range 才有边界来源（真实数据通道都在手册量程表外）。"
        "**这是对照实验的单变量**：同一个 config，只多这一个输入。",
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    config = PipelineConfig.from_yaml(REPO / args.config)
    if args.envelope:
        hit = False
        for spec in config.operators:
            if spec.op == "sensor_range":
                spec.params["envelope_path"] = args.envelope
                hit = True
        if not hit:
            raise SystemExit(f"{args.config} 里没有 sensor_range，--envelope 无处可挂")
    corpus = _limit_devices(load(args.source, args.windows), args.max_devices)
    readings = _reading(corpus)
    if not readings:
        raise SystemExit("语料里没有 reading_window 样本")

    # 1) 基线跑（deepcopy：算子就地写 meta，两次跑必须互不污染）
    #    **覆盖率从基线跑取**：它回答「不注入任何东西时，判据在这份真实数据上评了多少」，
    #    正是 R10 要盯的数（注入集上的覆盖率会被注入本身改变，不是那份数据的属性）。
    #    口径限定在**读数窗**——那是被判的对象，非窗记录不是清洗的分母。
    base_corpus = copy.deepcopy(corpus)
    reading_ids = {s.id for s in _reading(base_corpus)}
    base_stats = run_operators(config.operators, base_corpus, restrict=reading_ids)
    base_drops = {op: r["drops"] for op, r in base_stats.items()}
    base_ids = {s.id for s in base_corpus}

    # 2) 注入跑：把已知的数据质量缺陷注入真实窗（**替换**原记录，不追加，理由见 inject）
    envelopes = profile_envelopes(readings)
    units: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for s in readings:
        p = json.loads(s.text)
        units[(p["device_type"], p["channel"])][p["unit"]] += 1
    mode_unit = {
        k: sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, v in units.items()
    }

    inj_corpus = copy.deepcopy(corpus)
    candidates = eligible_windows(
        [s for s in inj_corpus if s.meta.get("sensor_record_type") == "reading_window"],
        args.ref_frac,
    )
    injected = inject(
        candidates,
        round(len(candidates) * args.inject_rate),
        args.seed,
        envelopes,
        mode_unit,
    )
    replaced = {s.id.rsplit("::inj", 1)[0] for s in injected}
    run_b = [s for s in inj_corpus if s.id not in replaced] + injected
    drop_b = drop_ids(config.operators, run_b)

    inj_by_kind = Counter(s.labels["dirty"] for s in injected)
    n_inj = len(injected)
    n_bg = len(run_b) - n_inj

    print(
        f"\n真实背景注入评测: {args.source}"
        f"（背景 {n_bg} 窗 / 注入 {n_inj} 窗，注入率 {args.inject_rate:.0%} of "
        f"{len(candidates)} 个候选窗，seed {args.seed}）"
    )
    print(f"  注入构成: {dict(sorted(inj_by_kind.items()))}")
    print(f"  剖面包络: {len(envelopes)} 个通道（边界取自**这份数据自己的分布**）")
    if args.envelope:
        print(f"  sensor_range 边界来源: {args.envelope}（否则该判据在真实数据上记**未评**）")
    print(f"  候选窗: {len(candidates)}（已排除每组最早 {args.ref_frac:.0%} 的参考段）")
    print(
        f"\n{'算子':<22}{'召回(注入)':>10}{'附带误丢':>9}{'背景自带异常率':>15}"
        f"{'已评覆盖率':>12}  判定"
    )
    rows = []
    invalid: list[str] = []
    for spec in config.operators:
        bid = drop_b[spec.op]
        caught = sum(1 for s in injected if s.id in bid)
        collateral = len((bid & base_ids) - base_drops[spec.op])
        # 分子要排除**被注入替换掉的原窗**：它们在基线里是原始数据，但已不在背景集里，
        # 计入会让背景自带异常率虚高（分母 `n_bg` 不含它们）——两侧必须同分母。
        bg_flagged = base_drops[spec.op] - replaced
        bg_flag = len(bg_flagged) / n_bg if n_bg else 0.0
        by_kind = {
            k: round(sum(1 for s in injected if s.labels["dirty"] == k and s.id in bid) / v, 4)
            for k, v in sorted(inj_by_kind.items())
        }
        # R10：覆盖率与召回一起印。没有分母的 0% 不配叫结论。
        st = base_stats[spec.op]
        coverage = st["scored"] / st["total"] if st["total"] else 0.0
        verdict = _coverage_verdict(st["scored"], st["total"], _COVERAGE_MIN)
        if verdict != "OK":
            invalid.append(spec.op)
        print(
            f"{spec.op:<22}{caught / n_inj if n_inj else 0:>9.1%}{collateral:>9}"
            f"{bg_flag:>14.2%}{coverage:>12.1%}  {verdict}"
            + ("   <<< 召回不可引用" if verdict != "OK" else "")
        )
        rows.append(
            {
                "op": spec.op,
                "recall_injected": round(caught / n_inj, 4) if n_inj else None,
                "recall_by_injected_kind": by_kind,
                "collateral_false_drop": collateral,
                "background_flag_rate": round(bg_flag, 4),
                "n_background_flagged": len(bg_flagged),
                "scored": st["scored"],
                "n_scored_pool": st["total"],
                "scored_rate": round(coverage, 4),
                "verdict": verdict,
            }
        )

    # 3) 漏斗口径（串联）：召回用注入集，附带误丢用与基线对齐的差分
    funnel = run_funnel(copy.deepcopy(run_b), config)
    f_all = {s.id for _op, s in funnel.dropped}
    f_base = {
        s.id for _op, s in run_funnel(copy.deepcopy(base_corpus), config).dropped
    } - replaced  # 同上：背景集不含被注入替换掉的原窗
    f_caught = sum(1 for s in injected if s.id in f_all)
    f_coll = len((f_all & base_ids) - f_base)
    f_by_kind = {
        k: round(
            sum(1 for s in injected if s.labels["dirty"] == k and s.id in f_all) / v, 4
        )
        for k, v in sorted(inj_by_kind.items())
    }
    summary = {
        "source": args.source,
        "windows_file": args.windows or "windows.jsonl",
        "config": args.config,
        "envelope": args.envelope,
        "seed": args.seed,
        "inject_rate": args.inject_rate,
        "ref_frac": args.ref_frac,
        "n_candidates": len(candidates),
        "injected": dict(sorted(inj_by_kind.items())),
        "n_background": n_bg,
        "n_injected": n_inj,
        "funnel": {
            "recall_injected": round(f_caught / n_inj, 4) if n_inj else None,
            "recall_by_injected_kind": f_by_kind,
            "collateral_false_drop": f_coll,
            "background_drop_rate": round(len(f_base) / n_bg, 4) if n_bg else None,
        },
        # R10：把「哪些算子根本没评过」写成结论的一部分，而不是留给读者从 0.0% 里猜。
        "validity": {
            "coverage_min": _COVERAGE_MIN,
            "operators_unscored_or_low": invalid,
            "all_operators_valid": not invalid,
            "reason": (
                None
                if not invalid
                else f"{', '.join(invalid)} 的已评覆盖率低于 {_COVERAGE_MIN:.0%}"
                "（或一次都没评过）——这些行的召回分母不成立，不可引用；"
                "不是「零召回」，是**没有分母**。"
                "补救：给 sensor_range 挂 params.envelope_path（见 scripts/build_envelopes.py）"
                "或在 config 里提供手册量程表。"
            ),
        },
        "operators": rows,
    }
    print(
        f"\n漏斗（串联）: 注入召回 {f_caught}/{n_inj} = {f_caught / n_inj if n_inj else 0:.1%}；"
        f"附带误丢 {f_coll}；背景自带异常率 {len(f_base) / n_bg if n_bg else 0:.2%}"
    )
    if invalid:
        print(
            f"\n!! 结论不可用（R10）: {', '.join(invalid)} 在这份数据上没评过或几乎没评过，"
            "\n   上面它们的 0.0% 不是零召回，是**没有分母**。"
            "\n   漏斗整体的召回仍成立（由其余算子贡献），但这些行不可单独引用。"
        )
    else:
        print("\nR10 覆盖率闸门: 全部算子均在此数据上实质评过 → 各行的召回可单独引用。")
    print(
        "\n口径提醒：**召回**的分母是注入集（真值已知，可对外讲）；"
        "**背景自带异常率**不是误杀率，它是这份真实数据自己有多脏的上界。"
    )

    out = (
        Path(args.out)
        if args.out
        else REPO / "data" / "reports" / f"real_injection_{args.source}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
