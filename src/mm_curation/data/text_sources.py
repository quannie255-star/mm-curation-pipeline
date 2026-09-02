"""文本语料下载器：wikimedia/wikipedia zh（V2 β 文本实例的底座）。

选型记录（设计门 spike 2026-09-02）：MNBVC 中文类目 schema 复杂（2.4 万分片，
wiki 类目实为英文 wikihow）→ 降级；维基 zh parquet 确认可得（最小分片
126.8MB），脏度由污染器注入并自带 ground truth，语料同时充当 PSI 参考分布
与训练对比的 held-out 测试集。
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

MIRROR = "https://hf-mirror.com"
SHARDS = [  # 按体积升序：先小后大
    "20231101.zh/train-00002-of-00006.parquet",
    "20231101.zh/train-00001-of-00006.parquet",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) mm-curation/0.2"


def download_shard(shard: str, dest: Path, *, retries: int = 3) -> Path:
    """流式下载单个 parquet 分片（原子写 + 重试 + 已存在跳过）。"""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{MIRROR}/datasets/wikimedia/wikipedia/resolve/main/{shard}"
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            part = dest.with_suffix(dest.suffix + ".part")
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=300) as r, open(part, "wb") as f:
                while chunk := r.read(1 << 22):  # 4MB 块流式写
                    f.write(chunk)
            part.replace(dest)
            logger.info(
                "分片就绪: %s (%.0fMB, %.0fs)",
                dest.name,
                dest.stat().st_size / 1e6,
                time.time() - t0,
            )
            return dest
        except (urllib.error.URLError, OSError) as e:
            logger.warning("下载失败(%s/%s): %s", attempt, retries, e)
            if attempt == retries:
                raise
            time.sleep(3 * attempt)
    raise RuntimeError("unreachable")


def extract_docs(parquet_path: Path, out_jsonl: Path, target: int) -> int:
    """pyarrow 按 row-group 增量读取，抽出 target 篇正文追加写 jsonl。"""
    import json

    import pyarrow.parquet as pq

    written = 0
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    pf = pq.ParquetFile(parquet_path)
    mode = "a" if out_jsonl.exists() else "w"
    with open(out_jsonl, mode, encoding="utf-8") as f:
        for batch in pf.iter_batches(batch_size=2048, columns=["id", "title", "text"]):
            for rid, title, text in zip(
                batch.column("id").to_pylist(),
                batch.column("title").to_pylist(),
                batch.column("text").to_pylist(),
            ):
                text = (text or "").strip()
                if len(text) < 20:
                    continue  # 重定向/空壳页
                row = {
                    "id": f"wiki{rid}",
                    "text": text,
                    "meta": {"source": "wikipedia-zh", "title": (title or "").strip()},
                    "labels": {},
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
            if written >= target:
                break
    return written
