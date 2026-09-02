"""下载文本语料：维基 zh 分片 → 30 万文档 text_corpus.jsonl（V2 β T0/T1）。

用法：python scripts/download_text_corpus.py [--docs 300000]
（幂等：分片与 jsonl 已存在则跳过对应工作）
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.data.text_sources import SHARDS, download_shard, extract_docs  # noqa: E402

CACHE = Path("data/raw/text_cache")
OUT = Path("data/raw/text_corpus.jsonl")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=300_000, help="目标文档数")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    existing = 0
    if OUT.exists():
        existing = sum(1 for _ in open(OUT, encoding="utf-8"))
        logging.info("已有语料: %s 条", existing)
    if existing >= args.docs:
        logging.info("语料已达标，跳过下载")
        return

    for shard in SHARDS:
        name = shard.split("/")[-1]
        download_shard(shard, CACHE / name)
        n = extract_docs(CACHE / name, OUT, args.docs - existing)
        existing += n
        logging.info("累计 %s 条", existing)
        if existing >= args.docs:
            break
    logging.info("完成: %s (%s 条)", OUT, existing)


if __name__ == "__main__":
    main()
