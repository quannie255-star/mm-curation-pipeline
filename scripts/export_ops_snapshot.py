#!/usr/bin/env python
"""把 prod 库的服务层数据**物化**成独立快照，供公网演示使用。

## 为什么需要物化，而不是直接发布 prod 库

`ads_dataset_health` / `dim_device` / `dws_dataset_day` / `dws_op_day` 在 prod 库里
全是 **VIEW**，定义是 `read_parquet('C:/Users/.../data/envs/prod/data/lake/...')`——
**硬编码的 Windows 绝对路径**。直接发布，DuckDB 会在沙箱里找不到任何文件，
每个视图查询都报 `IO Error: No files found that match the pattern`。

三条路：
① 连湖上 1137 个 parquet 一起发 → 8.95 MB，且路径依然写死，改机器就废
② 改视图定义让它相对路径 → **要动生产代码**，为了演示改生产路径是本末倒置
③ **物化成快照**（本脚本）→ 5032 行 / 几百 KB，与路径无关，往哪放都能读

选③。代价要说清：**这是快照，不是活库**。演示页读的是导出那一刻的状态，
不反映之后的跑批。所以快照必须带导出时间戳 + 行数，页面上要写明这一点——
让看的人知道自己在看什么时候的数据，比让他以为这是实时的更诚实。

## 为什么是 JSON 而不是 parquet

公网演示服务用**标准库 `http.server`** 实现，零第三方依赖：沙箱不用`pip install`，
不会有原生 wheel 装不上的风险，秒起。演示的用途是**给人看**，不是做分析——
parquet 的列式压缩在这里没有价值，JSON 的可读性反而让快照本身可被直接检视。
（真要分析，仓库里的 DuckDB 路径仍然完整。）

## 表清单从源码现取，不硬编码

服务层查的表可以从 `src/mm_curation/platform/service.py` 里扫 `FROM <表名>` 得到
（外加 `job_runs` 这张台账表，它在同一个库里但不被 FROM 直接引用）。
**硬编码清单会随新端点一起腐烂**，而「清单没更新」和「端点坏了」在报告上
长得一模一样——这是本项目反复栽过的坑（`test_promote_leaves_every_relation...`
那条门禁就是这么写的）。所以：扫不到表名就 `assert` 失败，不许静默跳过一个。

## 用法

    python -X utf8 scripts/export_ops_snapshot.py            # 导出
    python -X utf8 scripts/export_ops_snapshot.py --verify   # 只校验快照可用

导出目录 `webdemo/data/`（随公网发布一起上传），已进 .gitignore。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
PROD_DB = REPO / "data" / "envs" / "prod" / "data" / "warehouse" / "platform.duckdb"
SERVICE_SRC = REPO / "src" / "mm_curation" / "platform" / "service.py"
OUT_DIR = REPO / "webdemo" / "data"
MANIFEST = OUT_DIR / "snapshot_manifest.json"

# 不被 `FROM <表名>` 直接引用、但服务层确实读的表（台账）。
# 放在这里而不是「扫出来就不用」——扫不出来的东西必须**显式登记**并写明理由，
# 否则下一个人会以为它不存在而把它删掉。
EXTRA_TABLES = {"job_runs": "运行台账；`ServiceCore.runs()` 读它，但源码里不写 FROM"}


def _iso(v: object) -> object:
    """把 date/datetime/Decimal 之类转成 JSON 能存、且别的语言能读的形式。

    浮点保留原值不四舍五入——快照要能让人核对到与库里一致，
    在这里「美化」数字等于给快照掺假。
    """
    if isinstance(v, (datetime,)):
        return v.isoformat(sep=" ")
    if hasattr(v, "isoformat") and not isinstance(v, str):
        return v.isoformat()
    if isinstance(v, float):
        return v
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    return str(v)


def service_tables() -> list[str]:
    """服务层会查的表：从 `service.py` 源码现取，不硬编码清单。"""
    src = SERVICE_SRC.read_text(encoding="utf-8")
    found = sorted(set(re.findall(r"\bFROM\s+(\w+)", src)))
    assert found, f"在 {SERVICE_SRC.name} 里扫不到任何 FROM <表名>——判据失效，不许静默导出空快照"
    return found + sorted(EXTRA_TABLES)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true", help="只校验快照，不重新导出")
    args = ap.parse_args()

    if args.verify:
        return verify()

    if not PROD_DB.exists():
        print(f"[FAIL] prod 库不存在：{PROD_DB}", file=sys.stderr)
        print("先跑 python -X utf8 -m mm_curation.cli promote", file=sys.stderr)
        return 1

    tables = service_tables()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 清掉上一版快照（含清单），避免旧表残留在发布目录里被当成现役数据
    for old in OUT_DIR.glob("*.json"):
        old.unlink()

    con = duckdb.connect(str(PROD_DB), read_only=True)
    entries = []
    try:
        for t in tables:
            kind = con.execute(
                "select table_type from information_schema.tables where table_name = ?", [t]
            ).fetchone()
            if kind is None:
                print(f"[FAIL] 库里没有 {t}——服务层源码与库不一致", file=sys.stderr)
                return 1
            cur = con.execute(f"SELECT * FROM {t}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            # datetime/date 走 DuckDB 的 JSON 扩展会得到带空格的字符串，
            # 这里显式转 ISO 8601 —— 快照要能被别的语言读，格式不能是「Python 特有」。
            norm = [[_iso(v) for v in r] for r in rows]
            payload = {"columns": cols, "rows": norm}
            dst = OUT_DIR / f"{t}.json"
            dst.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            size = dst.stat().st_size
            entries.append(
                {
                    "table": t,
                    "table_type": kind[0],
                    "rows": len(norm),
                    "file": dst.name,
                    "bytes": size,
                }
            )
            print(f"  {t:22s} {kind[0]:12s} {len(norm):>6} 行  {size / 1024:>8.1f} KB")
    finally:
        con.close()

    manifest = {
        "exported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source_db": PROD_DB.name,
        "source_env": "prod",
        "tables": entries,
        "total_rows": sum(e["rows"] for e in entries),
        "total_bytes": sum(e["bytes"] for e in entries),
        "note": (
            "这是**导出那一刻的快照**，不是活库；演示页读到的就是上面这个时间点的状态。"
            "视图原本是 read_parquet 硬编码本机绝对路径，物化后与路径无关。"
        ),
        "extra_tables_reason": EXTRA_TABLES,
    }
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        f"\n已导出 {len(entries)} 张表 / {manifest['total_rows']} 行 / "
        f"{manifest['total_bytes'] / 1024:.1f} KB -> {OUT_DIR.relative_to(REPO)}"
    )
    print(f"清单：{MANIFEST.relative_to(REPO)}")

    n_claims = export_claims()
    print(f"对外数字：{n_claims} 个结论（现算自 claims.json + 评测报告，可按 claim id 取）")
    return 0


def export_claims() -> int:
    """把 `docs/claims.json` 里登记的对外数字**现算**成 `data/claims_rendered.json`。

    ## 为什么页面不直接写死数字

    项目一贯纪律是「数字接唯一真相源」：README /产品页 / 简历里的每个数字都来自
    `claims.json`，页面对不上注册表就生成失败。**网页也该守同一条**——
    否则公网那页会变成唯一一个「手抄数字」的地方，而它恰恰是最容易被截图传播、
    最不容易被发现抄错的地方（本地文档改了，线上那页还挂着旧数字，没人会发现）。

    所以：构建期现算→ 写进快照目录 → 页面运行时读它。数字仍然只有一处来源。

    **只取门面（facades），不取claims**：`claims` 是「报告里必须等于这个值」的门禁，
    页面要展示的是「文档里该写哪个字面量」——那是 `facades` 的职责，
    它的 `fmt` / `scale` / `suffix` 正是渲染成人类可读数字所需的三件东西。
    """
    reg = json.loads((REPO / "docs" / "claims.json").read_text(encoding="utf-8"))
    by_facade: dict[str, dict] = {}
    by_claim: dict[str, dict] = {}
    missing: list[str] = []

    for f in reg.get("facades", []):
        src = f.get("source") or {}
        claim_id = src.get("claim") or src.get("baseline") or src.get("derived")
        if not claim_id:
            continue
        try:
            value = resolve(reg, claim_id, f)
        except (KeyError, IndexError, TypeError, ValueError, FileNotFoundError) as exc:
            missing.append(f"{f['id']}({type(exc).__name__})")
            continue
        item = {"value": value, "text": render(value, f), "note": f.get("note", "")}
        by_facade[f["id"]] = item
        # 同一个 claim 被多个文档门面引用（实测最多 8 个），渲染结果一样。
        # **额外按 claim id 索引一份**，让网页不必知道门面 id 长什么样——
        # 门面 id 里带文档名（`README_md__xxx`），文档一改/新增页面就失效。
        # 页面真正要的是「`soul_dirty_r1` 这个结论的值」，与它写在哪份文档无关。
        # 冲突时（同一 claim 被渲染成不同字面量，说明注册表自相矛盾）必须报出来。
        prev = by_claim.get(claim_id)
        if prev is not None and prev["text"] != item["text"]:
            missing.append(f"CONFLICT {claim_id}: {prev['text']} vs {item['text']}")
        by_claim[claim_id] = item

    # 缺失必须显式报出来，且**计数**——静默少几条会让页面悄悄少一块内容
    payload = {
        "rendered_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "count": len(by_facade),
        "claim_count": len(by_claim),
        "missing": missing,
        "items": by_facade,
        "claims": by_claim,
    }
    (OUT_DIR / "claims_rendered.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    if missing:
        print(f"  [warn] {len(missing)} 条门面没能现算：{missing[:5]}", file=sys.stderr)
    return len(by_claim)


def _load_json(rel: str) -> object:
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


def _walk(doc: object, pointer: str) -> object:
    """只支持 `a.b[0].c`（与 `verify_claims.py` 一致；不支持 JSONPath 过滤）。"""
    cur = doc
    for part in pointer.split("."):
        while "[" in part:
            head, rest = part.split("[", 1)
            if head:
                cur = cur[head]
            idx, part = rest.split("]", 1)
            cur = cur[int(idx)]
            part = part.lstrip(".")
        if part:
            cur = cur[part]
    return cur


def resolve(reg: dict, claim_id: str, facade: dict) -> float:
    """取一个门面的底层数值。基线 / 派生 / 报告三种来源分别处理。

    ⚠️ **不要强转 float**。`fmt` 有`'d'`（整数）这一类，强转成 `625.0` 之后
    `format(625.0, 'd')` 抛 `ValueError: Unknown format code 'd'`。
    `verify_claims.render_value` 也是直接拿原值 `format`，所以这里必须同样——
    否则同一个门面在门禁里是过的、在网页导出时炸。

    **派生值直接 import `verify_claims.compute_derived`**，不在这里重写一遍 ——
    两处各写一份正则算法，早晚会漂移，而漂移的后果是「门禁过了但网页数字错了」
    或者反过来。**口径只有一处，才谈得上一致。**
    """
    src = facade.get("source") or {}
    if "baseline" in src:
        return reg["baselines"][claim_id]
    if "derived" in src:
        return _derived()[claim_id]
    for c in reg.get("claims", []):
        if c.get("id") == claim_id:
            return _walk(_load_json(c["file"]), c["pointer"])
    raise KeyError(claim_id)


_DERIVED: dict[str, int | None] | None = None


def _derived() -> dict[str, int | None]:
    global _DERIVED
    if _DERIVED is None:
        sys.path.insert(0, str(REPO / "scripts"))
        from verify_claims import compute_derived  # noqa: PLC0415 - 运行时才导入

        _DERIVED = compute_derived(json.loads((REPO / "docs" / "claims.json").read_text("utf-8")))
    return _DERIVED


def render(value: float, f: dict) -> str:
    """与 `verify_claims.py` 的 `render_value` 同构：`format(v*scale, fmt) + suffix`。"""
    return format(value * f.get("scale", 1), f.get("fmt", "g")) + f.get("suffix", "")


def verify() -> int:
    """校验快照可读且行数与清单一致——**发布前必跑**。

    只检查文件在不在是不够的：JSON 可能截断，而线上页面会安静地渲染成空表。
    这里真的把每个文件读回来、数行数、核对清单。
    """
    if not MANIFEST.exists():
        print(f"[RED] 清单不存在：{MANIFEST.relative_to(REPO)}", file=sys.stderr)
        return 1
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for e in m["tables"]:
        p = OUT_DIR / e["file"]
        if not p.exists():
            print(f"[RED] 缺文件：{e['file']}", file=sys.stderr)
            return 1
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[RED] {e['file']} 不是合法 JSON：{exc}", file=sys.stderr)
            return 1
        n = len(payload.get("rows", []))
        if n != e["rows"]:
            print(f"[RED] {e['table']} 行数不符：清单 {e['rows']} vs 实际 {n}", file=sys.stderr)
            return 1
        if n and len(payload["rows"][0]) != len(payload.get("columns", [])):
            print(f"[RED] {e['table']} 列数与列名数不一致", file=sys.stderr)
            return 1
    print(
        f"[GREEN] 快照可用：{len(m['tables'])} 张表 / {m['total_rows']} 行 / "
        f"导出于 {m['exported_at']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
