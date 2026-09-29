"""claims 数字版本锁定校验器（V5 β 补强）：文档数字 vs 落盘报告逐条比对。

回答审计质疑「PROOF_CHAIN 是手动维护，数字会漂移」：简历/文档引用的每个
数字在 docs/claims.json 注册（报告文件 + json 指针 + 期望值 + 容差），本脚本
逐条解析比对——漂移即 exit 1。可进 CI，也可 `--update` 重新锁定（打印新旧差）。

指针语法：点分路径 + [n] 列表下标，如 `others[0].result.recall_at_k.1`。
comparator：approx（绝对差 ≤ tol）/ exact（相等）/ historical（无法从落盘报告
复核的历史数字，只打印不校验——如实标注而非假装可验证）。

文档门面（facades）：文档/前端里**手写**的数字（README / INTERVIEW / RESUME /
ANALYSIS_REPORT 里的「0.556」「7.16」「683」等）在 `registry["facades"]` 登记——每条绑定
**来源**（某个 claim 或 `baselines` 里的基线条目）+ 渲染格式 + 字面量。校验时**两件事**
同时成立才算过：①字面量与来源一致（`render(来源值, fmt) == literal`，否则 registry-stale）；
②文档里真能找到该字面量（否则 doc-stale）。于是数字一改，门禁会把**所有还没同步的文档**
逐条点出来——把「靠人记着对齐」变成「CI 拦截」。

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
            current is not None and expected is not None and abs(current - expected) <= claim["tol"]
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


# ---------------------------------------------------------------------------
# 文档门面（facades）：文档里手写的数字 ↔ 来源（claim / baseline）一致性
# ---------------------------------------------------------------------------


def render_value(value: float, fmt: str, scale: float = 1, suffix: str = "") -> str:
    """把来源数值渲染成文档里应当出现的字面量，如 (0.5558, '.3f') -> '0.556'。"""
    return format(value * scale, fmt) + suffix


def count_literal(text: str, literal: str) -> int:
    """统计字面量在文本里的**独立**出现次数（前后不再是数字或小数点）。

    边界很关键：`0.13` 不应在 `0.131` 里被误计，`67` 不应在 `267`/`670` 里被误计。
    """
    import re

    pat = r"(?<![0-9.])" + re.escape(literal) + r"(?![0-9.])"
    return len(re.findall(pat, text))


def check_facade(facade: dict, registry: dict, repo: Path = REPO) -> dict:
    """校验一条门面：来源渲染出的字面量 == 登记字面量，且文档里能找到它。"""
    src = facade.get("source", {})
    value = None
    if "claim" in src:
        claim = next((c for c in registry.get("claims", []) if c["id"] == src["claim"]), None)
        if claim is None:
            return {**facade, "status": "source-missing", "canon": None, "count": None}
        value = claim.get("expected")
    elif "baseline" in src:
        value = registry.get("baselines", {}).get(src["baseline"])
    if value is None:
        return {**facade, "status": "source-missing", "canon": None, "count": None}

    canon = render_value(value, facade["fmt"], facade.get("scale", 1), facade.get("suffix", ""))
    doc = repo / facade["doc"]
    if not doc.exists():
        return {**facade, "status": "doc-missing", "canon": canon, "count": None}
    count = count_literal(doc.read_text(encoding="utf-8", errors="replace"), facade["literal"])
    if canon != facade["literal"]:
        status = "registry-stale"  # 登记的字面量已经对不上来源了
    elif count < facade.get("min_count", 1):
        status = "doc-stale"  # 文档里找不到锁定的字面量
    else:
        status = "pass"
    return {**facade, "status": status, "canon": canon, "count": count}


def check_facades(registry: dict, repo: Path = REPO) -> list[dict]:
    return [check_facade(f, registry, repo) for f in registry.get("facades", [])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="以当前报告值重新锁定 expected")
    parser.add_argument("--claims", default=str(CLAIMS))
    args = parser.parse_args()

    claims_path = Path(args.claims)
    registry = json.loads(claims_path.read_text(encoding="utf-8"))

    results = [check_claim(c) for c in registry["claims"]]
    fingerprints = check_fingerprints(registry)
    facades = check_facades(registry)

    print(f"{'claim':<32}{'状态':<10}{'期望':>10}{'当前':>12}  说明")
    n_drift = 0
    for r in results:
        flag = {
            "pass": "PASS",
            "drift": "DRIFT",
            "missing": "缺报告",
            "pointer-broken": "指针断",
            "historical": "历史数字",
        }.get(r["status"], r["status"])
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

    for fac in facades:
        cnt = fac["count"]
        print(
            f"门面 {fac['id']:<30}{fac['status']:<16}"
            f"literal={fac['literal']!r:<10} 出现 {cnt if cnt is not None else '—'} 处"
            f"  [{fac['doc']}]"
        )
        if fac["status"] != "pass":
            n_drift += 1
            if fac["status"] == "registry-stale":
                print(f"    ↳ 来源已变：登记字面量应改为 {fac['canon']!r}")
            elif fac["status"] == "doc-stale":
                print(f"    ↳ 文档里找不到 {fac['literal']!r}——应写 {fac['canon']!r}")

    if args.update:
        for r in results:
            locked = r["status"] in ("drift", "pass")
            if locked and r["current"] is not None and r["comparator"] != "exact":
                r["expected"] = r["current"]
        registry["claims"] = [
            {k: v for k, v in r.items() if k not in ("status", "current")} for r in results
        ]
        n_relocked_facade = 0
        for fac in facades:
            if fac["canon"] is not None and fac["literal"] != fac["canon"]:
                for orig in registry.get("facades", []):
                    if orig["id"] == fac["id"]:
                        orig["literal"] = fac["canon"]
                        n_relocked_facade += 1
        claims_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"\n--update：{len(results)} 条 claim 已按当前报告重新锁定；"
            f"门面 {n_relocked_facade} 条字面量已按来源重锁。"
            f"⚠️ 文档不会自动改——重跑本脚本，doc-stale 的行就是还没同步的文档。"
        )
        return 0

    n_fac_pass = sum(1 for f in facades if f["status"] == "pass")
    n_fac_bad = sum(1 for f in facades if f["status"] != "pass")
    print(
        f"\n{len(results)} 条 claim："
        f"{sum(1 for r in results if r['status'] == 'pass')} PASS, "
        f"{sum(1 for r in results if r['status'] == 'drift')} DRIFT, "
        f"{sum(1 for r in results if r['status'] in ('missing', 'pointer-broken'))} 缺失/断链, "
        f"{sum(1 for r in results if r['status'] == 'historical')} 历史数字（不校验）"
    )
    print(
        f"{len(facades)} 条门面：{n_fac_pass} PASS, {n_fac_bad} 漂移"
        f"（registry-stale = 字面量对不上来源；doc-stale = 文档里找不到该字面量）"
    )
    return 1 if n_drift else 0


if __name__ == "__main__":
    sys.exit(main())
