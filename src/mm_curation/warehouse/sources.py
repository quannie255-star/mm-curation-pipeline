"""数据源注册表：把磁盘上的 jsonl 声明成「数据集」。

为什么单独一张表：数仓的 raw/stg 层不该把路径硬编码进 ETL 脚本。
新增一个数据源 = 这里加一行，不动 ETL 代码。

`kind` 的三个取值决定它进哪一层：
- `raw`：原始语料 → L0 `raw_samples`
- `cleaned` / `dropped`：漏斗产物 → L1 `stg_samples`（两者并集，用 is_kept 区分）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    """一个可被数仓消费的数据集。

    `run` 是「同一次漏斗运行」的标识：cleaned 与 dropped 必须同 run 才能配对算保留率，
    否则分母分子来自不同批次（这是最容易造出假数字的地方）。
    """

    name: str
    kind: str  # raw | cleaned | dropped
    path: str  # 相对仓库根
    run: str = ""  # 该产物所属的运行名；cleaned/dropped 同名即配对
    modality: str = ""  # 空 = 从数据里推断
    note: str = ""


# ---------------------------------------------------------------------------
# 注册表（只用仓库里确实存在的文件；缺失文件在构建时跳过并记 SKIPPED，不报错）
# ---------------------------------------------------------------------------

SOURCES: tuple[Source, ...] = (
    # --- L0 原始语料 ---
    Source(
        name="news_raw",
        kind="raw",
        path="data/raw/news_corpus.jsonl",
        modality="text_article",
        note="真实爬取中文新闻（对照实验语料）",
    ),
    Source(
        name="fhir_synth_raw",
        kind="raw",
        path="data/raw/fhir_synth/corpus.jsonl",
        modality="fhir_resource",
        note="合成 FHIR R4",
    ),
    Source(
        name="skab_w64_raw",
        kind="raw",
        path="data/raw/real/skab/windows_w64.jsonl",
        modality="industrial_sensor",
        note="SKAB 真实试验台（64 读数窗）",
    ),
    # --- L1 漏斗产物（cleaned / dropped 成对，run 同名）---
    Source(
        name="text_funnel",
        kind="cleaned",
        path="data/processed/text_funnel/cleaned.jsonl",
        run="text_funnel",
        modality="text_article",
    ),
    Source(
        name="text_funnel",
        kind="dropped",
        path="data/processed/text_funnel/dropped.jsonl",
        run="text_funnel",
        modality="text_article",
    ),
    Source(
        name="image_funnel",
        kind="cleaned",
        path="data/processed/cn_flickr_curation_v2/cleaned.jsonl",
        run="image_funnel",
        modality="image",
    ),
    Source(
        name="image_funnel",
        kind="dropped",
        path="data/processed/cn_flickr_curation_v2/dropped.jsonl",
        run="image_funnel",
        modality="image",
    ),
    Source(
        name="fhir_funnel",
        kind="cleaned",
        path="data/processed/fhir_funnel/cleaned.jsonl",
        run="fhir_funnel",
        modality="fhir_resource",
    ),
    Source(
        name="fhir_funnel",
        kind="dropped",
        path="data/processed/fhir_funnel/dropped.jsonl",
        run="fhir_funnel",
        modality="fhir_resource",
    ),
    Source(
        name="finance_funnel",
        kind="cleaned",
        path="data/processed/finance_news_funnel/cleaned.jsonl",
        run="finance_funnel",
        modality="text_article",
        note="金融新闻（真实线上运维飞轮）",
    ),
    Source(
        name="finance_funnel",
        kind="dropped",
        path="data/processed/finance_news_funnel/dropped.jsonl",
        run="finance_funnel",
        modality="text_article",
    ),
)


def resolve(root: str | Path, src: Source) -> Path:
    return Path(root) / src.path


def available(root: str | Path, src: Source) -> bool:
    """文件是否可用（不只看存在，还看非空——0 字节文件会被静默读成空表）。"""
    p = resolve(root, src)
    return p.exists() and p.stat().st_size > 0


def runs() -> dict[str, dict[str, Source]]:
    """按 run 名汇聚，只保留 cleaned/dropped 成对齐全的运行。

    **为什么必须成对**：保留率 = kept/(kept+dropped)。缺一边就是拿不同批次
    的分子分母相除，得出的是没有意义的数字——宁可不算（记 NOT_EVALUATED）。
    """
    out: dict[str, dict[str, Source]] = {}
    for s in SOURCES:
        if s.kind in ("cleaned", "dropped") and s.run:
            out.setdefault(s.run, {})[s.kind] = s
    return {k: v for k, v in out.items() if "cleaned" in v and "dropped" in v}
