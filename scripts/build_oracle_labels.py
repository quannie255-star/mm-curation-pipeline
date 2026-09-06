"""θ：模拟用户标注生成（流程验收账，与真人标注分文件分账）。

用法：python -X utf8 scripts/build_oracle_labels.py --n-docs 250 \
        --out data/annot/pref_labels_oracle.jsonl
规则 oracle（精炼派 prefer=S）走与真人完全相同的 v2 通道；seed 固定可复现。
源文档 = 新闻爬取产物，结构性排除 judge/pref/ext 全部既有占用。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.tuning.preference import (  # noqa: E402
    DEFAULT_PROTOCOL,
    LABELER_ORACLE,
    load_news_corpus_excluded,
    oracle_labels_from_corpus,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-docs", type=int, default=250)
    parser.add_argument("--prefer", choices=["S", "F"], default="S")
    parser.add_argument("--protocol", default=DEFAULT_PROTOCOL)
    parser.add_argument("--out", default="data/annot/pref_labels_oracle.jsonl")
    parser.add_argument("--seed", type=int, default=53)
    parser.add_argument(
        "--exclude-from", default=None,
        help="标注文件：其中已出现的 source_id 不再生成（追加批次用，避免与旧批次重叠）",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    corpus = load_news_corpus_excluded()
    if args.exclude_from:
        used = {
            json.loads(ln)["source_id"]
            for ln in Path(args.exclude_from).read_text(encoding="utf-8").split("\n")
            if ln.strip()
        }
        corpus = [d for d in corpus if d["id"] not in used]
        logging.info("排除已标注 %s 篇后剩余 %s 篇", len(used), len(corpus))
    logging.info("示例语料（排除既有占用后）：%s 篇", len(corpus))
    rows = oracle_labels_from_corpus(
        corpus,
        protocol=args.protocol,
        prefer=args.prefer,
        n_docs=args.n_docs,
        seed=args.seed,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    logging.info("模拟用户标注 %s 条（labeler=%s）→ %s", len(rows), LABELER_ORACLE, out)


if __name__ == "__main__":
    main()
