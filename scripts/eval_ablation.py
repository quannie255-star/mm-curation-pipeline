"""消融实验：逐个移除算子，测检索指标变化（Week4 D4）。

回答「11 级漏斗里哪个算子对下游检索贡献最大」。

设计（与采样评测同源的零重编码技巧）：
- dirty_raw 索引含全量 2106 条向量；任何漏斗变体的存活集都是它的子集
- IndexFlatIP 精确检索，子集相对排名 = 全量排名过滤后顺序
- 因此「移除算子 X → 重跑漏斗 → 存活集 → 过滤 dirty_raw 检索结果 → 取 top-k」
  等价于「从存活集建索引检索」，但零重编码（CLIP 编码器缓存复用）

指标：每移除一个算子，held_out 查询的 recall@k/MRR vs 全量漏斗基准。
delta 为负 = 该算子有用（移除后变差）；为正 = 该算子可能误杀有用样本。

用法：
    python scripts/eval_ablation.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
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
from mm_curation.pipeline import PipelineConfig, run_funnel  # noqa: E402

INDEXES_ROOT = "data/indexes"
CONTAMINATED = "data/interim/contaminated/samples.jsonl"


@dataclass
class AblationResult:
    name: str
    n_kept: int
    recall_at_k: dict = field(default_factory=dict)
    mrr: float = 0.0
    seconds: float = 0.0


def evaluate_subset(searcher, queries, query_vecs, kept_ids, k_list=K_LIST):
    """过滤 dirty_raw 全量检索到子集后取 top-k，算 recall/mrr。"""
    from mm_curation.eval.metrics import mrr as _mrr
    from mm_curation.eval.metrics import recall_at_k

    kept = set(kept_ids)
    max_k = max(k_list)
    rankings = []
    for q, vec in zip(queries, query_vecs):
        all_hits = searcher.search_many_by_vectors([vec], len(query_vecs))[0]
        filtered = [h for h in all_hits if h.id in kept][:max_k]
        rankings.append(target_rank(filtered, q.target_id))
    return {
        "recall_at_k": {k: round(recall_at_k(rankings, k), 4) for k in k_list},
        "mrr": round(_mrr(rankings), 4),
        "n_targets_in_subset": sum(1 for q in queries if q.target_id in kept),
    }


def run_ablation(config, samples, searcher, queries, query_vecs):
    """对单个配置跑漏斗 + 评测。返回 AblationResult。"""
    t0 = time.perf_counter()
    result = run_funnel(list(samples), config)
    kept_ids = [s.id for s in result.kept]
    metrics = evaluate_subset(searcher, queries, query_vecs, kept_ids)
    return AblationResult(
        name=config.name,
        n_kept=len(kept_ids),
        recall_at_k=metrics["recall_at_k"],
        mrr=metrics["mrr"],
        seconds=round(time.perf_counter() - t0, 1),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/pipeline.example.yaml")
    parser.add_argument("--input", default=CONTAMINATED)
    parser.add_argument("--out", default="data/reports/ablation_eval.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    base_cfg = PipelineConfig.from_yaml(args.config)
    samples = [Sample.from_dict(json.loads(line)) for line in open(args.input, encoding="utf-8")]

    # held_out 查询：只用干净样本的 extra_captions
    clean = [s for s in samples if not s.labels]
    queries = [q for q in build_queries(clean) if q.origin == "held_out"]
    logging.info(
        "消融实验: %s 样本, %s 算子, %s held_out 查询",
        len(samples),
        len(base_cfg.operators),
        len(queries),
    )

    searcher = load_searcher(INDEXES_ROOT, "dirty_raw")
    vecs = clip_encoder.get_encoder().encode_texts([q.text for q in queries])

    # 全量漏斗（基准）
    logging.info("基准: 全量漏斗 (%s 算子)", len(base_cfg.operators))
    baseline = run_ablation(base_cfg, samples, searcher, queries, vecs)
    logging.info(
        "  全量: n=%s R@1=%.3f R@10=%.3f MRR=%.3f",
        baseline.n_kept,
        baseline.recall_at_k[1],
        baseline.recall_at_k[10],
        baseline.mrr,
    )

    # 逐个移除算子
    ablations = [baseline]
    for i, spec in enumerate(base_cfg.operators):
        ablated_ops = [s for s in base_cfg.operators if s.op != spec.op]
        ablated_cfg = PipelineConfig(
            name=f"no_{spec.op}",
            raw_jsonl=base_cfg.raw_jsonl,
            output_dir=base_cfg.output_dir,
            operators=ablated_ops,
        )
        logging.info("[%s/%s] 移除 %s ...", i + 1, len(base_cfg.operators), spec.op)
        res = run_ablation(ablated_cfg, samples, searcher, queries, vecs)
        logging.info(
            "  no_%s: n=%s R@1=%.3f R@10=%.3f MRR=%.3f (delta R@1 %+.3f)",
            spec.op,
            res.n_kept,
            res.recall_at_k[1],
            res.recall_at_k[10],
            res.mrr,
            res.recall_at_k[1] - baseline.recall_at_k[1],
        )
        ablations.append(res)

    report = _build_report(ablations, len(samples), len(queries))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(_table(ablations))
    logging.info("报告: %s (+ .md)", out)


def _build_report(ablations, n_input, n_queries):
    baseline = ablations[0]
    return {
        "n_input": n_input,
        "n_held_out": n_queries,
        "baseline": _result_dict(baseline),
        "ablations": [
            {**_result_dict(r), "delta_r1": round(r.recall_at_k[1] - baseline.recall_at_k[1], 4)}
            for r in ablations[1:]
        ],
    }


def _result_dict(r):
    return {
        "name": r.name,
        "n_kept": r.n_kept,
        "recall_at_k": {str(k): v for k, v in r.recall_at_k.items()},
        "mrr": r.mrr,
        "seconds": r.seconds,
    }


def _table(ablations):
    b = ablations[0]
    lines = [f"{'config':<22}{'n':>6}{'R@1':>8}{'R@10':>8}{'MRR':>8}{'ΔR@1':>8}"]
    lines.append(
        f"{'full':<22}{b.n_kept:>6}{b.recall_at_k[1]:>8.3f}{b.recall_at_k[10]:>8.3f}{b.mrr:>8.3f}{'—':>8}"
    )
    for r in ablations[1:]:
        d = r.recall_at_k[1] - b.recall_at_k[1]
        lines.append(
            f"{r.name:<22}{r.n_kept:>6}{r.recall_at_k[1]:>8.3f}{r.recall_at_k[10]:>8.3f}"
            f"{r.mrr:>8.3f}{d:>+8.3f}"
        )
    return "\n".join(lines)


def _markdown(report):
    b = report["baseline"]
    lines = [
        "# 消融实验：逐个移除算子测检索指标变化",
        "",
        f"- 输入 {report['n_input']} 条（脏全集），held_out 查询 {report['n_held_out']} 条",
        f"- 基准 = 全量 {b['n_kept']} 条漏斗（clean_v2 等价）",
        "- 每行 = 移除某算子后重跑漏斗，存活集过滤 dirty_raw 索引评测",
        "  （零重编码：CLIP 编码器缓存复用，IndexFlatIP 子集排名不变）",
        "",
        "| 配置 | 存活 | R@1 | R@5 | R@10 | MRR | ΔR@1 |",
        "|---|---|---|---|---|---|---|",
        f"| full（基准） | {b['n_kept']} | {b['recall_at_k']['1']:.3f} | "
        f"{b['recall_at_k']['5']:.3f} | {b['recall_at_k']['10']:.3f} | {b['mrr']:.3f} | — |",
    ]
    # 按 ΔR@1 排序（负值越大 = 移除后越差 = 算子越重要）
    for a in sorted(report["ablations"], key=lambda x: x["delta_r1"]):
        lines.append(
            f"| {a['name']} | {a['n_kept']} | {a['recall_at_k']['1']:.3f} | "
            f"{a['recall_at_k']['5']:.3f} | {a['recall_at_k']['10']:.3f} | "
            f"{a['mrr']:.3f} | {a['delta_r1']:+.3f} |"
        )
    lines += [
        "",
        "**解读**：ΔR@1 为负 = 移除该算子后检索变差（算子有用）；",
        "负值越大 = 该算子对下游检索贡献越大。",
    ]
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
