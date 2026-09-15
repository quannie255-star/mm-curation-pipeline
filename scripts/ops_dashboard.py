r"""OPS 运维驾驶舱：日报 / 趋势 / 丢弃审计 / 新闻语料 / 手动运行（五页签）。

30 天运维飞轮的"人看的那一层"——日报文件与台账的可视化，数据全部来自
ops_daily.py 的落盘产物（零额外采集）；「手动运行」复用 judge_studio 的
subprocess + 日志 tail 模式。仓库惯例：Streamlit 页面不算"真前端"（AGENTS
硬规则 7 所指为产品化 Web UI），是给单人运维用的驾驶舱。

启动：streamlit run scripts/ops_dashboard.py
依赖：仅读取 data/ 下产物，无网络要求；「手动运行」页会真触发 ops_daily。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from ops_daily import (  # noqa: E402
    FUNNEL_DIR,
    NEWS_CORPUS,
    REPORT_DIR,
    STATS_PATH,
    aggregate_drops,
)

st.set_page_config(page_title="OPS 运维驾驶舱", page_icon="🛰️", layout="wide")


def load_stats(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def report_dates(dir_path: Path) -> list[str]:
    if not dir_path.exists():
        return []
    return sorted((p.stem for p in dir_path.glob("*.md")), reverse=True)


def load_corpus(path: Path, limit: int = 200) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            rows.append(json.loads(line))
    return rows[-limit:]


def _run_ops_daily(skip_findata: bool, log_path: Path) -> int:
    """跑 ops_daily，实时 tail 日志（judge_studio 同款模式：训练期间勿刷新）。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-X", "utf8", str(REPO / "scripts" / "ops_daily.py")]
    if skip_findata:
        cmd.append("--skip-findata")
    box, bar = st.empty(), st.progress(0.02, text="运维管线运行中…")
    shown = 0.02
    with log_path.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=REPO, stdout=lf, stderr=subprocess.STDOUT
        )
        while proc.poll() is None:
            time.sleep(3)
            tail = log_path.read_text(encoding="utf-8", errors="ignore").split("\n")[-6:]
            box.code("\n".join(tail), language="text")
            shown = min(0.95, shown + 0.02)
            bar.progress(shown, text="运维管线运行中…")
    bar.progress(1.0, text="完成")
    return proc.returncode


stats = load_stats(STATS_PATH)
dates = report_dates(REPORT_DIR)

st.title("🛰️ OPS 运维驾驶舱")
st.caption(
    "30 天数据飞轮 · findata（仓库级巡检）× mm-curation（采样级清洗）· 数据源：ops_daily 落盘产物"
)

if not dates and not stats:
    st.warning("还没有任何运维产物。先跑一次：`python -X utf8 scripts/ops_daily.py --skip-findata`")

tab_today, tab_trend, tab_audit, tab_corpus, tab_run = st.tabs(
    ["📋 今日日报", "📈 趋势", "🔍 丢弃审计", "📰 新闻语料", "▶️ 手动运行"]
)

with tab_today:
    if dates:
        pick = st.selectbox("日期", dates, index=0)
        report_path = REPORT_DIR / f"{pick}.md"
        report_text = report_path.read_text(encoding="utf-8")
        abnormal = "## 今日异常" in report_text and "- 无" not in report_text.split("## 采集")[0]
        if abnormal:
            st.error("今日存在异常，见下方日报顶部")
        st.markdown(report_text)
    else:
        st.info("尚无日报产物")

with tab_trend:
    if stats:
        latest = stats[-1]
        c1, c2, c3, c4 = st.columns(4)
        kept = latest.get("funnel_kept", 0)
        fin = latest.get("funnel_in", 0)
        c1.metric("新闻库内累计", f"{latest.get('text_total', 0):,}")
        c2.metric("今日新增", f"{latest.get('text_new', 0):,}")
        c3.metric("漏斗保留率", f"{(kept / fin * 100) if fin else 0:.1f}%", f"{kept:,} / {fin:,}")
        c4.metric("磁盘已用", f"{latest.get('disk_used_pct', 0):.1f}%")
        st.divider()
        chart_data = {
            row["date"]: {
                "新增新闻": row.get("text_new", 0),
                "漏斗保留": row.get("funnel_kept", 0),
            }
            for row in stats
        }
        st.line_chart(chart_data)
        disk_data = {row["date"]: row.get("disk_used_pct", 0) for row in stats}
        st.line_chart(disk_data, height=160)
    else:
        st.info("台账为空（data/ops/stats.jsonl）")

with tab_audit:
    dropped_path = FUNNEL_DIR / "dropped.jsonl"
    if dropped_path.exists():
        audit = aggregate_drops(dropped_path.read_text(encoding="utf-8").split("\n"))
        if audit:
            st.bar_chart({op: slot["count"] for op, slot in audit.items()})
            for op, slot in sorted(audit.items(), key=lambda kv: -kv[1]["count"]):
                with st.expander(f"**{op}**：{slot['count']} 条（抽样 {len(slot['samples'])}）"):
                    for sample in slot["samples"]:
                        st.markdown(f"> {sample}")
        else:
            st.success("今日零丢弃")
    else:
        st.info("尚无漏斗丢弃产物")

with tab_corpus:
    if NEWS_CORPUS.exists():
        rows = load_corpus(NEWS_CORPUS, limit=500)
        symbols = sorted({r["meta"].get("symbol", "?") for r in rows})
        pick_symbol = st.multiselect("股票（空 = 全部）", symbols)
        view = [r for r in rows if not pick_symbol or r["meta"].get("symbol") in pick_symbol]
        st.caption(f"共 {len(view)} 条（显示最近 {min(len(view), 500)}）")
        st.dataframe(
            [
                {
                    "时间": r["meta"].get("published_at", ""),
                    "股票": f"{r['meta'].get('symbol_name', '')} {r['meta'].get('symbol', '')}",
                    "标题": r["text"].split("\n")[0][:60],
                    "来源": r["meta"].get("source", ""),
                }
                for r in view
            ],
            use_container_width=True,
            height=420,
        )
    else:
        st.info("尚无新闻语料（data/raw/finance_news/news_corpus.jsonl）")

with tab_run:
    st.caption("手动触发一次完整运维管线（findata 采集需数分钟；调度版见 ops_install_schedule.py）")
    skip = st.checkbox("--skip-findata（只跑文本链路，快）", value=False)
    if st.button("▶️ 运行 ops_daily", type="primary"):
        code = _run_ops_daily(skip, REPO / "data/ops/logs/manual_run.log")
        if code == 0:
            st.success("全部步骤通过，日报已更新")
        else:
            st.error(f"存在失败步骤（exit={code}），见「今日日报」页顶部")
        st.rerun()
