"""向干净种子集注入可控脏数据，产出带 ground truth 的评测数据集。

用法：
    python scripts/contaminate.py                                   # 默认配置
    python scripts/contaminate.py --config configs/contamination.default.yaml
    python scripts/contaminate.py --rate 0.4 --seed 7
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.contamination import ContaminationPlan, available_contaminators  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/raw/samples.jsonl")
    parser.add_argument("--out", default="data/interim/contaminated")
    parser.add_argument("--config", default=None, help="污染计划 YAML（默认内置比例）")
    parser.add_argument("--rate", type=float, default=None, help="覆盖注入比例")
    parser.add_argument("--seed", type=int, default=None, help="覆盖随机种子")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    defaults = dict(
        inject_rate=0.30,
        seed=42,
        kinds={
            "exact_duplicate": 0.10,
            "near_duplicate_image": 0.08,
            "near_duplicate_text": 0.08,
            "semantic_duplicate": 0.08,
            "low_resolution": 0.14,
            "blur": 0.14,
            "mismatched_pair": 0.12,
            "low_quality_text": 0.12,
            "watermark": 0.09,
            "nsfw_placeholder": 0.05,
        },
    )
    if args.config:
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        defaults.update(cfg)
    if args.rate is not None:
        defaults["inject_rate"] = args.rate
    if args.seed is not None:
        defaults["seed"] = args.seed

    samples = [Sample.from_dict(json.loads(line)) for line in open(args.input, encoding="utf-8")]
    logging.info(
        "读入 %s 条干净样本；可用污染类型: %s", len(samples), ", ".join(available_contaminators())
    )

    plan = ContaminationPlan(**defaults)
    mixed, manifest = plan.run(samples, Path(args.out) / "images")

    out_jsonl = Path(args.out) / "samples.jsonl"
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for s in mixed:
            f.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
    (Path(args.out) / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logging.info(
        "完成: %s（干净 %s + 注入 %s，分布 %s）",
        out_jsonl,
        manifest["n_clean"],
        manifest["n_injected"],
        manifest["counts"],
    )


if __name__ == "__main__":
    main()
