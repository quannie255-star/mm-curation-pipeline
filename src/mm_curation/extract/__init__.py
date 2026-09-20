"""抽取层（V6 W2）：原始 HTML → 正文，且**每一步都可回放**。

这一层补的是笔记 #65 暴露的真缺口：`data/web_sources.py` 抓完 HTML 立刻抽成
text，原始 HTML 从不落盘。于是"换抽取器"变成不可做的事——没有输入可重放，
对照实验无从谈起，那次误杀 778 篇的现场只能靠复述。

三个部件，各管一件事：

| 部件 | 职责 | 关键性质 |
|---|---|---|
| `RawDocStore` | 存原文（内容寻址 gzip + 边车） | 幂等、可校验、损坏即炸 |
| `Extractor` + 注册表 | 声明"怎么抽" | `name` / `tier` / `requires` 三个 ClassVar |
| `ExtractionChain` | 决定"谁来抽、为什么没抽成" | 逐级记 `ExtractAttempt`，失败带原因 |

**与算子体系的关系**：并列，不继承。抽取发生在 `Sample` 存在**之前**，
硬塞进 `Operator` 协议会逼着抽取出一个假的 Sample 出来——形状不对。

典型用法：

    from mm_curation.extract import RawDocStore, ExtractionChain

    store, chain = RawDocStore(), ExtractionChain.default()
    raw = store.put(html, url=url, meta={"source": "chinanews"})   # 先存档
    art, attempts = chain.extract(raw.text)                        # 再抽，可重放
    if art:
        sample = Sample(id=raw.sha256[:16], text=art.text(),
                        meta={"rawdoc": raw.sha256, "extractor": art.extractor})
"""

from __future__ import annotations

from .base import (
    MIN_CHARS_DEFAULT,
    ExtractAttempt,
    ExtractedArticle,
    ExtractionChain,
    Extractor,
    available_extractors,
    get_extractor,
    register_extractor,
    unregister_extractor,
)
from .heuristic import BOILERPLATE_BLOCKS, DROP_BLOCKS, HeuristicExtractor, strip_noise
from .news_cn import NEWS_CN, NewsCnExtractor
from .rawdoc import (
    SCHEMA_VERSION,
    RawDoc,
    RawDocCorrupted,
    RawDocStore,
    sha256_of_text,
    utc_now_iso,
)
from .trafilatura_ext import TrafilaturaExtractor

# 触发注册：导入本包即完成全部抽取器登记（顺序由 available_extractors 按 tier 排）
_REGISTERED = (
    NewsCnExtractor,  # tier 0
    TrafilaturaExtractor,  # tier 1
    HeuristicExtractor,  # tier 2
)

__all__ = [
    "BOILERPLATE_BLOCKS",
    "DROP_BLOCKS",
    "MIN_CHARS_DEFAULT",
    "NEWS_CN",
    "SCHEMA_VERSION",
    "ExtractAttempt",
    "ExtractedArticle",
    "ExtractionChain",
    "Extractor",
    "HeuristicExtractor",
    "NewsCnExtractor",
    "RawDoc",
    "RawDocCorrupted",
    "RawDocStore",
    "TrafilaturaExtractor",
    "available_extractors",
    "default_chain",
    "extract_html",
    "get_extractor",
    "register_extractor",
    "sha256_of_text",
    "strip_noise",
    "unregister_extractor",
    "utc_now_iso",
]

_chain_cache: dict[tuple[tuple[str, ...], int], ExtractionChain] = {}


def default_chain(
    *,
    names: tuple[str, ...] | None = None,
    min_chars: int = MIN_CHARS_DEFAULT,
) -> ExtractionChain:
    """默认抽取链（按参数缓存——链条是无状态的，重复构造纯属浪费）。"""
    key = (tuple(names) if names is not None else available_extractors(), min_chars)
    if key not in _chain_cache:
        _chain_cache[key] = ExtractionChain.default(
            names=key[0], min_chars=key[1]
        )
    return _chain_cache[key]


def extract_html(
    html: str,
    *,
    chain: ExtractionChain | None = None,
) -> tuple[ExtractedArticle | None, list[ExtractAttempt]]:
    """一行搞定抽取：`(正文 | None, 逐级尝试记录)`。

    **务必把 attempts 一起带走**——只留正文的话，阴性结果又会退化成
    一句无法归因的"新抽取器没变好"。
    """
    return (chain or default_chain()).extract(html)
