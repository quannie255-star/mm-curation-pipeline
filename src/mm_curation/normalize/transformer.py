"""归一化改写器：把 text_normalize 接入包侧改写通道（前置阶段）。

## 模态范围的刻意收窄（这是本模块最重要的一条设计）

只声明**自然语言模态** `text_article` / `image_caption`。

**刻意排除** `fhir_resource` 与 `industrial_sensor`——它们的 `text` 承载
canonical JSON 载荷（`FHIRSample` / `SensorSample` 约定）。对 JSON 做空白折叠
会**改掉字符串字面量内部的数据**：`"given": ["John  Smith"]` 里的双空格是数据，
不是噪声。这是「同形态多含义」陷阱（笔记里 C-MAPSS 恒值通道误杀 92.4% 同源）：
**看起来一样的空白，在散文里是噪声、在结构载荷里可能是值。**

模态不匹配的样本由 `run_pre_stages` 原样透传并计 `skipped`，不误杀。
"""

from __future__ import annotations

from typing import Any, Iterable

from curation_eval import Transformer, TransformResult, register_transformer

from .text_normalize import NormalizeOutcome, normalize_text

# 自然语言模态白名单（见模块 docstring：结构化载荷模态刻意不在内）
NATURAL_LANGUAGE_MODALITIES = frozenset({"text_article", "image_caption"})


@register_transformer(
    name="text_normalize",
    modalities=NATURAL_LANGUAGE_MODALITIES,
    required_fields=("text",),
)
class TextNormalizeTransformer(Transformer):
    """七规则文本归一化（逐样本；改写后为空串则判为不可用）。

    - `rules=None` → 全部规则；传子集做逐规则消融
    - `min_chars`：归一化后长度低于此值 → `unusable`（默认 1：只拦「改完什么都不剩」）
    - 规则未命中任何一条 → `unchanged`（不算改写，不写日志、不计入 changed）
    """

    def __init__(self, rules: Iterable[str] | None = None, min_chars: int = 1):
        self.rules = None if rules is None else tuple(rules)
        self.min_chars = min_chars

    def transform(self, sample) -> TransformResult:
        outcome: NormalizeOutcome = normalize_text(sample.text, rules=self.rules)
        log: dict[str, Any] = outcome.to_log()
        if outcome.new_len < self.min_chars:
            return TransformResult.unusable({**log, "reason": "empty_after_normalize"})
        if not outcome.changed:
            return TransformResult.unchanged(sample)
        sample.text = outcome.text
        return TransformResult.replaced(sample, log)
