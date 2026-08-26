"""下载种子数据集并统一为 samples.jsonl（Airflow DAG 与 Makefile 的入口）。

用法：
    python scripts/download_dataset.py                 # 全部可命中图像(~1.6k 对)
    python scripts/download_dataset.py --limit 300     # 冒烟规模
    python scripts/download_dataset.py --mirror https://hf-mirror.com
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.data.download import (  # noqa: E402
    DEFAULT_MIRROR,
    IMAGE_REPO,
    download_seed_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/raw", help="输出目录（默认 data/raw）")
    parser.add_argument("--limit", type=int, default=None, help="最多下载的图像数（冒烟测试用）")
    parser.add_argument(
        "--repo", default=IMAGE_REPO, help="图像镜像仓库（须含 COCO 原始文件名的 JPEG）"
    )
    parser.add_argument(
        "--mirror", default=DEFAULT_MIRROR, help="HF 镜像端点（国内网络建议保持默认）"
    )
    parser.add_argument("--workers", type=int, default=16, help="并发下载线程数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = download_seed_dataset(
        out_dir=args.out,
        mirror=args.mirror,
        repo=args.repo,
        limit=args.limit,
        workers=args.workers,
    )
    n = sum(1 for _ in open(out, encoding="utf-8"))
    logging.info("完成: %s (%s 条样本)", out, n)


if __name__ == "__main__":
    main()
