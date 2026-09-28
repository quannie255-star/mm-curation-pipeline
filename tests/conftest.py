"""pytest 配置：src 布局路径注入 + 平台层测试的共享夹具。

关于夹具的两条纪律：

1. **不读仓库里的大数据**。`data/lake/**` 与 `data/raw/**` 是生成产物、不入库，
   所以夹具自造小样本（3~8 条记录）。测试在任何机器上都能跑，
   也不会因为"本机恰好有那份数据"而假绿。
2. **走生产那条路径**。只造 `data/raw/real/metropt3/windows.jsonl` 与
   `data/raw/news_corpus.jsonl`——它们是 `registry.LAKE_SOURCES` 里**已登记的相对路径**，
   所以不需要 monkeypatch 注册表；其余 10 个源因文件不存在走 `SKIPPED` 分支
   （那条分支也因此被覆盖到）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# 源样本
# ---------------------------------------------------------------------------

# 传感器源：meta 里带 device_id / channel / unit，以及 window_start
# （`registry._DATE_KEYS` 里事件时间的第一优先来源）
_METROPT3 = [
    {
        "id": "mtp3_0001",
        "modality": "industrial_sensor",
        "text": "",
        "meta": {
            "device_id": "compressor_01",
            "device_type": "air_compressor",
            "channel": "TP2",
            "unit": "degC",
            "sampling_hz": 0.1,
            "operating_mode": "load",
            "window_start": "2020-04-01T00:00:00",
            "window_end": "2020-04-01T00:01:00",
            "score:robust_z": 0.42,
            "score:range_check": 0.9,
        },
    },
    {
        "id": "mtp3_0002",
        "modality": "industrial_sensor",
        "text": "",
        "meta": {
            "device_id": "compressor_01",
            "device_type": "air_compressor",
            "channel": "Reservoirs",
            "unit": "bar",
            "sampling_hz": 0.1,
            "operating_mode": "load",
            "window_start": "2020-04-01T00:01:00",
            "window_end": "2020-04-01T00:02:00",
            "score:robust_z": 0.11,
        },
    },
    {
        "id": "mtp3_0003",
        "modality": "industrial_sensor",
        "text": "",
        "meta": {
            "device_id": "compressor_02",
            "device_type": "air_compressor",
            "channel": "TP2",
            "unit": "degC",
            "sampling_hz": 0.1,
            "operating_mode": "unload",
            "window_start": "2020-04-02T00:00:00",
            "window_end": "2020-04-02T00:01:00",
            "score:range_check": 0.75,
        },
    },
    {
        # 畸形窗口：有 device_id 但没有 channel → 按设计不产生维行，
        # DWD 里算进 `n_no_channel` 而不是 `n_unmatched_sensor`
        # （"设计如此"和"出错了"必须分开，否则这个数字没有诊断价值）
        "id": "mtp3_0004",
        "modality": "industrial_sensor",
        "text": "",
        "meta": {"device_id": "compressor_09", "window_start": "2020-04-02T01:00:00"},
    },
]

# 文本源：事件时间从 meta.url 的 `/2026/09-06/` 形态解析（真实语料实测可用）
_NEWS = [
    {
        "id": "news_0001",
        "modality": "text_article",
        "text": "央行宣布下调存款准备金率0.25个百分点，释放长期资金约5000亿元。",
        "meta": {"url": "https://example.com/finance/2026/09-06/a1.html"},
    },
    {
        "id": "news_0002",
        "modality": "text_article",
        "text": "A股三大指数集体高开，券商板块领涨。",
        "meta": {"url": "https://example.com/finance/2026/09-06/a2.html"},
    },
    {
        # 没有 url、没有日期字段、id 里也没有时间戳 → 事件时间未知
        "id": "news_0003",
        "modality": "text_article",
        "text": "某公司发布业绩预告。",
        "meta": {},
    },
]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def lake_root(tmp_path: Path, monkeypatch) -> Path:
    """一个只含 2 个小源的最小仓库根。

    `monkeypatch.chdir` 是必要的：`lake.scan_sql` 在 root 为相对路径时会生成
    **相对 glob**，不锚定 cwd 就会被解析到别处（这正是生产代码里
    `make_context` 要把路径 resolve 成绝对路径的原因，两边是同一件事）。
    """
    monkeypatch.chdir(tmp_path)
    _write_jsonl(tmp_path / "data" / "raw" / "real" / "metropt3" / "windows.jsonl", _METROPT3)
    _write_jsonl(tmp_path / "data" / "raw" / "news_corpus.jsonl", _NEWS)
    return tmp_path


@pytest.fixture()
def metropt3_rows() -> list[dict]:
    return [dict(r) for r in _METROPT3]


@pytest.fixture()
def news_rows() -> list[dict]:
    return [dict(r) for r in _NEWS]
