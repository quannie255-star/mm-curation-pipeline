"""工业传感器模态评测（V5 α）：算子级 P/R + 漏斗串联门禁。

流程：合成语料（--seed 确定性）→ ContaminationPlan 注入五类合规脏数据 →
① 算子级独立评测（与 make eval-op 同格式）→ ② 漏斗串联跑门禁
（总体故障召回 ≥0.90 且干净误杀率 ≤0.05，跌破 exit 1，--no-gate 观测模式）。

用法：
    python -X utf8 scripts/eval_industrial.py                  # 全量 1200+ 窗 + 门禁
    python -X utf8 scripts/eval_industrial.py --scale 0.1      # 冒烟档
    python -X utf8 scripts/eval_industrial.py --no-gate        # 只出报告不变红
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "curation-eval" / "src"))

from curation_eval import ContaminationPlan  # noqa: E402

SENSOR_KINDS = {
    "sensor_cal_offset": 1.0,
    "sensor_flatline": 1.0,
    "sensor_out_of_range": 1.0,
    "sensor_unit_swap": 1.0,
    "sensor_unplanned_silence": 1.0,
}

GATES = {"recall_min": 0.90, "false_kill_max": 0.05}


def funnel_gate_metrics(dropped, n_dirty: int, n_clean: int) -> dict:
    """漏斗串联口径的门禁指标。dropped 为 FunnelResult.dropped（(算子名, 样本)）。"""
    dropped_samples = [s for _, s in dropped]
    caught = {s.id for s in dropped_samples if s.labels}
    clean_killed = sum(1 for s in dropped_samples if not s.labels)
    recall = len(caught) / n_dirty if n_dirty else 0.0
    false_kill = clean_killed / n_clean if n_clean else 0.0
    return {
        "recall": round(recall, 4),
        "false_kill_rate": round(false_kill, 4),
        "n_dirty": n_dirty,
        "n_dirty_caught": len(caught),
        "n_clean_killed": clean_killed,
    }


def worst_case_gate(runs: list[dict]) -> dict:
    """多 seed 聚合：取最差 seed 的召回/误杀（保守口径），全 seed 过门禁才算过。"""
    return {
        "recall": min(r["recall"] for r in runs),
        "false_kill_rate": max(r["false_kill_rate"] for r in runs),
        "n_dirty": runs[-1]["n_dirty"],
        "n_dirty_caught": min(r["n_dirty_caught"] for r in runs),
        "n_clean_killed": max(r["n_clean_killed"] for r in runs),
        "n_clean": runs[-1]["n_clean"],
        "passed": all(r["passed"] for r in runs),
        "n_seeds": len(runs),
    }


def evaluate_gate(metrics: dict, gates: dict | None = None) -> bool:
    gates = gates or GATES
    return (
        metrics["recall"] >= gates["recall_min"]
        and metrics["false_kill_rate"] <= gates["false_kill_max"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--seeds", default=None,
        help="逗号分隔多 seed 稳定性实验（如 42,7,2026）；提供时覆盖 --seed",
    )
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--inject-rate", type=float, default=0.3)
    parser.add_argument("--config", default="configs/funnel_industrial.yaml")
    parser.add_argument("--out", default="data/reports/operator_pr_industrial.json")
    parser.add_argument("--no-gate", action="store_true", help="只出报告，不按门禁设退出码")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from mm_curation.data.sensor_synth import generate_corpus
    from mm_curation.eval import evaluate_all, render_pr_markdown
    from mm_curation.operators.base import Sample
    from mm_curation.pipeline import PipelineConfig, run_funnel

    config = PipelineConfig.from_yaml(args.config)
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else [args.seed]

    def run_one(seed: int) -> dict:
        corpus = generate_corpus(seed=seed, scale=args.scale)
        plan = ContaminationPlan(
            inject_rate=args.inject_rate, seed=seed, kinds=dict(SENSOR_KINDS)
        )
        mixed, manifest = plan.run(corpus, Path("data/tmp_sensor_images"))
        samples = [Sample.from_dict(s.to_dict()) for s in mixed]
        results, dirty_totals, n_clean = evaluate_all(config.operators, samples)
        funnel = run_funnel([Sample.from_dict(s.to_dict()) for s in mixed], config)
        gate = funnel_gate_metrics(funnel.dropped, sum(dirty_totals.values()), n_clean)
        gate["passed"] = evaluate_gate(gate)
        gate["seed"] = seed
        gate["n_clean"] = n_clean
        gate["n_injected"] = manifest["n_injected"]
        gate["counts"] = manifest["counts"]
        gate["primary_recalls"] = {
            r.op: (
                min(
                    (
                        v
                        for v in (
                            r.recall_of(t, dirty_totals.get(t, 0))
                            for t in r.primary_target
                        )
                        if v is not None
                    ),
                    default=None,
                )
            )
            for r in results
        }
        logging.info(
            "seed=%d：语料 %d + 注入 %d，召回 %.1f%%，误杀 %.2f%%",
            seed, len(samples), manifest["n_injected"], gate["recall"] * 100,
            gate["false_kill_rate"] * 100,
        )
        return gate

    runs = [run_one(seed) for seed in seeds]
    gate = worst_case_gate(runs)
    # 算子级明细取最后一个 seed 的运行（多 seed 稳定性在报告单列一节）
    last = runs[-1]
    last_corpus = generate_corpus(seed=last["seed"], scale=args.scale)
    last_plan = ContaminationPlan(
        inject_rate=args.inject_rate, seed=last["seed"], kinds=dict(SENSOR_KINDS)
    )
    last_mixed, manifest = last_plan.run(last_corpus, Path("data/tmp_sensor_images"))
    results, dirty_totals, n_clean = evaluate_all(
        config.operators, [Sample.from_dict(s.to_dict()) for s in last_mixed]
    )

    report = {
        "pipeline": config.name,
        "seed": args.seed,
        "scale": args.scale,
        "inject_rate": args.inject_rate,
        "n_total": len(last_mixed),
        "n_clean": n_clean,
        "n_dirty": sum(dirty_totals.values()),
        "dirty_totals": dirty_totals,
        "eval_scope": "每个算子独立在全量脏集上跑一次，丢弃互不影响",
        "operators": [r.to_dict(dirty_totals, n_clean) for r in results],
        "funnel_gate": gate,
        "stability": runs if len(runs) > 1 else None,
        "manifest": manifest,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_pr_markdown(results, dirty_totals, n_clean, config.name)
    md += _gate_markdown(gate, args, runs)
    out.with_suffix(".md").write_text(md, encoding="utf-8")

    print(
        f"\n工业传感器 P/R: {config.name}（{len(last_mixed)} 条全集, {len(config.operators)} 算子）"
    )
    print(f"{'算子':<26}{'扔':>5}{'误杀':>5}{'precision':>10}{'主靶recall':>12}")
    for r in results:
        prec = "—" if r.precision is None else f"{r.precision:.1%}"
        prim = "/".join(
            f"{r.recall_of(t, dirty_totals.get(t, 0)) or 0:.0%}" for t in r.primary_target
        ) or "—"
        print(f"{r.op:<26}{r.n_dropped:>5}{r.clean_killed:>5}{prec:>10}{prim:>12}")
    print(
        f"漏斗门禁（{'%d seeds 最差口径' % len(runs) if len(runs) > 1 else '单 seed'}）: "
        f"召回 {gate['recall']:.1%}（门限 ≥{GATES['recall_min']:.0%}）, "
        f"误杀 {gate['false_kill_rate']:.2%}（门限 ≤{GATES['false_kill_max']:.0%}）→ "
        f"{'PASSED' if gate['passed'] else 'FAILED'}"
    )
    logging.info("报告: %s (+ .md)", out)
    if not args.no_gate and not gate["passed"]:
        return 1
    return 0


def _gate_markdown(gate: dict, args, runs: list[dict]) -> str:
    return "\n".join(
        [
            "",
            "## 漏斗串联门禁",
            "",
            f"- 语料 seed={args.seed}，scale={args.scale}，注入率 {args.inject_rate:.0%}",
            f"- 总体故障召回 **{gate['recall']:.1%}**（{gate['n_dirty_caught']}/{gate['n_dirty']}，"
            f"门限 ≥{GATES['recall_min']:.0%}）",
            f"- 干净误杀率 **{gate['false_kill_rate']:.2%}**"
            f"（{gate['n_clean_killed']}/{gate.get('n_clean', '—')} 条，"
            f"门限 ≤{GATES['false_kill_max']:.0%}）",
            f"- 结论：**{'PASSED' if gate['passed'] else 'FAILED'}**",
            "",
            "> 语料为程序生成的合成工业传感器数据，不含任何真实产线数据；",
            *(
                [
                    "- 多 seed 稳定性（同一门限逐 seed 判定）：",
                    *[
                        f"  - seed {r['seed']}：召回 {r['recall']:.1%}，"
                        f"误杀 {r['false_kill_rate']:.2%}"
                        f"（误杀 {r['n_clean_killed']}/{r['n_clean']}）"
                        for r in runs
                        if len(runs) > 1
                    ],
                ]
                if len(runs) > 1
                else []
            ),
            "> 工况切换瞬态不在合成范围（drift 算子只裁稳态窗），真实瞬态属 V5 β 真实数据轨。",
            "> 口径说明：批量算子（drift 等）的全局统计会被其他类型灾难注入污染，",
            "> 「独立评测」口径下其误杀偏高属预期现象；漏斗串联门禁（上游先拦灾难窗）",
            "> 才是端到端承诺口径。",
            "",
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
