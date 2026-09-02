"""Streamlit Demo：中文图文数据清洗与向量检索管道（Week4 D1-2）。

四个 tab 把 Week1-3 全部成果包装成可交互演示：
1. 检索：文搜图 / 图搜图，索引切换（脏 vs 净），top-k 结果网格
2. 清洗漏斗：各级通过率 + 检索对比（清洗收益）+ 采样对比（配比收益）
3. 算子评测：算子级 P/R 表 + 阈值敏感性曲线图
4. 丢弃样本：按算子抽样浏览被丢弃的图文对（清洗透明性）

启动：
    streamlit run scripts/streamlit_app.py
"""

from __future__ import annotations

import json
import random
import sys
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from mm_curation.index.searcher import list_indexes, load_searcher  # noqa: E402

INDEXES_ROOT = REPO / "data" / "indexes"
REPORTS = REPO / "data" / "reports"
PROCESSED = REPO / "data" / "processed" / "cn_flickr_curation_v2"


@st.cache_resource
def get_searcher(name: str):
    return load_searcher(INDEXES_ROOT, name)


@st.cache_data
def load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@st.cache_data
def load_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _img_path(rel: str) -> Path:
    """索引 store 里的 image_path 是相对仓库根的相对路径。"""
    return REPO / rel


def _render_hits(hits):
    """top-k 结果网格：图 + caption + 相似度。"""
    if not hits:
        st.info("无结果")
        return
    cols = st.columns(5)
    for i, h in enumerate(hits):
        with cols[i % 5]:
            p = _img_path(h.image_path)
            if p.exists():
                st.image(str(p), width="stretch")
            else:
                st.warning("图缺失")
            st.caption(f"**{h.score:.3f}** · {h.id[:30]}")
            label = h.labels.get("dirty") if h.labels else ""
            dirty_html = (
                f"<div style='font-size:10px;color:#d85a30'>⚠ {label}</div>" if label else ""
            )
            st.markdown(
                f"<div style='font-size:12px;min-height:48px'>{h.text}</div>{dirty_html}",
                unsafe_allow_html=True,
            )


st.set_page_config(page_title="多模态数据清洗管道", layout="wide", page_icon="🧹")

st.title("多模态图文数据清洗与向量检索管道")
st.caption("脏数据进 → 漏斗式清洗 → 质量可量化 → 向量索引 → 检索服务 → 清洗收益可证明")

tab1, tab2, tab3, tab4 = st.tabs(["🔍 检索", "📊 清洗漏斗", "🔬 算子评测", "🗑️ 丢弃样本"])

# ======================== Tab 1: 检索 ========================


with tab1:
    indexes = [m.name for m in list_indexes(INDEXES_ROOT)]
    if not indexes:
        st.error("未找到索引，请先运行 `make index-clean index-dirty`")
        st.stop()

    col_a, col_b, col_c = st.columns([2, 2, 1])
    with col_a:
        idx_name = st.selectbox("索引", indexes, help="clean_v2=清洗后, dirty_raw=清洗前")
    with col_c:
        top_k = st.slider("top-k", 5, 30, 10)

    searcher = get_searcher(idx_name)
    st.caption(f"索引 {idx_name}: {searcher.n_items} 条向量, dim={searcher.manifest.dim}")

    mode = st.radio("检索方式", ["文搜图", "图搜图"], horizontal=True)

    if mode == "文搜图":
        query = st.text_input(
            "输入中文描述",
            value="一只猫坐在沙发上",
            placeholder="试试：街道夜景 / 男人打棒球 / 食物特写",
        )
        if st.button("检索", type="primary") and query.strip():
            with st.spinner("编码 + 检索中..."):
                hits = searcher.search_by_text(query, top_k)
            st.success(f"返回 {len(hits)} 条结果（{idx_name}）")
            _render_hits(hits)
    else:
        uploaded = st.file_uploader("上传图片", type=["jpg", "jpeg", "png", "webp"])
        if uploaded and st.button("检索", type="primary"):
            img = Image.open(BytesIO(uploaded.read())).convert("RGB")
            with st.spinner("编码 + 检索中..."):
                hits = searcher.search_by_image(img, top_k)
            st.success(f"返回 {len(hits)} 条结果（{idx_name}）")
            col_prev, col_res = st.columns([1, 3])
            with col_prev:
                st.image(img, caption="查询图", width="stretch")
            with col_res:
                _render_hits(hits)


# ======================== Tab 2: 清洗漏斗 ========================


with tab2:
    stats = load_json(PROCESSED / "funnel_stats.json")
    if not stats:
        st.error("未找到漏斗统计，请先运行 `make funnel`")
    else:
        gt = stats["ground_truth"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("输入", stats["n_input"])
        m2.metric("存活", stats["n_kept"])
        m3.metric("脏数据召回", f"{gt['dirty_recall']:.1%}", f"漏 {gt['dirty_leak']}")
        m4.metric("干净误杀", f"{gt['clean_kill_rate']:.1%}", f"{gt['clean_falsely_killed']} 条")

        st.subheader("各级通过率")
        stages = stats["stages"]

        df = pd.DataFrame(
            [
                {
                    "算子": s["op"],
                    "进入": s["n_in"],
                    "丢弃": s["dropped"],
                    "通过率": s["pass_rate"],
                    "类型": "批量" if s["batch"] else "单样本",
                }
                for s in stages
            ]
        )
        st.dataframe(df, width="stretch", hide_index=True)

        st.bar_chart(df.set_index("算子")["丢弃"])

        st.subheader("检索对比实验：清洗收益")
        ret = load_json(REPORTS / "retrieval_eval.json")
        if ret:
            base = ret["base"]
            rows = [
                {
                    "索引": base["index"],
                    "n": base["n_queries"],
                    "R@1": base["recall_at_k"]["1"],
                    "R@5": base["recall_at_k"]["5"],
                    "R@10": base["recall_at_k"]["10"],
                    "MRR": base["mrr"],
                }
            ]
            for o in ret.get("others", []):
                r = o["result"]
                rows.append(
                    {
                        "索引": r["index"],
                        "n": r["n_queries"],
                        "R@1": r["recall_at_k"]["1"],
                        "R@5": r["recall_at_k"]["5"],
                        "R@10": r["recall_at_k"]["10"],
                        "MRR": r["mrr"],
                    }
                )
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        st.subheader("采样策略对比：配比收益")
        samp = load_json(REPORTS / "sampling_eval.json")
        if samp:
            samp_rows = []
            for b in samp["budgets"]:
                for name, m in b["methods"].items():
                    rk = m["recall_at_k"]
                    samp_rows.append(
                        {
                            "预算": b["budget"],
                            "方法": name,
                            "R@1": rk["1"],
                            "R@10": rk["10"],
                            "MRR": m["mrr"],
                        }
                    )
            st.dataframe(pd.DataFrame(samp_rows), width="stretch", hide_index=True)


# ======================== Tab 3: 算子评测 ========================


with tab3:
    opr = load_json(REPORTS / "operator_pr.json")
    if not opr:
        st.error("未找到算子评测，请先运行 `make eval-op`")
    else:
        st.subheader("算子级 P/R（独立评测，非漏斗串联）")
        rows = []
        for op in opr["operators"]:
            prim = op.get("primary_recall", {})
            prim_str = ", ".join(f"{k} {v or 0:.0%}" for k, v in prim.items()) if prim else "—"
            rows.append(
                {
                    "算子": op["op"],
                    "主靶": ", ".join(op["primary_target"]) or "—",
                    "扔": op["n_dropped"],
                    "误杀": op["clean_killed"],
                    "precision": op["precision"],
                    "主靶recall": prim_str,
                    "误杀率": op["clean_kill_rate"],
                }
            )
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        st.subheader("完整召回矩阵（行=算子，列=脏类型）")
        totals = opr["dirty_totals"]
        types = sorted(totals)
        matrix = []
        for op in opr["operators"]:
            rbt = op["recall_by_type"]
            matrix.append({"算子": op["op"], **{t: rbt.get(t) for t in types}})
        st.dataframe(pd.DataFrame(matrix), width="stretch", hide_index=True)

        st.subheader("阈值敏感性曲线")
        scan = load_json(REPORTS / "threshold_scan.json")
        if scan:
            ops = list(scan["operators"].keys())
            chosen = st.multiselect("选择算子", ops, default=ops[:3])
            for op_name in chosen:
                data = scan["operators"][op_name]
                chart_path = REPO / data["chart"]
                if chart_path.exists():
                    st.image(str(chart_path), caption=f"{op_name}（默认 {data['default']}）")


# ======================== Tab 4: 丢弃样本 ========================


with tab4:
    dropped = load_jsonl(PROCESSED / "dropped.jsonl")
    if not dropped:
        st.error("未找到丢弃明细，请先运行 `make funnel`")
    else:
        from collections import Counter

        by_op = Counter(d.get("dropped_by", "?") for d in dropped)
        st.subheader(f"丢弃样本 {len(dropped)} 条，按算子分布")
        st.bar_chart(pd.Series(dict(by_op)))

        ops = sorted(by_op, key=lambda k: -by_op[k])
        chosen_op = st.selectbox("按算子过滤", ["全部"] + ops)
        if chosen_op != "全部":
            pool = [d for d in dropped if d.get("dropped_by") == chosen_op]
        else:
            pool = dropped
        st.caption(f"当前池 {len(pool)} 条，随机抽样 15 条展示")
        sample = random.sample(pool, min(15, len(pool)))

        cols = st.columns(5)
        for i, d in enumerate(sample):
            with cols[i % 5]:
                p = _img_path(d["image_path"])
                if p.exists():
                    st.image(str(p), width="stretch")
                else:
                    st.warning("图缺失")
                dirty = d.get("labels", {}).get("dirty", "")
                op_by = d.get("dropped_by", "?")
                st.markdown(
                    "<div style='font-size:11px;min-height:36px'>"
                    f"{(d.get('text') or d.get('caption', ''))[:40]}</div>"
                    f"<div style='font-size:10px;color:#d85a30'>⚠ {dirty}</div>"
                    f"<div style='font-size:10px;color:#888'>扔 by: {op_by}</div>",
                    unsafe_allow_html=True,
                )
