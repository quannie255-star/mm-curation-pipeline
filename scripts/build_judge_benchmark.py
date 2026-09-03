"""ζ2 入口：构建冻结 benchmark。

用法：python -X utf8 scripts/build_judge_benchmark.py --corpus data/raw/news_corpus.jsonl
产物：benchmarks/judge_news_v1/{items.jsonl, manifest.json}（一起提交进仓库）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from curation_eval import Sample  # noqa: E402

from mm_curation.benchmarks.builder import BenchmarkSpec, build_benchmark  # noqa: E402

TRAIN_SEEDS = (23,)  # 已知在用的污染 seed（δ eval_judge 用 23；训练集将用 23 族）


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="data/raw/news_corpus.jsonl")
    parser.add_argument("--out", default="benchmarks/judge_news_v1")
    parser.add_argument("--n-clean", type=int, default=150)
    parser.add_argument("--n-dirty", type=int, default=150)
    parser.add_argument("--train-jsonl", default="data/interim/judge_train.jsonl")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    corpus_path = Path(args.corpus)
    rows = [
        json.loads(ln) for ln in corpus_path.read_text(encoding="utf-8").split("\n") if ln.strip()
    ]
    corpus = [
        Sample(id=r["id"], text=r["text"])
        for r in rows
        if len(r["text"]) >= 200  # 判官 benchmark 取长文（短文无质量语义）
    ]
    logging.info("域语料 %s 篇（≥200 字的 %s 篇）", len(rows), len(corpus))

    spec = BenchmarkSpec(
        name="judge_news_v1",
        domain_desc="中文新闻正文（中国新闻网，2026-09 滚动新闻）的训练适用性判定",
        n_clean=args.n_clean,
        n_dirty=args.n_dirty,
        seed=9000,
        train_seeds=TRAIN_SEEDS,
    )
    manifest = build_benchmark(
        corpus,
        spec,
        Path(args.out),
        train_jsonl=Path(args.train_jsonl) if args.train_jsonl else None,
    )
    logging.info(
        "冻结完成: %s（%s 条，泄漏 md5=%s minhash=%s）",
        args.out,
        manifest["n_items"],
        len(manifest["leakage_check"]["md5_leaks"]),
        len(manifest["leakage_check"]["minhash_leaks"]),
    )


if __name__ == "__main__":
    main()
