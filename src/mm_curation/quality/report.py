"""漏斗质量报告：把 FunnelResult 渲染成 Markdown（人读）+ 保留 JSON（机器读）。

报告三层信息：
1. 漏斗总览：输入 -> 存活，各级通过率
2. 每级分数分布（min/p50/max）——阈值敏感性分析的依据
3. 每级丢弃样本的 ground truth 构成——有了污染器标注，能直接看出
   "这一级扔的是不是它该扔的"（L2 算子上线前 nsfw/语义重复会明确露出）
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from ..pipeline.runner import FunnelResult


def build_report_data(result: FunnelResult, pipeline_name: str, n_input: int) -> dict[str, Any]:
    """聚合出报告数据（JSON 持久化 + Markdown 渲染共用）。"""
    dropped_by_stage: dict[str, Counter] = {}
    for op_name, s in result.dropped:
        dropped_by_stage.setdefault(op_name, Counter())[
            s.labels.get("dirty") or "clean/未标注"
        ] += 1
    stage_rows = []
    for st in result.stats:
        stage_rows.append(
            {
                **asdict(st),
                "pass_rate": round(st.pass_rate, 4),
                "dropped_kinds": dict(dropped_by_stage.get(st.op, {})),
            }
        )
    clean_total = sum(1 for s in result.kept + [s for _, s in result.dropped] if not s.labels)
    dirty_total = sum(1 for s in result.kept + [s for _, s in result.dropped] if s.labels)
    dirty_kept = sum(1 for s in result.kept if s.labels)
    clean_dropped = sum(1 for _, s in result.dropped if not s.labels)
    return {
        "pipeline": pipeline_name,
        "n_input": n_input,
        "n_kept": len(result.kept),
        "stages": stage_rows,
        "ground_truth": {
            "clean_total": clean_total,
            "dirty_total": dirty_total,
            "dirty_recall": round((dirty_total - dirty_kept) / dirty_total, 4)
            if dirty_total
            else None,
            "clean_falsely_killed": clean_dropped,
            "clean_kill_rate": round(clean_dropped / clean_total, 4) if clean_total else None,
            "dirty_leak": dirty_kept,
        },
    }


def render_markdown(data: dict[str, Any]) -> str:
    gt = data["ground_truth"]
    lines = [
        f"# 清洗漏斗报告: {data['pipeline']}",
        "",
        f"- 输入 **{data['n_input']}** -> 存活 **{data['n_kept']}**",
    ]
    if gt.get("dirty_total"):
        lines += [
            f"- 脏数据召回: **{gt['dirty_recall']:.1%}**"
            f"（漏 {gt['dirty_leak']}/{gt['dirty_total']}）",
            f"- 干净误杀: **{gt['clean_kill_rate']:.1%}**"
            f"（{gt['clean_falsely_killed']}/{gt['clean_total']}）",
        ]
    lines += [
        "",
        "| 级 | 算子 | 进入 | 存活 | 丢弃 | 通过率 | score min/p50/max | 丢弃构成 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for st in data["stages"]:
        score = (
            "—"
            if st["score_p50"] is None
            else (f"{st['score_min']:.2f} / {st['score_p50']:.2f} / {st['score_max']:.2f}")
        )
        kinds = "、".join(
            f"{k}×{v}" for k, v in sorted(st["dropped_kinds"].items(), key=lambda kv: -kv[1])
        )
        lines.append(
            f"| {st['op']} | {'批量' if st['batch'] else '单样本'} | {st['n_in']} "
            f"| {st['n_out']} | {st['dropped']} | {st['pass_rate']:.1%} | {score} | {kinds} |"
        )
    lines.append("")
    return "\n".join(lines)
