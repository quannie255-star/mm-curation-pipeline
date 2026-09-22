#!/usr/bin/env python
"""mmc —— 项目统一 CLI 入口。

    python -X utf8 scripts/mmc.py build      # 建数仓（ODS/DWD/DWS/ADS 四层）
    python -X utf8 scripts/mmc.py sql "SELECT * FROM dws_dataset_profile"
    python -X utf8 scripts/mmc.py metrics [--freeze | --verify]
    python -X utf8 scripts/mmc.py scorecard  # 数据质量记分卡 + SLO
    python -X utf8 scripts/mmc.py lineage --impact doc_length
    python -X utf8 scripts/mmc.py contracts  # 数据契约校验（失败 exit 1）

为什么需要它：仓库里有 50+ 个脚本，入口靠「记」和「找」（GAP_AUDIT P1-8）。
本 CLI 只收口**数据链路侧**的高频动作，scripts/ 其余脚本仍是内部实现。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_DB = ROOT / "data" / "warehouse" / "curation.duckdb"


def _print_rows(cols, rows) -> None:
    if not rows:
        print("(0 rows)")
        return
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    print("  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


# ---------------------------------------------------------------------------


def cmd_build(a) -> int:
    from mm_curation.warehouse.model import build_warehouse

    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    rep = build_warehouse(ROOT, DEFAULT_DB)
    print(json.dumps(rep, ensure_ascii=False, indent=2))

    # 一个数据源都用不上 = 这次构建没有任何意义，必须**显式失败**。
    # 早先的实现会把 n_raw=0 的报告当作成功打印出来，下一个命令再给出
    # 「口径返回空行」这种指向错误方向的报错（真问题是没有数据，不是口径错）。
    # 数据产物不入库（见 .gitignore），所以新克隆的仓库**必然**走到这里。
    if rep["n_stg"] == 0 and rep["n_raw"] == 0:
        print(
            "\n[FAIL] 没有任何数据源可用：data/raw 与 data/processed 下的产物"
            "都不存在或为空。\n"
            "       这些是生成产物、不入库（见 .gitignore），新克隆的仓库需要先"
            "生成一遍。\n"
            "       生成命令见 docs/RUNBOOK.md §1（完整复现，每步有验收数字）。",
            file=sys.stderr,
        )
        return 2
    return 0


def cmd_sql(a) -> int:
    from mm_curation.warehouse.model import Warehouse

    wh = Warehouse(DEFAULT_DB)
    cols, rows = wh.query(a.query)
    if a.json:
        print(
            json.dumps(
                {"columns": cols, "rows": [list(r) for r in rows]}, ensure_ascii=False, default=str
            )
        )
    else:
        _print_rows(cols, rows)
    return 0


def cmd_metrics(a) -> int:
    from mm_curation.warehouse.metrics import (
        freeze,
        load_baselines,
        load_metrics,
        verify,
    )
    from mm_curation.warehouse.model import Warehouse

    wh = Warehouse(DEFAULT_DB)
    specs = load_metrics()
    if a.freeze:
        payload = freeze(_con(wh), specs)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    rep = verify(_con(wh), specs, load_baselines())
    if a.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_rows(
            ["metric", "dataset", "dim", "value", "denominator", "baseline", "delta", "ok"],
            [
                (
                    r.name,
                    r.dataset,
                    r.dim or "-",
                    _fmt(r.value),
                    _fmt(r.denominator),
                    _fmt(r.baseline),
                    _fmt(r.delta),
                    "-" if r.ok is None else ("OK" if r.ok else "DRIFT"),
                )
                for r in rep.results
            ],
        )
    if a.verify and not rep.ok:
        print(f"\n[FAIL] {len(rep.failures)} 个指标偏离基线", file=sys.stderr)
        return 1
    return 0


def _con(wh):
    """取一个连接（用完不关——脚本进程结束即释放）。"""
    return wh.connect()


def _fmt(v):
    return "-" if v is None else f"{v:.6g}"


def cmd_scorecard(a) -> int:
    from mm_curation.quality.scorecard import build_scorecard, load_slo, summarize
    from mm_curation.warehouse.model import Warehouse

    wh = Warehouse(DEFAULT_DB)
    _, prof = wh.query("SELECT dataset, n_total FROM marts_dataset_profile")
    profile = {d: {"n_total": n} for d, n in prof}
    _, st = wh.query("SELECT dataset, op, n_scored, n_dropped FROM marts_funnel_stage")
    stages: dict[str, dict[str, dict[str, int]]] = {}
    for d, op, ns, nd in st:
        stages.setdefault(d, {})[op] = {"n_scored": ns, "n_drop": nd}

    cfg = load_slo(ROOT / "configs" / "quality_slo.yaml")
    cards = build_scorecard(profile=profile, stages=stages, cfg=cfg)
    summary = summarize(cards)
    if a.json:
        print(
            json.dumps(
                {"summary": summary, "cards": [c.to_dict() for c in cards]},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 1 if summary["breaches"] else 0
    _print_rows(
        ["dataset", "dimension", "ops(eval/total)", "coverage", "score", "status"],
        [
            (
                c.dataset,
                c.dimension,
                f"{c.ops_evaluated}/{c.ops_total}",
                _fmt(c.coverage),
                _fmt(c.score),
                c.status,
            )
            for c in cards
        ],
    )
    # 健康度必须和覆盖率一起报：0.16 覆盖率上的 1.0 分不该被单独引用
    print(
        f"\nhealth_score = {_fmt(summary['health_score'])}（覆盖率加权）"
        f"   mean_coverage = {_fmt(summary['mean_coverage'])}"
        f"   破线 {len(summary['breaches'])}"
        f"   未评 {len(summary['not_evaluated'])}"
        f"   无SLO {summary['by_status'].get('NO_SLO', 0)}"
        f"   仅观测 {summary['by_status'].get('OBSERVED', 0)}"
    )
    return 1 if summary["breaches"] else 0


def cmd_lineage(a) -> int:
    from mm_curation.lineage import lineage_from_file

    path = a.verdict or (ROOT / "data" / "reports" / "normalize_ablation" / "B" / "verdict.jsonl")
    g = lineage_from_file(path, limit=a.limit or 0)
    if a.impact:
        print(f"job: {a.impact}")
        print(f"  upstream_entities(n) = {len(g.upstream_entities(a.impact))}")
        print(f"  downstream_jobs      = {g.downstream_of(a.impact)}")
        return 0
    if a.mermaid:
        print(g.to_mermaid())
        return 0
    print(json.dumps(g.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_contracts(a) -> int:
    from mm_curation.lineage import check_contract, load_contracts
    from mm_curation.warehouse.model import Warehouse

    wh = Warehouse(DEFAULT_DB)
    contracts = load_contracts(ROOT / "configs" / "contracts")
    if not contracts:
        print("(no contracts)")
        return 0
    results = [check_contract(wh, c) for c in contracts]
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        for r in results:
            flag = "OK " if r["ok"] else "FAIL"
            print(
                f"[{flag}] {r['dataset']}@v{r['version']}  "
                f"{r['n_checks']} checks, {r['n_fail']} fail, {r['n_error']} error"
            )
            for c in r["checks"]:
                if c["status"] != "PASS":
                    print(
                        f"        {c['status']:5s} {c['name']}  "
                        f"actual={c['actual']} expect={c['expect']}"
                    )
    return 1 if any(not r["ok"] for r in results) else 0


# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(prog="mmc", description="mm-curation 数据链路 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="构建四层数仓").set_defaults(fn=cmd_build)

    s = sub.add_parser("sql", help="即席 SQL 查询")
    s.add_argument("query")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sql)

    m = sub.add_parser("metrics", help="指标字典求值 / 冻结 / 校验")
    m.add_argument("--freeze", action="store_true", help="把当前值冻成基线")
    m.add_argument("--verify", action="store_true", help="偏离基线则 exit 1")
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_metrics)

    sc = sub.add_parser("scorecard", help="数据质量记分卡 + SLO")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(fn=cmd_scorecard)

    lg = sub.add_parser("lineage", help="血缘图与影响分析")
    lg.add_argument("--verdict", default="")
    lg.add_argument("--limit", type=int, default=0)
    lg.add_argument("--impact", default="", help="分析某算子的影响面")
    lg.add_argument("--mermaid", action="store_true")
    lg.set_defaults(fn=cmd_lineage)

    ct = sub.add_parser("contracts", help="数据契约校验")
    ct.add_argument("--json", action="store_true")
    ct.set_defaults(fn=cmd_contracts)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
