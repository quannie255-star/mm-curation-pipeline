"""阈值敏感性曲线扫描（design 2.3，Week3 D4）。

对带阈值的算子扫一段阈值区间，逐点记录「主靶 recall」与「干净误杀率」，
定位决策拐点——这是 configs/ 里阈值「有依据」的可视化版本（而非拍脑袋）。

用法：
    python scripts/threshold_scan.py                  # 全部阈值算子，默认区间
    python scripts/threshold_scan.py --out data/reports/threshold_scan.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.eval.operator_pr import OPERATOR_TARGETS, evaluate_operator  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline import OperatorSpec  # noqa: E402

# 算子 -> (阈值参数名, 扫描值列表, 当前生产默认)。
# 区间按 ROADMAP 校准记录的真实数据分布选取，覆盖「太松」到「太紧」。
THRESHOLD_SPECS: dict[str, tuple[str, list[float], float]] = {
    "phash_near": ("threshold", [4, 6, 8, 10, 12, 14, 16, 20], 12),
    "minhash_lsh": ("threshold", [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.85], 0.65),
    "clip_alignment": ("min", [0.20, 0.25, 0.30, 0.35, 0.38, 0.42, 0.46, 0.50], 0.38),
    "semantic_dedup": ("threshold", [0.85, 0.88, 0.90, 0.92, 0.93, 0.95, 0.97, 0.99], 0.93),
    "blur": ("min", [5, 8, 10, 12, 15, 20, 25, 30], 12),
}


@dataclass
class ScanPoint:
    op: str
    threshold: float
    n_dropped: int
    clean_killed: int
    primary_recall: float | None
    clean_kill_rate: float | None


def scan_operator(op_name: str, samples: list[Sample]) -> tuple[list[ScanPoint], list[str]]:
    param_name, values, default = THRESHOLD_SPECS[op_name]
    targets = OPERATOR_TARGETS.get(op_name, [])
    # 各主靶脏类型在全集中的总数（recall 的分母）
    from collections import Counter

    dirty_totals = Counter(s.labels["dirty"] for s in samples if s.labels)

    points: list[ScanPoint] = []
    for v in values:
        spec = OperatorSpec(op=op_name, params={param_name: v})
        r = evaluate_operator(spec, samples)
        # 主靶 recall：取各主靶 recall 的平均（多数算子只有一个主靶）
        recalls = [r.recall_of(t, dirty_totals.get(t, 0)) or 0.0 for t in targets]
        prim = sum(recalls) / len(recalls) if recalls else None
        points.append(
            ScanPoint(
                op=op_name,
                threshold=v,
                n_dropped=r.n_dropped,
                clean_killed=r.clean_killed,
                primary_recall=round(prim, 4) if prim is not None else None,
                clean_kill_rate=round(r.clean_kill_rate, 4)
                if r.clean_kill_rate is not None
                else None,
            )
        )
    return points, targets


def _plot(op_name: str, points: list[ScanPoint], default: float, out_dir: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    # Windows 中文字体回退（DejaVu 无 CJK 字形，会渲染成方框）
    from matplotlib import font_manager, rcParams

    for fname in ("Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC"):
        if any(fname.lower() in f.name.lower() for f in font_manager.fontManager.ttflist):
            rcParams["font.sans-serif"] = [fname]
            break
    rcParams["axes.unicode_minus"] = False
    import matplotlib.pyplot as plt

    fig, ax1 = plt.subplots(figsize=(7, 4))
    xs = [p.threshold for p in points]
    recalls = [p.primary_recall or 0 for p in points]
    kills = [(p.clean_kill_rate or 0) * 100 for p in points]  # 误杀率按百分比

    ax1.set_xlabel("阈值")
    ax1.set_ylabel("主靶 recall", color="#d62728")
    ax1.plot(xs, recalls, "o-", color="#d62728", label="主靶 recall")
    ax1.axvline(default, color="#888", ls="--", lw=1, label=f"生产默认 {default}")
    ax1.tick_params(axis="y", labelcolor="#d62728")

    ax2 = ax1.twinx()
    ax2.set_ylabel("干净误杀率 (%)", color="#2ca02c")
    ax2.plot(xs, kills, "s--", color="#2ca02c", label="干净误杀率")
    ax2.tick_params(axis="y", labelcolor="#2ca02c")

    ax1.set_title(f"{op_name} 阈值敏感性")
    fig.legend(loc="upper right", bbox_to_anchor=(0.88, 0.88))
    fig.tight_layout()
    p = out_dir / f"threshold_{op_name}.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/interim/contaminated/samples.jsonl")
    parser.add_argument("--out", default="data/reports/threshold_scan.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    input_path = Path(args.input)
    samples = [Sample.from_dict(json.loads(line)) for line in open(input_path, encoding="utf-8")]
    logging.info("阈值扫描: %s 条样本, %s 个阈值算子", len(samples), len(THRESHOLD_SPECS))

    report: dict = {"n_total": len(samples), "operators": {}}
    out_dir = Path(args.out).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    for op_name in THRESHOLD_SPECS:
        points, targets = scan_operator(op_name, samples)
        default = THRESHOLD_SPECS[op_name][2]
        chart = _plot(op_name, points, default, out_dir)
        report["operators"][op_name] = {
            "param": THRESHOLD_SPECS[op_name][0],
            "default": default,
            "targets": targets,
            "points": [
                {
                    "threshold": p.threshold,
                    "n_dropped": p.n_dropped,
                    "clean_killed": p.clean_killed,
                    "primary_recall": p.primary_recall,
                    "clean_kill_rate": p.clean_kill_rate,
                }
                for p in points
            ],
            "chart": str(chart),
        }
        logging.info("  %s: %s 个阈值点, 图表 %s", op_name, len(points), chart)

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = Path(args.out).with_suffix(".md")
    md_path.write_text(_markdown(report), encoding="utf-8")
    logging.info("报告: %s (+ .md + %s 张图)", args.out, len(THRESHOLD_SPECS))


def _markdown(report: dict) -> str:
    lines = ["# 阈值敏感性扫描", "", f"- 全集 {report['n_total']} 条", ""]
    for op, data in report["operators"].items():
        lines += [
            f"## {op}",
            f"- 主靶: {', '.join(data['targets']) or '—'}，"
            f"阈值参数: `{data['param']}`，生产默认: **{data['default']}**",
            "",
            "| 阈值 | 扔 | 误杀 | 主靶 recall | 干净误杀率 |",
            "|---|---|---|---|---|",
        ]
        for p in data["points"]:
            recall = "—" if p["primary_recall"] is None else f"{p['primary_recall']:.1%}"
            kill = "—" if p["clean_kill_rate"] is None else f"{p['clean_kill_rate']:.2%}"
            mark = " ←生产默认" if p["threshold"] == data["default"] else ""
            lines.append(
                f"| {p['threshold']} | {p['n_dropped']} | {p['clean_killed']} "
                f"| {recall} | {kill} |{mark}"
            )
        lines += [f"![{op} 阈值曲线]({Path(data['chart']).name})", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
