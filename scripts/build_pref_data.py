"""η-a 入口：构造偏好 DPO 三元组 + 冻结偏好 benchmark。

用法：python -X utf8 scripts/build_pref_data.py
产物：data/interim/pref_dpo.jsonl（每 persona 400 三元组）
      benchmarks/pref_news_v1/{items.jsonl, manifest.json}（held-out 150 题）

隔离（结构性）：排除 judge_news_v1 源文档 + 按 ζ 同款逻辑复现 judge SFT
的 500 篇选样并排除——偏好数据与既有训练/评测零重叠。
"""

from __future__ import annotations

import json
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.tuning.judge_data import TRAIN_SEED  # noqa: E402
from mm_curation.tuning.preference import build_pref_items, write_benchmark  # noqa: E402

NEWS = Path("data/raw/news_corpus.jsonl")
BENCH_JUDGE = Path("benchmarks/judge_news_v1/items.jsonl")
SFT_OUT = Path("data/interim/pref_dpo.jsonl")
BENCH_OUT = Path("benchmarks/pref_news_v1")
N_SFT_USED = 500  # judge 微调用掉的文档数（finetune_judge_lora --n-clean 500）


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rows = [json.loads(ln) for ln in NEWS.read_text(encoding="utf-8").split("\n") if ln.strip()]
    corpus = {
        r["id"]: {"id": r["id"], "title": r["meta"]["title"], "text": r["text"]}
        for r in rows
        if len(r["text"]) >= 200
    }
    logging.info("新闻语料 %s 篇（≥200 字）", len(corpus))

    # 结构性排除 1：judge_news_v1 benchmark 的源文档
    exclude = set()
    for ln in BENCH_JUDGE.read_text(encoding="utf-8").split("\n"):
        if ln.strip():
            sid = json.loads(ln).get("source_id")
            if sid:
                exclude.add(sid)
    # 结构性排除 2：复现 judge SFT 的选样（同 corpus 同 seed 同参数 → 同一批）
    pool = sorted((d for d in corpus.values() if d["id"] not in exclude), key=lambda d: d["id"])
    random.Random(TRAIN_SEED).shuffle(pool)
    exclude |= {d["id"] for d in pool[:N_SFT_USED]}
    logging.info(
        "结构性排除 %s 篇（benchmark 源 %s + judge SFT 选样 %s）",
        len(exclude),
        150,
        min(N_SFT_USED, len(pool)),
    )

    pref_pool = [d for d in corpus.values() if d["id"] not in exclude]
    triples, items = build_pref_items(pref_pool)
    n_by = {}
    for t in triples:
        n_by[f"{t['persona']}/{t['kind']}"] = n_by.get(f"{t['persona']}/{t['kind']}", 0) + 1
    logging.info("DPO 三元组 %s（%s）", len(triples), n_by)

    SFT_OUT.parent.mkdir(parents=True, exist_ok=True)
    SFT_OUT.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in triples) + "\n",
        encoding="utf-8",
    )
    manifest = write_benchmark(items, BENCH_OUT, train_jsonl=SFT_OUT)
    logging.info(
        "冻结完成: %s（%s 题，泄漏 md5=%s minhash=%s）",
        BENCH_OUT,
        manifest["n_items"],
        len(manifest["leakage_check"]["md5_leaks"]),
        len(manifest["leakage_check"]["minhash_leaks"]),
    )


if __name__ == "__main__":
    main()
