"""findata 健康巡检 stage（B2 联动证据）。

这个脚本证明 findata 可以被 mm-curation-pipeline 作为外部模块 import，
并在 mm-curation 的数据流水线末尾加一道"仓库级健康巡检"。

## 为什么是这一步
mm-curation 的漏斗是**采样级**质量控制（每条样本进/出）。
findata 的巡检是**仓库级**质量控制（按天/按表/按标的）。

两者串联：mm-curation 刚清洗完的样本集合，去 findata 做一次仓库级
健康巡检——如果今天清洗后样本数暴减/暴增，或新出现异常分布，
就由 findata 的归因器判"是上游采样问题、还是 mm-curation 的算子问题"。

## 真实约束
- mm-curation 没有 DuckDB 仓库，findata 的合成语料（Snapshot.from_synthetic）
  提供了零数据演示路径
- 这个 stage 默认就调合成语料；接入真实数据时换成
  `Snapshot.from_duckdb(duckdb_path)` 即可，findata 的接口无需改
- 路径：默认找桌面 `findata` 仓库，可用环境变量 `FINDATA_PATH` 覆盖

## 跑法
    python scripts/findata_health_stage.py \\
        --report data/reports/ablation_eval.json \\
        --output data/reports/findata_health.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 把 findata 加进 sys.path（包外引用，最轻量的"模块化"形态）
_DEFAULT_FINDATA = Path("~/WorkBuddy/Worktrees/FinData-Agent/master-975715f9").expanduser()
_FINDATA_SRC = Path(os.environ.get("FINDATA_PATH", str(_DEFAULT_FINDATA)) + "/src")


def _bootstrap_findata() -> None:
    if not _FINDATA_SRC.exists():
        raise FileNotFoundError(
            f"找不到 findata 源码路径 {_FINDATA_SRC}。\n"
            f"请设置环境变量 FINDATA_PATH 指向 findata 仓库根目录，"
            f"或把项目放在默认桌面路径下。"
        )
    if str(_FINDATA_SRC) not in sys.path:
        sys.path.insert(0, str(_FINDATA_SRC))


def main() -> int:
    parser = argparse.ArgumentParser(description="mm-curation → findata 健康巡检 stage")
    parser.add_argument(
        "--report",
        default="data/reports/ablation_eval.json",
        help="mm-curation 的清洗收益报告（用于写进 findata 巡检上下文）",
    )
    parser.add_argument(
        "--output",
        default="data/reports/findata_health.md",
        help="findata 巡检报告落点",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="不打印巡检告警，只写文件",
    )
    args = parser.parse_args()

    _bootstrap_findata()
    from findata.eval._interop import import_curation_eval  # noqa: F401  顺手验证

    ce = import_curation_eval()
    if ce is None and not args.quiet:
        print(
            "[警告] 未找到 mm-curation-pipeline 的 curation_eval 包，"
            "Cohen κ 一致性指标将走本地副本。"
        )

    # mm-curation 的产物摘要（写进 findata 巡检上下文里）
    upstream_summary = _load_summary(args.report)

    from findata.report import Snapshot, render_markdown, run_inspection

    snap = Snapshot.from_synthetic(seed=20240102)
    result = run_inspection(snap)
    report_md = render_markdown(
        result,
        header_extra=(
            f"## 上下游：本次巡检由 mm-curation-pipeline 触发\n\n"
            f"- 上游阶段：{upstream_summary.get('stage', 'ablation_eval')}\n"
            f"- 上游输入样本数：{upstream_summary.get('n_input', '?')}\n"
            f"- 上游漏斗后样本数：{upstream_summary.get('n_kept', '?')}\n"
            f"- 上游 Recall@1：{upstream_summary.get('recall_at_1', '?')}\n"
            f"- 上游 MRR：{upstream_summary.get('mrr', '?')}\n"
            f"- findata 巡检摘要：信号 {result.summary.n_findings}、"
            f"告警 {result.summary.n_alerts}、抑制 {result.summary.n_suppressed}、"
            f"健康分 {result.summary.health_score}\n"
        ),
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_md, encoding="utf-8")

    if not args.quiet:
        print(f"已写入 {out}")
        print(f"健康分 {result.summary.health_score} ({result.summary.grade()})")
        sev = result.summary.by_severity
        print(
            f"告警 {result.summary.n_alerts} 条"
            f"（P0 {sev.get('P0', 0)} · P1 {sev.get('P1', 0)} · P2 {sev.get('P2', 0)}）"
        )
        print(f"误报抑制 {result.summary.n_suppressed} 条")
        if result.alerts:
            print("\n前 3 条告警：")
            for a in result.alerts[:3]:
                print(
                    f"  {a.diagnosis.severity.value} {a.finding.symbol} "
                    f"{a.finding.probe} → {a.diagnosis.root_cause.value}"
                )

    # 把上游摘要 + 巡检结果一起写到 json，方便后续看板上挂
    json_out = out.with_suffix(".json")
    json_out.write_text(
        json.dumps(
            {
                "upstream": upstream_summary,
                "findata": {
                    "asof": result.asof.isoformat(),
                    "n_findings": result.summary.n_findings,
                    "n_alerts": result.summary.n_alerts,
                    "n_suppressed": result.summary.n_suppressed,
                    "health_score": result.summary.health_score,
                    "health_grade": result.summary.grade(),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if not args.quiet:
        print(f"已写入 {json_out}")

    return 0 if result.summary.by_severity.get("P0", 0) == 0 else 2


def _load_summary(path: str) -> dict:
    """从 mm-curation 的产物里抽最小可用摘要。

    不是为了"完整翻译"——是为了在 findata 报告里写一句"谁触发的"，
    让联动有迹可循。任何"上游传什么"的设计在这里都过度工程。
    """
    p = Path(path)
    if not p.exists():
        return {"stage": "unavailable", "_note": f"未找到 {p}"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"stage": "unavailable", "_note": f"{p} 解析失败"}
    base = data.get("baseline", {})
    rk = base.get("recall_at_k", {})
    return {
        "stage": "ablation_eval",
        "n_input": data.get("n_input"),
        "n_held_out": data.get("n_held_out"),
        "n_kept": base.get("n_kept"),
        "recall_at_1": rk.get("1"),
        "recall_at_5": rk.get("5"),
        "mrr": base.get("mrr"),
        "seconds": base.get("seconds"),
    }


if __name__ == "__main__":
    sys.exit(main())
