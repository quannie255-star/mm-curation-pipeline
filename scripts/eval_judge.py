"""δ2 验收：LLM-judge 可信度实验（Cohen's kappa，决策 7 的评测协议）。

流程：干净维基样本 + 程序化污染（带标注脏集）→ judge 全评 → 三个一致性：
(a) judge vs 脏标签（可信度主证）
(b) judge vs L1 漏斗判定（增量信息：κ 高 = L3 冗余，κ 低 = 互补）
(c) L1 vs 脏标签（上下文参照）
kappa 只在 judge 实际评判的样本上结算（解析失败/未评判的不掺假）。

前置：python -X utf8 scripts/serve_judge.py（另一个终端）
用法：python -X utf8 scripts/eval_judge.py [--n 400] [--base-url http://127.0.0.1:8100/v1]
产物：data/reports/judge_kappa.{json,md}
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from curation_eval import (  # noqa: E402
    ContaminationPlan,
    LocalSequentialExecutor,
    Sample,
    cohen_kappa,
    pr_from_drops,
)
from curation_eval.metrics import recall_at_k  # noqa: F401,E402  (口径说明用)

from mm_curation.operators.llm_judge import LlmJudgeOp  # noqa: E402
from mm_curation.pipeline.config import PipelineConfig  # noqa: E402

CORPUS = Path("data/raw/text_corpus.jsonl")
CONFIG = Path("configs/text_funnel.yaml")
REPORT = Path("data/reports/judge_kappa")
L3_OPS = {"text_minhash", "perplexity", "llm_judge"}


def load_clean(n: int, min_chars: int = 80) -> list[Sample]:
    lines = CORPUS.read_text(encoding="utf-8").split("\n")
    out, i = [], 0
    for ln in lines:
        if len(out) >= n:
            break
        if not ln.strip():
            continue
        text = json.loads(ln)["text"]
        if len(text) < min_chars:
            continue
        out.append(Sample(id=f"clean{i:06d}", text=text))
        i += 1
    return out


def l1_ops() -> list:
    """text_funnel.yaml 的 L1 六级（剔除去重/困惑度/L3）。"""
    cfg = PipelineConfig.from_yaml(CONFIG)
    return [spec.build() for spec in cfg.operators if spec.op not in L3_OPS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=400, help="干净样本数")
    parser.add_argument("--inject-rate", type=float, default=0.5)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100/v1")
    parser.add_argument("--min", type=float, default=0.5, help="judge 通过阈值")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    clean = load_clean(args.n)
    logging.info("干净样本 %s 篇", len(clean))
    mixed, manifest = ContaminationPlan(
        inject_rate=args.inject_rate,
        seed=23,
        kinds={"paragraph_repeat": 0.5, "boilerplate_inject": 0.5},
    ).run(clean, Path("data/interim/judge_kappa_images"))
    dirty_ids = {s.id for s in mixed if s.labels.get("dirty")}
    logging.info("混合集 %s 篇（脏 %s）", len(mixed), len(dirty_ids))

    # L1 漏斗判定
    l1 = LocalSequentialExecutor().run(l1_ops(), mixed)
    l1_dropped = {s.id for _, s in l1.dropped}
    y_l1 = {s.id: (1 if s.id in l1_dropped else 0) for s in mixed}
    l1_pr = pr_from_drops(sorted(l1_dropped), mixed)
    logging.info("L1: 丢弃 %s，P/R = %s/%s", len(l1_dropped), l1_pr["precision"], l1_pr["recall"])

    # judge 全评
    judge = LlmJudgeOp(
        base_url=args.base_url, sample_rate=1.0, max_workers=args.workers, min=args.min
    )
    judge.run_batch(list(mixed))
    scores = {s.id: s.meta.get("score:llm_judge") for s in mixed if "score:llm_judge" in s.meta}
    judged = {sid: sc for sid, sc in scores.items() if sc is not None}
    y_judge = {sid: (1 if sc < args.min else 0) for sid, sc in judged.items()}
    judge_drop_ids = {sid for sid, d in y_judge.items() if d == 1}
    judge_pr = pr_from_drops(sorted(judge_drop_ids), mixed)
    logging.info(
        "judge: 评判 %s（解析失败 %s），丢弃 %s，P/R = %s/%s",
        len(judged),
        judge.n_unparsed,
        len(judge_drop_ids),
        judge_pr["precision"],
        judge_pr["recall"],
    )

    # 三个一致性（judge 口径只对实际评判的样本结算）
    kappa_judge_dirty = cohen_kappa(
        [y_judge[i] for i in y_judge], [1 if i in dirty_ids else 0 for i in y_judge]
    )
    kappa_judge_l1 = cohen_kappa([y_judge[i] for i in y_judge], [y_l1[i] for i in y_judge])
    kappa_l1_dirty = cohen_kappa(
        [y_l1[s.id] for s in mixed], [1 if s.id in dirty_ids else 0 for s in mixed]
    )
    agreement = sum(1 for i in y_judge if y_judge[i] == y_l1[i]) / len(y_judge)

    # 分歧样本（judge 与 L1 判定不同）
    kind_of = {s.id: (s.labels.get("dirty") or "clean") for s in mixed}
    text_of = {s.id: s.text for s in mixed}
    disagreements = [
        {
            "id": i,
            "kind": kind_of.get(i, "?"),
            "judge_score": judged[i],
            "l1_drop": y_l1[i],
            "preview": text_of.get(i, "")[:60].replace("\n", " "),
        }
        for i in y_judge
        if y_judge[i] != y_l1[i]
    ][:8]

    # 阈值扫描：judge 分数分布可能整体压在高位（小模型常见），min=0.5
    # 未必是可用工作点——κ(t) 曲线找最优工作点，与 α 阈值联合校准同方法论
    scan = []
    for t_ in [0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
        y_t = {sid: (1 if sc < t_ else 0) for sid, sc in judged.items()}
        k_d = cohen_kappa(list(y_t.values()), [1 if i in dirty_ids else 0 for i in y_t])
        drops_t = {sid for sid, d in y_t.items() if d == 1}
        pr_t = pr_from_drops(sorted(drops_t), mixed)
        scan.append(
            {
                "threshold": t_,
                "kappa_judge_dirty": k_d,
                "precision": pr_t["precision"],
                "recall": pr_t["recall"],
                "n_drop": len(drops_t),
            }
        )
    best = max(
        scan, key=lambda r: r["kappa_judge_dirty"] if r["kappa_judge_dirty"] is not None else -1
    )

    result = {
        "n_mixed": len(mixed),
        "n_dirty": len(dirty_ids),
        "n_judged": len(judged),
        "n_unparsed": judge.n_unparsed,
        "kappa": {
            "judge_vs_dirty": kappa_judge_dirty,
            "judge_vs_l1": kappa_judge_l1,
            "l1_vs_dirty": kappa_l1_dirty,
        },
        "threshold_scan": scan,
        "best_operating_point": best,
        "scores": judged,
        "judge_agrees_l1": round(agreement, 4),
        "pr": {"judge": judge_pr, "l1": l1_pr},
        "judge_stats": judge.stats_snapshot(),
        "disagreements": disagreements,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.with_suffix(".json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def _band(k):
        if k is None:
            return "不可评"
        if k >= 0.8:
            return "几乎完全一致"
        if k >= 0.6:
            return "强一致"
        if k >= 0.4:
            return "中等"
        if k >= 0.2:
            return "偏弱"
        return "弱（接近随机）"

    md = [
        "# δ2 验收：LLM-judge 可信度（Cohen's kappa）",
        "",
        f"- 混合集 {len(mixed)} 篇（脏 {len(dirty_ids)}，paragraph_repeat + "
        f"boilerplate_inject 各半）；judge 实际评判 {len(judged)} 篇"
        f"（解析失败 {judge.n_unparsed}）",
        "",
        "| 判定对 | κ | 解读 |",
        "|---|---|---|",
        f"| judge vs 脏标签 | {kappa_judge_dirty:.3f} | "
        f"{_band(kappa_judge_dirty)}——judge 的可信度 |",
        f"| judge vs L1 漏斗 | {kappa_judge_l1:.3f} | {_band(kappa_judge_l1)}"
        f"（一致率 {agreement:.1%}；κ 低 = 与规则互补，高 = 冗余） |",
        f"| L1 vs 脏标签（参照） | {kappa_l1_dirty:.3f} | {_band(kappa_l1_dirty)} |",
        "",
        "| 判定者 | precision | recall |",
        "|---|---|---|",
        f"| judge | {judge_pr['precision']} | {judge_pr['recall']} |",
        f"| L1 六级 | {l1_pr['precision']} | {l1_pr['recall']} |",
        f"| judge 调用 | {judge.n_calls} 次 / 错误 {judge.n_errors} / "
        f"平均 {judge.stats_snapshot()['avg_latency_s']:.2f}s | |",
        "",
        "## 阈值扫描（judge vs 脏标签的 κ(t) 曲线）",
        "",
        "| 阈值 | κ | precision | recall | 丢弃数 |",
        "|---|---|---|---|---|",
    ]
    for r in scan:
        k_disp = r["kappa_judge_dirty"] if r["kappa_judge_dirty"] is not None else "—"
        md.append(
            f"| {r['threshold']} | {k_disp} | {r['precision']} | {r['recall']} | {r['n_drop']} |"
        )
    md += [
        "",
        f"最优工作点：t={best['threshold']}（κ={best['kappa_judge_dirty']}，"
        f"P/R={best['precision']}/{best['recall']}）——"
        "小模型分数分布压缩在高位是常态，工作点必须扫描不能拍脑袋",
        "",
        "## 分歧样本（judge vs L1）",
        "",
        "| id | 类型 | judge 分 | L1 丢弃 | 预览 |",
        "|---|---|---|---|---|",
    ]
    for d in disagreements:
        md.append(
            f"| {d['id']} | {d['kind']} | {d['judge_score']} "
            f"| {bool(d['l1_drop'])} | {d['preview']} |"
        )
    REPORT.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    logging.info("报告: %s.{json,md}", REPORT)


if __name__ == "__main__":
    main()
