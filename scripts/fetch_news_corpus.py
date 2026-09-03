"""ζ1 数据获取：中新网爬取 → data/raw/news_corpus.jsonl（V3 ζ，专属判官的域语料）。

用法：python -X utf8 scripts/fetch_news_corpus.py [--max-docs 2000] [--delay 1.0]
幂等：重跑只补增量。产物行：{id, text, meta:{url,title,source}}
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.data.web_sources import crawl  # noqa: E402

OUT = Path("data/raw/news_corpus.jsonl")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-docs", type=int, default=2000)
    parser.add_argument("--delay", type=float, default=1.0, help="请求间隔秒（对源站客气）")
    parser.add_argument("--listing-pages", type=int, default=40)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    crawl(OUT, max_docs=args.max_docs, delay=args.delay, max_listing_pages=args.listing_pages)
    logging.info(
        "完成: %s（累计 %s 条）",
        OUT,
        sum(1 for ln in OUT.read_text(encoding="utf-8").split("\n") if ln.strip()),
    )


if __name__ == "__main__":
    main()
