"""数据质量平台演示门户（V5 β）：四模态一个框架的统一演示入口。

设计语言：「数据净水厂」——脏数据是原水，算子是滤级，门禁是出厂验收。
视觉收敛（洁净室蓝绿，语义色只给门禁结论），唯一大胆处是首屏净水流程条
与实时门禁读数仪。包装层面向第一次来的个人使用者：30 秒看懂 + 角色路线。

启动：python -m streamlit run scripts/showcase_app.py
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
from datetime import datetime
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

# 各模态的算子数**不手写**：注册表的 `meta.modalities` 是唯一真相源。
# 教训：`DOMAINS["image"]["blurb"]` 曾写「11 级滤芯」——实点 image_caption 模态是
# **12** 个（8 个纯图文 + 4 个图文/文本双模态），而 11 恰好是 text_article 的数量。
# 两个数字串了档，正是「凭记忆填品牌数字」的典型腐烂方式。改成现算 + 降级。
MODALITY_KEYS = ("image_caption", "text_article", "fhir_resource", "industrial_sensor")


def _modality_counts() -> dict[str, int]:
    """实点各模态算子数；注册表不可用（裸环境）时返回空 dict，由调用方降级。"""
    try:
        from curation_eval.registry import available_operator_metas

        import mm_curation.operators  # noqa: F401  —— 注册靠导入触发
    except ImportError:
        return {}
    metas = available_operator_metas()
    return {
        m: sum(1 for meta in metas.values() if m in (meta.modalities or ()))
        for m in MODALITY_KEYS
    }


MODALITY_COUNTS = _modality_counts()


def modality_phrase(modality: str, fallback: str = "多级") -> str:
    """「N 级滤芯」；注册表不可用时退回定性说法——**宁可不说数字，也不编数字**。"""
    n = MODALITY_COUNTS.get(modality)
    return f"{n} 级滤芯" if n else f"{fallback}滤芯"


_LABELS = {
    "image_caption": "图文",
    "text_article": "文本",
    "fhir_resource": "医疗",
    "industrial_sensor": "工业",
}


def _modality_breakdown() -> str:
    """「图文 12 / 文本 11 / 医疗 5 / 工业 5」——现算，注册表不可用时如实说明。"""
    if not MODALITY_COUNTS:
        return "注册表不可用，模态分布未取到"
    return " / ".join(f"{_LABELS.get(k, k)} {v}" for k, v in MODALITY_COUNTS.items())


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
        "blurb": f"{modality_phrase('image_caption')}：模糊图、重复图、图文不符、低质描述……"
                 "清洗后检索准确率提升 21%",
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

/* 读数卡的「门禁量规条」：填充 ∝ 召回（0–100%，量纲正确）。
   为什么叫量规而不是趋势：报告里**没有时间序列**，硬画一条趋势线就是编数据；
   这里画的是当前读数在 0–100% 上的位置，能画多长就画多长。
   语义色沿用 pass 绿 / fail 红，**不套股票涨跌约定**——本项目是数据质量门禁。 */
.readout .cell { flex-direction: column; align-items: stretch; gap: 7px; }
.readout .row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.gauge { height: 5px; border-radius: 999px; background: var(--line); overflow: hidden; }
.gauge > i { display: block; height: 100%; border-radius: 999px; }

/* 真实数据页：判据/形态小标签 + 页内固定口径声明 */
.chip { display: inline-block; font-size: 12px; font-weight: 600; padding: 1px 8px;
        border-radius: 6px; border: 1px solid var(--line); color: var(--sub);
        background: var(--paper); }
.chip.good { color: var(--pass); border-color: var(--pass); }
.chip.bad { color: var(--fail); border-color: var(--fail); }
.decl { border-left: 3px solid var(--water); padding: 3px 0 3px 12px; color: var(--sub);
        font-size: 13.5px; line-height: 1.6; margin: 8px 0; }
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


# --- F2 真实数据页签：消费 F1 的预计算 JSON，不在交互回路里跑算子 ---------------
# 为什么前端只做呈现：真实轨评测是分钟级（10 万+ 窗 × 5 算子），页面每刷新一次都重算
# 不可接受；更要紧的是**口径只能有一处**——前端自己算统计，迟早就和报告对不上。
# 这一页回答两件事：①同一批真实窗、只换判据尺度，误杀掉多少；②业界最常回避的那格
# ——**「未评」（score=None）既不是通过，也不是失败**。

REAL_REPORT = "real_sensor_interactive.json"
REAL_ARM_KEYS = {"new": "d", "old": "d0"}


def real_datasets(payload: dict | None) -> list[dict]:
    """面板里的数据集列表（空/损坏 payload 一律退化为空表，由调用方走降级）。"""
    if not payload:
        return []
    return [d for d in payload.get("datasets") or [] if d.get("id")]


def real_dataset(payload: dict | None, key: str) -> dict | None:
    return next((d for d in real_datasets(payload) if d["id"] == key), None)


def real_arm_stats(ds: dict | None, arm: str = "new") -> dict:
    """按**窗级数组**重算某一档（new=新判据 / old=旧判据）的召回、误杀、存活率。

    口径与 `eval_real_sensor` 一致：**误杀率的分母是真实干净窗**，不是总窗数——
    拿总窗数当分母会把「这份数据脏得多」算成「误杀更低」，是这类表格最常见的假精确。
    两档读的是**同一批窗**，唯一变量是配置（`pooled σ` ↔ `mad σ`），所以两者可比。
    """
    key = REAL_ARM_KEYS[arm]
    n = n_clean = n_dirty = n_dropped = clean_killed = dirty_caught = 0
    for blob in (ds or {}).get("channels", {}).values():
        for lab, drop in zip(blob.get("lab") or [], blob.get(key) or []):
            n += 1
            n_dropped += drop
            if lab:
                n_dirty += 1
                dirty_caught += drop
            else:
                n_clean += 1
                clean_killed += drop
    return {
        "arm": arm,
        "n": n,
        "n_clean": n_clean,
        "n_dirty": n_dirty,
        "n_dropped": n_dropped,
        "clean_killed": clean_killed,
        "dirty_caught": dirty_caught,
        "recall": (dirty_caught / n_dirty) if n_dirty else None,
        "kill_rate": (clean_killed / n_clean) if n_clean else None,
        "survival": ((n - n_dropped) / n) if n else None,
    }


def real_window_mix(ds: dict | None, arm: str = "new") -> list[dict]:
    """保留窗的三分解：全算子都评过 / 通过但有算子未评 / 被丢弃。

    「通过但有算子未评」这格是本项目的核心不变式：`score is None` 表示该算子在这窗上
    **没干活**。把它并进「通过」就是虚报——这一页把它单列，就是为了看得见。
    """
    key = REAL_ARM_KEYS[arm]
    buckets = {"全算子都评过（真通过）": 0, "通过但有算子未评": 0, "被丢弃": 0}
    for blob in (ds or {}).get("channels", {}).values():
        for drop, un in zip(blob.get(key) or [], blob.get("u") or []):
            if drop:
                buckets["被丢弃"] += 1
            elif un:
                buckets["通过但有算子未评"] += 1
            else:
                buckets["全算子都评过（真通过）"] += 1
    return [{"状态": k, "窗数": v} for k, v in buckets.items()]


def real_unscored_total(ds: dict | None) -> int:
    """「未评（样本 × 算子）」对的总数：每一对都是一格「没干活」，不是一格「通过」。"""
    return sum(op.get("n_unscored", 0) for op in (ds or {}).get("operators") or [])


def real_op_rows(ds: dict | None) -> list[dict]:
    """算子级「旧判据 → 新判据」并排读数。两个 `%` 列是**百分点**（分数 × 100）。"""
    rows = []
    for op in (ds or {}).get("operators") or []:
        old = op.get("old") or {}
        forms = op.get("forms") or {}
        kr_old, kr_new = old.get("kill_rate"), op.get("kill_rate")
        rows.append(
            {
                "算子": op.get("op"),
                "旧·丢弃": old.get("n_dropped", 0),
                "旧·误杀率%": None if kr_old is None else round(kr_old * 100, 3),
                "新·丢弃": op.get("n_dropped", 0),
                "新·误杀": op.get("clean_killed", 0),
                "新·误杀率%": None if kr_new is None else round(kr_new * 100, 3),
                "真实脏命中": op.get("n_dirty_caught", 0),
                "未评窗": op.get("n_unscored", 0),
                "命中的判据形态": " · ".join(f"{k}×{v}" for k, v in forms.items()) or "—",
            }
        )
    return rows


def real_curve_rows(ds: dict | None, op: str = "sensor_drift") -> list[dict]:
    """召回-误杀权衡面的点：**预计算的参数网格**（尺度 × z），不是现场重算算子。"""
    rows = []
    for p in ((ds or {}).get("curves") or {}).get(op) or []:
        params = p.get("params") or {}
        rows.append(
            {
                "尺度": params.get("scale", "—"),
                "z": params.get("z"),
                "标签": p.get("label"),
                "召回": p.get("recall"),
                "误杀率": p.get("kill_rate"),
                "丢弃数": p.get("n_dropped"),
            }
        )
    return rows


def real_curve_point(rows: list[dict], scale: str, z: float) -> dict | None:
    """取网格上离 `(scale, z)` 最近的工作点——z 落在两档之间时取近档（界面上如实标注）。"""
    cands = [
        r for r in rows if r["尺度"] == scale and r["z"] is not None and r["召回"] is not None
    ]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(r["z"] - z))


def real_applicability_rows(ds: dict | None) -> list[dict]:
    """判据适用性：**「恒过」不等于「通过」**——靶子在数据里不存在时，那一档是在空转。"""
    return [
        {
            "算子": a.get("op"),
            "这一档要找的靶子": a.get("target"),
            "靶子数": a.get("n_target", 0),
            "分母": a.get("n_windows", 0),
            "本数据集适用": "是" if a.get("applicable") else "否（空转）",
            "说明": a.get("hint"),
        }
        for a in (ds or {}).get("applicability") or []
    ]


def real_reachability_rows(ds: dict | None) -> list[dict]:
    """判据可达性：门槛够不够（分组最小规模 vs 判据要求的窗口数）。"""
    return [
        {
            "算子": r.get("op"),
            "分组键": r.get("group_by"),
            "要求": f"≥{r.get('requirement')} 窗",
            "组数": r.get("n_groups"),
            "组规模中位": r.get("group_size_p50"),
            "组规模最大": r.get("group_size_max"),
            "规模不足的组": r.get("n_groups_shorter"),
            "可达": "是" if r.get("reachable") else "否",
        }
        for r in (ds or {}).get("reachability") or []
    ]


def _fmt_epoch(t: int | float | None) -> str:
    """epoch 秒 → 本地可读时间；非法值如实标注，不假装有值。"""
    if not isinstance(t, (int, float)) or t <= 0:
        return "—"
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def real_kill_rows(
    ds: dict | None,
    *,
    op: str | None = None,
    channel: str | None = None,
    labeled_only: bool = False,
    limit: int = 300,
) -> list[dict]:
    """被丢弃窗的抽检清单（可筛）。

    **这是「误杀率是上界」的人工复核入口**：真实数据无标签 ≠ 干净，
    只有逐条看读数极值才能把「误杀」和「其实真是坏窗」分开。
    """
    out: list[dict] = []
    for r in (ds or {}).get("kills") or []:
        if op and r.get("op") != op:
            continue
        if channel and r.get("channel") != channel:
            continue
        if labeled_only and not r.get("label"):
            continue
        out.append(
            {
                "算子": r.get("op"),
                "设备": r.get("device"),
                "通道": r.get("channel"),
                "窗口起始": _fmt_epoch(r.get("t")),
                "命中判据": r.get("rule") or "—",
                "数据集标签": r.get("label") or "（无标签）",
                "读数最小": r.get("reading_min"),
                "读数最大": r.get("reading_max"),
                "读数σ": r.get("reading_std"),
            }
        )
        if len(out) >= limit:
            break
    return out


def real_kill_filters(ds: dict | None) -> tuple[list[str], list[str]]:
    """抽检清单的两个筛选项（算子 / 通道）从数据里现取，不写死。"""
    kills = (ds or {}).get("kills") or []
    ops = sorted({r.get("op") for r in kills if r.get("op")})
    chans = sorted({r.get("channel") for r in kills if r.get("channel")})
    return ops, chans


def rows_to_csv(rows: list[dict]) -> str:
    """表 → CSV 文本（下载按钮用）。空表返回空串，调用方据此禁用按钮。"""
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


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
            fill = max(0.0, min(1.0, g["recall"])) * 100
            gauge = (
                f'<div class="gauge" title="召回 {g["recall"]:.0%}'
                f'（抓到的坏数据占全部坏数据的比例，满格 100%）">'
                f'<i style="width:{fill:.1f}%;background:var(--pass)"></i></div>'
            )
        else:
            val = "报告未生成"
            pill = '<span class="pill na">待质检</span>'
            gauge = '<div class="gauge" title="报告未生成，无量规可画"></div>'
        cells.append(
            f'<div class="cell"><div class="row"><span class="nm">{spec["title"]}</span>'
            f'<span class="val">{val}</span>{pill}</div>{gauge}</div>'
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


def _pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}%}"


def _delta_pct(new: float | None, old: float | None, digits: int = 2) -> str | None:
    """新档相对旧档的差值（百分点）。任一侧缺失就返回 None，不假装有对比。"""
    if new is None or old is None:
        return None
    return f"{new - old:+.{digits}%}（对旧判据）"


def _inline_md(text: str) -> str:
    """把文案里的 `**粗体**` 转成 HTML——这些字符串要塞进自己写的 div，markdown 不生效。"""
    parts = text.split("**")
    if len(parts) == 1:
        return text
    return "".join(p if i % 2 == 0 else f"<b>{p}</b>" for i, p in enumerate(parts))


def render_real() -> None:
    """第 9 页签：真实工业数据（消费 F1 预计算 JSON，交互回路里不跑算子）。"""
    payload = load_report(REAL_REPORT)
    ds_list = real_datasets(payload)
    st.markdown(
        "**三个公开真实数据集的原始读数**——MetroPT-3（空压机）、C-MAPSS（涡扇退化仿真）、"
        "SKAB（水泵试验台）。同一批窗、同一批算子，**只换判据的尺度**：看误杀掉多少、"
        "真脏抓到多少。合成轨的 100% 在这里不存在，这一页就是回答「迁到真实数据还剩多少」。"
    )
    if not ds_list:
        st.warning("这份面板还没生成。两条命令，一分钟内跑完：")
        st.code(
            "python -X utf8 scripts/build_real_interactive.py\n"
            "python -X utf8 scripts/build_real_data_html.py",
            language="bash",
        )
        return

    meta = payload.get("meta") or {}
    with st.expander("⚠️ 读这一页之前的三条口径声明（不是免责，是量纲）"):
        for note in meta.get("honesty_note") or []:
            st.markdown(f'<div class="decl">{_inline_md(note)}</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="decl">{_inline_md(meta.get("unscored_note", ""))}</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            f"合成档 `{meta.get('config_synth')}` ↔ 真实档 `{meta.get('config_real')}`"
            f" · 预计算于 {meta.get('generated_at', '—')} · 复现："
            "`python -X utf8 scripts/build_real_interactive.py`"
        )

    labels = [d.get("label") or d["id"] for d in ds_list]
    pick = st.radio("数据集", labels, horizontal=True, key="real_ds")
    ds = next((d for d in ds_list if (d.get("label") or d["id"]) == pick), ds_list[0])
    st.caption(
        f"{ds.get('blurb', '')} · 窗 {ds.get('n_total')} 条"
        f"（数据集自带标签：干净 {ds.get('n_clean')} / 脏 {ds.get('n_dirty')}）"
    )

    new = real_arm_stats(ds, "new")
    old = real_arm_stats(ds, "old")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "新判据·误杀率（上界）",
        _pct(new["kill_rate"], 2),
        delta=_delta_pct(new["kill_rate"], old["kill_rate"]),
        delta_color="inverse",
        border=True,
        help="分母是真实干净窗（不是总窗数）。新判据 = 真实档配置，sensor_drift 换 MAD 尺度。",
    )
    c2.metric(
        "真实脏召回（上界）",
        _pct(new["recall"], 1),
        delta=_delta_pct(new["recall"], old["recall"]),
        border=True,
        help="真实轨标签是数据集自带的窗级弱标签，不是注入器给的「一条脏样本一个主靶」。",
    )
    c3.metric(
        "存活率",
        _pct(new["survival"], 1),
        delta=_delta_pct(new["survival"], old["survival"]),
        delta_color="off",
        border=True,
        help="保留窗 / 总窗。它与误杀率不是同一件事：误杀只统计真实干净窗里被错杀的比例。",
    )
    c4.metric(
        "未评（样本 × 算子）",
        real_unscored_total(ds),
        border=True,
        help="每一格都是「这个算子在这窗上没干活」，既不是通过也不是失败——"
        "报告里「未评」一栏大，是空转信号，不是好数字。",
    )

    st.dataframe(
        [
            {
                "判据档": "新判据（真实档：sensor_drift 用 MAD 尺度）",
                "真实脏召回": _pct(new["recall"], 1),
                "误杀率（上界）": _pct(new["kill_rate"], 2),
                "存活率": _pct(new["survival"], 1),
                "丢弃窗": new["n_dropped"],
            },
            {
                "判据档": "旧判据（合成档：pooled σ）",
                "真实脏召回": _pct(old["recall"], 1),
                "误杀率（上界）": _pct(old["kill_rate"], 2),
                "存活率": _pct(old["survival"], 1),
                "丢弃窗": old["n_dropped"],
            },
        ],
        width="stretch",
        hide_index=True,
    )

    st.markdown("#### 每个算子各发生了什么")
    st.dataframe(
        real_op_rows(ds),
        width="stretch",
        hide_index=True,
        column_config={
            "旧·误杀率%": st.column_config.NumberColumn(
                format="%.2f", help="百分点（分数 × 100），分母 = 真实干净窗"
            ),
            "新·误杀率%": st.column_config.NumberColumn(
                format="%.2f", help="百分点（分数 × 100），分母 = 真实干净窗"
            ),
        },
    )
    st.caption(
        "「命中的判据形态」是本轮修复的落点：真实数据里没有合成靶子那种完美形态，"
        "判据改成能认出**设备级停机 / 持续塌陷 / 跨度缺口**之后，误杀才降下来。"
    )

    st.markdown("#### 召回-误杀权衡面：同一批窗，只改判据的尺度")
    curve = real_curve_rows(ds)
    if not curve:
        st.info("这个数据集没有预计算曲线（网格扫描只做了 `sensor_drift` 这一级）。")
    else:
        scales = sorted({r["尺度"] for r in curve})
        zs = sorted({r["z"] for r in curve if r["z"] is not None}, reverse=True)
        f1, f2 = st.columns(2)
        scale = f1.selectbox(
            "尺度（两者回答的问题不同）",
            scales,
            index=scales.index("mad") if "mad" in scales else 0,
            key="real_scale",
            help="pooled：窗内噪声能推出多大窗均值抖动（合成档用的）。"
                 "mad：这个通道自己观察到的窗间波动有多大（只会更宽松）。",
        )
        z = f2.select_slider("阈值档（z 倍数，预计算网格）", options=zs,
                             value=zs[len(zs) // 2], key="real_z")
        point = real_curve_point(curve, scale, z)
        try:
            import altair as alt
        except ImportError:
            alt = None
        if alt is None:  # 裸环境降级：曲线画不出来，至少把点列出来
            st.dataframe(curve, width="stretch", hide_index=True)
        else:
            import pandas as pd

            frame = pd.DataFrame([r for r in curve if r["召回"] is not None])
            mad = frame[frame["尺度"] == "mad"].sort_values("召回")
            chart = (
                alt.Chart(mad)
                .mark_area(opacity=0.15, color="#0E7490", interpolate="monotone")
                .encode(x="召回:Q", y="误杀率:Q")
                + alt.Chart(frame)
                .mark_line(point=True, strokeWidth=2)
                .encode(
                    x=alt.X("召回:Q", title="真实脏召回（上界口径）",
                            axis=alt.Axis(format="%")),
                    y=alt.Y("误杀率:Q", title="误杀率（上界口径）",
                            axis=alt.Axis(format="%")),
                    color=alt.Color(
                        "尺度:N",
                        title="判据尺度",
                        scale=alt.Scale(domain=["pooled", "mad"],
                                        range=["#94A3B8", "#0E7490"]),
                    ),
                    tooltip=["标签:N", "z:Q", "召回:Q", "误杀率:Q", "丢弃数:Q"],
                )
            )
            if point:
                mark = pd.DataFrame([point])
                chart = (
                    chart
                    + alt.Chart(mark)
                    .mark_point(size=150, filled=True, color="#B91C1C")
                    .encode(x="召回:Q", y="误杀率:Q")
                    + alt.Chart(mark)
                    .mark_text(dy=-13, fontSize=11, color="#B91C1C")
                    .encode(x="召回:Q", y="误杀率:Q", text="标签:N")
                )
            st.altair_chart(chart, width="stretch")
        if point:
            st.caption(
                f"当前工作点（{scale} · z={point['z']}）：召回 {_pct(point['召回'], 1)} · "
                f"误杀率 {_pct(point['误杀率'], 2)} · 丢弃 {point['丢弃数']} 窗。"
                "滑块在**预计算网格**上取值，不是现场重跑算子——网格之外的 z 没有数据，"
                "所以这里不给连续滑块。"
            )

    st.markdown("#### 被丢弃窗抽检：误杀率的「上界」要靠这里定论")
    ops, chans = real_kill_filters(ds)
    k1, k2, k3 = st.columns([1, 1, 1])
    pick_op = k1.selectbox("算子", ["全部"] + ops, key="real_kill_op")
    pick_ch = k2.selectbox("通道", ["全部"] + chans, key="real_kill_ch")
    only_lab = k3.checkbox("只看数据集已标脏的", key="real_kill_lab")
    kill_rows = real_kill_rows(
        ds,
        op=None if pick_op == "全部" else pick_op,
        channel=None if pick_ch == "全部" else pick_ch,
        labeled_only=only_lab,
    )
    st.dataframe(kill_rows, width="stretch", hide_index=True)
    st.caption(
        f"显示 {len(kill_rows)} 条（单页上限 300）。勾上「只看数据集已标脏的」就能看到："
        "被丢弃的窗里有一部分**本来就是坏的**——它们被算成「误杀」，只是因为数据集没标。"
    )
    csv_text = rows_to_csv(kill_rows)
    if csv_text:
        st.download_button(
            "下载当前筛选结果（CSV，带 BOM 便于 Excel 直开）",
            data=csv_text.encode("utf-8-sig"),
            file_name=f"real_{ds.get('id', 'ds')}_kills_filtered.csv",
            mime="text/csv",
            key="real_kill_csv",
        )

    st.markdown("#### 保留下来的窗，有多少是「真通过」")
    st.dataframe(real_window_mix(ds), width="stretch", hide_index=True)
    st.caption(
        "三格之和 = 该数据集全部窗。中间那格是**通过但有算子未评**——"
        "它被放行了，但不是每个算子都真的判过它。把这一格并进「通过」就是虚报。"
    )

    st.markdown("#### 判据适用性：「恒过」不等于「通过」")
    st.dataframe(real_applicability_rows(ds), width="stretch", hide_index=True)
    st.caption(
        "靶子数为 0 = 这一档在这份数据上**没有可判的东西**，它的「通过」是空转。"
        "本轮把「判据可达性（门槛够不够）」与「判据适用性（靶子有没有）」拆成了两个问题。"
    )
    with st.expander("判据可达性：分组规模够不够"):
        st.dataframe(real_reachability_rows(ds), width="stretch", hide_index=True)

    st.info(
        "同一份数据的**独立自包含单页**在 `docs/real_data.html`"
        "（零外部依赖、双击即开，含窗级时间线与全部曲线点）；本页是门户内的快速视图。"
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
        tab_real,
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
            "真实数据",
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
                f"实点 `available_operators()`（{TEST_COUNT_ASOF}）——{OP_COUNT} 个算子共用同一张"
                f"注册表，按 `meta.modalities` 声明各自适用模态（{_modality_breakdown()}），"
                "不是四套独立实现。"
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

    with tab_real:
        render_real()

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
