"""归一化层（V6 决策点 2）：文本改写的前置阶段。

- `text_normalize`：七条纯函数规则 + 留痕（`NormalizeOutcome`）+ 语料级聚合
- `TextNormalizeTransformer`：接入 curation-eval 的改写通道（前置阶段）

默认 opt-in：既有 config 一字不动，由调用方显式传 `pre_stages=`。
"""

from .text_normalize import (
    RULES,
    NormalizeAggregate,
    NormalizeOutcome,
    aggregate,
    normalize_text,
    whitespace_ratio,
)
from .transformer import NATURAL_LANGUAGE_MODALITIES, TextNormalizeTransformer

__all__ = [
    "RULES",
    "NormalizeAggregate",
    "NormalizeOutcome",
    "aggregate",
    "normalize_text",
    "whitespace_ratio",
    "NATURAL_LANGUAGE_MODALITIES",
    "TextNormalizeTransformer",
]
