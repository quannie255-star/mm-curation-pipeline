"""脏/净索引检索对比评测（design 2.2）。

用法：
    python scripts/eval_retrieval.py                       # clean_v2 vs dirty_raw
    python scripts/eval_retrieval.py --indexes clean_v2 dirty_raw --out data/reports/x.json

退出码：0 / 1(索引缺失) / 2(评测异常)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.embedding import clip_encoder  # noqa: E402
from mm_curation.eval.retrieval import (  # noqa: E402
    build_queries,
    compare,
    evaluate_index,
)
from mm_curation.index.searcher import load_searcher  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402

INDEXES_ROOT = "data/indexes"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--indexes",
        nargs="+",
        default=["clean_v2", "dirty_raw"],
        help="参与对比的索引名（第一个为基准）",
    )
    parser.add_argument(
        "--queries-from",
        default="clean_v2",
        help="查询集来源索引（取其 manifest.source_jsonl 的样本）",
    )
    parser.add_argument("--out", default="data/reports/retrieval_eval.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        base = load_searcher(INDEXES_ROOT, args.queries_from)
    except KeyError as e:
        logging.error("%s", e)
        sys.exit(1)

    # 查询集来自净索引的源 jsonl：extra_captions 在样本 meta 里（不在索引 store）
    samples = [
        Sample.from_dict(json.loads(line))
        for line in open(base.manifest.source_jsonl, encoding="utf-8")
        if not json.loads(line)["labels"]
    ]  # 只用干净样本构造查询
    queries = build_queries(samples)
    n_held = sum(1 for q in queries if q.origin == "held_out")
    logging.info(
        "查询集: %s 条（held_out %s / self %s），目标=对应图像",
        len(queries),
        n_held,
        len(queries) - n_held,
    )

    try:
        vecs = clip_encoder.get_encoder().encode_texts([q.text for q in queries])
        results = []
        for name in args.indexes:
            searcher = base if name == args.queries_from else load_searcher(INDEXES_ROOT, name)
            results.append(evaluate_index(searcher, queries, vecs))
    except Exception as e:
        logging.error("评测失败: %s", e)
        sys.exit(2)

    report = {
        "n_queries": len(queries),
        "origin_split": {"held_out": n_held, "self": len(queries) - n_held},
        **compare(results),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out.with_suffix(".md")).write_text(_markdown(report), encoding="utf-8")

    print(_table(results))
    logging.info("报告: %s (+ .md)", out)


def _table(results) -> str:
    lines = [f"{'index':<12}{'n':>6}{'R@1':>8}{'R@5':>8}{'R@10':>8}{'MRR':>8}"]
    for r in results:
        lines.append(
            f"{r.index:<12}{r.n_queries:>6}"
            f"{r.recall_at_k[1]:>8.3f}{r.recall_at_k[5]:>8.3f}"
            f"{r.recall_at_k[10]:>8.3f}{r.mrr:>8.3f}"
        )
    return "\n".join(lines)


def _markdown(report: dict) -> str:
    base = report["base"]
    others = report["others"]
    lines = [
        "# 检索对比实验：脏索引 vs 净索引",
        "",
        f"- 查询集 {report['n_queries']} 条"
        f"（held_out {report['origin_split']['held_out']} /"
        f" self {report['origin_split']['self']}），ground truth = 查询对应图像",
        "",
        "| 索引 | n | Recall@1 | Recall@5 | Recall@10 | MRR |",
        "|---|---|---|---|---|---|",
        f"| {base['index']} | {base['n_queries']} | "
        f"{base['recall_at_k']['1']:.3f} | {base['recall_at_k']['5']:.3f} | "
        f"{base['recall_at_k']['10']:.3f} | {base['mrr']:.3f} |",
    ]
    for o in others:
        r, d = o["result"], o["delta_vs_base"]
        lines.append(
            f"| {r['index']} | {r['n_queries']} | "
            f"{r['recall_at_k']['1']:.3f} | {r['recall_at_k']['5']:.3f} | "
            f"{r['recall_at_k']['10']:.3f} | {r['mrr']:.3f} |"
        )
    for o in others:
        d = o["delta_vs_base"]
        r = o["result"]["index"]
        lines += [
            "",
            f"**{r} 相对基准的变化**：" + "，".join(f"{k} {v:+.3f}" for k, v in d.items()),
        ]
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
