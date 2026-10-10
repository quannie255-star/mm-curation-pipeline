"""训练并评测文本质量分类器（G3）。

⚠️ 这个脚本的**重点不是分类器**，而是**正例分布 + 双风格组评测**。
DataComp-LM 的实证：①廉价 n-gram 线性分类器胜过昂贵 LLM 打分；
②**「正例参考分布定义得好，比分类器 sophistication 更重要」**
（OpenHermes+ELI5 作正例 Core 41.0vs Wikipedia 作正例 35.7）。

所以产物里有两样东西同等重要：
  1. 分类器权重（`models/text_quality/`）
  2. **它是为哪个下游目标训的**（meta.json 里的 `positives_kind` + 它的定义原文）

用法：
    python -X utf8 scripts/train_text_quality_classifier.py
    python -X utf8 scripts/train_text_quality_classifier.py --n-pos 4000
产物：models/text_quality/text_quality_clf.{pkl,meta.json}
     data/reports/text_quality_classifier.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mm_curation.text_quality_classifier import (  # noqa: E402
    A_SUBSTYLES,
    POSITIVE_KINDS,
    STYLE_A,
    STYLE_B,
    assert_no_overlap,
    assert_not_memorized,
    evaluate,
    group_by_style,
    pick_threshold,
    save_model,
    split_positives,
    train_classifier,
)

CORPUS = ROOT / "data/raw/text_corpus.jsonl"
OUT_DIR = ROOT / "models/text_quality"
REPORT = ROOT / "data/reports/text_quality_classifier.json"
log = logging.getLogger("text_quality")


def load_clean_texts(n: int) -> list[str]:
    """读**真实抓取语料**作正例候选。

    ⚠️ 不自己造「干净文本」：合成正例会让分类器学到合成器的指纹，
    那正是本项目要防的循环论证。真实维基语料是唯一诚实的正例来源。
    """
    if not CORPUS.exists():
        raise SystemExit(f"❌ 语料缺失: {CORPUS}\n   先跑 python scripts/download_text_corpus.py")
    out: list[str] = []
    with CORPUS.open(encoding="utf-8") as fh:
        for ln in fh:
            if not ln.strip():
                continue
            try:
                t = json.loads(ln).get("text", "")
            except json.JSONDecodeError:
                continue
            if len(t) >= 200:
                out.append(t)
            if len(out) >= n:
                break
    if len(out) < n:
        raise SystemExit(
            f"❌ 语料不足：需要 {n} 条 ≥200 字，只有 {len(out)} 条。"
            "**不许降低质量门槛充数** —— 正例质量决定了分类器的上限。"
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-pos", type=int, default=4000, help="正例候选条数")
    parser.add_argument("--n-test", type=int, default=200, help="每风格的测试集条数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--positives-kind",
        default="wikipedia_zh",
        choices=sorted(POSITIVE_KINDS),
        help="正例分布 id（会写进 meta.json，改名等于破坏可比性）",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    kind = args.positives_kind
    log.info("正例分布：%s —— %s", kind, POSITIVE_KINDS[kind])

    texts = load_clean_texts(args.n_pos + args.n_test * 6)
    # ⚠️ 先切分后注入：否则同一条原文的两个损伤版本可能分居训练/测试两侧。
    train_pos, test_pos = split_positives(texts, args.seed, args.n_test)

    # A 组（训练内）：**用全部子风格**扩训练分布。
    # ⚠️ 只用单一风格训练时，B 组负例召回实测0.022（几乎全失效）——
    #   分类器只学会了那一种损伤的表面特征。扩分布是唯一的正解，
    #   放宽阈值等于把门禁调绿（见 A_SUBSTYLES 的注释）。
    neg_a_train: list[str] = []
    for sub in A_SUBSTYLES:
        neg_a_train.extend(group_by_style(train_pos, sub, args.seed))
    neg_a_test = group_by_style(test_pos, STYLE_A, args.seed)
    neg_b_test = group_by_style(test_pos, STYLE_B, args.seed)

    assert_no_overlap(train_pos, test_pos)
    log.info(
        "训练正例 %s / 测试正例 %s；A组负例 %s / %s；B组负例 %s",
        len(train_pos),
        len(test_pos),
        len(neg_a_train),
        len(neg_a_test),
        len(neg_b_test),
    )
    log.info("A 组子风格 %d 个：", len(A_SUBSTYLES))
    for sub in A_SUBSTYLES:
        log.info("   %s", sub.describe())
    log.info("B 组（held-out）: %s", STYLE_B.describe())

    vec, clf = train_classifier(train_pos, neg_a_train, seed=args.seed)
    # 阈值现取，只用 A 组（第一版硬编码 0.5 → 扩分布后 balance塌到 0.56）
    th, sweep = pick_threshold(vec, clf, test_pos, neg_a_test)
    log.info("阈值扫描（A 组）: %s", sweep)
    log.info("选定阈值=%.2f（**仅由 A 组选出，不碰 B 组**）", th)
    report = evaluate(vec, clf, test_pos, neg_a_test, test_pos, neg_b_test, threshold=th)
    report["threshold_sweep_on_A"] = sweep

    # B 组塌下来就报错 —— 分类器不能只是背下了 A 组的表面特征
    try:
        assert_not_memorized(report)
        log.info(
            "泛化 gap = %+.4f（B组 balance %s vs A组 %s）",
            report["generalization_gap"],
            round(report["testB"]["balance"], 4),
            round(report["testA"]["balance"], 4),
        )
    except AssertionError as exc:
        log.error("❌ %s", exc)
        report["generalization_check"] = "FAIL"
    else:
        report["generalization_check"] = "PASS"

    meta = {
        "positives_kind": kind,
        "positives_definition": POSITIVE_KINDS[kind],
        "feature": "char_wb 2-4gram tfidf + logistic regression",
        "why_cheap_classifier": (
            "DataComp-LM 1B 档实证：fastText bigram 30.2 Core > "
            "Perplexity 29.0 > AskLLM 28.6 —— 廉价 n-gram 线性分类器胜过 LLM 打分"
        ),
        "style_group_a": [s.describe() for s in A_SUBSTYLES],
        "style_group_b": STYLE_B.describe(),
        "anti_circular_argumentation": (
            "A/B 在**损伤类型、字面量池、强度、截断比例**四个维度错开，"
            "不是随机切分（随机切分会泄漏「污染器随机种子」）。"
            "B 组不参与任何训练与选择决策，只做最终泛化评测。"
        ),
        "seed": args.seed,
        "threshold": report["threshold"],
        "threshold_selection": (
            "在 A 组（训练内分布）上扫 0.05..0.95 取 balance 最优。"
            "**不使用 B 组调阈值** —— 那样泛化检查会立刻失去意义。"
            "第一版硬编码 0.5，扩训练分布后 balance塌到 0.56（阈值是事件不是状态）。"
        ),
        "n_train_pos": len(train_pos),
        "n_train_neg_a": len(neg_a_train),
        "n_test_pos": len(test_pos),
        "caveat": (
            "本分类器只承诺「像不像真实干净维基」，**不承诺「对下游任务有用」**。"
            "DataComp-LM 明确指出「看起来高质量」不等于「对下游有用」，"
            "且人类判断与 P:R 都不足以预测训练效用 —— "
            "真要证明有用，须跑 scripts/eval_training_utility.py。"
        ),
    }
    path = save_model(vec, clf, OUT_DIR, meta)
    report["meta"] = meta
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("模型已落盘: %s", path)
    log.info("报告已落盘: %s", REPORT)
    return 0 if report.get("generalization_check") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
