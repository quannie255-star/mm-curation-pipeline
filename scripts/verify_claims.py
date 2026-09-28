"""claims 数字版本锁定校验器（V5 β 补强）：文档数字 vs 落盘报告逐条比对。

回答审计质疑「PROOF_CHAIN 是手动维护，数字会漂移」：简历/文档引用的每个
数字在 docs/claims.json 注册（报告文件 + json 指针 + 期望值 + 容差），本脚本
逐条解析比对——漂移即 exit 1。可进 CI，也可 `--update` 重新锁定（打印新旧差）。

指针语法：点分路径 + [n] 列表下标，如 `others[0].result.recall_at_k.1`。
comparator：approx（绝对差 ≤ tol）/ exact（相等）/ historical（无法从落盘报告
复核的历史数字，只打印不校验——如实标注而非假装可验证）。

用法：
    python -X utf8 scripts/verify_claims.py            # 校验全部，漂移 exit 1
    python -X utf8 scripts/verify_claims.py --update   # 以当前报告值重新锁定
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLAIMS = REPO / "docs" / "claims.json"


def resolve(doc, pointer: str):
    """解析 `a.b[0].c.1` 形式指针；路径不存在抛 KeyError。"""
    import re

    cur = doc
    for token in re.findall(r"\[|\]|[^.\[\]]+", pointer):
        if token in ("[", "]"):
            continue
        if isinstance(cur, list):
            cur = cur[int(token)]
        elif isinstance(cur, dict):
            cur = cur[token]
        else:
            raise KeyError(f"指针 {pointer} 在标量 {token!r} 上无法继续")
    return cur


def check_claim(claim: dict, repo: Path = REPO) -> dict:
    if claim.get("comparator") == "historical" or not claim.get("file"):
        return {**claim, "status": "historical", "current": None}
    path = repo / claim["file"]
    if not path.exists():
        return {**claim, "status": "missing", "current": None}
    doc = json.loads(path.read_text(encoding="utf-8"))
    try:
        current = resolve(doc, claim["pointer"])
    except (KeyError, IndexError, ValueError):
        return {**claim, "status": "pointer-broken", "current": None}
    expected = claim["expected"]
    if claim["comparator"] == "exact":
        ok = current == expected
    else:
        ok = (
            current is not None
            and expected is not None
            and abs(current - expected) <= claim["tol"]
        )
    return {**claim, "status": "pass" if ok else "drift", "current": current}


def check_fingerprints(registry: dict, repo: Path = REPO) -> list[dict]:
    """数据指纹校验：注册的 md5/行数与当前文件比对（回归基线带数据指纹，笔记 #66）。"""
    out = []
    for rel, meta in registry.get("meta", {}).get("data_fingerprints", {}).items():
        path = repo / rel
        if not path.exists():
            out.append({"file": rel, "status": "missing"})
            continue
        raw = path.read_bytes()
        rows = len([x for x in raw.decode("utf-8").split("\n") if x.strip()])
        md5 = hashlib.md5(raw).hexdigest()
        out.append(
            {
                "file": rel,
                "status": "pass" if (md5 == meta["md5"] and rows == meta["rows"]) else "drift",
                "md5": md5,
                "rows": rows,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="以当前报告值重新锁定 expected")
    parser.add_argument("--claims", default=str(CLAIMS))
    args = parser.parse_args()

    claims_path = Path(args.claims)
    registry = json.loads(claims_path.read_text(encoding="utf-8"))

    results = [check_claim(c) for c in registry["claims"]]
    fingerprints = check_fingerprints(registry)

    print(f"{'claim':<32}{'状态':<10}{'期望':>10}{'当前':>12}  说明")
    n_drift = 0
    for r in results:
        flag = {"pass": "PASS", "drift": "DRIFT", "missing": "缺报告",
                "pointer-broken": "指针断", "historical": "历史数字"}.get(r["status"], r["status"])
        exp = r["expected"]
        cur = r["current"]
        line = (
            f"{r['id']:<32}{flag:<10}"
            f"{exp if exp is not None else '—':>10}"
            f"{round(cur, 4) if isinstance(cur, float) else (cur if cur is not None else '—'):>12}"
            f"  {r.get('note', r.get('desc', ''))[:44]}"
        )
        print(line)
        if r["status"] == "drift":
            n_drift += 1

    for f in fingerprints:
        md5_head = f.get("md5", "—")[:8]
        print(f"指纹 {f['file']}: {f['status']} (md5={md5_head}…, rows={f.get('rows', '—')})")
        if f["status"] == "drift":
            n_drift += 1

    if args.update:
        for r in results:
            locked = r["status"] in ("drift", "pass")
            if locked and r["current"] is not None and r["comparator"] != "exact":
                r["expected"] = r["current"]
        registry["claims"] = [
            {k: v for k, v in r.items() if k not in ("status", "current")} for r in results
        ]
        claims_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n--update：{len(results)} 条 claim 已按当前报告重新锁定（请 review diff 后提交）")
        return 0

    print(
        f"\n{len(results)} 条 claim："
        f"{sum(1 for r in results if r['status'] == 'pass')} PASS, "
        f"{sum(1 for r in results if r['status'] == 'drift')} DRIFT, "
        f"{sum(1 for r in results if r['status'] in ('missing', 'pointer-broken'))} 缺失/断链, "
        f"{sum(1 for r in results if r['status'] == 'historical')} 历史数字（不校验）"
    )
    return 1 if n_drift else 0


if __name__ == "__main__":
    sys.exit(main())
