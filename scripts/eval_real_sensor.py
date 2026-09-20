"""真实工业数据评测（V5 β 真实数据轨 + P2 修复的度量器）。

与 `eval_industrial.py` 的区别——**ground truth 的来源不同**：
- 合成轨：污染器注入 → 每条脏样本有主靶算子（P/R 可算，门禁绿）；
- 真实轨（本脚本）：数据集**自带**标签（SKAB 逐点 anomaly / MetroPT-3 官方失败表 /
  C-MAPSS 运行到失效），标签粒度与语义都与合成不同 → **不套用合成门限判合格**，
  只如实报数，并把差异写进报告。

口径：
- 算子级独立评测——每个算子在全量真实语料上单独跑一遍；
- 漏斗串联（`run_funnel`）——端到端存活率与拦截构成；
- 误杀率分母 = 真实「干净」窗（数据集标为非异常/非故障的窗）。

P2 修复后新增的三项能力（`docs/design_tables.md`「P2 补表」R6）——它们是三条验收
指标的**度量器**，必须先有，否则「判据改了但没法验收」：

1. `--export-kills`：误杀清单导出（通道 + 时间 + 读数极值 + 分数 + 判据代号）。
   「误杀率只是上界」这句话原先只能靠临时脚本查证，现在固化成可一键交人工裁决的表。
2. `--sweep`：阈值扫描。把「调参无解」从一次性探针变成**可重复的曲线**。
   分数与阈值无关，所以扫描只重算「按这条门限会丢哪些」，不重跑算子。
3. **未评统计**：每个算子有多少窗是 `None`（无法计分）、哪些通道整条未评。
   这是「把『没评』伪装成『通过』」这个失效模式的常驻可见性。

用法：
    python -X utf8 scripts/eval_real_sensor.py --source skab
    python -X utf8 scripts/eval_real_sensor.py --source metropt3 \\
        --export-kills data/reports/real_metropt3_kills.csv
    python -X utf8 scripts/eval_real_sensor.py --source cmapss \\
        --sweep sensor_drift --sweep-side min --set scale=mad
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "curation-eval" / "src"))

from mm_curation.eval.operator_pr import (  # noqa: E402
    OPERATOR_TARGETS,
    OperatorPR,
    run_operator,
)
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline import OperatorSpec, PipelineConfig, run_funnel  # noqa: E402

REAL = REPO / "data" / "raw" / "real"

HONESTY_NOTE = [
    "> **三条口径声明（不可省略）**：",
    "> 1. **误杀率是上界**：真实数据**无标签 ≠ 干净**。MetroPT-3 有一段约 163 小时的",
    ">    7 通道同步恒定被官方标签完全漏掉（`sensor_stuck` 抓到了它，却被算作误杀）",
    ">    —— 所以真实轨的误杀数**必须**配 `--export-kills` 的人工抽检才能定论。",
    "> 2. **与合成轨不可横向对比**：合成注入率 30%，真实故障占比 1.97%（差 15 倍）；",
    ">    合成标签是「一条脏样本一个主靶」，真实标签是窗级弱标签。",
    "> 3. **本报告的数字不进 `claims.json`**：真实轨判据仍在演进，未到可锁定状态。",
    "",
    "> 阅读纪律：**未评（`None`）不是通过**。报告里「未评」一栏若很大，说明该算子",
    "> 在这份数据上基本没干活——那是一个**空转**信号，不是一个好数字。",
]


def load_samples(source: str, windows: str | None = None) -> list[Sample]:
    path = Path(windows) if windows else REAL / source / "windows.jsonl"
    if not path.exists():
        raise SystemExit(
            f"未找到 {path}。先跑：\n"
            f"  python -X utf8 scripts/ingest_real_sensor.py --source {source}"
        )
    raw_lines = path.read_text(encoding="utf-8").split("\n")
    rows = [json.loads(line) for line in raw_lines if line.strip()]
    return [Sample.from_dict(r) for r in rows]


def _is_reading(sample: Sample) -> bool:
    return sample.meta.get("sensor_record_type") == "reading_window"


def reading_windows(samples: list[Sample]) -> list[Sample]:
    """读数窗（事件样本不参与算子级 P/R 的分母讨论）。"""
    return [s for s in samples if _is_reading(s)]


def _payload_of(sample: Sample) -> dict:
    try:
        return json.loads(sample.text)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}


def _window_stats(payload: dict) -> dict:
    """窗内读数极值/均值/标准差——人工抽检要看的量，读不出就给空。"""
    readings = payload.get("readings")
    if not isinstance(readings, list) or not readings:
        return {}
    n = len(readings)
    mean = sum(readings) / n
    var = sum((v - mean) ** 2 for v in readings) / n
    return {
        "n_readings": n,
        "reading_min": min(readings),
        "reading_max": max(readings),
        "reading_mean": mean,
        "reading_std": max(0.0, var) ** 0.5,
    }


def evaluate_independently(
    specs: list[OperatorSpec], samples: list[Sample]
) -> tuple[list[OperatorPR], list[list[Sample]], dict[str, int], int]:
    """每个算子独立跑一遍，同时把**丢弃清单**留下来（抽检与扫描都要它）。

    与 `evaluate_all` 的区别只有一条：不丢中间结果。算子只跑一次，
    抽检导出 / 阈值扫描 / 未评统计全部复用这一遍的 `sample.meta`。
    """
    dirty_totals: Counter = Counter(s.labels["dirty"] for s in samples if s.labels)
    n_clean = sum(1 for s in samples if not s.labels)
    results: list[OperatorPR] = []
    dropped_all: list[list[Sample]] = []
    for spec in specs:
        op = spec.build()
        _kept, dropped = run_operator(op, list(samples))
        results.append(
            OperatorPR(
                op=spec.op,
                n_in=len(samples),
                n_dropped=len(dropped),
                clean_killed=sum(1 for s in dropped if not s.labels),
                dirty_caught=Counter(
                    s.labels.get("dirty") or "clean/未标注" for s in dropped if s.labels
                ),
                primary_target=OPERATOR_TARGETS.get(spec.op, []),
            )
        )
        dropped_all.append(dropped)
    return results, dropped_all, dict(dirty_totals), n_clean


def unscored_stats(samples: list[Sample], op: str) -> dict:
    """某算子的未评统计：多少窗无法计分、哪些通道整条未评。

    「未评」= `meta["score:<op>"] is None`。它不是通过，也不是失败——
    是**这个算子在这份数据上没干活**。旧版报告把它和 1.0 混在一起，
    于是「空转」读起来像「全部通过」（P2 的 F3 失效模式）。
    """
    wins = reading_windows(samples)
    unscored = [s for s in wins if s.meta.get(f"score:{op}") is None]
    by_channel: dict[tuple[str, str], list[bool]] = {}
    for s in wins:
        p = _payload_of(s)
        key = (str(p.get("device_type", "")), str(p.get("channel", "")))
        by_channel.setdefault(key, []).append(s.meta.get(f"score:{op}") is None)
    full = sorted(k for k, flags in by_channel.items() if flags and all(flags))
    return {
        "n_windows": len(wins),
        "n_unscored_windows": len(unscored),
        "unscored_rate": len(unscored) / len(wins) if wins else None,
        "n_channels": len(by_channel),
        "n_channels_fully_unscored": len(full),
        "example_fully_unscored": [f"{d}/{c}" for d, c in full[:8]],
    }


def build_kill_rows(
    specs: list[OperatorSpec],
    dropped_all: list[list[Sample]],
) -> list[dict]:
    """误杀清单（**含全部丢弃**，不筛）——人工抽检要能自己判断谁是误杀。

    刻意不预筛「按标签算的误杀」：真实数据无标签 ≠ 干净，
    预筛会把 MetroPT-3 那 1337 条真实冻结（官方标签漏掉的真缺陷）提前扔掉。
    """
    rows: list[dict] = []
    for spec, dropped in zip(specs, dropped_all, strict=True):
        for s in dropped:
            p = _payload_of(s)
            row = {
                "op": spec.op,
                "device_id": p.get("device_id", ""),
                "channel": p.get("channel", ""),
                "device_type": p.get("device_type", ""),
                "window_start": p.get("window_start", ""),
                "window_end": p.get("window_end", ""),
                "operating_mode": p.get("operating_mode", ""),
                "score": s.meta.get(f"score:{spec.op}"),
                "dataset_label": s.labels.get("dirty", ""),
                "dataset_says_clean": "yes" if not s.labels else "",
                "evidence_rule": (s.meta.get(f"evidence:{spec.op}") or {}).get("rule", ""),
            }
            row.update(_window_stats(p))
            rows.append(row)
    return rows


def write_kills_csv(rows: list[dict], path: Path) -> None:
    """写 CSV。用 `utf-8-sig`：这份表是给人/Excel 看的，不是给程序读的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "op", "device_id", "channel", "device_type", "window_start", "window_end",
        "operating_mode", "score", "dataset_label", "dataset_says_clean", "evidence_rule",
        "n_readings", "reading_min", "reading_max", "reading_mean", "reading_std",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sweep_threshold(
    op_name: str,
    samples: list[Sample],
    *,
    params: dict,
    side: str,
    points: int,
) -> dict | None:
    """阈值扫描：给定门限，重算「会丢哪些」→ 召回 / 误杀 曲线。

    分数与门限无关（算子把分数写进 meta，`keep()` 才看 min/max），所以扫描
    **不重跑算子**，只按门限重算丢弃集——这也是曲线可重复的原因。
    扫描的分母强制为「可计分的窗」：`None` 既不参与召回也不参与误杀。
    """
    spec = OperatorSpec(op=op_name, params=params)
    op = spec.build()
    run_operator(op, list(samples))  # 副作用即写入 score:<op>
    pairs: list[tuple[float, bool]] = []
    for s in reading_windows(samples):
        v = s.meta.get(f"score:{op_name}")
        if v is not None:
            pairs.append((float(v), bool(s.labels)))
    if not pairs:
        return None
    n_dirty = sum(1 for _v, dirty in pairs if dirty)
    n_clean = len(pairs) - n_dirty
    vals = sorted({v for v, _ in pairs})
    if len(vals) <= 2:
        thresholds = vals  # 二值分数：两档就够，插值只会造出假精度
    else:
        lo, hi = vals[0], vals[-1]
        thresholds = [lo + (hi - lo) * i / (points - 1) for i in range(points)]
    rows = []
    for thr in thresholds:
        if side == "min":
            dropped = [(v, d) for v, d in pairs if v < thr]
        else:
            dropped = [(v, d) for v, d in pairs if v > thr]
        caught = sum(1 for _v, d in dropped if d)
        killed = sum(1 for _v, d in dropped if not d)
        rows.append(
            {
                "threshold": thr,
                "n_dropped": len(dropped),
                "dirty_caught": caught,
                "clean_killed": killed,
                "recall": caught / n_dirty if n_dirty else None,
                "kill_rate": killed / n_clean if n_clean else None,
            }
        )
    return {
        "op": op_name,
        "side": side,
        "params": params,
        "n_scored": len(pairs),
        "n_dirty": n_dirty,
        "n_clean": n_clean,
        "score_min": vals[0],
        "score_max": vals[-1],
        "curve": rows,
    }


def grid_scan(
    op_name: str,
    samples: list[Sample],
    *,
    base: dict,
    axes: dict[str, list],
) -> list[dict]:
    """算子**参数**网格扫描（与阈值扫描是两件事，别混）。

    阈值扫描：参数固定，只动门限 → 回答「这条曲线是不是只是平移」。
    参数网格：动**判据本身的参数**（如 stuck 的 α / k）→ 回答「判据参数怎么定的」。
    两者都必须出数，否则「阈值有依据」就是空话（本项目对业界的三处差异化之一）。

    成本提醒：参数改了分数就变了，**必须重跑算子**——所以网格要小
    （2D × 3 档 = 9 次），别拿它扫连续域。
    """
    import itertools

    if "min" not in base and "max" not in base:
        # fail-fast：没有门限时 keep() 恒真 → 全部 0，且看起来像「参数无影响」
        raise SystemExit(f"网格扫描 {op_name} 缺少 min/max 门限，结果会恒为 0（见 grid_scan 注释）")
    keys = list(axes)
    rows: list[dict] = []
    wins = reading_windows(samples)
    n_dirty = sum(1 for s in wins if s.labels)
    n_clean = len(wins) - n_dirty
    for combo in itertools.product(*(axes[k] for k in keys)):
        params = {**base, **dict(zip(keys, combo, strict=True))}
        op = OperatorSpec(op=op_name, params=params).build()
        _kept, dropped = run_operator(op, list(samples))
        dropped_wins = [s for s in dropped if _is_reading(s)]
        caught = sum(1 for s in dropped_wins if s.labels)
        killed = len(dropped_wins) - caught
        rows.append(
            {
                "params": params,
                "n_dropped": len(dropped_wins),
                "dirty_caught": caught,
                "clean_killed": killed,
                "recall": caught / n_dirty if n_dirty else None,
                "kill_rate": killed / n_clean if n_clean else None,
            }
        )
    return rows


def render_grid(op_name: str, rows: list[dict]) -> list[str]:
    """参数网格的表体（标题由调用方加）。"""
    if not rows:
        return []
    keys = sorted({k for r in rows for k in r["params"]})
    out = [
        "| " + " | ".join(keys) + " | 扔 | 命中真脏 | 误杀 | 召回 | 误杀率 |",
        "|" + "---|" * (len(keys) + 6),
    ]
    for r in rows:
        cells = " | ".join(str(r["params"].get(k, "")) for k in keys)
        out.append(
            f"| {cells} | {r['n_dropped']} | {r['dirty_caught']} | {r['clean_killed']} | "
            f"{_fmt_pct(r['recall'])} | {_fmt_pct(r['kill_rate'])} |"
        )
    out.append("")
    return out


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1%}"


def render_md(
    source: str,
    config_name: str,
    samples: list[Sample],
    results: list[OperatorPR],
    dirty_totals: dict[str, int],
    n_clean: int,
    funnel,
    unscored: dict[str, dict],
    kills_path: Path | None,
    kill_rows: list[dict],
    sweeps: list[dict],
    grids: list[tuple[str, list[dict]]],
    reach: list[dict],
    applic: list[dict],
) -> str:
    reading = reading_windows(samples)
    events = [s for s in samples if s.meta.get("sensor_record_type") == "maintenance_event"]
    n_dirty = sum(dirty_totals.values())
    lines = [
        f"# 真实数据评测：{source}（工业模态算子）",
        "",
        "> 与合成轨的关键差异：**ground truth 来自数据集自带标签**（粒度=窗级弱标签），",
        "> 与污染器注入的「一条脏样本一个主靶」不同，因此**不套用合成门限判合格**。",
        "> 本报告只回答一个问题：**合成语料上标定的算子与阈值，迁到真实数据后还剩多少。**",
        "",
        *HONESTY_NOTE,
        "",
        f"流水线配置：`{config_name}`",
        "",
        "## 语料",
        "",
        f"- 样本总数 **{len(samples)}**（读数窗 {len(reading)} + 检修事件 {len(events)}）",
        f"- 真实干净窗 **{n_clean}**，真实脏窗 **{n_dirty}**：{dict(dirty_totals) or '（无）'}",
        "",
        "## 算子级独立评测（每个算子在全量真实语料上单独跑一遍）",
        "",
        "| 算子 | 扔 | 其中误杀 | 命中真实脏 | precision | 真实脏召回 | 真实干净误杀率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        prec = "—" if r.precision is None else f"{r.precision:.1%}"
        # 分母是**真实干净窗数**，不是全集：用 n_in 会把误杀按脏占比等比低估
        # （SKAB w64 实测 24.48% 曾被显示成 13.67%）。
        kill_rate = "—" if not n_clean else f"{r.clean_killed / n_clean:.2%}"
        recall = (
            f"{r.dirty_caught.get('__any__', sum(r.dirty_caught.values())) / n_dirty:.1%}"
            if n_dirty
            else "—"
        )
        lines.append(
            f"| `{r.op}` | {r.n_dropped} | {r.clean_killed} | "
            f"{sum(r.dirty_caught.values())} | {prec} | {recall} | {kill_rate} |"
        )

    lines += [
        "",
        "## 未评（无法计分）统计——**「没评」不是「通过」**",
        "",
        "| 算子 | 读数窗 | 未评窗 | 未评占比 | 通道数 | 整条未评的通道 |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        u = unscored[r.op]
        ex = ", ".join(u["example_fully_unscored"]) or "—"
        lines.append(
            f"| `{r.op}` | {u['n_windows']} | {u['n_unscored_windows']} | "
            f"{_fmt_pct(u['unscored_rate'])} | {u['n_channels']} | "
            f"{u['n_channels_fully_unscored']}（{ex}） |"
        )
    lines += [
        "",
        "> 读法：未评占比接近 100% 的算子在这份数据上**基本没干活**（判据形态在真实",
        "> 数据里不存在），它的「0 误杀」是空转而不是性能。整条未评的通道是**无信息",
        "> 通道**或**量程表外通道**——前者不该判、后者缺量程依据，两者都必须记 `None`",
        "> 而不是记 1.0。",
    ]

    lines += ["", *render_reachability(reach)]
    lines += ["", *render_applicability(applic)]

    if sweeps:
        lines += ["", "## 阈值扫描（分数与门限解耦，只重算丢弃集）", ""]
        for sw in sweeps:
            lines += [
                f"### `{sw['op']}`（方向 {sw['side']}，参数 {sw['params']}）",
                "",
                f"可计分窗 **{sw['n_scored']}**（脏 {sw['n_dirty']} / 干净 {sw['n_clean']}）；"
                f"分数域 [{sw['score_min']:.4g}, {sw['score_max']:.4g}]",
                "",
                "| 门限 | 扔 | 命中真脏 | 误杀 | 召回 | 误杀率 |",
                "|---|---|---|---|---|---|",
            ]
            for row in sw["curve"]:
                lines.append(
                    f"| {row['threshold']:.4g} | {row['n_dropped']} | {row['dirty_caught']} | "
                    f"{row['clean_killed']} | {_fmt_pct(row['recall'])} | "
                    f"{_fmt_pct(row['kill_rate'])} |"
                )
            lines.append("")

    for op_name, rows in grids:
        lines += [
            "",
            f"## 参数网格扫描 `{op_name}`（动的是**判据参数**，每次都要重跑算子）",
            "",
            "> 与阈值扫描的分工：阈值扫描回答「这条曲线是不是只是平移」，",
            "> 参数网格回答「**判据参数怎么定的**」。后者才是「阈值有依据」的落点。",
            "",
            *render_grid(op_name, rows),
        ]

    if kills_path is not None:
        n_all_kills = len(kill_rows)
        n_clean_kills = sum(1 for r in kill_rows if r["dataset_says_clean"] == "yes")
        lines += [
            "## 误杀清单（人工抽检入口）",
            "",
            f"- 导出 **{n_all_kills}** 条丢弃记录 → `{kills_path}`",
            f"- 其中数据集标为「干净」的 **{n_clean_kills}** 条（**上界**，需人工裁决）",
            "- 列含：算子 / 设备 / 通道 / 窗口起止 / 工况 / 分数 / 判据代号 / 读数极值与 σ",
            "",
            "> 抽检步骤：按 `channel` 分组看 `reading_std`，极值恒定或 σ 突降的分组",
            "> 大概率是真实缺陷（数据集标签没覆盖），不是误杀。",
        ]

    lines += [
        "",
        "## 漏斗串联（端到端）",
        "",
        f"- 存活 **{len(funnel.kept)}/{len(samples)}**"
        f"（保留率 {len(funnel.kept) / len(samples):.1%}）",
        f"- 拦截构成：真实脏 {sum(funnel_caught(funnel).values())}，"
        f"**真实干净被误杀 {funnel_clean_killed(funnel)}/{n_clean}"
        f"（{funnel_clean_killed(funnel) / n_clean if n_clean else 0:.2%}）**",
        "",
        "| 级 | 进 | 出 | 扔 | 存活率 |",
        "|---|---|---|---|---|",
    ]
    for st in funnel.stats:
        lines.append(
            f"| `{st.op}` | {st.n_in} | {st.n_out} | {st.dropped} | {st.pass_rate:.1%} |"
        )
    lines.append("")
    return "\n".join(lines)


def funnel_caught(funnel) -> Counter:
    caught: Counter = Counter()
    for _op, s in funnel.dropped:
        if s.labels:
            caught[s.labels["dirty"]] += 1
    return caught


def funnel_clean_killed(funnel) -> int:
    return sum(1 for _op, s in funnel.dropped if not s.labels)


def judge_reachability(
    samples: list[Sample], config: PipelineConfig
) -> list[dict]:
    """判据可达性：确认延迟 / 建基线类参数是否被**数据规模结构性锁死**。

    这是 P2 修复引出的一个新失效类，必须常驻可见：`sensor_stuck` 的塌陷档要
    「连续 min_run 窗」、`sensor_drift` 要「同组 >5 窗建基线」——若数据集的
    同组窗数普遍低于该门槛，这两档就**结构性不可达**：不是「没检出」，是
    「永远不可能检出」。报告只显示「0 次丢弃」，看不出是「没有卡死」还是
    「根本判不了」。

    实测：SKAB 每组仅 3~4 窗 → `min_run=10` 的塌陷档在 SKAB 上完全失效，
    drift 的基线档在窗 256 档也失效（建不出基线）。这就是 REAL_DATA_REPORT
    「SKAB 五算子全静默」的可计算解释，而不是一句断言。

    门槛从算子模块**导入**而非复制常量——单一事实源（`_BASELINE` 是私有名，
    但复制一份到脚本里迟早会对不上）。
    """
    from mm_curation.operators.industrial_quality import (
        _BASELINE as DRIFT_BASELINE,
    )

    def sizes_of(key_fields: tuple[str, ...]) -> list[int]:
        groups: dict[tuple, int] = {}
        for s in reading_windows(samples):
            p = _payload_of(s)
            groups[tuple(str(p.get(f, "")) for f in key_fields)] = (
                groups.get(tuple(str(p.get(f, "")) for f in key_fields), 0) + 1
            )
        return sorted(groups.values())

    stuck_min_run = next(
        (spec.params.get("min_run", 10) for spec in config.operators
         if spec.op == "sensor_stuck"),
        None,
    )
    checks: list[dict] = []
    for op, fields, need, desc in (
        ("sensor_stuck", ("device_id", "channel"), stuck_min_run,
         "塌陷档需**连续 N 窗**"),
        ("sensor_drift", ("device_id", "channel", "operating_mode"), DRIFT_BASELINE + 1,
         "需同组 >N 窗才能建基线"),
    ):
        if need is None:
            continue
        sz = sizes_of(fields)
        if not sz:
            continue
        too_short = sum(1 for n in sz if n < need)
        checks.append(
            {
                "op": op,
                "group_by": "×".join(fields),
                "requirement": need,
                "desc": desc,
                "n_groups": len(sz),
                "group_size_p50": sz[len(sz) // 2],
                "group_size_max": sz[-1],
                "n_groups_shorter": too_short,
                "reachable": too_short < len(sz),
                "structurally_unreachable": too_short == len(sz),
            }
        )
    return checks


def render_reachability(checks: list[dict]) -> list[str]:
    if not checks:
        return []
    out = [
        "## 判据可达性检查——**「没检出」和「判不了」必须可区分**",
        "",
        "| 算子 | 分组键 | 门槛 | 组数 | 组长度 p50 | 组长度 max | 短于门槛的组 | 结论 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in checks:
        verdict = (
            "**结构性不可达**（该档在该数据集上永不触发）"
            if c["structurally_unreachable"]
            else ("可达" if c["reachable"] and not c["n_groups_shorter"] else "部分组不可达")
        )
        out.append(
            f"| `{c['op']}` | {c['group_by']} | {c['desc'].replace('N', str(c['requirement']))} "
            f"| {c['n_groups']} | {c['group_size_p50']} | {c['group_size_max']} "
            f"| {c['n_groups_shorter']} | {verdict} |"
        )
    out.append("")
    return out


def judge_applicability(samples: list[Sample]) -> list[dict]:
    """判据**适用性**：该判据的「目标形态」在这个数据集里存在吗？

    可达性检查回答「门槛够不够」，适用性检查回答「靶子有没有」——两者是
    「**没检出 ≠ 没问题**」这一句话的两个分母，必须都常驻可见。

    P2 的 F1 失效模式（空转）正是后者：`fault_vs_maintenance` 判的是「全哨兵
    静默窗」，而三个公开真实数据集里**一个哨兵窗都没有** → 算子恒过 → 报告
    显示「0 误杀 / 通过」，读者会理解成「静默检查通过了」，实际是**一次都没评过**。
    同理 `unit_consistency`：真实数据集的单位是文档/采集脚本统一填的，同
    `(device_type, channel)` 组内只有一种单位 → 判据**没有区分度**。

    判据与阈值都从算子模块**导入**，不在这里复制一份口径。
    """
    from mm_curation.data.sensor_synth import SENTINEL as SENT
    from mm_curation.operators.industrial_quality import (
        _GAP_RATIO as GAP_RATIO,
    )
    from mm_curation.operators.industrial_quality import (
        _device_stop_keys,
        _span_ratio,
    )

    wins = reading_windows(samples)
    payloads = [_payload_of(s) for s in wins]
    n = len(payloads)
    if not n:
        return []

    parsed = [(s, _payload_of(s)) for s in wins]
    stops = _device_stop_keys(parsed)

    n_sentinel = sum(1 for p in payloads if all(v == SENT for v in p["readings"]))
    n_flat = sum(1 for p in payloads if max(p["readings"]) - min(p["readings"]) == 0.0)
    n_span = sum(
        1 for p in payloads
        if (_span_ratio(p) or 0.0) > GAP_RATIO
    )
    unit_groups: dict[tuple, set] = {}
    for p in payloads:
        unit_groups.setdefault((p["device_type"], p["channel"]), set()).add(p["unit"])
    n_unit_groups = len(unit_groups)
    n_multi_unit = sum(1 for u in unit_groups.values() if len(u) > 1)

    return [
        {
            "op": "fault_vs_maintenance",
            "target": "全哨兵静默窗（形态 ①）",
            "n_target": n_sentinel,
            "n_windows": n,
            "applicable": n_sentinel > 0,
            "hint": (
                f"真实数据无哨兵形态 → 该档在本数据集**无适用性**；"
                f"另两种真实形态：设备级停机 {len(stops)} 窗、跨度缺口 {n_span} 窗"
            ),
        },
        {
            "op": "unit_consistency",
            "target": "同 (device_type, channel) 出现 >1 种单位",
            "n_target": n_multi_unit,
            "n_windows": n_unit_groups,
            "applicable": n_multi_unit > 0,
            "hint": (
                f"{n_unit_groups} 个单位组中 {n_multi_unit} 组有多单位 → "
                "该数据集单位由采集侧统一填写，判据**无区分度**（恒过不是「通过」）"
            ),
        },
        {
            "op": "sensor_stuck",
            "target": "严格平坦窗（极差 == 0）",
            "n_target": n_flat,
            "n_windows": n,
            "applicable": n_flat > 0,
            "hint": (
                f"严格平坦 {n_flat} 窗（其中设备级停机 {len(stops)} 窗 → 记 None，"
                "判据不适用）"
            ),
        },
    ]


def render_applicability(checks: list[dict]) -> list[str]:
    if not checks:
        return []
    out = [
        "## 判据适用性检查——**「恒过」不等于「通过」**",
        "",
        "「目标形态在本数据集存在吗」与「门槛够不够」（上一节）是同一句话的两个分母：",
        "形态不存在 → 算子**一次都没评过**，此时报「0 误杀」会被误读成「检查通过了」。",
        "",
        "| 算子 | 目标形态 | 命中数 | 分母 | 结论 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for c in checks:
        verdict = "适用" if c["applicable"] else "**无适用性（空转）**"
        out.append(
            f"| `{c['op']}` | {c['target']} | {c['n_target']} | {c['n_windows']} "
            f"| {verdict} | {c['hint']} |"
        )
    out.append("")
    return out


def parse_overrides(items: list[str]) -> dict:
    """`--set key=value`：值按 JSON 解析，解析失败按字符串。"""
    out: dict = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--set 需要 key=value 形式，得到 {item!r}")
        key, _, raw = item.partition("=")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def config_params(config: PipelineConfig, op_name: str) -> dict:
    """取 config 里该算子的参数（含 `min`/`max` 门限）。

    **必须继承**：算子只实现「打分」，丢弃与否由 `Operator.keep()` 按 `min`/`max`
    决定。重建算子时若丢了 `min`，`keep()` 恒真 → 扫描结果全 0，而且看起来
    像「这个参数没影响」——一个安静的错误（踩过一次）。
    """
    for spec in config.operators:
        if spec.op == op_name:
            return dict(spec.params)
    raise SystemExit(f"--grid/--sweep 的算子 {op_name!r} 不在 config 的算子链里")


def parse_axes(items: list[str]) -> dict[str, list]:
    """`--grid-axis alpha=0.01,0.05` → `{"alpha": [0.01, 0.05]}`（值按 JSON 解析）。"""
    axes: dict[str, list] = {}
    for item in items:
        key, _, raw = item.partition("=")
        if not key or not raw:
            raise SystemExit(f"--grid-axis 需要 k=v1,v2 形式，得到 {item!r}")
        vals = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                vals.append(json.loads(chunk))
            except json.JSONDecodeError:
                vals.append(chunk)
        if vals:
            axes[key] = vals
    return axes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=["skab", "metropt3", "cmapss"])
    parser.add_argument(
        "--windows", default=None,
        help="显式指定 windows.jsonl（默认 data/raw/real/<source>/windows.jsonl）；"
             "用于同一数据集的另一档窗宽，如 skab/windows_w64.jsonl",
    )
    parser.add_argument(
        "--config",
        default="configs/funnel_industrial_real.yaml",
        help="真实数据档配置（sensor_drift 用 MAD 尺度）。传 "
             "configs/funnel_industrial.yaml 可跑合成门禁档的 pooled 尺度做对照",
    )
    parser.add_argument("--out", default=None)
    parser.add_argument("--export-kills", default=None, help="误杀清单 CSV 输出路径")
    parser.add_argument("--sweep", action="append", default=[], help="阈值扫描的算子名（可多次）")
    parser.add_argument(
        "--sweep-side", default="min", choices=["min", "max"], help="扫描方向（默认 min）"
    )
    parser.add_argument("--sweep-points", type=int, default=21, help="扫描点数（非二值分数时）")
    parser.add_argument("--set", action="append", default=[], help="传给被扫描算子的参数 k=v")
    parser.add_argument("--grid", default=None, help="参数网格扫描的算子名")
    parser.add_argument(
        "--grid-axis", action="append", default=[],
        help="网格轴 k=v1,v2,v3（可多次；值按 JSON 解析）。如 --grid-axis alpha=0.01,0.05",
    )
    args = parser.parse_args()

    config = PipelineConfig.from_yaml(args.config)
    samples = load_samples(args.source, args.windows)
    results, dropped_all, dirty_totals, n_clean = evaluate_independently(
        config.operators, samples
    )
    funnel = run_funnel(samples, config)
    unscored = {r.op: unscored_stats(samples, r.op) for r in results}
    reach = judge_reachability(samples, config)
    applic = judge_applicability(samples)
    kill_rows = build_kill_rows(config.operators, dropped_all)
    kills_path = Path(args.export_kills) if args.export_kills else None
    if kills_path is not None:
        write_kills_csv(kill_rows, kills_path)
    sweeps = [
        sw
        for name in args.sweep
        if (sw := sweep_threshold(
            name, samples,
            params={**config_params(config, name), **parse_overrides(args.set)},
            side=args.sweep_side, points=args.sweep_points,
        ))
    ]
    grids: list[tuple[str, list[dict]]] = []
    if args.grid:
        axes = parse_axes(args.grid_axis)
        if not axes:
            raise SystemExit("--grid 需要至少一个 --grid-axis k=v1,v2")
        grids.append(
            (args.grid, grid_scan(
                args.grid, samples,
                base={**config_params(config, args.grid), **parse_overrides(args.set)},
                axes=axes,
            ))
        )

    lines = render_md(
        args.source, config.name, samples, results, dirty_totals, n_clean,
        funnel, unscored, kills_path, kill_rows, sweeps, grids, reach, applic,
    )

    n_dirty = sum(dirty_totals.values())
    out = Path(args.out) if args.out else REPO / "data" / "reports" / f"real_{args.source}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "source": args.source,
        "pipeline": config.name,
        "n_total": len(samples),
        "n_clean": n_clean,
        "n_dirty": n_dirty,
        "dirty_totals": dirty_totals,
        "eval_scope": "算子级独立口径 + 漏斗串联口径；ground truth 为数据集自带标签",
        "operators": [r.to_dict(dirty_totals, n_clean) for r in results],
        "unscored": unscored,
        "judge_reachability": reach,
        "judge_applicability": applic,
        "sweeps": sweeps,
        "grids": [{"op": name, "rows": rows} for name, rows in grids],
        "kills_csv": str(kills_path) if kills_path else None,
        "n_kills_rows": len(kill_rows),
        "funnel": {
            "n_kept": len(funnel.kept),
            "n_dropped": len(funnel.dropped),
            "caught_by_kind": dict(funnel_caught(funnel)),
            "clean_killed": funnel_clean_killed(funnel),
            "stats": [
                {"op": st.op, "n_in": st.n_in, "n_out": st.n_out, "dropped": st.dropped}
                for st in funnel.stats
            ],
        },
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(lines + "\n", encoding="utf-8")

    print(f"\n真实数据评测: {args.source}（{len(samples)} 条：干净 {n_clean} / 脏 {n_dirty}）")
    print(f"{'算子':<24}{'扔':>7}{'误杀':>7}{'命中真脏':>9}{'未评窗':>9}{'误杀率':>9}")
    for r in results:
        kr = "—" if not n_clean else f"{r.clean_killed / n_clean:.2%}"
        print(
            f"{r.op:<24}{r.n_dropped:>7}{r.clean_killed:>7}"
            f"{sum(r.dirty_caught.values()):>9}"
            f"{unscored[r.op]['n_unscored_windows']:>9}{kr:>9}"
        )
    print(
        f"\n漏斗: {len(samples)} → {len(funnel.kept)}"
        f"（保留 {len(funnel.kept) / len(samples):.1%}）；"
        f"真实脏命中 {sum(funnel_caught(funnel).values())}/{n_dirty}，"
        f"真实干净误杀 {funnel_clean_killed(funnel)}/{n_clean}"
    )
    if kills_path is not None:
        print(f"误杀清单: {kills_path}（{len(kill_rows)} 行）")
    for sw in sweeps:
        print(f"扫描 {sw['op']}（{sw['side']}）：{len(sw['curve'])} 个点 → 见报告")
    print(f"报告: {out} (+ .md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
