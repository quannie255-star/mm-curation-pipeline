"""生成**数据包络**表（R8，2026-09-22）：给「没有手册量程」的通道一个可追溯的边界来源。

## 为什么需要它

`sensor_range` 的职责是抓「超量程 / 物理不可能值」。它的边界本来只能来自手册
（数据集文档、传感器手册），但三个真实数据集的通道基本**全在手册表外** —— 判定记
`None`（未评），判据于是 **100% 静默空转**。实测：向真实窗注入 1888 条已知越界缺陷，
`sensor_range` 召回 **0.0%**；漏斗里 `out_of_envelope` 这一类也只有 **26.2%**
（是 `sensor_drift` 顺带抓到的，不是本职）。

所以问题不在阈值，在**缺输入**。本脚本补的就是这个输入。

## 边界怎么定（四条纪律）

1. **只看参考段**。参考段 = 每台设备每个通道每个工况**最早** `ref_frac` 比例的窗
   （工控「黄金批次」口径，与 `sensor_multivariate` 一致）。**绝不用整份数据的事后
   分布**——那份分布里已经含了要判的缺陷，用它定边界是同义反复。
2. **读数级、不是窗均值级**。判据是逐读数比边界，所以边界也必须由参考段的**全部
   读数**算出。若用「窗均值 ± 窗均值散布」当边界，宽度会小一个 `√n` 量级
   （窗均值散布 ≈ 窗内 σ/√n），拿它去比单个读数 → 疯狂误杀。
3. **跨设备取并集**（最宽），不取交集。边界反映的是「这个测点在正常期的读数范围」，
   个体差异属合法范围，不该判越界——取并集使判据只抓「比所有设备的正常范围都更
   极端」的值，与「粗大误差」的语义一致。
4. **带 `n_ref`**。每个通道记下它用了多少个**参考窗**（窗才是独立单位，窗内读数高度
   相关，不该按读数计数）。`sensor_range` 的 `min_ref` 会拦掉支撑不足的边界——
   宁可未评，不给不可信的边界。

## 用法

    python -X utf8 scripts/build_envelopes.py --source cmapss --margin 6 \\
        --out data/envelopes/cmapss.json

生成后由 `sensor_range` 的 `params.envelope_path` 消费。表里的 `_meta` 记下了
它是怎么算的（口径、参数、参考窗数），**谁用谁可复核**。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.operators.robust import mad_scale, median  # noqa: E402

REAL = REPO / "data" / "raw" / "real"


def load(source: str, windows: str | None = None) -> list[Sample]:
    path = REAL / source / (windows or "windows.jsonl")
    if not path.exists():
        raise SystemExit(f"未找到 {path}")
    return [
        Sample.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    ]


def build(
    readings: list[Sample],
    ref_frac: float,
    margin: float,
    max_holdout_violation: float = 0.05,
) -> tuple[
    dict[tuple[str, str], dict], int, int, int, dict[tuple[str, str], list[float]]
]:
    """按 `(device_type, channel)` 生成包络。

    返回 `(包络, 参考窗总数, 无法定界的组数, 留出集自检被拒的组数, 被拒明细)`。

    组 = `(device_type, channel, operating_mode, device_id)`：参考段是「**每台设备**
    最早一段」，而边界按 `(device_type, channel)` 汇总（同型号同测点共享边界）。
    """
    groups: dict[tuple, list[tuple[str, list[float]]]] = defaultdict(list)
    for s in readings:
        p = json.loads(s.text)
        if p.get("record_type") != "reading_window":
            continue
        groups[
            (p["device_type"], p["channel"], p.get("operating_mode"), p["device_id"])
        ].append((p["window_start"], p["readings"]))

    env: dict[tuple[str, str], dict] = {}
    n_ref_windows = 0
    n_skipped = 0
    n_rejected = 0
    rejected: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (device_type, channel, mode, dev), members in groups.items():
        members.sort(key=lambda t: t[0])
        n_ref = max(1, math.ceil(ref_frac * len(members)))
        pooled = [v for _ts, vals in members[:n_ref] for v in vals]
        if not pooled:
            n_skipped += 1
            continue
        center = median(pooled)
        scale = mad_scale(pooled, center)
        if scale <= 0:
            # 零离散度 → 定不出边界。与 `.robust.robust_limit` 同规矩：不猜。
            n_skipped += 1
            continue
        lo, hi = center - margin * scale, center + margin * scale

        # 留出集自检（MetroPT-3 逼出来的）：边界是给**参考段之后**的数据用的，
        # 所以必须在参考段之外先验一遍。固定参考段隐含「数据稳态」这个假设，
        # 而真实设备会工况漂移 —— 漂移一旦成立，固定的边界会把大量**正常**读数
        # 判成越界。实测：MetroPT-3 上 margin=6 的边界让 54.4% 的真实窗越界
        # （C-MAPSS 1.1% / SKAB 0.12%），这就是「工况漂移」与「稳态假设」的差距。
        # 自检不过 → **不写这条边界**，宁可让判据记未评：不给会失灵的边界。
        # 注意这不是「用整份数据调边界」（那是同义反复）：边界仍只由参考段**拟合**，
        # 后续数据只用来**接受/拒绝**它，等同留出集验证。
        holdout = [v for _ts, vals in members[n_ref:] for v in vals]
        if holdout:
            violation = sum(1 for v in holdout if v < lo or v > hi) / len(holdout)
            if violation > max_holdout_violation:
                n_rejected += 1
                rejected[(device_type, channel)].append(violation)
                continue

        n_ref_windows += n_ref
        key = (device_type, channel)
        acc = env.get(key)
        if acc is None:
            env[key] = {"lo": lo, "hi": hi, "n_ref": n_ref, "modes": [mode], "devices": 1}
        else:
            acc["lo"] = min(acc["lo"], lo)
            acc["hi"] = max(acc["hi"], hi)
            acc["n_ref"] += n_ref
            if mode not in acc["modes"]:
                acc["modes"].append(mode)
            acc["devices"] += 1
    return env, n_ref_windows, n_skipped, n_rejected, rejected


def main() -> int:
    parser = argparse.ArgumentParser(description="生成数据包络表（sensor_range 的边界来源）")
    parser.add_argument("--source", required=True, choices=["skab", "metropt3", "cmapss"])
    parser.add_argument("--windows", default=None, help="窗文件名（默认 windows.jsonl）")
    parser.add_argument("--ref-frac", type=float, default=0.3)
    parser.add_argument(
        "--margin",
        type=float,
        default=6.0,
        help="边界 = 参考段读数的 中位数 ± margin × 1.4826×MAD（默认 6：粗大误差级，"
        "不是 3σ 那种「正常波动也算」的宽度）",
    )
    parser.add_argument(
        "--max-holdout-violation",
        type=float,
        default=0.05,
        help="留出集自检：参考段之外的读数越界比例超过此值 → **丢弃**该组边界"
        "（默认 5%%。理由：固定参考段隐含数据稳态假设，工况漂移会让正常读数大面积"
        "越界。实测 MetroPT-3 未设此闸门时真实窗越界率 54.4%%，设闸门后回到未评）",
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    samples = load(args.source, args.windows)
    readings = [s for s in samples if s.meta.get("sensor_record_type") == "reading_window"]
    if not readings:
        raise SystemExit("语料里没有 reading_window 样本")

    env, n_ref_windows, n_skipped, n_rejected, rejected = build(
        readings, args.ref_frac, args.margin, args.max_holdout_violation
    )
    n_refs = sorted(v["n_ref"] for v in env.values())
    text: dict = {
        "_meta": {
            "source": args.source,
            "windows_file": args.windows or "windows.jsonl",
            "ref_frac": args.ref_frac,
            "margin": args.margin,
            "max_holdout_violation": args.max_holdout_violation,
            "stat": "参考段**读数**的中位数 ± margin × 1.4826×MAD；跨设备/工况取并集",
            "n_channels": len(env),
            "n_reference_windows": n_ref_windows,
            "n_groups_skipped": n_skipped,
            "n_groups_rejected_by_holdout": n_rejected,
            "rejected_channels": sorted({f"{dt}/{ch}" for dt, ch in rejected}),
            "generated_by": "scripts/build_envelopes.py",
            "caveat": (
                "代理边界，不是物理量程：只能发现**偏离自身历史形态**的值，"
                "发现不了「一直在量程外但稳定」的值（那种要靠手册量程）。"
                "边界只取自参考段（每组最早 ref_frac 的窗），不含要判的区域；"
                "且必须通过留出集自检（参考段之外越界率 <= max_holdout_violation），"
                "否则整组边界不写入 —— 稳态假设不成立时，宁可不判。"
            ),
        }
    }
    for (device_type, channel), acc in sorted(env.items()):
        text[f"{device_type}/{channel}"] = {
            "lo": round(acc["lo"], 6),
            "hi": round(acc["hi"], 6),
            "n_ref": acc["n_ref"],
            "source": "ref_segment_robust",
            "devices": acc["devices"],
            "modes": sorted(acc["modes"]),
        }

    out = Path(args.out) if args.out else REPO / "data" / "envelopes" / f"{args.source}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(text, ensure_ascii=False, indent=1), encoding="utf-8")

    print(
        f"\n包络表: {args.source}（{args.windows or 'windows.jsonl'}）"
        f"\n  通道 {len(env)} 个；参考窗共 {n_ref_windows}；参考段无法定界的组 {n_skipped}"
        f"\n  margin {args.margin}（= 中位数 ± {args.margin} × 1.4826×MAD，读数级）"
        f"\n  留出集自检: 被拒组 {n_rejected}"
        f"（阈值 {args.max_holdout_violation:.1%}；被拒 = 边界在参考段之外会大面积越界，"
        f"说明这份数据不满足稳态假设 → 宁可不判）"
    )
    if rejected:
        worst = sorted(
            ((f"{dt}/{ch}", max(vs)) for (dt, ch), vs in rejected.items()),
            key=lambda t: -t[1],
        )[:3]
        print(
            "    被拒最严重的通道: "
            + "；".join(f"{name} {rate:.1%}" for name, rate in worst)
        )
    if n_refs:
        below = sum(1 for n in n_refs if n < 15)
        print(
            f"  n_ref 分布: 最小 {n_refs[0]} / 中位 {n_refs[len(n_refs) // 2]} / 最大 {n_refs[-1]}"
            f"；低于 15 的通道 {below} 个（这些通道判据仍记**未评**）"
        )
    print(f"  报告: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
