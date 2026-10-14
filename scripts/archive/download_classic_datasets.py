"""下载轻量经典数据集并转成清洗漏斗的 Sample jsonl。

下载走 HuggingFace 直链（huggingface.co）的分块 GET——本机代理对 datasets 库的
多请求握手会挂死，且多数 HF 仓库 resolve 被代理拦截，实测可靠的经典文本集只有
 ag_news 与 GLUE 系列（sst2/qnli）。20 Newsgroups 的 SetFit 仓库被代理 404，
故以同为经典的 SST-2 情感分类基准替补。

输出（每行 {"id","text","meta":{"source","label"}}，modality 默认 text_article）：
    data/raw/classic/ag_news_train.jsonl     # AG News 新闻主题分类（经典）
    data/raw/classic/sst2_train.jsonl        # SST-2 情感分类（GLUE 经典基准）

用法：
    python -X utf8 scripts/download_classic_datasets.py
    python -X utf8 scripts/download_classic_datasets.py --limit 8000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("download_classic")

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "raw" / "classic"

# 实测代理可下（2026-10-13）：huggingface.co 直链 resolve parquet。
# text_col 各数据集不同（sst2 是 sentence），统一抽到 Sample.text。
_DATASETS = {
    "ag_news": {
        "url": "https://huggingface.co/datasets/ag_news/resolve/main/data/train-00000-of-00001.parquet",
        "text_col": "text",
        "label_col": "label",
    },
    "sst2": {
        "url": "https://huggingface.co/datasets/glue/resolve/main/sst2/train-00000-of-00001.parquet",
        "text_col": "sentence",
        "label_col": "label",
    },
}


def _chunked_get(url: str, dest: Path, timeout: int = 600) -> None:
    """分块下载，带进度日志（代理限速下比一次性 read() 稳）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    got = 0
    last = t0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length", "0"))
        logger.info("GET %s (Content-Length=%d)", url.split("/datasets/")[-1], total)
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last >= 10:
                    logger.info("  %.0fs 已下 %d/%d KB", now - t0, got // 1024, total // 1024)
                    last = now
    logger.info("GET 完成 %s : %d 字节 (%.0fs)", dest.name, got, time.time() - t0)


def _convert(name: str, spec: dict, limit: int | None) -> list[dict]:
    import pyarrow.parquet as pq

    dest = OUT_DIR / f"_dl_{name}.parquet"
    if not (dest.exists() and dest.stat().st_size > 0):
        _chunked_get(spec["url"], dest)
    table = pq.read_table(dest)
    cols = table.column_names
    text_idx = cols.index(spec["text_col"])
    label_idx = cols.index(spec["label_col"]) if spec["label_col"] in cols else None
    n = table.num_rows
    take = min(limit, n) if limit else n
    rows = []
    for i in range(take):
        text = table.column(text_idx)[i].as_py()
        if not text:
            continue
        meta = {"source": name}
        if label_idx is not None:
            meta["label"] = table.column(label_idx)[i].as_py()
        rows.append({"id": f"{name}-{i}", "text": str(text), "meta": meta})
    dest.unlink(missing_ok=True)
    return rows


def _dump(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("写出 %s : %d 条 -> %s", path.name, len(rows), path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=10000, help="每个数据集取的条数（默认 10000）")
    args = ap.parse_args()

    for name, spec in _DATASETS.items():
        _dump(OUT_DIR / f"{name}_train.jsonl", _convert(name, spec, args.limit))
    logger.info("完成。输出目录: %s", OUT_DIR)


if __name__ == "__main__":
    sys.exit(main())
