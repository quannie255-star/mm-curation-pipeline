"""判官微调数据生成（V3 ζ3）：域语料 + 程序化污染 → SFT 训练对。

与 benchmark 的隔离（独立性三原则的另一半，见 benchmarks/builder.py）：
- 训练 seed 族与 benchmark seed（9000）不同
- 训练损伤配比与 benchmark 配比不同（含 pii_inject，权重不同）
- 泄漏检查由 benchmark 侧对训练集文件执行（构建 benchmark 时强制）

SFT 格式与 LlmJudgeOp 的 rubric prompt 完全一致——微调后的判官是同一个
协议位上的即插即用替换（同一 prompt、同一 JSON 输出契约）。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from curation_eval import ContaminationPlan, Sample

from ..operators.llm_judge import _JUDGE_PROMPT

# 与 benchmark（builder 默认 kinds）刻意不同：加入 pii_inject 且权重不同
TRAIN_KINDS = {
    "paragraph_repeat": 0.35,
    "boilerplate_inject": 0.25,
    "whitespace_pad": 0.2,
    "pii_inject": 0.2,
}
TRAIN_SEED = 23  # 训练 seed 族（benchmark 用 9000，构建时会强制断言隔离）

_DIRTY_REASONS = {
    "paragraph_repeat": "段落大量复读",
    "boilerplate_inject": "含广告推广模板句",
    "whitespace_pad": "大量空白填充",
    "pii_inject": "包含联系方式等隐私信息",
}


def build_sft_rows(
    corpus: list[Sample],
    *,
    n_clean: int,
    n_dirty: int,
    seed: int = TRAIN_SEED,
    images_out: Path | None = None,
    exclude_source_ids: set[str] | None = None,
) -> list[dict]:
    """域语料 → SFT 行：{prompt, completion, label, kind, source_id}。

    exclude_source_ids：benchmark 已占用的源文档 id——同源文档换 seed 污染
    ≠ 独立样本，必须结构性排除（泄漏检查只防文本级重合，防不了同源增强）。
    """
    exclude = exclude_source_ids or set()
    pool_all = [s for s in corpus if s.id not in exclude]
    if len(pool_all) < n_clean:
        raise ValueError(f"排除 benchmark 源后域语料不足：需 {n_clean}，只有 {len(pool_all)}")
    rng = random.Random(seed)
    pool = sorted(pool_all, key=lambda s: s.id)
    rng.shuffle(pool)
    clean = pool[:n_clean]

    plan = ContaminationPlan(inject_rate=1.0, seed=seed, kinds=TRAIN_KINDS)
    mixed, _ = plan.run(
        [Sample(id=s.id, text=s.text) for s in clean],
        images_out or Path("data/interim/tune_images"),
    )
    dirty = [s for s in mixed if s.labels.get("dirty")][:n_dirty]

    def score_for(label: str, kind: str) -> int:
        if label == "clean":
            return rng.randint(7, 10)
        return {
            "paragraph_repeat": rng.randint(0, 2),
            "boilerplate_inject": rng.randint(1, 3),
            "whitespace_pad": rng.randint(2, 4),
            "pii_inject": rng.randint(0, 2),
        }.get(kind, 1)

    rows = []
    for s in clean:
        rows.append(_row(s.text, score_for("clean", ""), "clean", "clean", s.id))
    for s in dirty:
        kind = s.labels["dirty"]
        rows.append(_row(s.text, score_for("dirty", kind), "dirty", kind, None))
    rng.shuffle(rows)
    return rows


def _row(text: str, score: int, label: str, kind: str, source_id: str | None) -> dict:
    reason = (
        "内容正常，适合作为训练语料" if label == "clean" else _DIRTY_REASONS.get(kind, "质量低下")
    )
    completion = json.dumps({"score": score, "reason": reason}, ensure_ascii=False)
    return {
        "prompt": _JUDGE_PROMPT + text[:2000],
        "completion": completion,
        "label": label,
        "kind": kind,
        "source_id": source_id,
    }


def write_sft_jsonl(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
