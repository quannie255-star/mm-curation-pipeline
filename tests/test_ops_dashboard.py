"""OPS 驾驶舱纯函数单测：产物加载的容错（坏行跳过/空产物/limit）——零 streamlit 交互。"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "src", REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ops_dashboard import load_corpus, load_stats, report_dates  # noqa: E402


def test_load_stats_tolerates_bad_lines(tmp_path: Path) -> None:
    p = tmp_path / "stats.jsonl"
    p.write_text(
        json.dumps({"date": "2026-09-16", "text_new": 5}, ensure_ascii=False)
        + "\n{bad\n\n",
        encoding="utf-8",
    )
    stats = load_stats(p)
    assert len(stats) == 1 and stats[0]["text_new"] == 5
    assert load_stats(tmp_path / "missing.jsonl") == []


def test_report_dates_descending(tmp_path: Path) -> None:
    for name in ("2026-09-15", "2026-09-17", "2026-09-16"):
        (tmp_path / f"{name}.md").write_text("# d", encoding="utf-8")
    (tmp_path / "not_md.txt").write_text("x", encoding="utf-8")
    assert report_dates(tmp_path) == ["2026-09-17", "2026-09-16", "2026-09-15"]
    assert report_dates(tmp_path / "nope") == []


def test_load_corpus_limit_keeps_tail(tmp_path: Path) -> None:
    p = tmp_path / "corpus.jsonl"
    rows = [{"id": f"n{i}"} for i in range(5)]
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    view = load_corpus(p, limit=3)
    assert [r["id"] for r in view] == ["n2", "n3", "n4"]  # 保留最新尾部
    assert load_corpus(tmp_path / "missing.jsonl") == []
