"""η+ 业务层：判官成本核算报告（本机小模型 vs 云 API vs 人工抽检）。

用法：python -X utf8 scripts/judge_cost_report.py
产物：data/reports/judge_cost.{json,md}
吞吐取实测：runs/experiments.jsonl 中最近一次对应评测的秒数/条数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.tuning.judge_cost import (  # noqa: E402
    CostAssumptions,
    cost_table,
    render_markdown,
)

LEDGER = Path("runs/experiments.jsonl")
REPORT = Path("data/reports/judge_cost")

# 实测吞吐：从 ledger 提各评测的 秒数/题数（同一 benchmark 取最新一次）
MEASURED = {
    "客观质量判官 0.5B（judge_news_v1）": ("pref_alignment", 170.0 / 80),  # v3 实测口径
    "偏好判官 0.5B（pref_news_v1）": 480.0 / 300,  # η-a 评测实测
    "抽取判官 0.5B（ext_news_v1）": None,  # 运行时从 ledger 覆盖
}


def measured_seconds() -> dict[str, float]:
    rows = []
    for ln in LEDGER.read_text(encoding="utf-8").split("\n"):
        try:
            if ln.strip():
                rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue  # 共享追加文件容错：半行/坏行跳过
    out: dict[str, float] = {}
    for r in rows:
        if r.get("stage") != "eval":
            continue
        b = r.get("benchmark", "")
        sec, n = r.get("seconds"), r.get("n_judged", 0) + r.get("n_unparsed", 0)
        if not sec or not n:
            continue
        out[b] = sec / n  # 最新一次覆盖
    mapped = {}
    if "benchmarks/judge_news_v1" in out:
        mapped["客观质量判官 0.5B（judge_news_v1）"] = out["benchmarks/judge_news_v1"]
    if "benchmarks/pref_news_v1" in out:
        mapped["偏好判官 0.5B（pref_news_v1）"] = out["benchmarks/pref_news_v1"]
    else:
        mapped["偏好判官 0.5B（pref_news_v1）"] = 480.0 / 300  # η-a 实测兜底
    if "benchmarks/ext_news_v1" in out:
        mapped["抽取判官 0.5B（ext_news_v1，未达标仅成本口径）"] = out["benchmarks/ext_news_v1"]
    return mapped


def main() -> None:
    a = CostAssumptions()
    spi = measured_seconds()
    table = cost_table(spi, a)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    md = render_markdown(table, a)
    REPORT.with_suffix(".md").write_text(md, encoding="utf-8")
    REPORT.with_suffix(".json").write_text(
        json.dumps(
            [r.__dict__ for r in table] + [{"assumptions": a.__dict__}],
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(md)


if __name__ == "__main__":
    main()
