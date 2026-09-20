"""数据质量平台演示门户（V5 β）：四模态一个框架的统一演示入口。

设计语言：「数据净水厂」——脏数据是原水，算子是滤级，门禁是出厂验收。
视觉收敛（洁净室蓝绿，语义色只给门禁结论），唯一大胆处是首屏净水流程条
与实时门禁读数仪。包装层面向第一次来的个人使用者：30 秒看懂 + 角色路线。

启动：python -m streamlit run scripts/showcase_app.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "data" / "reports"

# 门面数字：每一轮收工用 `pytest --collect-only` 实点回写，并把日期写在 help 里。
# 历史教训——这个数被写错过四次，根因都是"凭记忆填"；把它做成常量 + 带日期，
# 至少能让下一个改它的人看见它是"某天实点的快照"而不是永恒真理。
# 改这里要同步：README 状态表 / DEV_PLAN 测试基线行 / PROOF_CHAIN 代码测试行。
TEST_COUNT_MAIN = 328
TEST_COUNT_PKG = 67
TEST_COUNT_ASOF = "2026-09-18"
# 实点 `available_operators()`（同一注册表跨四模态共用）。曾写「24」——腐烂了。
OP_COUNT = 29

# 轻量评测白名单：只有纯 CPU 秒级脚本允许现场重跑（面试现场不可等重活）
RERUN_WHITELIST = {
    "fhir": [sys.executable, "-X", "utf8", "scripts/eval_fhir.py"],
    "industrial": [sys.executable, "-X", "utf8", "scripts/eval_industrial.py"],
}

DOMAINS = {
    "image": {
        "title": "图文数据",
        "tag": "图像 + 文字描述",
        "report": "operator_pr.json",
        "blurb": "11 级滤芯：模糊图、重复图、图文不符、低质描述……清洗后检索准确率提升 21%",
        "cmd": "python scripts/eval_operators.py",
        "cost": "约 4 分钟（需要先准备图文数据集）",
    },
    "text": {
        "title": "文本数据",
        "tag": "网页文章 / 语料库",
        "report": "text_dedup_benchmark.json",
        "blurb": "30 万篇维基文本：转载重复、乱码、模板水文。去重 exact 100%，微调质量 +7.5%",
        "cmd": "python scripts/data_ci_benchmark.py",
        "cost": "约 0.5 秒",
    },
    "fhir": {
        "title": "医疗数据",
        "tag": "医院信息系统的 FHIR 资源",
        "report": "operator_pr_fhir.json",
        "blurb": "隐私残留、编码写错、单位混乱、时间倒挂、检查单指向不存在的患者——五个滤芯各管一种",
        "cmd": "python -X utf8 scripts/eval_fhir.py",
        "cost": "约 6 秒",
    },
    "industrial": {
        "title": "工业传感器",
        "tag": "产线设备时序读数",
        "report": "operator_pr_industrial.json",
        "blurb": "传感器卡死、超量程、校准漂移、单位混用；计划检修的停数是正常的，链路断了才是故障",
        "cmd": "python -X utf8 scripts/eval_industrial.py",
        "cost": "约 6 秒",
    },
}

CSS = """
<style>
:root {
  --paper: #F7F9FA; --ink: #1C2B33; --sub: #5B6E76; --line: #D8E1E5;
  --water: #0E7490; --wash: #E8F2F4; --pass: #15803D; --fail: #B91C1C;
}
.stApp { background: var(--paper); color: var(--ink); }
#MainMenu { visibility: hidden; }
h1 { font-weight: 650; letter-spacing: -0.02em; }
p, li { line-height: 1.65; }
.block-container { padding-top: 2.2rem; max-width: 1180px; }

/* 指标读数卡：仪器面板风，细边框直读，无投影 */
[data-testid="stMetric"] {
  background: #FFFFFF; border: 1px solid var(--line);
  border-radius: 10px; padding: 12px 16px;
}
[data-testid="stMetricValue"] { font-weight: 700; color: var(--ink); }
[data-testid="stMetricLabel"] { color: var(--sub); }

/* 页签：分段控制 */
.stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid var(--line); }
.stTabs [data-baseweb="tab"] {
  padding: 8px 18px; border-radius: 8px 8px 0 0;
  color: var(--sub); font-weight: 550;
}
.stTabs [aria-selected="true"] {
  background: #FFFFFF; color: var(--water);
  border-top: 2px solid var(--water);
}

/* 按钮：主动语态的实心主行动 */
.stButton > button {
  border-radius: 9px; border: 1px solid var(--water);
  background: var(--water); color: #FFFFFF; font-weight: 600;
}
.stButton > button:hover { background: #155E75; border-color: #155E75; color: #FFF; }

[data-testid="stDataFrame"] {
  border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  background: #FFF;
}
[data-testid="stSidebar"] { background: #FFFFFF; border-right: 1px solid var(--line); }
.stCodeBlock code { border-radius: 8px; }

/* 净水流程条：唯一的视觉重心 */
.hero { background: #FFFFFF; border: 1px solid var(--line); border-radius: 14px;
        padding: 26px 30px 18px; margin-bottom: 14px; }
.hero-flow { display: flex; align-items: stretch; gap: 0; margin: 14px 0 6px; }
.hero-step { flex: 1; text-align: center; padding: 14px 8px;
             background: var(--wash); border-radius: 10px; }
.hero-step .ico { font-size: 22px; }
.hero-step .nm { font-weight: 650; margin-top: 4px; }
.hero-step .ds { font-size: 12.5px; color: var(--sub); margin-top: 2px; line-height: 1.4; }
.hero-pipe { display: flex; align-items: center; padding: 0 7px; color: var(--water);
             font-size: 20px; font-weight: 300; }
.readout { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
.readout .cell { flex: 1; min-width: 200px; border: 1px solid var(--line);
                 border-radius: 8px; padding: 8px 14px; background: var(--paper);
                 display: flex; justify-content: space-between; align-items: center; }
.readout .nm { font-weight: 600; font-size: 13.5px; }
.readout .val { font-size: 13px; color: var(--sub); }
.pill { font-weight: 700; font-size: 12.5px; padding: 2px 10px; border-radius: 999px; }
.pill.pass { color: var(--pass); border: 1px solid var(--pass); }
.pill.na { color: var(--sub); border: 1px solid var(--line); }
@media (max-width: 820px) { .hero-flow { flex-direction: column; }
  .hero-pipe { transform: rotate(90deg); padding: 2px 0; justify-content: center; } }
</style>
"""

WHAT_IS_THIS = (
    "这是一套**给数据做质检的流水线**。把脏数据当成原水：先沉淀（规则检查，"
    "比如太短的文本、模糊的图片）、再过滤（去重、结构校验）、最后深度净化"
    "（模型判读）。每一级滤芯都有质检报告——抓坏了多少（**召回**）、错伤了多少"
    "好数据（**误杀**）——出厂前还要过一道验收门禁，不达标不出厂。"
)


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
        recall_txt = "/".join(f"{v:.0%}" for v in prim.values() if v is not None) or "—"
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
    label = {"base": "基线（未微调）", "clean_ft": "用干净数据训练", "dirty_ft": "用脏数据训练"}
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


# --- V6 清洗过程可视化（设计表决策点 10）-------------------------------
# 与业界的差异化落点：Data-Juicer 的 op_effect 能调参数看保留/丢弃，但不告诉你
# 阈值该定多少、依据是什么；tracer 能看被过滤样本，但那是运行期内存态、不落盘。
# 这三个纯函数对应的就是那三样：逐级水位、记录级台账、有预算依据的门限反推。


def load_verdicts(relpath: str) -> list[dict]:
    """读判决台账 JSONL（禁用 splitlines——U+2028 陷阱，笔记 #44）。"""
    path = REPORTS / relpath
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 半行/损坏行跳过，不因一条坏数据让整页空转
    return rows


VERDICT_RELPATH = "normalize_ablation/B/verdict.jsonl"


@st.cache_data(show_spinner=False)
def cached_verdicts(relpath: str = VERDICT_RELPATH) -> list[dict]:
    """UI 侧缓存包装：判决台账是**万行级**文件，不缓存会被 Streamlit 的重跑机制放血。

    Streamlit 每次控件交互都从头重跑脚本 —— 阈值沙盘的滑块动一下、判决表的
    下拉换一项，都要重新解析一遍整个台账。缓存只挡「同一文件重复读」，
    不改变纯函数 `load_verdicts` 的语义：单测直接测纯函数（bare 模式下
    `st.cache_data` 退化为普通调用，仍可离线跑）。
    """
    return load_verdicts(relpath)


def waterfall_rows(report: dict | None) -> list[dict]:
    """漏斗逐级水位对照（原样 vs 归一化）——「补这一层有没有用」的现场读数。"""
    if not report:
        return []
    return [
        {
            "滤级": r["op"],
            "原样·拦截": r["dropped_A"],
            "归一化·拦截": r["dropped_B"],
            "Δ": r["delta_dropped"],
        }
        for r in report.get("comparison", {}).get("per_op", [])
    ]


def verdict_table(
    rows: list[dict],
    op: str | None = None,
    decision: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """判决台账（可筛）——「这一条为什么被删」的单条可读视图。"""
    out: list[dict] = []
    for r in rows:
        if op and r.get("op") != op:
            continue
        if decision and r.get("decision") != decision:
            continue
        out.append(
            {
                "级": r.get("seq"),
                "滤芯": r.get("op"),
                "判决": "删" if r.get("decision") == "drop" else "留",
                "分数": None if r.get("score") is None else round(r["score"], 4),
                "门限": ", ".join(f"{k}={v}" for k, v in (r.get("threshold") or {}).items())
                or "—",
                "判据": r.get("rule"),
                "指纹": (r.get("input_fingerprint") or "")[7:19],
            }
        )
        if len(out) >= limit:
            break
    return out


def score_values(rows: list[dict], op: str) -> list[float]:
    """某一级滤芯的分数列（判决台账是唯一数据源，不另算）。"""
    return [
        r["score"]
        for r in rows
        if r.get("op") == op and isinstance(r.get("score"), (int, float))
    ]


def score_histogram(scores: list[float], bins: int = 24) -> list[dict]:
    """分数分布直方图（st.bar_chart 的数据形态；纯函数，可单测）。"""
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi <= lo:
        return [{"区间": f"{lo:.3g}", "样本数": len(scores)}]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in scores:
        counts[min(int((v - lo) / width), bins - 1)] += 1
    return [{"区间": f"{lo + i * width:.3g}", "样本数": c} for i, c in enumerate(counts)]


def recommend_threshold(scores: list[float], *, side: str, max_drop_rate: float) -> dict | None:
    """由分数分布 + **丢弃预算**反推门限（「有依据的阈值」的最小形态）。

    口径诚实声明：这里给的是**丢弃预算**（愿意最多丢多少），**不是误杀率**。
    误杀率需要 ground truth 或人工复核标签——W3 的人审层接上后才成立。
    混用这两个口径就是「假精确」，所以界面上分两处标清楚。
    """
    if not scores:
        return None
    s = sorted(scores)
    n = len(s)
    if side == "min":
        k = min(max(int(round(max_drop_rate * n)), 0), n - 1)
        thr = s[k]
        dropped = sum(1 for v in s if v < thr)
    else:
        k = min(max(int(round((1 - max_drop_rate) * n)), 0), n - 1)
        thr = s[k]
        dropped = sum(1 for v in s if v > thr)
    return {
        "threshold": thr,
        "drop_rate": dropped / n,
        "n": n,
        "score_min": s[0],
        "score_p50": s[n // 2],
        "score_max": s[-1],
    }


def current_threshold(rows: list[dict], op: str) -> dict:
    """该级滤芯当前生效的门限（取台账里任一行的 threshold 字段）。"""
    for r in rows:
        if r.get("op") == op and r.get("threshold"):
            return r["threshold"]
    return {}


def run_rerun(cmd: list[str]) -> None:
    """现场重跑：subprocess 流式 tail 日志（judge_studio 同款），成功后刷新。"""
    with st.status("质检运行中，正在重新生成报告…", expanded=True) as status:
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
            status.update(label="质检完成，报告已刷新", state="complete")
            st.rerun()
        else:
            status.update(
                label=f"运行结束但门禁未通过（退出码 {rc}），报告已照常落盘", state="error"
            )


def render_hero() -> None:
    gates = {k: gate_cards(load_report(s["report"])) for k, s in DOMAINS.items()}
    st.markdown(
        """
        <div class="hero">
        <div class="hero-flow">
          <div class="hero-step"><div class="ico">🚰</div><div class="nm">原水</div>
            <div class="ds">脏数据：重复、乱码、隐私残留、传感器坏数</div></div>
          <div class="hero-pipe">──</div>
          <div class="hero-step"><div class="ico">🧱</div><div class="nm">沉淀</div>
            <div class="ds">规则滤芯：长度、量程、编码格式</div></div>
          <div class="hero-pipe">──</div>
          <div class="hero-step"><div class="ico">🧺</div><div class="nm">过滤</div>
            <div class="ds">去重与校验：转载、断链、单位混用</div></div>
          <div class="hero-pipe">──</div>
          <div class="hero-step"><div class="ico">🔬</div><div class="nm">深度净化</div>
            <div class="ds">模型与判别：漂移、时间一致性</div></div>
          <div class="hero-pipe">──</div>
          <div class="hero-step"><div class="ico">💧</div><div class="nm">出水</div>
            <div class="ds">带质检报告的可信数据集</div></div>
        </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    cells = []
    for key, spec in DOMAINS.items():
        g = gates.get(key)
        if g:
            val = f"召回 {g['recall']:.0%} · 误杀 {g['false_kill']:.1%}"
            pill = (
                '<span class="pill pass">合格出厂</span>'
                if g["passed"]
                else '<span class="pill na">未达标</span>'
            )
        else:
            val = "报告未生成"
            pill = '<span class="pill na">待质检</span>'
        cells.append(
            f'<div class="cell"><span class="nm">{spec["title"]}</span>'
            f'<span class="val">{val}</span>{pill}</div>'
        )
    st.markdown(f'<div class="readout">{"".join(cells)}</div>', unsafe_allow_html=True)


def render_domain(key: str) -> None:
    spec = DOMAINS[key]
    st.markdown(f"**{spec['tag']}**——{spec['blurb']}")
    report = load_report(spec["report"])
    gate = gate_cards(report) if key != "text" else None

    if report is None:
        st.warning(
            f"这个领域的质检报告还没生成。运行下面的命令（{spec['cost']}），"
            "跑完回到本页就能看到读数。"
        )
        st.code(spec["cmd"], language="bash")
    else:
        if gate:
            c1, c2, c3 = st.columns(3)
            c1.metric("故障召回（坏数据抓住了多少）", f"{gate['recall']:.1%}", border=True)
            c2.metric("误杀率（好数据错伤了多少）", f"{gate['false_kill']:.2%}", border=True)
            c3.metric("出厂验收", "合格" if gate["passed"] else "未达标", border=True)
        st.dataframe(pr_rows(report), width="stretch", hide_index=True)
        st.caption(
            "每一行是一个滤芯：扔 = 拦下的数据量，误杀 = 错拦的好数据，"
            "主靶recall = 对它负责的那类脏数据抓到了多少。"
        )

    with st.expander("想自己跑一遍？"):
        st.code(spec["cmd"], language="bash")
        st.caption(f"耗时：{spec['cost']}。数据与报告不入库，全部可由命令重新生成。")

    if key in RERUN_WHITELIST and st.button("重跑门禁，约 6 秒", key=f"rerun_{key}"):
        run_rerun(RERUN_WHITELIST[key])

    if key == "industrial":
        st.info(
            "读数口径：独立评测时，漂移滤芯的全局统计会被其他坏数据干扰，"
            "误杀偏高是已知现象；**流水线串联门禁**（先拦灾难数据、再测漂移）"
            "才是对外的验收口径。"
        )


def main() -> None:
    st.set_page_config(page_title="mm-curation · 个人数据质量助手", page_icon="💧", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    st.title("把脏数据，变成可信数据集")
    st.caption(
        "mm-curation 是一套跑在你自己电脑上的个人数据质量助手：四个领域（图文、文本、"
        "医疗、工业传感器）共用同一套质检流水线，每个领域都带着可以当场重跑的验收门禁。"
    )
    render_hero()

    with st.expander("第一次来？30 秒看懂它在做什么"):
        st.markdown(WHAT_IS_THIS)
        st.markdown(
            "**接下来去哪，取决于你是谁：**\n"
            "- *我只是好奇*——点上面的「医疗数据」页签，按一下「重跑门禁」，"
            "看它现场质检一遍\n"
            "- *想评估这个项目*——直接看最后的「效果证据」页签：清洗前后的检索、"
            "训练对比数字都在\n"
            "- *想接自己的领域*——照 docs/DOMAIN_PACKS.md 的六步，"
            "写一个领域增强包（最薄的包一个下午能跑通）"
        )

    (
        tab_over,
        tab_img,
        tab_text,
        tab_fhir,
        tab_ind,
        tab_evi,
        tab_proc,
        tab_cal,
    ) = st.tabs(
        [
            "总览",
            "图文数据",
            "文本数据",
            "医疗数据",
            "工业传感器",
            "效果证据",
            "清洗过程",
            "阈值沙盘",
        ]
    )

    with tab_over:
        gates = {k: gate_cards(load_report(s["report"])) for k, s in DOMAINS.items()}
        passed = sum(1 for k in ("fhir", "industrial") if gates.get(k) and gates[k]["passed"])
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("已接入领域", len(DOMAINS), border=True)
        c2.metric(
            "已注册质检算子",
            OP_COUNT,
            border=True,
            help=(
                "实点 `available_operators()`（2026-09-18）——29 个算子共用同一张注册表，"
                "跨四个模态按 `meta.modalities` 声明各自适用范围，不是四套独立实现。"
            ),
        )
        c3.metric("本机门禁合格", passed, border=True)
        c4.metric(
            "自动化测试",
            f"{TEST_COUNT_MAIN} + {TEST_COUNT_PKG}",
            border=True,
            help=(
                f"主仓库 {TEST_COUNT_MAIN} + curation-eval 包 {TEST_COUNT_PKG}，"
                f"实点 `pytest --collect-only`（{TEST_COUNT_ASOF}）。"
                "包侧 5 条 Ray 测试需装 ray 才被收集（本机含）。"
            ),
        )
        st.dataframe(
            [
                {
                    "领域": spec["title"],
                    "质检报告": spec["report"],
                    "召回": "—" if not gates.get(k) else f"{gates[k]['recall']:.1%}",
                    "误杀": "—" if not gates.get(k) else f"{gates[k]['false_kill']:.2%}",
                    "验收": (
                        "未生成" if not gates.get(k)
                        else ("合格" if gates[k]["passed"] else "未达标")
                    ),
                }
                for k, spec in DOMAINS.items()
            ],
            width="stretch",
            hide_index=True,
        )
        st.markdown(
            "**为什么能信这些数字？** 每个领域的质检报告都由同一套方法生成：先程序化注入"
            "已知坏数据（自带标准答案），再让滤芯去抓——抓没抓到、抓错了多少，全是可复现的"
            "硬数字，同一条命令任何机器重跑结果一致。"
        )

    with tab_img:
        render_domain("image")
    with tab_text:
        st.markdown(f"**{DOMAINS['text']['tag']}**——{DOMAINS['text']['blurb']}")
        reports = load_report(DOMAINS["text"]["report"])
        if reports is None:
            st.warning(
                f"质检报告还没生成。运行下面的命令（{DOMAINS['text']['cost']}），"
                "跑完回来看读数。"
            )
            st.code(DOMAINS["text"]["cmd"], language="bash")
        else:
            st.dataframe(dedup_rows(reports), width="stretch", hide_index=True)
            st.caption("验收线：exact ≥99%、near ≥90%、好数据错伤 ≤1%（CI 每次自动跑）。")
    with tab_fhir:
        render_domain("fhir")
    with tab_ind:
        render_domain("industrial")

    with tab_evi:
        st.markdown("### 用脏数据训练模型，代价是多少？")
        ft = ft_rows(load_report("finetune_eval.json"))
        if ft:
            st.dataframe(ft, width="stretch", hide_index=True)
            st.caption(
                "同一批图文检索任务：不微调 55.6%；用干净数据微调涨到 68.8%；"
                "用脏数据微调反而掉到 63.6%——不如不训。脏数据不只是没用，是有害的。"
            )
        else:
            st.warning(
                "这份报告还没生成。运行 `python -X utf8 scripts/finetune_clip.py`"
                "（约 20 分钟 GPU）。"
            )

        st.markdown("### 拆掉一级滤芯，整体会变差吗？")
        ab = ablation_rows(load_report("ablation_eval.json"))
        if ab:
            st.dataframe(ab[:8], width="stretch", hide_index=True)
            st.caption(
                "去重滤芯拆掉后检索 R@1 掉 0.017——它是唯一显著的一组，"
                "说明清洗是系统工程，不是单点技巧。"
            )
        else:
            st.warning("这份报告还没生成。运行 `python scripts/eval_ablation.py`（约 3 分钟）。")

        st.markdown("### 还有一组数字")
        st.markdown(
            "- 文本语言模型：干净数据微调困惑度 7.16，脏数据 7.70（越低越好，差 7.5%）"
            "——命令 `finetune_gpt2.py`\n"
            "- 固定预算挑数据：分层采样比随机采样检索 R@1 高 24%——命令 `eval_sampling.py`\n"
            "- 领域判官微调：κ 从 -0.024 到 +0.560（V3 个人微调平台）——命令 `eval_judge.py`"
        )

    with tab_proc:
        st.markdown("### 每一级滤芯拦了多少，以及为什么拦")
        cmp_report = load_report("normalize_ablation.json")
        wf = waterfall_rows(cmp_report)
        if wf:
            st.caption(
                "同一批语料跑两遍：**原样**进漏斗 vs 先过一遍**归一化**"
                "（把空白、回车、隐形字符理平）。看哪一级的判决会变——"
                "这直接回答「补这一层到底有没有用」。"
            )
            st.dataframe(wf, width="stretch", hide_index=True)
            if cmp_report.get("headline"):
                st.info(cmp_report["headline"].replace("**", ""))
            shape = cmp_report.get("corpus_A") or {}
            if shape:
                c1, c2, c3 = st.columns(3)
                c1.metric(
                    "空白占比 p90（原样 → 归一化后）",
                    f"{shape.get('whitespace_ratio_p90', 0):.2f} → "
                    f"{(cmp_report.get('corpus_B') or {}).get('whitespace_ratio_p90', 0):.2f}",
                    border=True,
                )
                c2.metric(
                    "空白占比 >50% 的篇目", shape.get("n_whitespace_dominant", 0), border=True
                )
                ns = cmp_report.get("normalize_aggregate") or {}
                c3.metric(
                    "被归一化改写", f"{ns.get('n_changed', 0)}/{ns.get('n', 0)}", border=True
                )
                st.caption(
                    "读法：空白占比的**中位数**几乎不动、**p90** 却差一个数量级——"
                    "语料是双峰的（干净篇目 + 严重膨胀篇目）。归一化作用在那条长尾上，"
                    "**漏斗的拦截数几乎看不见它，进下游的字符总量看得见**。"
                )
        else:
            st.warning("这份对照报告还没生成。跑一次就会同时产出对照读数与判决台账：")
            st.code("python -X utf8 scripts/normalize_ablation.py", language="bash")

        st.markdown("#### 判决台账：这一条到底为什么被删")
        rows_b = cached_verdicts()
        if rows_b:
            ops = sorted({r.get("op") for r in rows_b if r.get("op")})
            f1, f2 = st.columns(2)
            pick = f1.selectbox("滤芯", ["全部"] + ops, key="verdict_op")
            dec = f2.selectbox("判决", ["全部", "drop", "keep"], key="verdict_dec")
            table = verdict_table(
                rows_b,
                op=None if pick == "全部" else pick,
                decision=None if dec == "全部" else dec,
            )
            st.dataframe(table, width="stretch", hide_index=True)
            st.caption(
                f"显示 {len(table)} 条（单页上限 200）。每条都带**机器可读判据**、"
                "**门限**与**输入指纹**——删掉任何一条，都能回答"
                "「哪一级、什么分数、比哪个门限、依据是什么」。"
            )
            with st.expander("这跟业界已有的东西差在哪"):
                st.markdown(
                    "- Data-Juicer 的 `tracer` 也能看「哪些样本被过滤」，但那是"
                    "**运行期内存态**：不落盘、不带判据、不带输入指纹，跑完就没了\n"
                    "- Croissant 的 PROV-O 血缘做到**数据集/文件级**，没落到单条记录\n"
                    "- 这份台账是**落盘的记录级血缘**，并预留了人工复核回填位"
                    "（下一步的人审队列会往里写推翻记录）"
                )
        else:
            st.warning("判决台账还没生成（跑一次对照实验就有了）。")

    with tab_cal:
        st.markdown("### 门限该定多少？——从分布反推，不是拍脑袋")
        rows_cal = cached_verdicts()
        cal_ops = sorted({r.get("op") for r in rows_cal if r.get("op")})
        if not cal_ops:
            st.warning("判决台账还没生成。跑一次对照实验即可：")
            st.code("python -X utf8 scripts/normalize_ablation.py", language="bash")
        else:
            op = st.selectbox("选一级滤芯", cal_ops, key="cal_op")
            scores = score_values(rows_cal, op)
            if not scores:
                # 批量算子（去重类）按整批样本的集合关系裁决，不给逐样本打分——
                # 没有分布就没有阈值的「门」，这一格不是坏了，是不适用。
                st.info(
                    f"`{op}` 是**批量算子**：它按整批样本的集合关系裁决"
                    "（比如去重时「谁是簇代表」取决于全局谁最小），"
                    "**不产生逐样本分数**——所以没有分布可看、也没有阈值可推。"
                    "门限沙盘只对逐样本打分的算子成立，这里如实说明而不是给个空图。"
                )
            else:
                side_txt = st.radio(
                    "这一级的门限方向",
                    ["下限 min：低于此值删除", "上限 max：高于此值删除"],
                    horizontal=True,
                    key="cal_side",
                )
                budget = st.slider(
                    "丢弃预算：最多愿意删掉多少", 0.0, 0.5, 0.05, 0.01, key="cal_budget"
                )
                hist = score_histogram(scores)
                if hist:
                    st.bar_chart(hist, x="区间", y="样本数", height=220, color="#0e7c7b")
                side = "min" if side_txt.startswith("下限") else "max"
                rec = recommend_threshold(scores, side=side, max_drop_rate=budget)
                cur = current_threshold(rows_cal, op)
                if rec:
                    cur_val = cur.get(side)
                    c1, c2, c3 = st.columns(3)
                    c1.metric(
                        "当前门限",
                        "—" if cur_val is None else f"{cur_val:.4g}",
                        border=True,
                    )
                    c2.metric("按预算反推的门限", f"{rec['threshold']:.4g}", border=True)
                    c3.metric("该门限下的删除率", f"{rec['drop_rate']:.2%}", border=True)
                    st.caption(
                        f"这一级共 {rec['n']} 个分数：最小 {rec['score_min']:.4g} / "
                        f"中位 {rec['score_p50']:.4g} / 最大 {rec['score_max']:.4g}。"
                    )
                    st.warning(
                        "**口径别混**：这里反推的是**丢弃预算**（愿意最多删多少），"
                        "**不是误杀率**——误杀率要有 ground truth 或人工复核标签才算得出来。"
                        "Data-Juicer 的 op_effect 能拖滑块看保留/丢弃，但不给预算线、"
                        "不给推荐值、也不说这个数是怎么来的；这一页补的就是那一格。"
                        "等人审队列接上（W3），这里会升级成真正的「按误杀预算反推」。"
                    )
    st.sidebar.markdown("### 想看得更深？")
    st.sidebar.caption("这些是专题工作台，日常演示用本页就够：")
    st.sidebar.code("streamlit run scripts/streamlit_app.py\n  # 图文检索体验", language="text")
    st.sidebar.code("streamlit run scripts/ops_dashboard.py\n  # 每日运维驾驶舱", language="text")
    st.sidebar.code("streamlit run scripts/judge_studio.py\n  # 训练你的领域判官", language="text")
    st.sidebar.code("streamlit run scripts/platform_app.py\n  # 微调平台控制台", language="text")
    st.sidebar.markdown("### 自己动手")
    st.sidebar.page_link(
        "https://github.com/quannie255-star/mm-curation-pipeline", label="GitHub 仓库"
    )
    st.sidebar.markdown(
        "所有数字都能用一条命令重新生成（数据与报告不入库）：\n\n"
        "复现手册 docs/RUNBOOK.md · 扩展指南 docs/DOMAIN_PACKS.md"
    )


if __name__ == "__main__":
    main()
