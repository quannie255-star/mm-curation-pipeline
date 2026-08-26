"""算子级 P/R 独立评测（design 2.3，Week3 D4）。

每个算子在全量脏集上独立跑一次（与漏斗串联不同），用 ground truth
算 precision / recall / 干净误杀率。这是「哪个算子抓什么、抓得准不准」
的最终答案，也是阈值扫描与消融实验的输入。

用法：
    python scripts/eval_operators.py                         # 默认配置 + 全量脏集
    python scripts/eval_operators.py --config configs/pipeline.example.yaml
    python scripts/eval_operators.py --input data/interim/contaminated/samples.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.eval import evaluate_all, render_pr_markdown  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline import PipelineConfig  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/pipeline.example.yaml")
    parser.add_argument(
        "--input", default=None, help="输入脏数据 jsonl（默认取配置里的 dataset.raw_jsonl）"
    )
    parser.add_argument("--out", default="data/reports/operator_pr.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = PipelineConfig.from_yaml(args.config)
    input_path = Path(args.input) if args.input else config.raw_jsonl
    samples = [Sample.from_dict(json.loads(line)) for line in open(input_path, encoding="utf-8")]
    n_dirty = sum(1 for s in samples if s.labels)
    logging.info(
        "算子级评测: %s 算子, %s 条样本（干净 %s / 脏 %s）",
        len(config.operators),
        len(samples),
        len(samples) - n_dirty,
        n_dirty,
    )

    results, dirty_totals, n_clean = evaluate_all(config.operators, samples)

    report = {
        "pipeline": config.name,
        "n_total": len(samples),
        "n_clean": n_clean,
        "n_dirty": sum(dirty_totals.values()),
        "dirty_totals": dirty_totals,
        "eval_scope": "每个算子独立在全量脏集上跑一次，丢弃互不影响",
        "operators": [r.to_dict(dirty_totals, n_clean) for r in results],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out.with_suffix(".md")
    md = render_pr_markdown(results, dirty_totals, n_clean, config.name)
    md_path.write_text(md, encoding="utf-8")

    print(f"\n算子级 P/R: {config.name}  ({len(samples)} 条全集, {len(config.operators)} 算子)")
    print(f"{'算子':<18}{'扔':>6}{'误杀':>7}{'precision':>11}{'主靶recall':>14}{'误杀率':>9}")
    for r in results:
        prec = "—" if r.precision is None else f"{r.precision:.1%}"
        kill = "—" if r.clean_kill_rate is None else f"{r.clean_kill_rate:.2%}"
        if r.primary_target:
            recalls = [
                f"{r.recall_of(t, dirty_totals.get(t, 0)) or 0:.0%}" for t in r.primary_target
            ]
            prim = "/".join(recalls)
        else:
            prim = "—"
        print(f"{r.op:<18}{r.n_dropped:>6}{r.clean_killed:>7}{prec:>11}{prim:>14}{kill:>9}")
    logging.info("报告: %s (+ .md)", out)


if __name__ == "__main__":
    main()
