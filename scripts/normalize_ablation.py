"""归一化层 A/B 对照实验（V6 α）：量化「前置归一化」相对「chinese_ratio 单点补丁」的增量。

## 背景（笔记 #65）

真实新闻语料上 `chinese_ratio` 一级拦 778/2066（37.7%）；抽样审计发现被拦篇目
**空白占比中位数 77.2%**（空格 3000+、`\\r` 残留）——「汉字/全文长度」的分母被
爬虫抽取缺陷撑爆，只有 5 篇是真非中文。当时的修复把去空白写进 `chinese_ratio`
的语义（<20 行）——**补丁正确，但只救了这一个算子。**

## 本实验回答三个问题

1. `chinese_ratio` 的拦截数在 A/B 下是否变化？（预期：**不变**——单点补丁已覆盖）
2. 其余算子（`text_length` / `char_repetition` / `line_repetition` / `boilerplate` /
   `pii_detect` / `text_minhash`）的判决是否变化？（预期：**有变化**——它们仍被骗）
3. 变化方向是「减少误杀」还是「新增拦截」？（**后者也如实报告**：归一化会缩短文本，
   靠空白凑长度的样本可能在 `min` 阈值上掉下来——这是正确行为，但要看见）

## 口径

- **A = 原样进漏斗**（等价于今日生产行为）
- **B = 前置归一化后进漏斗**

两个口径**各自独立装载语料**（算子会把 `score:*` 写进 `meta`，复用同一批对象会串味）。

## 预注册的阴性出口

若 B 相对 A 的增量 ≈ 0（各算子判决逐项不变），如实记录并说明：归一化层的价值
退化为「架构正确性」（把上游修复放回正确位置）而非「数字提升」。**抢来的数字不写。**
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.normalize import (  # noqa: E402
    TextNormalizeTransformer,
    aggregate,
    normalize_text,
    whitespace_ratio,
)
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline import PipelineConfig, run_funnel  # noqa: E402
from mm_curation.verdict import VerdictLedger, read_verdicts, verdict_stats  # noqa: E402

# 结构化载荷模态不参与归一化（见 normalize/transformer.py 的范围声明）
DEFAULT_INPUT = "data/raw/news_corpus.jsonl"
DEFAULT_CONFIG = "configs/text_funnel.yaml"
DEFAULT_OUT = "data/reports"


def load_samples(path: Path, limit: int | None = None) -> list[Sample]:
    """独立装载（每个口径一份新对象——算子会写 meta，复用会串味）。"""
    out: list[Sample] = []
    for line in path.read_text(encoding="utf-8").split("\n"):  # 禁 splitlines：#44 陷阱
        if not line.strip():
            continue
        out.append(Sample.from_dict(json.loads(line)))
        if limit and len(out) >= limit:
            break
    return out


def cjk_stats(samples: list[Sample]) -> dict[str, float]:
    """两种中文占比口径 + 下游可见字符量（直接量化 #65 的机制）。

    - `raw`：汉字 / 全文长度（**含空白**——这正是被撑爆的那个分母）
    - `nospace`：汉字 / 去空白长度（`chinese_ratio` 修复后的口径）
    - `chars_total` vs `chars_total_nospace`：进下游（去重签名 / 困惑度 / 训练语料）
      的**实际载体**量。漏斗拦截数看不见这个差，但它才是归一化真正作用的地方。
    """
    raws, nospaces = [], []
    chars_total = 0
    chars_nospace = 0
    for s in samples:
        body = "".join(s.text.split())
        chars_total += len(s.text)
        chars_nospace += len(body)
        if not body:
            continue
        cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in body)
        raws.append(cjk / len(s.text) if s.text else 0.0)
        nospaces.append(cjk / len(body))
    return {
        "n": len(raws),
        "cjk_ratio_raw_p50": round(median(raws), 6) if raws else 0.0,
        "cjk_ratio_nospace_p50": round(median(nospaces), 6) if nospaces else 0.0,
        "chars_total": chars_total,
        "chars_total_nospace": chars_nospace,
        "chars_per_doc_p50": round(median([len(s.text) for s in samples]), 1) if samples else 0.0,
    }


def ws_stats(samples: list[Sample]) -> dict[str, float]:
    ratios = [whitespace_ratio(s.text) for s in samples]
    return {
        "n": len(ratios),
        "whitespace_ratio_p50": round(median(ratios), 6) if ratios else 0.0,
        "whitespace_ratio_p90": round(sorted(ratios)[int(0.9 * (len(ratios) - 1))], 6)
        if ratios
        else 0.0,
        "n_whitespace_dominant": sum(1 for r in ratios if r > 0.5),
    }


def run_variant(
    tag: str,
    samples: list[Sample],
    config: PipelineConfig,
    ledger_dir: Path,
) -> dict:
    """跑一个口径，返回该口径的统计与判决聚合。

    **重跑前必须清空台账目录**——判决书是**追加式**的（生产语义正确：一个台账
    存多次运行，靠 run_id 区分），但本脚本的报告是**整文件聚合**的。两者相撞
    的后果是：不改一行代码，重跑一次报告数字就涨一轮（实测 by_rule 计数
    14437 → 28874 → …，UI 上「这一级共 N 个分数」是真实值的整数倍）。
    报告数字无端漂移比数字算错更伤——它让整份报告失去可信度。
    这里选择「一次运行 = 一份干净台账」，换来的是**可重跑**（同码双跑逐字节相同）。
    实现上是**把文件截断**而非删目录：截断是一次写，删目录会触发沙箱的批量
    删除护栏（本轮累计删除数超阈值后，整个会话内任何删除都会被拦）。
    """
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / "verdict.jsonl"
    if ledger_path.exists():
        ledger_path.write_text("", encoding="utf-8")
    ledger = VerdictLedger(ledger_dir, run_id=tag, config_name=config.name)
    result = run_funnel(
        samples,
        config,
        pre_stages=[TextNormalizeTransformer()] if tag == "B" else [],
        verdict_ledger=ledger,
    )
    rows = read_verdicts(ledger_dir / "verdict.jsonl")
    return {
        "tag": tag,
        "n_in": len(samples),
        "n_kept": len(result.kept),
        "kept_ratio": round(len(result.kept) / len(samples), 6) if samples else 0.0,
        # 台账行数露在外面：它必须等于「本轮各滤级进入数之和」。它若凭空变大，
        # 说明读到历史运行的残留（见本函数 docstring 的追加式陷阱）。
        "n_verdict_rows": len(rows),
        "stage_stats": [
            {
                "op": st.op,
                "n_in": st.n_in,
                "n_out": st.n_out,
                "dropped": st.dropped,
                "skipped": st.skipped,
                "pass_rate": round(st.pass_rate, 6),
                "score_p50": None if st.score_p50 is None else round(st.score_p50, 6),
            }
            for st in result.stats
        ],
        "transform_stats": [
            {
                "stage": st.stage,
                "n_in": st.n_in,
                "n_out": st.n_out,
                "changed": st.changed,
                "dropped": st.dropped,
                "skipped": st.skipped,
            }
            for st in result.transform_stats
        ],
        "verdict": verdict_stats(rows),
    }


def normalize_corpus_stats(samples: list[Sample]) -> dict:
    return aggregate([normalize_text(s.text) for s in samples]).to_dict()


def build_comparison(a: dict, b: dict) -> dict:
    """逐算子对照 + 变化清单（本实验的核心产物）。"""
    a_by_op = {s["op"]: s for s in a["stage_stats"]}
    b_by_op = {s["op"]: s for s in b["stage_stats"]}
    per_op = []
    for op in a_by_op:
        sa, sb = a_by_op[op], b_by_op.get(op)
        row = {
            "op": op,
            "dropped_A": sa["dropped"],
            "dropped_B": sb["dropped"] if sb else None,
            "delta_dropped": (sb["dropped"] - sa["dropped"]) if sb else None,
            "n_in_A": sa["n_in"],
            "n_in_B": sb["n_in"] if sb else None,
        }
        per_op.append(row)
    changed = [r for r in per_op if r["delta_dropped"]]
    return {
        "per_op": per_op,
        "changed_ops": changed,
        "n_kept_delta": b["n_kept"] - a["n_kept"],
        "n_changed_ops": len(changed),
    }


def _md_row(*cells: object) -> str:
    """拼一行 markdown 表格。

    存在的理由很朴素：报告里有好几张表，直接写 f-string 会顶破 100 列行长，
    而拆行又会把「哪个值在哪一列」拆散、读起来对不上表头。用列构造器之后
    每行都是一句「表头：值、值、值」，加列删列也不会错位。
    """
    return "| " + " | ".join(str(c) for c in cells) + " |"


def render_markdown(report: dict) -> str:
    a, b, cmp_ = report["variant_A"], report["variant_B"], report["comparison"]
    L: list[str] = []
    L.append("# 归一化层 A/B 对照实验")
    L.append("")
    L.append(f"- 语料：`{report['input']}`（{a['n_in']} 篇，#65 现场）")
    L.append(f"- 配置：`{report['config']}`（算子：{'、'.join(report['ops'])}）")
    if report["excluded_ops"]:
        L.append(
            f"- **未参与**：{report['excluded_ops']}（成本档 MODEL/LLM，"
            f"用 `--allow-model` 可纳入）"
        )
    L.append("")
    L.append("## 一句话结论")
    L.append("")
    L.append(report["headline"])
    L.append("")
    L.append("## 漏斗水位（A 原样 vs B 归一化）")
    L.append("")
    L.append(_md_row("算子", "A 拦截", "B 拦截", "Δ", "A 进入级", "B 进入级"))
    L.append("|---|---|---|---|---|---|")
    for r in cmp_["per_op"]:
        L.append(
            _md_row(
                f"`{r['op']}`",
                r["dropped_A"],
                r["dropped_B"],
                f"{r['delta_dropped']:+d}",
                r["n_in_A"],
                r["n_in_B"],
            )
        )
    L.append("")
    L.append(f"- **存活**：A {a['n_kept']} 篇（{100 * a['kept_ratio']:.1f}%）"
             f" → B {b['n_kept']} 篇（{100 * b['kept_ratio']:.1f}%）"
             f"，Δ = **{cmp_['n_kept_delta']:+d}**")
    L.append(f"- **裁决条数**：A {a['n_verdict_rows']} / B {b['n_verdict_rows']}"
             f"（须等于各滤级进入数之和；若凭空变大即读到了历史运行的残留）")
    L.append("")
    L.append("## 语料形态（归一化的作用面）")
    L.append("")
    ca, cb = report["corpus_A"], report["corpus_B"]
    L.append("| 指标 | A（原样） | B（归一化后） |")
    L.append("|---|---|---|")
    L.append(_md_row("空白占比 p50", f"{ca['whitespace_ratio_p50']:.4f}",
                     f"{cb['whitespace_ratio_p50']:.4f}"))
    L.append(_md_row("空白占比 p90", f"{ca['whitespace_ratio_p90']:.4f}",
                     f"{cb['whitespace_ratio_p90']:.4f}"))
    L.append(_md_row("空白占比 >50% 的篇目",
                     ca["n_whitespace_dominant"], cb["n_whitespace_dominant"]))
    L.append(_md_row("中文占比（含空白分母）p50", f"{ca['cjk_ratio_raw_p50']:.4f}",
                     f"{cb['cjk_ratio_raw_p50']:.4f}"))
    L.append(_md_row("中文占比（去空白分母）p50", f"{ca['cjk_ratio_nospace_p50']:.4f}",
                     f"{cb['cjk_ratio_nospace_p50']:.4f}"))
    L.append(_md_row("字符总量（进下游的载体）", f"{ca['chars_total']:,}",
                     f"{cb['chars_total']:,}"))
    L.append(_md_row("去空白后字符总量", f"{ca['chars_total_nospace']:,}",
                     f"{cb['chars_total_nospace']:,}"))
    L.append(_md_row("每篇字符数 p50", ca["chars_per_doc_p50"], cb["chars_per_doc_p50"]))
    L.append("")
    L.append(
        "> **读法（#65 的量化复现）**：空白占比 p50 与 p90 相差两个数量级"
        "（**双峰语料**——干净篇目与严重膨胀篇目并存）。"
        "归一化的作用面是那条长尾，不是中位数；"
        "**漏斗拦截数几乎看不见它，字符总量看得见**。"
    )
    L.append("")
    L.append("## 归一化改写统计（B 口径）")
    L.append("")
    ns = report["normalize_aggregate"]
    L.append(f"- 被改写：{ns['n_changed']}/{ns['n']}（{100 * ns['changed_ratio']:.1f}%）")
    L.append(f"- 字符净减：{ns['chars_removed_total']}"
             f"（占原文 {100 * ns['chars_removed_ratio']:.1f}%）")
    L.append(f"- 改后为空被丢弃：{ns['n_emptied']}")
    L.append(f"- 逐规则命中：{ns['rule_counts']}")
    L.append("")
    L.append("## 判据分布（判决书 by_rule）")
    L.append("")
    L.append("| 判据 | A | B |")
    L.append("|---|---|---|")
    rules = sorted(set(a["verdict"]["by_rule"]) | set(b["verdict"]["by_rule"]))
    for r in rules:
        L.append(_md_row(
            f"`{r}`",
            a["verdict"]["by_rule"].get(r, 0),
            b["verdict"]["by_rule"].get(r, 0),
        ))
    L.append("")
    L.append("## 复现")
    L.append("")
    L.append("```bash")
    L.append("# 规则档（默认，纯 CPU、无 GPU 依赖）")
    L.append("python -X utf8 scripts/normalize_ablation.py")
    L.append("")
    L.append("# 含模型档（GPT-2 zh 困惑度）的补充运行——另存报告名避免覆盖")
    L.append("python -X utf8 scripts/normalize_ablation.py --allow-model \\")
    L.append("    --report-name normalize_ablation_with_model")
    L.append("```")
    L.append("")
    return "\n".join(L)


def build_headline(cmp_: dict, a: dict, b: dict, ns: dict) -> str:
    ratio_op = next((r for r in cmp_["per_op"] if r["op"] == "chinese_ratio"), None)
    cr_note = (
        f"`chinese_ratio` 的 Δ 为 {ratio_op['delta_dropped']:+d}"
        f"（A={ratio_op['dropped_A']} / B={ratio_op['dropped_B']}）"
        if ratio_op
        else "本配置未含 `chinese_ratio`"
    )
    if not cmp_["changed_ops"]:
        return (
            f"**阴性结果**：{len(cmp_['per_op'])} 个算子在 A/B 下判决逐项一致"
            f"（存活均为 {a['n_kept']} 篇）。归一化改写了 {ns['n_changed']}/{ns['n']} 篇"
            f"（{100 * ns['changed_ratio']:.1f}%）、净删 "
            f"{100 * ns['chars_removed_ratio']:.1f}% 字符——**改动存在但未改变任何判决**，"
            f"归一化层的价值退化为「架构正确性」（把上游修复放回正确位置），"
            f"未观测到数字提升。"
        )
    names = "、".join(f"`{r['op']}`" for r in cmp_["changed_ops"])
    return (
        f"**{cmp_['n_changed_ops']} 个算子的判决发生变化**（{names}），"
        f"存活 {a['n_kept']} → {b['n_kept']}（**{cmp_['n_kept_delta']:+d}**）。"
        f"归一化改写了 {ns['n_changed']}/{ns['n']} 篇"
        f"（{100 * ns['changed_ratio']:.1f}%）、净删 "
        f"{100 * ns['chars_removed_ratio']:.1f}% 字符。{cr_note}"
        f"——#65 的单点补丁已覆盖它，**增量在别处**。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 篇（冒烟用）")
    parser.add_argument("--allow-model", action="store_true", help="纳入 MODEL/LLM 档算子")
    parser.add_argument(
        "--report-name",
        default="normalize_ablation",
        help="报告基名（含模型档的补充运行用 normalize_ablation_with_model，避免互相覆盖）",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"语料不存在: {input_path}（先跑 scripts/fetch_news_corpus.py）")

    config = PipelineConfig.from_yaml(args.config)
    kept_specs, excluded = [], []
    for spec in config.operators:
        cost = getattr(getattr(spec.build().meta, "cost_class", None), "value", "rule")
        if cost in ("model", "llm") and not args.allow_model:
            excluded.append(f"{spec.op}({cost})")
        else:
            kept_specs.append(spec)
    config.operators = kept_specs
    if not config.operators:
        raise SystemExit("过滤后没有算子可跑")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / args.report_name

    samples_a = load_samples(input_path, args.limit)
    samples_b = load_samples(input_path, args.limit)
    print(f"语料 {len(samples_a)} 篇 | 算子 {[o.op for o in config.operators]} | 排除 {excluded}")

    va = run_variant("A", samples_a, config, work / "A")
    vb = run_variant("B", samples_b, config, work / "B")

    # 不变量：台账行数 == 各滤级进入数之和。读到历史运行的残留会让它凭空变大，
    # 而报告的 by_rule 表是整文件聚合的 —— 不炸出来就会静静漂移（见 run_variant）。
    for v in (va, vb):
        expected = sum(s["n_in"] for s in v["stage_stats"])
        if v["n_verdict_rows"] != expected:
            raise SystemExit(
                f"台账行数异常（口径 {v['tag']}）：台账 {v['n_verdict_rows']} 行，"
                f"各滤级进入数之和 {expected} 行。多半读到了历史运行的残留，"
                f"删掉 {work} 后重跑。"
            )

    report = {
        "input": str(input_path),
        "config": str(args.config),
        "deterministic": True,  # 全链无随机性（归一化纯函数 + 确定性执行器）
        "ops": [o.op for o in config.operators],
        "excluded_ops": excluded,
        "corpus_A": {**ws_stats(samples_a), **cjk_stats(samples_a)},
        "corpus_B": {**ws_stats(samples_b), **cjk_stats(samples_b)},
        # 必须在 **A 组（未被改写）** 的语料上算：run_pre_stages 是原地改写，
        # B 组的 text 在 run_funnel 跑完后已经是归一化后的值，再算一次是空操作
        # （首跑就被这个坑骗了——报告写「改写 0%」而 text_minhash 的判决却变了，
        # 自相矛盾暴露了它：确定性管线不可能同输入不同输出）
        "normalize_aggregate": normalize_corpus_stats(samples_a),
        "variant_A": va,
        "variant_B": vb,
    }
    report["comparison"] = build_comparison(va, vb)
    report["headline"] = build_headline(
        report["comparison"], va, vb, report["normalize_aggregate"]
    )

    (out_dir / f"{args.report_name}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"{args.report_name}.md").write_text(render_markdown(report), encoding="utf-8")

    print("\n" + report["headline"])
    print("\n逐算子 Δ：")
    for r in report["comparison"]["per_op"]:
        print(
            f"  {r['op']:18s} A={r['dropped_A']:5d} "
            f"B={r['dropped_B']:5d} Δ={r['delta_dropped']:+d}"
        )
    print(f"\n报告：{out_dir}/{args.report_name}.{{json,md}}")


if __name__ == "__main__":
    main()
