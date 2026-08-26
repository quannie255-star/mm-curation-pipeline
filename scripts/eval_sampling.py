"""采样策略对比评测（design 2.4，Week3 D5）。

实验设计：固定预算 B，对比 random / stratified(质量+类目) 两种采样，
用同一批 held_out 查询测 recall@k/MRR——回答「清洗后该怎么采样」。

加速技巧（不重建索引）：clean_v2 已编码全量 1585 条向量，IndexFlatIP 是
精确检索，子集的内部相对排名 = 全量排名过滤后的顺序。因此对每个查询先取
全量 top-N，再按采样集 id 过滤、取 top-k，等价于从子集建索引检索，
但零重编码成本（GPU 只跑一次）。

用法：
    python scripts/eval_sampling.py                  # 默认 3 个预算点
    python scripts/eval_sampling.py --budgets 1200 1000 800
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.embedding import clip_encoder  # noqa: E402
from mm_curation.eval.retrieval import (  # noqa: E402
    K_LIST,
    build_queries,
    target_rank,
)
from mm_curation.index.searcher import load_searcher  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.sampling import (  # noqa: E402
    RandomSampler,
    SamplingConfig,
    StratifiedSampler,
)

INDEXES_ROOT = "data/indexes"
CLEAN_SOURCE = "data/processed/cn_flickr_curation_v2/cleaned.jsonl"


@dataclass
class BudgetResult:
    budget: int
    methods: dict[str, dict] = field(default_factory=dict)


def evaluate_subset(
    searcher,
    queries,
    query_vecs,
    sampled_ids: set[str],
    k_list: tuple[int, ...] = K_LIST,
) -> dict:
    """对采样子集评测：全量 top-N 过滤到子集后取 top-k。

    target 不在子集 → rank=None（自然计入未命中）。
    """
    sampled_set = sampled_ids
    max_k = max(k_list)
    rankings: list[int | None] = []
    for q, vec in zip(queries, query_vecs):
        all_hits = searcher.search_many_by_vectors([vec], len(query_vecs))[0]
        # 等价于「只在 sampled_set 内检索」：过滤后相对顺序不变
        filtered = [h for h in all_hits if h.id in sampled_set][:max_k]
        rankings.append(target_rank(filtered, q.target_id))

    from mm_curation.eval.metrics import mrr, recall_at_k

    return {
        "n_queries": len(queries),
        "n_indexed": len(sampled_set),
        "recall_at_k": {k: round(recall_at_k(rankings, k), 4) for k in k_list},
        "mrr": round(mrr(rankings), 4),
        "n_targets_in_subset": sum(1 for q in queries if q.target_id in sampled_set),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--indexes", default=INDEXES_ROOT)
    parser.add_argument("--clean-source", default=CLEAN_SOURCE)
    parser.add_argument("--budgets", nargs="+", type=int, default=[1200, 1000, 800])
    parser.add_argument("--out", default="data/reports/sampling_eval.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    searcher = load_searcher(args.indexes, "clean_v2")

    pool = [
        Sample.from_dict(json.loads(line)) for line in open(args.clean_source, encoding="utf-8")
    ]
    queries_all = build_queries(pool)
    held_out = [q for q in queries_all if q.origin == "held_out"]
    logging.info(
        "采样评测: 索引池 %s 条, held_out 查询 %s 条, 预算点 %s",
        len(pool),
        len(held_out),
        args.budgets,
    )

    vecs = clip_encoder.get_encoder().encode_texts([q.text for q in held_out])
    samplers = [("random", RandomSampler()), ("stratified", StratifiedSampler())]

    results: list[BudgetResult] = []
    for budget in args.budgets:
        br = BudgetResult(budget=budget)
        for name, sampler in samplers:
            cfg = SamplingConfig(budget=budget)
            recipe = sampler.sample(pool, cfg)
            metrics = evaluate_subset(searcher, held_out, vecs, set(recipe.sampled_ids))
            br.methods[name] = {
                **metrics,
                "recipe": recipe.to_dict(),
            }
            logging.info(
                "  %s budget=%s: R@1=%.3f R@10=%.3f MRR=%.3f (target 命中 %s/%s)",
                name,
                budget,
                metrics["recall_at_k"][1],
                metrics["recall_at_k"][10],
                metrics["mrr"],
                metrics["n_targets_in_subset"],
                metrics["n_queries"],
            )
        results.append(br)

    report = _build_report(results, len(pool), len(held_out))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(_table(results))
    logging.info("报告: %s (+ .md)", out)


def _build_report(results, n_pool, n_held) -> dict:
    return {
        "n_pool": n_pool,
        "n_held_out": n_held,
        "budgets": [
            {
                "budget": br.budget,
                "methods": br.methods,
            }
            for br in results
        ],
    }


def _table(results: list[BudgetResult]) -> str:
    lines = [f"{'budget':>8}{'method':>12}{'R@1':>8}{'R@5':>8}{'R@10':>8}{'MRR':>8}"]
    for br in results:
        for name, m in br.methods.items():
            rk = m["recall_at_k"]
            lines.append(
                f"{br.budget:>8}{name:>12}{rk[1]:>8.3f}{rk[5]:>8.3f}{rk[10]:>8.3f}{m['mrr']:>8.3f}"
            )
    return "\n".join(lines)


def _markdown(report: dict) -> str:
    lines = [
        "# 采样策略对比实验：随机 vs 分层",
        "",
        f"- 索引池 {report['n_pool']} 条（clean_v2 漏斗产出，带 score:clip_alignment）",
        f"- held_out 查询 {report['n_held_out']} 条（ground truth = 查询对应图像）",
        "- 子集检索模拟：clean_v2 全量 top-N 过滤到采样集后取 top-k",
        "  （IndexFlatIP 精确检索，子集相对排名不变，零重编码）",
        "",
        "| 预算 | 方法 | 索引量 | R@1 | R@5 | R@10 | MRR | target 命中 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for b in report["budgets"]:
        for name, m in b["methods"].items():
            rk = m["recall_at_k"]
            lines.append(
                f"| {b['budget']} | {name} | {m['n_indexed']} | "
                f"{rk[1]:.3f} | {rk[5]:.3f} | "
                f"{rk[10]:.3f} | {m['mrr']:.3f} | "
                f"{m['n_targets_in_subset']}/{m['n_queries']} |"
            )
    lines += [
        "",
        "## 分层统计（stratified）",
        "",
    ]
    for b in report["budgets"]:
        s = b["methods"].get("stratified", {}).get("recipe", {}).get("strata_summary", {})
        if s:
            top = sorted(s.items(), key=lambda kv: -kv[1])[:8]
            lines.append(f"- budget={b['budget']}: " + ", ".join(f"{k}={v}" for k, v in top))
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
