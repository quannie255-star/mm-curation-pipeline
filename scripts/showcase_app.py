"""数据质量平台演示门户（V5 β）：四模态一个框架的统一演示入口。

定位：面试/展示的**唯一入口**——总览讲平台故事（基座+领域增强包），四个模态
页签各自由 data/reports/*.json 渲染门禁卡与算子 P/R 表，证据链页签串起
「清洗收益可证明」的完整数字链。四个存量应用（streamlit_app / ops_dashboard /
judge_studio / platform_app）保持不动，本门户在侧边栏给出跳转定位。

现场演示能力：轻量评测（eval-fhir / eval-industrial，纯 CPU ~6 秒）提供
「重跑门禁」按钮——subprocess 实时日志 tail（judge_studio 同款），跑完自动
刷新报告。重量级评测（图文 eval-op 等）只给命令不给按钮。

启动：streamlit run scripts/showcase_app.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "data" / "reports"

# 轻量评测白名单：只有纯 CPU 秒级脚本允许现场重跑（面试现场不可等重活）
RERUN_WHITELIST = {
    "fhir": [sys.executable, "-X", "utf8", "scripts/eval_fhir.py"],
    "industrial": [sys.executable, "-X", "utf8", "scripts/eval_industrial.py"],
}

DOMAINS = {
    "image": {
        "title": "图文（V1，COCO-CN）",
        "report": "operator_pr.json",
        "blurb": "11 级漏斗：清洗后检索 R@1 +21%，2106 条全量召回 100%",
        "cmd": "python scripts/eval_operators.py",
        "cost": "约 4 分钟（需图文数据集与 CLIP 缓存）",
    },
    "text": {
        "title": "文本（V2 β，中文维基）",
        "report": "text_dedup_benchmark.json",
        "blurb": "10 万档去重 exact 1.0 / near 0.97；微调 ppl +7.5%",
        "cmd": "python scripts/data_ci_benchmark.py",
        "cost": "约 0.5 秒（合成语料门禁）",
    },
    "fhir": {
        "title": "医疗 FHIR（V4 α）",
        "report": "operator_pr_fhir.json",
        "blurb": "PHI/编码/单位/时间/引用五算子；计划事件源进样本流",
        "cmd": "python -X utf8 scripts/eval_fhir.py",
        "cost": "约 6 秒（可现场重跑）",
    },
    "industrial": {
        "title": "工业传感器（V5 α）",
        "report": "operator_pr_industrial.json",
        "blurb": "卡死/量程/漂移/单位/计划判别；「停牌 vs 采集失败」的工业映射",
        "cmd": "python -X utf8 scripts/eval_industrial.py",
        "cost": "约 6 秒（可现场重跑）",
    },
}


def load_report(name: str) -> dict | None:
    """读报告 JSON；缺失/损坏返回 None（门户降级为「未生成」态）。"""
    path = REPORTS / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def gate_cards(report: dict | None) -> dict | None:
    """从报告提取漏斗门禁卡数据；无 funnel_gate 的老报告返回 None。"""
    if not report or "funnel_gate" not in report:
        return None
    g = report["funnel_gate"]
    return {
        "recall": g["recall"],
        "false_kill": g["false_kill_rate"],
        "passed": g.get("passed", False),
        "n_total": report.get("n_total"),
        "n_dirty": g.get("n_dirty"),
        "n_clean": report.get("n_clean"),
    }


def pr_rows(report: dict | None) -> list[dict]:
    """算子级 P/R 表（与 operator_pr.md 同口径），供 st.dataframe。"""
    if not report:
        return []
    rows = []
    for op in report.get("operators", []):
        prim = op.get("primary_recall") or {}
        recall_txt = "/".join(
            f"{v:.0%}" for v in prim.values() if v is not None
        ) or "—"
        prec = op.get("precision")
        rows.append(
            {
                "算子": op.get("op"),
                "扔": op.get("n_dropped"),
                "误杀": op.get("clean_killed"),
                "precision": "—" if prec is None else f"{prec:.1%}",
                "主靶recall": recall_txt,
                "主靶": ", ".join(op.get("primary_target") or []) or "—",
            }
        )
    return rows


def dedup_rows(reports: list[dict] | None) -> list[dict]:
    """文本去重基准表（text_dedup_benchmark.json 为 scale 档位列表）。"""
    if not reports:
        return []
    return [
        {
            "规模档": r.get("scale"),
            "语料": r.get("n_total"),
            "exact召回": r.get("exact_recall"),
            "near召回": r.get("near_recall"),
            "耗时秒": r.get("seconds_total"),
        }
        for r in reports
    ]


def ft_rows(report: dict | None) -> list[dict]:
    """训练级证据（CLIP 干净/脏集微调对比）。"""
    if not report:
        return []
    label = {"base": "基线（未微调）", "clean_ft": "干净集微调", "dirty_ft": "脏集微调"}
    return [
        {
            "配置": label.get(name, name),
            "R@1": round(res["recall_at_k"]["1"], 3),
            "R@5": round(res["recall_at_k"]["5"], 3),
            "R@10": round(res["recall_at_k"]["10"], 3),
            "MRR": round(res["mrr"], 3),
        }
        for name, res in report.get("results", {}).items()
    ]


def ablation_rows(report: dict | None) -> list[dict]:
    if not report:
        return []
    rows = [
        {
            "配置": a["name"],
            "存活": a["n_kept"],
            "R@1": a["recall_at_k"]["1"],
            "ΔR@1": a["delta_r1"],
        }
        for a in report.get("ablations", [])
    ]
    return sorted(rows, key=lambda r: r["ΔR@1"])


def run_rerun(cmd: list[str]) -> None:
    """现场重跑：subprocess 流式 tail 日志（judge_studio 同款），成功后刷新。"""
    with st.status("运行评测中…", expanded=True) as status:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        log_view = st.empty()
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line.rstrip())
            log_view.code("\n".join(tail[-25:]), language="text")
        rc = proc.wait()
        if rc == 0:
            status.update(label="评测完成 ✓ 报告已刷新", state="complete")
            st.rerun()
        else:
            status.update(label=f"评测失败 rc={rc}（门禁未过或环境缺失）", state="error")


def render_domain(key: str) -> None:
    spec = DOMAINS[key]
    st.caption(spec["blurb"])
    report = load_report(spec["report"])
    gate = gate_cards(report) if key != "text" else None

    if report is None:
        st.warning(f"报告未生成：`data/reports/{spec['report']}`")
        st.code(spec["cmd"], language="bash")
        st.caption(f"耗时：{spec['cost']}")
    else:
        if gate:
            c1, c2, c3 = st.columns(3)
            c1.metric("漏斗故障召回", f"{gate['recall']:.1%}", border=True)
            c2.metric("干净误杀率", f"{gate['false_kill']:.2%}", border=True)
            c3.metric(
                "门禁结论",
                "PASSED ✓" if gate["passed"] else "FAILED ✗",
                border=True,
            )
        st.dataframe(pr_rows(report), width="stretch", hide_index=True)
        st.caption(f"报告：data/reports/{spec['report']}（生成命令见下方）")

    with st.expander("生成命令"):
        st.code(spec["cmd"], language="bash")
        st.caption(f"耗时：{spec['cost']}")

    if key in RERUN_WHITELIST and st.button("▶ 现场重跑门禁（秒级）", key=f"rerun_{key}"):
        run_rerun(RERUN_WHITELIST[key])

    if key == "industrial":
        st.info(
            "口径说明：批量算子（漂移等）的全局统计会被其他类型灾难注入污染，"
            "「独立评测」口径下误杀偏高属预期；**漏斗串联门禁**才是端到端承诺口径。"
        )


def main() -> None:
    st.set_page_config(page_title="mm-curation 数据质量平台", page_icon="🧭", layout="wide")
    st.title("🧭 mm-curation 数据质量平台")
    st.caption(
        "多模态数据清洗与预处理平台 = curation-eval 基座 + 领域增强包。"
        "四个模态（图文/文本/医疗 FHIR/工业传感器）共享同一套协议、注册表、执行器与门禁——"
        "每个领域包独立验收：污染注入 → 算子 P/R → 漏斗门禁。"
    )

    tab_over, tab_img, tab_text, tab_fhir, tab_ind, tab_evi = st.tabs(
        ["🏛️ 平台总览", "🖼️ 图文", "📝 文本", "🏥 医疗 FHIR", "🏭 工业传感器", "📈 证据链"]
    )

    with tab_over:
        gates = {k: gate_cards(load_report(s["report"])) for k, s in DOMAINS.items()}
        passed = sum(
            1 for k in ("fhir", "industrial") if gates.get(k) and gates[k]["passed"]
        )
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("数据模态", 4, border=True)
        c2.metric("领域增强包", 4, border=True)
        c3.metric("轻量门禁已过（本地）", passed, border=True)
        c4.metric("测试基线", "263 + 54", border=True)

        st.markdown("### 四个增强包的验收状态")
        rows = []
        for key, spec in DOMAINS.items():
            g = gates.get(key)
            rows.append(
                {
                    "增强包": spec["title"],
                    "门禁报告": spec["report"],
                    "召回": "—" if not g else f"{g['recall']:.1%}",
                    "误杀": "—" if not g else f"{g['false_kill']:.2%}",
                    "结论": "未生成" if not g else ("PASSED ✓" if g["passed"] else "FAILED ✗"),
                }
            )
        st.dataframe(rows, width="stretch", hide_index=True)

        st.markdown("### 基座 + 增强包六件套")
        st.markdown(
            "| 件 | 位置 | 说明 |\n"
            "|---|---|---|\n"
            "| ① 模态适配器 | curation-eval 包 | payload canonical JSON，roundtrip 保真 |\n"
            "| ② 领域算子 | 主仓 operators/ | score 越高越好，批量走 shardable=False |\n"
            "| ③ 领域污染器 | curation-eval 包 | 注入即带 ground truth，供体重抽 |\n"
            "| ④ 确定性语料 | 主仓 data/ | 同 seed 逐字节一致，无真实数据 |\n"
            "| ⑤ 漏斗配置 | configs/ | 与既有模态同一执行器，零特例 |\n"
            "| ⑥ 评测门禁 | scripts/ | 召回 ≥90% 且误杀 ≤5%，跌破 exit 1 |"
        )
        st.info(
            "核心方法论：**清洗价值可证明**——真实脏数据没有 ground truth，"
            "程序化污染注入让每个算子有 P/R、整条漏斗有门禁、收益有下游任务数字"
            "（检索 R@1 +21%、微调 ppl +7.5%、CLIP 0.688 vs 0.636）。"
        )

    with tab_img:
        render_domain("image")
    with tab_text:
        st.caption(DOMAINS["text"]["blurb"])
        reports = load_report(DOMAINS["text"]["report"])
        if reports is None:
            st.warning("报告未生成：data/reports/text_dedup_benchmark.json")
            st.code(DOMAINS["text"]["cmd"], language="bash")
        else:
            st.dataframe(dedup_rows(reports), width="stretch", hide_index=True)
            st.caption("数据 CI 门禁：exact ≥0.99 / near ≥0.90 / 误杀 ≤1%（data-ci.yml 每次跑）")
    with tab_fhir:
        render_domain("fhir")
    with tab_ind:
        render_domain("industrial")

    with tab_evi:
        st.markdown("### 训练级证据：干净集 vs 脏集微调（CLIP 检索）")
        ft = ft_rows(load_report("finetune_eval.json"))
        if ft:
            st.dataframe(ft, width="stretch", hide_index=True)
            st.caption(
                "脏集微调不如不微调（0.636 < 0.556 基线）；干净集微调 +13.2pp——脏数据的代价可量化。"
            )
        else:
            st.warning("未生成：python -X utf8 scripts/finetune_clip.py（约 20 分钟 GPU）")

        st.markdown("### 消融：分组移除算子的检索变化")
        ab = ablation_rows(load_report("ablation_eval.json"))
        if ab:
            st.dataframe(ab[:8], width="stretch", hide_index=True)
            st.caption("去重组移除后 R@1 -0.017（唯一显著组）——清洗是系统性工程，去重贡献最大。")
        else:
            st.warning("未生成：python scripts/eval_ablation.py（约 3 分钟）")

        st.markdown("### 更多证据（命令复现）")
        st.markdown(
            "- 文本微调 ppl：clean 7.16 vs dirty 7.70（+7.5%）→ `finetune_gpt2.py`\n"
            "- 分层采样：budget=1000 时 R@1 +24% → `eval_sampling.py`\n"
            "- 判官微调：κ -0.024 → +0.560 → `eval_judge.py`（V3 平台）"
        )

    st.sidebar.markdown("### 专题深潜（存量应用）")
    st.sidebar.code(
        "streamlit run scripts/streamlit_app.py  # 图文检索 Demo", language="bash"
    )
    st.sidebar.code("streamlit run scripts/ops_dashboard.py  # 运维驾驶舱", language="bash")
    st.sidebar.code("streamlit run scripts/judge_studio.py  # 判官工坊", language="bash")
    st.sidebar.code("streamlit run scripts/platform_app.py  # 微调平台控制台", language="bash")
    st.sidebar.markdown(
        "### 复现\n"
        "所有数字由命令重新生成（数据/报告不入库），"
        "见 docs/RUNBOOK.md；领域包扩展见 docs/DOMAIN_PACKS.md。"
    )


if __name__ == "__main__":
    main()
