"""F1：把真实轨评测结果预计算成一份**前端可直接消费**的 JSON。

为什么要预计算而不是让前端现算：
- 三个数据集的真实轨评测（10 万+ 窗 × 5 算子 × 两种尺度）在本机要跑分钟级，
  前端每刷新一次都重算是不可接受的；
- 更重要的：**口径必须只有一处**。前端只做呈现，不做统计——否则「页面上的
  数字」和「报告里的数字」迟早对不上（本项目已因口径分裂吃过一次亏）。

产出 `data/reports/real_sensor_interactive.json`，含六块：
1. `meta`：口径声明（三条诚实声明 + 未评语义）；
2. `datasets[].operators`：每算子「旧判据(pooled) vs 新判据(mad)」的并排读数；
3. `datasets[].channels`：**窗级时间线**（每通道的时间戳 + 标签 + 丢弃位 + 未评位数）
   —— **不存读数数组**（体积控制；读数明细走 `kills` 抽检清单）；
4. `datasets[].kills`：误杀清单（人工抽检入口，页面可直接筛/导出）；
5. `datasets[].curves`：召回-误杀曲线（尺度 × 阈值网格，来自 `real_*_r4grid.json`）；
6. `datasets[].applicability` / `reachability`：判据适用性与可达性（「没评 ≠ 通过」）。

用法：
    python -X utf8 scripts/build_real_interactive.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "curation-eval" / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import eval_real_sensor as ers  # noqa: E402
from mm_curation.pipeline import PipelineConfig  # noqa: E402

REPORTS = REPO / "data" / "reports"
OUT = REPORTS / "real_sensor_interactive.json"

CONFIG_OLD = "configs/funnel_industrial.yaml"       # pooled 尺度（合成门禁档）
CONFIG_NEW = "configs/funnel_industrial_real.yaml"  # mad 尺度（真实数据档）

DATASETS: list[dict] = [
    {
        "id": "metropt3",
        "label": "MetroPT-3",
        "blurb": "空气压缩机（UCI 791）· 7 通道 · 12 秒/行 · 官方故障表 + 检修记录",
        "windows": None,
        "grid": "real_metropt3_r4grid.json",
    },
    {
        "id": "cmapss",
        "label": "C-MAPSS FD001",
        "blurb": "涡扇发动机退化仿真（NASA）· 21 传感器 · 一周期一小时 · 末段标 degraded",
        "windows": None,
        "grid": "real_cmapss_r4grid.json",
    },
    {
        "id": "skab_w64",
        "label": "SKAB（64 点窗）",
        "blurb": "水泵试验台（waico/SKAB）· 8 传感器 · 窗宽 64 的严格档",
        "windows": "data/raw/real/skab/windows_w64.jsonl",
        "grid": "real_skab_w64_r4grid.json",
    },
]

HONESTY_NOTE = [
    "误杀率是**上界**：真实数据集的标签只标「是不是故障」，不标「这窗能不能用」。"
    "被丢弃但未标注的窗里混着真实缺陷（MetroPT-3 的 7 通道同步冻结 1337 条就是），"
    "也混着我们判断合理的不可用窗。",
    "真实轨与合成轨**不可横向比数**：合成轨的 ground truth 是注入器给的、一条脏样本一个主靶；"
    "真实轨是数据集自带的窗级弱标签。",
    "本节数字**不进** docs/claims.json：它是适用性证据，不是能力声明。",
]

UNSCORED_NOTE = (
    "「未评」（score=None）既不是通过也不是失败——是该算子在这份数据上**没干活**。"
    "本页把三种状态分开显示：通过 / 丢弃 / 未评。"
)


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def _verdict_arrays(specs, samples) -> dict:
    """跑一遍配置，把每个样本的「是否被任一算子丢弃」与「未评数」记下来。

    必须在**同一批 Sample 对象**上跑：算子把分数写进 `sample.meta`，
    这也是框架的口径（`run_operator` 不复制样本）。**每套配置只跑一遍**，
    丢弃清单留着复用（抽检导出要它）。
    """
    results, dropped_all, dirty_totals, n_clean = ers.evaluate_independently(specs, samples)
    dropped_ids = {s.id for group in dropped_all for s in group}
    unscored = {
        s.id: sum(1 for sp in specs if s.meta.get(f"score:{sp.op}") is None)
        for s in samples
    }
    return {
        "results": results,
        "dropped_all": dropped_all,
        "dropped_ids": dropped_ids,
        "unscored": unscored,
        "dirty_totals": dirty_totals,
        "n_clean": n_clean,
    }


def _op_forms(samples, specs) -> dict[str, dict[str, int]]:
    """每个算子的**判据代号分布**（证据里为什么被判成非通过/未评）。"""
    forms: dict[str, dict[str, int]] = {}
    for sp in specs:
        counter: dict[str, int] = {}
        for s in samples:
            ev = s.meta.get(f"evidence:{sp.op}")
            if isinstance(ev, dict) and ev.get("rule"):
                counter[ev["rule"]] = counter.get(ev["rule"], 0) + 1
        forms[sp.op] = dict(sorted(counter.items()))
    return forms


def _op_rows(result, results_pooled, samples, spec, n_clean) -> dict:
    unscored = ers.unscored_stats(samples, spec.op)
    return {
        "op": result.op,
        "n_in": result.n_in,
        "n_dropped": result.n_dropped,
        "clean_killed": result.clean_killed,
        "kill_rate": result.clean_killed / n_clean if n_clean else None,
        "dirty_caught": dict(result.dirty_caught),
        "n_dirty_caught": sum(result.dirty_caught.values()),
        "n_unscored": unscored["n_unscored_windows"],
        "unscored_rate": unscored["unscored_rate"],
        "n_channels_fully_unscored": unscored["n_channels_fully_unscored"],
        "example_fully_unscored": unscored["example_fully_unscored"],
        "primary_target": list(result.primary_target),
        "old": {
            "n_dropped": results_pooled[result.op].n_dropped,
            "clean_killed": results_pooled[result.op].clean_killed,
            "kill_rate": (
                results_pooled[result.op].clean_killed / n_clean if n_clean else None
            ),
            "n_dirty_caught": sum(results_pooled[result.op].dirty_caught.values()),
        },
    }


def build_dataset(entry: dict) -> dict:
    samples = ers.load_samples(entry["id"].replace("_w64", ""), entry["windows"])
    cfg_old = PipelineConfig.from_yaml(CONFIG_OLD)
    cfg_new = PipelineConfig.from_yaml(CONFIG_NEW)

    # 顺序有讲究：算子把分数写进 `sample.meta`，**后跑的会覆盖先跑的**。
    # 所以先跑旧档（只取快照：丢弃集合 + OperatorPR），再跑新档，
    # 这样后面所有「读 meta」的统计（未评统计 / 判据形态）都是新档口径。
    old = _verdict_arrays(cfg_old.operators, samples)
    new = _verdict_arrays(cfg_new.operators, samples)
    by_id = {s.id: s for s in samples}

    # --- 窗级时间线（每通道：时间戳 / 标签 / 丢弃位 / 未评位数）---
    label_names: dict[str, int] = {}
    channels: dict[str, dict] = {}
    for s in ers.reading_windows(samples):
        p = ers._payload_of(s)
        ch = p.get("channel") or "—"
        blob = channels.setdefault(ch, {"t": [], "lab": [], "d": [], "d0": [], "u": []})
        kind = s.labels.get("dirty") if s.labels else None
        if kind and kind not in label_names:
            label_names[kind] = len(label_names) + 1
        blob["t"].append(_epoch(p["window_start"]))
        blob["lab"].append(label_names.get(kind, 0) if kind else 0)
        blob["d"].append(1 if s.id in new["dropped_ids"] else 0)
        blob["d0"].append(1 if s.id in old["dropped_ids"] else 0)
        blob["u"].append(new["unscored"][s.id])
    for ch, blob in channels.items():
        order = sorted(range(len(blob["t"])), key=lambda i: blob["t"][i])
        for k in ("t", "lab", "d", "d0", "u"):
            blob[k] = [blob[k][i] for i in order]

    # --- 误杀清单（页面可筛可导出）---
    kill_rows = ers.build_kill_rows(cfg_new.operators, new["dropped_all"])
    kills = [
        {
            "op": r["op"],
            "device": r["device_id"],
            "channel": r["channel"],
            "t": _epoch(r["window_start"]) if r["window_start"] else None,
            "rule": r["evidence_rule"],
            "label": r["dataset_label"],
            "reading_min": r["reading_min"],
            "reading_max": r["reading_max"],
            "reading_std": r["reading_std"],
        }
        for r in kill_rows
    ]

    # --- 召回-误杀曲线（尺度 × 阈值网格，复用已算好的报告）---
    curves: dict[str, list[dict]] = {}
    gpath = REPORTS / entry["grid"]
    if gpath.exists():
        grid = json.loads(gpath.read_text(encoding="utf-8"))
        for g in grid.get("grids", []):
            pts = []
            for row in g["rows"]:
                pts.append(
                    {
                        "label": " · ".join(f"{k}={v}" for k, v in row["params"].items()
                                             if k not in {"min"}),
                        "recall": row["recall"],
                        "kill_rate": row["kill_rate"],
                        "n_dropped": row["n_dropped"],
                        "params": row["params"],
                    }
                )
            curves[g["op"]] = sorted(pts, key=lambda p: (p["recall"], -p["kill_rate"]))

    forms = _op_forms(samples, cfg_new.operators)
    ops = [
        _op_rows(r, {x.op: x for x in old["results"]}, samples, spec, new["n_clean"])
        for r, spec in zip(new["results"], cfg_new.operators, strict=True)
    ]
    for row in ops:
        row["forms"] = forms.get(row["op"], {})

    return {
        **{k: entry[k] for k in ("id", "label", "blurb")},
        "n_total": len(samples),
        "n_clean": new["n_clean"],
        "n_dirty": sum(new["dirty_totals"].values()),
        "dirty_kinds": new["dirty_totals"],
        "label_names": label_names,
        "config_synth": CONFIG_OLD,
        "config_synth_name": cfg_old.name,
        "config_real": CONFIG_NEW,
        "config_real_name": cfg_new.name,
        "operators": ops,
        "funnel": {
            "n_kept": len(samples) - len(new["dropped_ids"]),
            "n_dropped": len(new["dropped_ids"]),
            "n_clean_killed": sum(
                1 for i in new["dropped_ids"] if not by_id[i].labels
            ),
            "n_dirty_caught": sum(
                1 for i in new["dropped_ids"] if by_id[i].labels
            ),
            "old_n_dropped": len(old["dropped_ids"]),
        },
        "channels": channels,
        "kills": kills,
        "curves": curves,
        "applicability": ers.judge_applicability(samples),
        "reachability": ers.judge_reachability(samples, cfg_new),
    }


def main() -> int:
    report = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": "真实工业数据清洗效果 · 交互式面板",
            "honesty_note": HONESTY_NOTE,
            "unscored_note": UNSCORED_NOTE,
            "config_synth": CONFIG_OLD,
            "config_real": CONFIG_NEW,
            # 窗级数组的字段说明。**曾经这里写的是 `state_code`，而那是错的**：
            # `{"0":"未评","1":"通过","2":"丢弃"}` 与实际编码不符——`d`/`d0` 只有 0/1
            # （通过/丢弃）两个取值，「未评」根本不在两个数组里，而是 `u`（未评算子数）。
            # 声明一个与数据编码打架的映射 = 「假精确」，且没有任何消费者读它，
            # 纯属给下一个人埋坑。改成真话，并让字段自解释。
            "columns": {
                "t": "窗口起始时间（epoch 秒）",
                "lab": "数据集自带脏标签码：0 = 无标注，其余见 label_names",
                "d": "新档裁决：0 = 通过，1 = 被任一算子丢弃",
                "d0": "旧档（合成判据）裁决：同上编码",
                "u": "该窗「未评算子数」= score is None 的算子个数（未评既非通过也非失败）",
            },
        },
        "datasets": [],
    }
    for entry in DATASETS:
        d = build_dataset(entry)
        report["datasets"].append(d)
        print(
            f"[{d['id']}] {d['n_total']} 条（干净 {d['n_clean']} / 脏 {d['n_dirty']}）"
            f"  通道 {sorted(d['channels'])}  误杀清单 {len(d['kills'])} 行"
            f"  曲线 {list(d['curves'])}"
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"\n产出: {OUT}（{OUT.stat().st_size / 1e6:.2f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
