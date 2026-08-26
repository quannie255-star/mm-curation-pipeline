"""运行一条清洗漏斗：脏数据 jsonl -> 干净 jsonl + 漏斗统计 + 丢弃明细。

用法：
    python scripts/run_pipeline.py                          # 默认配置与输入
    python scripts/run_pipeline.py --config configs/pipeline.example.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline import PipelineConfig, run_funnel  # noqa: E402
from mm_curation.quality import build_report_data, render_markdown  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", default=None, help="输入 jsonl（默认取配置里的 dataset.raw_jsonl）"
    )
    parser.add_argument("--config", default="configs/pipeline.example.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = PipelineConfig.from_yaml(args.config)
    input_path = Path(args.input) if args.input else config.raw_jsonl
    samples = [Sample.from_dict(json.loads(line)) for line in open(input_path, encoding="utf-8")]
    logging.info("漏斗 %s: %s 条样本, %s 级算子", config.name, len(samples), len(config.operators))

    result = run_funnel(samples, config)

    out_dir = config.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "cleaned.jsonl", "w", encoding="utf-8") as f:
        for s in result.kept:
            f.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
    with open(out_dir / "dropped.jsonl", "w", encoding="utf-8") as f:  # 评测闭环的原料
        for stage, s in result.dropped:
            row = s.to_dict()
            row["dropped_by"] = stage
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out_dir / "funnel_stats.json").write_text(
        json.dumps(
            build_report_data(result, config.name, len(samples)),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(
        render_markdown(build_report_data(result, config.name, len(samples))),
        encoding="utf-8",
    )

    print(f"\n漏斗: {config.name}  ({len(samples)} -> {len(result.kept)})")
    print(f"{'stage':<18}{'n_in':>7}{'n_out':>8}{'drop':>7}{'pass%':>8}   score_p50")
    for st in result.stats:
        p50 = "—" if st.score_p50 is None else f"{st.score_p50:.2f}"
        print(
            f"{st.op:<18}{st.n_in:>7}{st.n_out:>8}{st.dropped:>7}"
            f"{100 * st.pass_rate:>7.1f}%   {p50}"
        )
    logging.info("输出: %s (cleaned/dropped/funnel_stats)", out_dir)


if __name__ == "__main__":
    main()
