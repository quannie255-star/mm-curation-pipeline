"""θ：从 v2 真人标注构造个人偏好 DPO 数据 + 冻结 benchmark。

用法：python -X utf8 scripts/build_user_pref_data.py \
        --labels data/annot/pref_labels_v2.jsonl \
        --out-dpo data/interim/pref_user_dpo.jsonl \
        --out-benchmark benchmarks/pref_user_v1
退出码：0=成功；2=标注不足 / 协议不一致 / 数据文件问题（向导据此给出白话提示）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.tuning.preference import build_user_pref_data, write_user_benchmark  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", default="data/annot/pref_labels_v2.jsonl")
    parser.add_argument("--out-dpo", default="data/interim/pref_user_dpo.jsonl")
    parser.add_argument("--out-benchmark", default="benchmarks/pref_user_v1")
    parser.add_argument("--holdout", type=float, default=0.25)
    parser.add_argument("--limit", type=int, default=0, help="学习曲线通道：只取前 N 个有效对")
    parser.add_argument("--min-pairs", type=int, default=80, help="最低有效对数（学习曲线时放宽）")
    parser.add_argument(
        "--freeze-eval-from", default=None,
        help="冻结考卷：从既有 benchmark items.jsonl 取 main 题 source_id，"
        "这些来源的对强制进评测、其余全进训练（加量不换考卷）",
    )
    parser.add_argument("--seed", type=int, default=53)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    labels_path = Path(args.labels)
    if not labels_path.exists():
        logging.error("标注文件不存在：%s", labels_path)
        sys.exit(2)
    labels = [
        json.loads(ln) for ln in labels_path.read_text(encoding="utf-8").split("\n") if ln.strip()
    ]
    if not labels:
        logging.error("标注文件为空：%s", labels_path)
        sys.exit(2)

    frozen: set[str] | None = None
    if args.freeze_eval_from:
        frozen = set()
        for ln in Path(args.freeze_eval_from).read_text(encoding="utf-8").split("\n"):
            if not ln.strip():
                continue
            it = json.loads(ln)
            if it.get("kind") == "main" and it.get("source_id"):
                frozen.add(it["source_id"])
    try:
        triples, items, stats = build_user_pref_data(
            labels,
            holdout_ratio=args.holdout,
            seed=args.seed,
            limit=args.limit,
            min_pairs=args.min_pairs,
            eval_source_ids=frozen,
        )
    except ValueError as e:
        logging.error("%s", e)
        sys.exit(2)

    out_dpo = Path(args.out_dpo)
    out_dpo.parent.mkdir(parents=True, exist_ok=True)
    out_dpo.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in triples) + "\n", encoding="utf-8"
    )
    manifest = write_user_benchmark(
        items, Path(args.out_benchmark), train_jsonl=out_dpo, stats=stats
    )
    logging.info(
        "标注 %s（有效 %s / REJECT %s）→ 三元组 %s（main %s + 对照 %s），"
        "冻结 %s 题（main %s + 对照 %s）；泄漏 md5=%s minhash=%s",
        stats["n_labels"],
        stats["n_valid"],
        stats["n_reject"],
        len(triples),
        stats["n_train_main"],
        stats["n_train_control"],
        manifest["n_items"],
        stats["n_eval_main"],
        stats["n_eval_control"],
        len(manifest["leakage_check"]["md5_leaks"]),
        len(manifest["leakage_check"]["minhash_leaks"]),
    )


if __name__ == "__main__":
    main()
