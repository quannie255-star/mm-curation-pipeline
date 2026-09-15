"""OPS w1 离线单测：编排壳步骤表 / 预期带告警 / 审计聚合 / 日报渲染 + 新闻适配器幂等。

全部零网络零 subprocess（步骤表只构建不执行，fetch 的 akshare 调用在函数内部可 mock）。
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "src", REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fetch_finance_news import load_seen_urls, make_row, select_new  # noqa: E402
from ops_daily import (  # noqa: E402
    aggregate_drops,
    append_stats,
    build_steps,
    count_lines,
    evaluate_band,
    load_history,
    render_report,
)


def test_build_steps_order_and_commands() -> None:
    steps = build_steps(skip_findata=True, findata_py=None)
    assert [s.name for s in steps] == ["fetch_text", "funnel"]
    fetch, funnel = steps
    assert any("fetch_finance_news.py" in c for c in fetch.cmd)
    assert fetch.check_path is not None and fetch.check_path.name == "news_corpus.jsonl"
    assert any("run_pipeline.py" in c for c in funnel.cmd)
    assert any("text_funnel_finance.yaml" in c for c in funnel.cmd)
    assert funnel.check_path is not None and funnel.check_path.name == "cleaned.jsonl"


def test_build_steps_findata_variants() -> None:
    ok_steps = build_steps(skip_findata=False, findata_py=Path("fake/python.exe"))
    assert ok_steps[0].name == "findata_daily"
    assert any("daily_pipeline.py" in c for c in ok_steps[0].cmd)
    assert ok_steps[0].pre_fail_reason is None

    missing = build_steps(skip_findata=False, findata_py=None)
    assert missing[0].pre_fail_reason is not None  # venv 缺失 → 预失败，不进 subprocess


def test_evaluate_band() -> None:
    assert evaluate_band([], 100) is None  # 无历史跳过
    assert evaluate_band([100, 120], 10) is None  # 历史 <3 天跳过
    assert evaluate_band([100, 110, 120, 90], 100) is None  # 健康带内
    alert = evaluate_band([100, 110, 120], 30)
    assert alert is not None and "疑似断采" in alert
    assert evaluate_band([100, 0, 110, 120], 30) is not None  # 零值历史行不进中位数


def test_aggregate_drops() -> None:
    lines = [
        json.dumps({"dropped_by": "doc_length", "text": "太短" * 50}, ensure_ascii=False),
        json.dumps({"dropped_by": "doc_length", "text": "b" * 100}, ensure_ascii=False),
        json.dumps({"dropped_by": "pii_detect", "text": "含手机号"}, ensure_ascii=False),
        "{bad json",
        "",
    ]
    audit = aggregate_drops(lines)
    assert set(audit) == {"doc_length", "pii_detect"}
    assert audit["doc_length"]["count"] == 2
    assert len(audit["doc_length"]["samples"]) == 2
    assert all(len(s) <= 80 for s in audit["doc_length"]["samples"])


def test_render_report_normal_and_abnormal() -> None:
    normal = render_report(
        "2026-09-16", [], "ok", 500, 120, None, 500, 480, {}, 40.0
    )
    assert "## 今日异常" in normal and "- 无" in normal
    assert "库内 500 条（今日新增 120）" in normal
    assert "保留率 96.0%" in normal

    bad = render_report(
        "2026-09-16",
        ["funnel: exit=1"],
        "exit=1",
        500,
        10,
        "text_new=10 低于近3天中位数 100 的 50%，疑似断采/接口异常",
        0,
        0,
        {"pii_detect": {"count": 2, "samples": ["x"]}},
        85.0,
    )
    assert "[失败] funnel: exit=1" in bad
    assert "疑似断采" in bad
    assert "[磁盘] 已用 85.0%" in bad
    assert "**pii_detect**：2 条" in bad


def test_stats_roundtrip_excludes_today(tmp_path: Path) -> None:
    stats = tmp_path / "stats.jsonl"
    for date, new in [("2026-09-14", 100), ("2026-09-15", 110), ("2026-09-16", 7)]:
        append_stats(stats, {"date": date, "text_new": new})
    assert count_lines(stats) == 3
    assert load_history(stats, "2026-09-16") == [100, 110]  # 当日记录不进历史
    assert load_history(tmp_path / "missing.jsonl", "2026-09-16") == []


def test_news_adapter_dedup_and_stable_id(tmp_path: Path) -> None:
    row_a1 = make_row("600519", "贵州茅台", "标题", "正文", "http://u/1", "t", "r", "c", "s")
    row_a2 = make_row("600519", "贵州茅台", "标题改", "正文改", "http://u/1", "t", "r", "c", "s")
    row_b = make_row("600519", "贵州茅台", "标题", "正文", "http://u/2", "t", "r", "c", "s")
    assert row_a1["id"] == row_a2["id"]  # 同 url 跨运行 id 稳定（幂等键）
    assert row_a1["id"] != row_b["id"]
    assert row_a1["text"].startswith("标题") and row_a1["modality"] == "text_article"

    out = tmp_path / "corpus.jsonl"
    out.write_text(json.dumps(row_a1, ensure_ascii=False) + "\n", encoding="utf-8")
    seen = load_seen_urls(out)
    assert seen == {"http://u/1"}
    new = select_new([row_a2, row_b], seen)
    assert [r["id"] for r in new] == [row_b["id"]]  # 只补增量


def test_load_seen_urls_empty_and_missing(tmp_path: Path) -> None:
    assert load_seen_urls(tmp_path / "nope.jsonl") == set()
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n \n", encoding="utf-8")
    assert load_seen_urls(empty) == set()
