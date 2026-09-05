"""个人微调平台控制台（V3 η+ 业务层交互入口）。

启动：streamlit run scripts/platform_app.py
四个业务页签：
1. 判官能力矩阵——任务 × 基座的达标状态（读 runs/experiments.jsonl 实测数字）
2. 成本选型计算器——本机小模型 vs 云 API vs 人工（参数可调，实时算钱）
3. A/B 偏好标注——真人选择 → 偏好协议文件（persona-oracle 的真人化入口）
4. 命令速查——四步骨架的一键复现命令
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.tuning.judge_cost import (  # noqa: E402
    CostAssumptions,
    api_cost_per_item,
    cost_table,
    human_cost_per_item,
    local_cost_per_item,
)

LEDGER = Path("runs/experiments.jsonl")
ANNOT = Path("data/annot/pref_labels.jsonl")

# 验收门槛（与 PRD/设计表一致：质量数字一律来自冻结 benchmark 实测）
ACCEPTANCE = {
    "judge_news_v1": ("客观质量裁决", 0.50, "κ"),
    "pref_news_v1": ("偏好裁决", 0.75, "命中率"),
    "ext_news_v1": ("抽取忠实性", 0.75, "命中率"),
}

MODELS = {"0.5B": "Qwen2.5-0.5B", "1.5B": "Qwen2.5-1.5B"}


def load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = []
    for ln in LEDGER.read_text(encoding="utf-8").split("\n"):
        try:
            if ln.strip():
                rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue  # 共享追加文件容错
    return rows


def latest_evals(rows: list[dict]) -> dict[tuple, dict]:
    """按 (benchmark, adapter) 取最新一次评测。"""
    out: dict[tuple, dict] = {}
    for r in rows:
        if r.get("stage") != "eval":
            continue
        key = (r.get("benchmark", "").split("/")[-1], r.get("adapter", "generic"))
        out[key] = r
    return out


# ---- 页面 1：判官能力矩阵 ----


def page_matrix():
    st.header("判官能力矩阵")
    st.caption("任务 × 基座 → 冻结 benchmark 实测结论。全部数字可经 runs/experiments.jsonl 回溯。")
    mfile = Path(__file__).resolve().parents[1] / "benchmarks" / "capability_matrix.json"
    if not mfile.exists():
        st.warning("尚无能力矩阵数据。")
        return
    m = json.loads(mfile.read_text(encoding="utf-8"))
    rows = []
    for model in m["models"]:
        for task in m["tasks"]:
            score = model["scores"].get(task["task"])
            if score is None:
                rows.append(
                    {
                        "任务": task["task"],
                        "判官基座": model["model"],
                        task["metric"]: "未测",
                        "达标线": str(task["threshold"]),
                        "结论": "—",
                    }
                )
                continue
            status = (
                "✅ 可上岗"
                if score >= task["threshold"]
                else ("🔴 资质不足" if score < task["threshold"] - 0.15 else "🟡 边缘")
            )
            rows.append(
                {
                    "任务": task["task"],
                    "判官基座": model["model"],
                    task["metric"]: f"{score:.3f}",
                    "达标线": str(task["threshold"]),
                    "结论": status,
                }
            )
    st.dataframe(rows, use_container_width=True)
    st.info(
        "读法：**❌ 不是失败，是资质结论**——抽取忠实性在 0.5B/1.5B 上均 ≈ 通用基线"
        "（能力悬崖在 1.5B 之后），需 7B+ 基座或任务重设计。矩阵每格由冻结 benchmark "
        "便宜测定——这就是「先造 benchmark 再训练」流程的业务产出。"
    )
    st.subheader("原始评测记录（最新在前）")
    ledger_rows = load_ledger()
    st.json(
        [
            {k: r[k] for k in ("ts", "run", "benchmark", "adapter") if k in r}
            for r in reversed(ledger_rows[-12:])
        ],
        expanded=False,
    )


def page_cost():
    st.header("成本选型计算器")
    st.caption("口径：本机=电费（设备折旧不计）；API=牌价×token 用量；人工=时薪×单条耗时。")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        elec = st.slider("电价（¥/kWh）", 0.3, 1.5, 0.6, 0.05)
    with c2:
        api_in = st.slider("API 输入牌价（¥/百万 tokens）", 0.5, 20.0, 2.0, 0.5)
        api_out = st.slider("API 输出牌价（¥/百万 tokens）", 1.0, 40.0, 4.0, 1.0)
    with c3:
        hourly = st.slider("标注时薪（¥/h）", 15, 80, 30, 5)
        sec_per = st.slider("人工单条耗时（s）", 10, 120, 30, 5)
    with c4:
        n_docs = st.select_slider("待审数据量（条）", options=[10_000, 100_000, 1_000_000])
        tokens_in = st.slider("每条输入 tokens", 400, 2400, 1200, 100)
    a = CostAssumptions(
        electricity_per_kwh=elec,
        api_input_per_m=api_in,
        api_output_per_m=api_out,
        annotator_hourly=float(hourly),
        annotator_seconds_per_item=float(sec_per),
        tokens_in_per_item=float(tokens_in),
    )
    # 实测吞吐（秒/条）：judge_news_v1 ledger 最新；缺省 0.6
    rows = load_ledger()
    spi = 0.6
    for r in reversed(rows):
        if r.get("stage") == "eval" and "judge_news_v1" in r.get("benchmark", ""):
            if r.get("seconds") and (r.get("n_judged") or r.get("n_unparsed") is not None):
                n = r.get("n_judged", 0) + r.get("n_unparsed", 0)
                if n:
                    spi = r["seconds"] / n
                break
    rows_out = cost_table({"0.5B 判官": spi}, a)
    data = {r.judge: r.cost_per_item * n_docs for r in rows_out}
    data["云 API（通用大模型）"] = api_cost_per_item(a) * n_docs
    data["人工抽检"] = human_cost_per_item(a) * n_docs
    local_c = local_cost_per_item(spi, a) * n_docs
    st.subheader(f"审 {n_docs:,} 条的总成本（¥，对数感知柱图）")
    st.bar_chart(data)
    m1, m2, m3 = st.columns(3)
    m1.metric("本机小判官", f"¥{local_c:,.0f}", f"{spi:.2f}s/条（实测）")
    m2.metric(
        "云 API",
        f"¥{data['云 API（通用大模型）']:,.0f}",
        f"{local_c and data['云 API（通用大模型）'] / max(local_c, 1e-9):,.0f}× 本机",
    )
    m3.metric(
        "人工抽检",
        f"¥{data['人工抽检']:,.0f}",
        f"{data['人工抽检'] / max(local_c, 1e-9):,.0f}× 本机",
    )
    st.dataframe(
        [
            {
                "判官": r.judge,
                "¥/条": round(r.cost_per_item, 5),
                f"¥/{n_docs:,.0f} 条": round(r.cost_per_item * n_docs),
                "质量口径": r.quality,
            }
            for r in rows_out
        ]
        + [
            {
                "判官": "云 API（通用大模型）",
                "¥/条": round(api_cost_per_item(a), 5),
                f"¥/{n_docs:,.0f} 条": round(api_cost_per_item(a) * n_docs),
                "质量口径": "未经你的 benchmark 验收",
            },
            {
                "判官": "人工抽检",
                "¥/条": round(human_cost_per_item(a), 5),
                f"¥/{n_docs:,.0f} 条": round(human_cost_per_item(a) * n_docs),
                "质量口径": "金标准，不可全量",
            },
        ],
        use_container_width=True,
    )
    st.warning(
        "业务读法：本机小判官的成本优势是三个数量级的——但它只能上岗能力矩阵里 ✅ 的任务。"
        "❌ 任务（如忠实性裁决）要么换更大基座（成本上移），要么继续用规则+人工。"
        "选型 = 能力矩阵 × 本页。"
    )


# ---- 页面 3：A/B 偏好标注 ----


def page_annotate():
    st.header("A/B 偏好标注（真人偏好 → 协议文件）")
    st.caption("η-a 的 persona-oracle 由此升级为真人闭环：你的选择就是训练信号。")
    protocol = st.text_area(
        "偏好协议（将写入每条标注，DPO 数据构造时按协议归组）",
        value="只保留核心事实：时间、地点、主体、结果。冗余细节应删尽删。",
        height=90,
    )
    st.markdown("**候选甲**")
    cand_a = st.text_area("候选甲全文", height=140, label_visibility="collapsed")
    st.markdown("**候选乙**")
    cand_b = st.text_area("候选乙全文", height=140, label_visibility="collapsed")
    note = st.text_input("备注（可选，如文档来源）")
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("我选 甲", use_container_width=True, type="primary"):
            _save_label(protocol, "甲", cand_a, cand_b, note)
    with c2:
        if st.button("我选 乙", use_container_width=True, type="primary"):
            _save_label(protocol, "乙", cand_a, cand_b, note)
    with c3:
        if st.button("丢弃（两个都不合格）", use_container_width=True):
            _save_label(protocol, "REJECT", cand_a, cand_b, note)
    st.divider()
    if ANNOT.exists():
        labels = [
            json.loads(ln) for ln in ANNOT.read_text(encoding="utf-8").split("\n") if ln.strip()
        ]
        st.success(f"已积累 {len(labels)} 条真人偏好标注 → {ANNOT}")
        choices = [lb["choice"] for lb in labels]
        st.write(
            {"甲": choices.count("甲"), "乙": choices.count("乙"), "丢弃": choices.count("REJECT")}
        )
        st.caption("标注文件即 DPO 数据构造的输入（dev: preference.py 消费）。")
    else:
        st.info("尚无标注。选一个候选即可开始积累。")


def _save_label(protocol: str, choice: str, a: str, b: str, note: str) -> None:
    ANNOT.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "protocol": protocol,
        "choice": choice,
        "cand_a_chars": len(a),
        "cand_b_chars": len(b),
        "note": note,
    }
    with ANNOT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    st.toast(f"已记录：{choice}")


# ---- 页面 4：命令速查 ----


def page_commands():
    st.header("四步骨架·一键复现")
    st.caption("所有命令 Windows/Git Bash 实测；完整口径见 docs/RUNBOOK.md。")
    blocks = {
        "① 数据获取（爬虫，robots 合规/幂等）": (
            "python -X utf8 scripts/fetch_news_corpus.py --max-docs 2000"
        ),
        "② 客观质量判官（ζ）": (
            "python -X utf8 scripts/build_judge_benchmark.py\n"
            "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \\\n"
            "  python -X utf8 scripts/finetune_judge_lora.py \\\n"
            "  --n-clean 500 --n-dirty 500 --epochs 3 --batch 4\n"
            "python -X utf8 scripts/run_judge_benchmark.py --adapter models/judge_lora_v1"
        ),
        "③ 偏好闭环（η-a）": (
            "python -X utf8 scripts/build_pref_data.py\n"
            "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \\\n"
            "  python -X utf8 scripts/finetune_judge_dpo.py \\\n"
            "  --persona PA --out models/judge_pref_PA\n"
            "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \\\n"
            "  python -X utf8 scripts/finetune_judge_dpo.py \\\n"
            "  --persona PB --out models/judge_pref_PB\n"
            "python -X utf8 scripts/run_pref_benchmark.py"
        ),
        "④ 抽取忠实性判官（η-b）": (
            "python -X utf8 scripts/build_ext_data.py\n"
            "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \\\n"
            "  python -X utf8 scripts/finetune_judge_dpo.py \\\n"
            "  --persona EXT --data data/interim/ext_dpo.jsonl \\\n"
            "  --out models/judge_ext_v1 --max-prompt 1120 --max-length 1168\n"
            "python -X utf8 scripts/run_pref_benchmark.py \\\n"
            "  --benchmark benchmarks/ext_news_v1 \\\n"
            "  --adapters EXT=models/judge_ext_v1 --max-length 1700"
        ),
        "成本报告": "python -X utf8 scripts/judge_cost_report.py",
    }
    for name, cmd in blocks.items():
        with st.expander(name):
            st.code(cmd, language="bash")


PAGES = {
    "判官能力矩阵": page_matrix,
    "成本选型计算器": page_cost,
    "A/B 偏好标注": page_annotate,
    "命令速查": page_commands,
}

if __name__ == "__main__":
    page = st.sidebar.radio("导航", list(PAGES), label_visibility="collapsed")
    st.sidebar.caption(
        "个人微调平台 · 自己的数据 → 自己的 benchmark → 自己的模型\n\ndocs/PRD.md · docs/RUNBOOK.md"
    )
    PAGES[page]()
