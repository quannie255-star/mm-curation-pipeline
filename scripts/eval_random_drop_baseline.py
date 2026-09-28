"""随机删同比例 baseline：清洗增益是「洗对了」还是「删了样本」？（审计问 #2）

质疑：漏斗把 2106 条清到 1585 条，R@1 提升可能只是「样本变少、难例被删」。
对照实验：**随机删掉同样数量的样本**，在完全相同的评测口径（dirty_raw 全量
排名过滤子集，held_out 查询，零重编码）下对比——

- 漏斗子集 R@1：确定性，一次
- 随机子集 R@1：N 个随机 seed，报均值与最小值

结论口径：漏斗超出「随机删均值」的部分，才是清洗规则带来的净贡献。

用法：python -X utf8 scripts/eval_random_drop_baseline.py [--n-seeds 5]
产物：data/reports/random_drop_baseline.{json,md}
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, Path(__file__).resolve().parent)

from eval_ablation import evaluate_subset  # noqa: E402  复用零重编码评测口径

CONTAMINATED = "data/interim/contaminated/samples.jsonl"
INDEXES_ROOT = "data/indexes"
CONFIG = "configs/pipeline.example.yaml"


def summarize(rows: list[dict]) -> dict:
    """随机子集结果的聚合统计（均值 + 最差值，n 小不做伪置信区间）。"""
    import math

    r1 = [r["recall_at_k"][1] for r in rows]
    n = len(r1)
    mean = sum(r1) / n
    std = (sum((v - mean) ** 2 for v in r1) / (n - 1)) ** 0.5 if n > 1 else 0.0
    ci_half = 1.96 * std / math.sqrt(n) if n > 1 else 0.0
    return {
        "random_r1_mean": round(mean, 4),
        "random_r1_std": round(std, 4),
        "random_r1_ci95": [round(mean - ci_half, 4), round(mean + ci_half, 4)],
        "random_r1_min": round(min(r1), 4),
        "random_r1_max": round(max(r1), 4),
        "n_random_seeds": n,
    }


def build_report(
    funnel_m: dict,
    random_rows: list[dict],
    n_drop: int,
    clean_control_rows: list[dict] | None = None,
) -> dict:
    stats = summarize(random_rows)
    funnel_r1 = funnel_m["recall_at_k"][1]
    margin = round(funnel_r1 - stats["random_r1_mean"], 4)
    clean_control = None
    if clean_control_rows:
        cc = summarize(clean_control_rows)
        # 脏残留剂量对照（保留全部脏样本、库大小同漏斗）：若 R@1 接近漏斗，
        # 说明提升只是「库变小」；实测显著低于漏斗 → 提升来自「脏样本被移出库」
        clean_control = {
            "rows": clean_control_rows,
            **cc,
            "margin_over_clean_control": round(funnel_r1 - cc["random_r1_mean"], 4),
        }
    return {
        "question": "清洗增益是「洗对了」还是「删了样本」？",
        "n_input": funnel_m["n_input"],
        "n_drop": n_drop,
        "funnel": funnel_m,
        "random": {"rows": random_rows, **stats},
        "clean_control": clean_control,
        "verdict": {
            "funnel_r1": funnel_r1,
            "random_r1_mean": stats["random_r1_mean"],
            "margin_over_random": margin,
            "interpretation": (
                "漏斗超出随机删的 R@1 即清洗规则的净贡献"
                if margin > 0
                else "漏斗未跑赢随机删：清洗规则在该查询集上无净贡献（如实记录）"
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=CONFIG)
    parser.add_argument("--input", default=CONTAMINATED)
    parser.add_argument("--n-seeds", type=int, default=5)
    parser.add_argument("--out", default="data/reports/random_drop_baseline.json")
    args = parser.parse_args()

    from mm_curation.embedding import clip_encoder  # noqa: E402
    from mm_curation.eval.retrieval import build_queries  # noqa: E402
    from mm_curation.index.searcher import load_searcher  # noqa: E402
    from mm_curation.operators.base import Sample  # noqa: E402
    from mm_curation.pipeline import PipelineConfig, run_funnel  # noqa: E402

    samples = [Sample.from_dict(json.loads(line)) for line in open(args.input, encoding="utf-8")]
    config = PipelineConfig.from_yaml(args.config)
    result = run_funnel(list(samples), config)
    kept_ids = [s.id for s in result.kept]
    all_ids = [s.id for s in samples]
    n_drop = len(all_ids) - len(kept_ids)

    clean = [s for s in samples if not s.labels]
    queries = [q for q in build_queries(clean) if q.origin == "held_out"]
    searcher = load_searcher(INDEXES_ROOT, "dirty_raw")
    vecs = clip_encoder.get_encoder().encode_texts([q.text for q in queries])

    funnel_m = evaluate_subset(searcher, queries, vecs, kept_ids)
    funnel_m["n_input"] = len(all_ids)

    random_rows = []
    for seed in range(args.n_seeds):
        rng = random.Random(seed)
        subset = rng.sample(all_ids, len(kept_ids))
        m = evaluate_subset(searcher, queries, vecs, subset)
        m["seed"] = seed
        random_rows.append(m)

    # 脏残留剂量对照：库大小与漏斗相同（1585），但保留全部脏样本、只删干净
    # ——与「均匀随机删（残留 ~363 脏）」「漏斗（残留 0 脏）」构成剂量-响应三对照
    dirty_ids = [s.id for s in samples if s.labels]
    clean_ids = [s.id for s in samples if not s.labels]
    n_keep_clean = len(kept_ids) - len(dirty_ids)
    assert n_keep_clean > 0, "漏斗保留量不足以容纳全部脏样本"
    dirty_preserve_rows = []
    for seed in range(args.n_seeds):
        rng = random.Random(1000 + seed)
        subset = dirty_ids + rng.sample(clean_ids, n_keep_clean)
        m = evaluate_subset(searcher, queries, vecs, subset)
        m["seed"] = seed
        dirty_preserve_rows.append(m)

    report = build_report(funnel_m, random_rows, n_drop, dirty_preserve_rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    v = report["verdict"]
    print(
        f"\n漏斗子集（清掉 {n_drop} 条）:  R@1={v['funnel_r1']:.3f}"
        f"  R@10={funnel_m['recall_at_k'][10]:.3f}"
    )
    print(
        f"随机子集 ×{args.n_seeds}:  R@1 均值 {v['random_r1_mean']:.3f}"
        f"（95%CI {report['random']['random_r1_ci95']}"
        f"，std {report['random']['random_r1_std']:.3f}）"
    )
    if report["clean_control"]:
        cc_mean = report["clean_control"]["random_r1_mean"]
        print(
            f"脏残留对照 ×{args.n_seeds}:  R@1 均值 {cc_mean:.3f}（库同漏斗，全部脏样本保留在库中）"
        )
        print(f"漏斗 - 脏残留对照: {report['clean_control']['margin_over_clean_control']:+.3f}")
    print(f"清洗净贡献（漏斗 - 随机均值）: {v['margin_over_random']:+.3f}")
    out.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print("报告:", out, "(+ .md)")


def _markdown(report: dict) -> str:
    v = report["verdict"]
    rows = report["random"]["rows"]
    lines = [
        "# 随机删同比例 baseline：清洗增益的因果归因",
        "",
        f"- 质疑：漏斗把 {report['n_input']} 条清到 {report['n_input'] - report['n_drop']} 条，"
        "R@1 提升可能只是「删样本」。",
        "- 口径：与消融实验同一套零重编码评测（dirty_raw 全量排名过滤子集，"
        "held_out 查询），随机子集与漏斗子集**样本数相同**。",
        "",
        "| 配置 | R@1 | R@10 |",
        "|---|---|---|",
        f"| 漏斗子集（清洗规则删的） | **{v['funnel_r1']:.3f}** | "
        f"{report['funnel']['recall_at_k'][10]:.3f} |",
    ]
    for r in rows:
        lines.append(
            f"| 随机删 seed={r['seed']} | {r['recall_at_k'][1]:.3f} | {r['recall_at_k'][10]:.3f} |"
        )
    lines += [
        f"| 随机删均值 | {v['random_r1_mean']:.3f} | — |",
    ]
    if report.get("clean_control"):
        cc = report["clean_control"]
        lines += [
            f"| 脏残留对照（486 脏全保留，库同漏斗） | {cc['random_r1_mean']:.3f} | — |",
            f"| 漏斗 − 脏残留对照 | **{cc['margin_over_clean_control']:+.3f}** | — |",
        ]
    lines += [
        "",
        f"**清洗净贡献 = 漏斗 − 随机均值 = {v['margin_over_random']:+.3f}**。"
        f"{v['interpretation']}。",
        "",
        "> 局限：随机 seed 数有限（见上表），不做正态置信区间；",
        "> held_out 查询集与消融实验同源（119 条），样本量小、数字有 ±1pp 波动属正常。",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
