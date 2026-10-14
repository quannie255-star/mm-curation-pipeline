"""在已下载的经典数据集上跑清洗漏斗，并落盘每份报告的 cleaned/dropped/统计。

配合 coverage 使用以统计清洗过程中被执行的代码：
    coverage run -m scripts.run_classic_cleaning
    coverage json -o coverage_classic.json && coverage html -d coverage_classic_html

本脚本只编排，不新增清洗逻辑：复用 PipelineConfig / run_funnel / quality 报告。

用法：
    python -X utf8 scripts/run_classic_cleaning.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.operators import register_all  # 触发全部算子注册
from mm_curation.operators.base import Sample
from mm_curation.pipeline import PipelineConfig, run_funnel
from mm_curation.quality import build_report_data, render_markdown

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_classic")

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "archive" / "classic_text.yaml"

# (名称, 输入 jsonl, 输出子目录)
DATASETS = [
    ("ag_news", "data/raw/classic/ag_news_train.jsonl", "ag_news"),
    ("sst2", "data/raw/classic/sst2_train.jsonl", "sst2"),
]


def run_one(name: str, input_rel: str, out_sub: str) -> dict:
    config = PipelineConfig.from_yaml(CONFIG)
    config.raw_jsonl = ROOT / input_rel
    config.output_dir = ROOT / "data" / "processed" / "classic_text" / out_sub

    input_path = config.raw_jsonl
    samples = [
        Sample.from_dict(json.loads(line))
        for line in input_path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    ]
    logger.info("漏斗 %s: %s 条样本, %s 级算子", config.name, len(samples), len(config.operators))

    result = run_funnel(samples, config)

    out_dir = config.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cleaned.jsonl").write_text(
        "\n".join(json.dumps(s.to_dict(), ensure_ascii=False) for s in result.kept) + "\n",
        encoding="utf-8",
    )
    with open(out_dir / "dropped.jsonl", "w", encoding="utf-8") as f:
        for stage, s in result.dropped:
            row = s.to_dict()
            row["dropped_by"] = stage
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    report_data = build_report_data(result, config.name, len(samples))
    (out_dir / "funnel_stats.json").write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_markdown(report_data), encoding="utf-8")

    summary = {
        "dataset": name,
        "n_in": len(samples),
        "n_kept": len(result.kept),
        "n_dropped": len(result.dropped),
        "stages": [
            {
                "op": st.op,
                "n_in": st.n_in,
                "n_out": st.n_out,
                "dropped": st.dropped,
                "pass_rate": round(st.pass_rate, 4),
            }
            for st in result.stats
        ],
    }
    logger.info(
        "完成 %s: %d -> %d (丢弃 %d)", name, len(samples), len(result.kept), len(result.dropped)
    )
    return summary


def main() -> None:
    register_all()
    summaries = [run_one(*d) for d in DATASETS]
    out_path = ROOT / "data" / "processed" / "classic_text" / "cleaning_summary.json"
    out_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 清洗汇总 ===")
    for s in summaries:
        print(f"{s['dataset']:<14} {s['n_in']:>7} -> {s['n_kept']:>7}  (丢弃 {s['n_dropped']})")
    for s in summaries:
        print(f"\n[{s['dataset']}] 逐算子")
        print(f"{'stage':<16}{'n_in':>7}{'n_out':>8}{'drop':>7}{'pass%':>8}")
        for st in s["stages"]:
            print(
                f"{st['op']:<16}{st['n_in']:>7}{st['n_out']:>8}{st['dropped']:>7}"
                f"{100*st['pass_rate']:>7.1f}%"
            )


if __name__ == "__main__":
    sys.exit(main())
