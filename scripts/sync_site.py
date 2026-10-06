#!/usr/bin/env python
"""把 `docs/product.html` 同步到 `site/index.html`，供公网发布用。

**为什么需要这个脚本，而不是手动拷一次文件**

产品页的数字来自 `claims.json` + prod DuckDB，每次跑批/改结论都会变。
公网链接指向的是 `site/` 里的快照——如果靠手动拷，早晚会发生「本地已经更新、
线上还是三个月前那版」，而**线上那版是别人真正看到的东西**。这类腐烂极难发现：
本地自检全绿，线上 quietly 停在上一个版本。所以同步动作必须可复跑、
且每次发布前强制跑一遍。

**为什么不直接把 `docs/` 整个发布**

`docs/` 里有 PROOF_CHAIN / ENGINEERING_NOTES / ROADMAP 等内部文档。
发布目录只放产品页，其余一概不上公网——这是有意的边界，不是不小心漏了。

**发布前必须先跑** `scripts/build_product_page.py` 重新生成 `docs/product.html`，
本脚本只做同步，不生成。两个职责分开是为了让「页面内容对不对」和
「发布的是不是最新版」各自有一个可单独验收的判据。

    python -X utf8 scripts/sync_site.py --check   # 只查不写，CI/门禁用
    python -X utf8 scripts/sync_site.py           # 同步

退出码：0=已是最新 / 已同步；1=源页缺失或 --check 发现不一致。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "docs" / "product.html"
SITE = REPO / "site"
DST = SITE / "index.html"


def digest(path: Path) -> str:
    """内容指纹。用于「线上是不是这一版」的判定，不依赖 mtime（拷来拷去会变）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只校验不写；不一致时退出码 1")
    args = ap.parse_args()

    if not SRC.exists():
        print(f"[FAIL] 源页不存在：{SRC}", file=sys.stderr)
        print("       先跑 python -X utf8 scripts/build_product_page.py", file=sys.stderr)
        return 1

    src_sum = digest(SRC)
    if args.check:
        if not DST.exists():
            print(f"[RED] {DST.relative_to(REPO)} 不存在——还没同步过", file=sys.stderr)
            return 1
        dst_sum = digest(DST)
        if dst_sum != src_sum:
            print(
                f"[RED] 发布目录与源页不一致：docs={src_sum} site={dst_sum}\n"
                f"      线上会是旧版。跑 python -X utf8 scripts/sync_site.py",
                file=sys.stderr,
            )
            return 1
        print(f"[GREEN] 发布目录已是最新版（{src_sum}）")
        return 0

    # 只往 site/ 写一个文件：先清掉可能存在的旧副本，避免上一版残留文件被一起发布
    if SITE.exists():
        for old in SITE.iterdir():
            if old.is_file() and old.name != "index.html":
                old.unlink()
    SITE.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST)
    print(f"已同步 docs/product.html -> site/index.html（{src_sum}，{DST.stat().st_size} B）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
