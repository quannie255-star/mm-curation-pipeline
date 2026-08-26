"""构建向量索引（design 2.2：CLI 约定与退出码）。

用法：
    python scripts/build_index.py --name clean_v2 \
        --input data/processed/cn_flickr_curation_v2/cleaned.jsonl

退出码：0 成功 / 1 输入缺失 / 2 编码或构建失败
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.index.store import build_index  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="样本 jsonl 路径")
    parser.add_argument("--name", required=True, help="索引名（data/indexes/<name>）")
    parser.add_argument("--out", default="data/indexes", help="索引根目录")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    src = Path(args.input)
    if not src.exists():
        logging.error("输入不存在: %s", src)
        sys.exit(1)
    try:
        manifest = build_index(src, args.name, args.out)
    except Exception as e:  # 编码失败/归一化校验失败等
        logging.error("构建失败: %s", e)
        sys.exit(2)
    logging.info(
        "索引就绪: %s/%s (%s 条, dim=%s)", args.out, manifest.name, manifest.n_items, manifest.dim
    )
    print(
        json.dumps(
            {
                "name": manifest.name,
                "n_items": manifest.n_items,
                "dim": manifest.dim,
                "built_at": manifest.built_at,
            }
        )
    )


if __name__ == "__main__":
    main()
