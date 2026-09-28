"""源注册表（平台层）：哪个文件是哪个数据集、**事件时间从哪来**。

## 与 `warehouse/sources.py` 的分工

`warehouse/sources.py` 服务语义层（口径怎么算），只关心"这个文件里有什么列"；
本表服务作业层，多关心一件事：**这条记录属于哪一天**——没有它就没有分区，
也就没有增量与分区裁剪。

刻意不改 `warehouse/sources.py`：**既有 4 个 config 与既有测试基线都不许动**。
两张表并存，各自服务一层，比强行合并安全。

## 事件时间的取法（按优先级，取不到就是取不到）

1. `meta.window_start`——工业传感器窗口的真实采集时刻；
2. `meta.published_at` / `meta.date` 等常见字段；
3. 从 `meta.url` 的路径里解析 `/2026/09-06/` 形态的日期（中文新闻语料实测可用）。

三条都取不到 → `UNKNOWN`。**不许拿"导入当天"顶替**：
那会把"我不知道这条数据是哪天的"伪装成"这条数据是今天的"，
而 `obs.py` 的新鲜度指标正是靠这个字段判死活的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UNKNOWN_DATE = "unknown"

_DATE_KEYS = ("window_start", "published_at", "pub_time", "publish_time", "date", "created_at")
_URL_DATE = re.compile(r"/(\d{4})/(\d{1,2})-(\d{1,2})/")


@dataclass(frozen=True)
class LakeSource:
    """一个可被平台层消费的数据集。"""

    dataset: str  # 逻辑数据集名（= 分区键 dataset 的取值 + DWD 的维）
    name: str  # 源名（与 watermark 的 source 对应）
    path: str  # 相对仓库根
    modality: str
    kind: str = "raw"  # raw | cleaned | dropped
    run: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# 注册表（只用仓库里确实存在的文件；缺失的源在构建时记 SKIPPED，不报错）
# ---------------------------------------------------------------------------

LAKE_SOURCES: tuple[LakeSource, ...] = (
    # --- 真实工业传感器（唯一有真实事件时间的一条线）---
    LakeSource(
        dataset="metropt3",
        name="metropt3_windows",
        path="data/raw/real/metropt3/windows.jsonl",
        modality="industrial_sensor",
        note="MetroPT-3 空压机 151 万行 → 41478 窗 / 212 个事件日（0.1Hz）",
    ),
    LakeSource(
        dataset="skab_w64",
        name="skab_w64_windows",
        path="data/raw/real/skab/windows_w64.jsonl",
        modality="industrial_sensor",
        note="SKAB 试验台 34 设备 / 8 通道 / 4512 窗 / 3 个事件日",
    ),
    LakeSource(
        dataset="cmapss",
        name="cmapss_windows",
        path="data/raw/real/cmapss/windows.jsonl",
        modality="industrial_sensor",
        note="C-MAPSS 仿真退化 200 设备 / 29757 窗 / 17 个事件日",
    ),
    # --- 中文新闻语料（事件时间从 URL 解析）---
    LakeSource(
        dataset="news_corpus",
        name="news_corpus_raw",
        path="data/raw/news_corpus.jsonl",
        modality="text_article",
        note="真实爬取中文新闻；事件时间解析自 meta.url",
    ),
    # --- 漏斗产物（无事件时间字段 → 除非有 URL，否则落 UNKNOWN 分区）---
    LakeSource(
        dataset="text_funnel",
        name="text_funnel_cleaned",
        path="data/processed/text_funnel/cleaned.jsonl",
        modality="text_article",
        kind="cleaned",
        run="text_funnel",
    ),
    LakeSource(
        dataset="text_funnel",
        name="text_funnel_dropped",
        path="data/processed/text_funnel/dropped.jsonl",
        modality="text_article",
        kind="dropped",
        run="text_funnel",
    ),
    LakeSource(
        dataset="image_funnel",
        name="image_funnel_cleaned",
        path="data/processed/cn_flickr_curation_v2/cleaned.jsonl",
        modality="image",
        kind="cleaned",
        run="image_funnel",
    ),
    LakeSource(
        dataset="image_funnel",
        name="image_funnel_dropped",
        path="data/processed/cn_flickr_curation_v2/dropped.jsonl",
        modality="image",
        kind="dropped",
        run="image_funnel",
    ),
    LakeSource(
        dataset="fhir_funnel",
        name="fhir_funnel_cleaned",
        path="data/processed/fhir_funnel/cleaned.jsonl",
        modality="fhir_resource",
        kind="cleaned",
        run="fhir_funnel",
    ),
    LakeSource(
        dataset="fhir_funnel",
        name="fhir_funnel_dropped",
        path="data/processed/fhir_funnel/dropped.jsonl",
        modality="fhir_resource",
        kind="dropped",
        run="fhir_funnel",
    ),
    LakeSource(
        dataset="finance_funnel",
        name="finance_funnel_cleaned",
        path="data/processed/finance_news_funnel/cleaned.jsonl",
        modality="text_article",
        kind="cleaned",
        run="finance_funnel",
    ),
    LakeSource(
        dataset="finance_funnel",
        name="finance_funnel_dropped",
        path="data/processed/finance_news_funnel/dropped.jsonl",
        modality="text_article",
        kind="dropped",
        run="finance_funnel",
    ),
)


def resolve(root: str | Path, src: LakeSource) -> Path:
    return Path(root) / src.path


def available(root: str | Path, src: LakeSource) -> bool:
    """存在且非空（0 字节文件会被静默读成空表）。"""
    p = resolve(root, src)
    return p.exists() and p.stat().st_size > 0


def event_date_of(rec: dict[str, Any]) -> str:
    """一条记录的事件日期（`YYYY-MM-DD`），取不到返回 `UNKNOWN_DATE`。"""
    meta = rec.get("meta") or {}
    for key in _DATE_KEYS:
        raw = meta.get(key)
        if isinstance(raw, str) and len(raw) >= 10 and raw[4] == "-":
            return raw[:10]
    url = meta.get("url")
    if isinstance(url, str):
        m = _URL_DATE.search(url)
        if m:
            y, mo, d = m.groups()
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    # 兜底：样本 id 里嵌的时间戳（`..._20200301T154406`）
    sid = str(rec.get("id") or "")
    m = re.search(r"_(\d{4})(\d{2})(\d{2})T\d{6}$", sid)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}"
    return UNKNOWN_DATE


def datasets() -> list[str]:
    """注册表里出现过的逻辑数据集（去重、保序）。"""
    out: list[str] = []
    for s in LAKE_SOURCES:
        if s.dataset not in out:
            out.append(s.dataset)
    return out


def select(dataset: str = "", kind: str = "") -> list[LakeSource]:
    out = list(LAKE_SOURCES)
    if dataset:
        out = [s for s in out if s.dataset == dataset]
    if kind:
        out = [s for s in out if s.kind == kind]
    return out
