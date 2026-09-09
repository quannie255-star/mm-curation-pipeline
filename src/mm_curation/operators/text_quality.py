"""文本质量算子（L1 规则层：无重依赖，纯 Python，毫秒级）。

文本/图文双模态通用：对 image_caption 样本评判 caption，对 text_article
样本评判正文——同一算子服务两种实例。
"""

from __future__ import annotations

from typing import Optional

from curation_eval import CostClass, register_operator

from .base import Operator, Sample


@register_operator(
    name="text_length",
    modalities=frozenset({"text_article", "image_caption"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class TextLengthOp(Operator):
    """文本字符数。太短信息量不足，太长多为噪音（拼接的 alt 文本等），
    用 min/max 双阈值，score 直接取长度。"""

    def score(self, sample: Sample) -> Optional[float]:
        return float(len(sample.text.strip()))


@register_operator(
    name="chinese_ratio",
    modalities=frozenset({"text_article", "image_caption"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class ChineseRatioOp(Operator):
    """中文字符占比（去空白后计）。面向中文语料，纯英文文本（爬取混入）与
    乱码（编码错误、OCR 噪声）都会把该比值压低。空白不是语言证据——
    爬虫抽取缺陷的空白膨胀曾把分母撑爆造成 773/778 误杀（真实数据试跑，
    笔记 #65），故先去全部空白再算比值。"""

    @staticmethod
    def _cjk_ratio(text: str) -> float:
        body = "".join(text.split())
        if not body:
            return 0.0
        cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in body)
        return cjk / len(body)

    def score(self, sample: Sample) -> Optional[float]:
        return self._cjk_ratio(sample.text)


@register_operator(
    name="char_repetition",
    modalities=frozenset({"text_article", "image_caption"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class CharRepetitionOp(Operator):
    """字符重复率：最长单字符游程占文本比例。攻击"哈哈哈哈…"类
    水文本与低质爬取文本。score = 1 - 重复率，越高越好。"""

    def score(self, sample: Sample) -> Optional[float]:
        text = sample.text.strip()
        if len(text) < 4:
            return 1.0
        longest = 1
        run = 1
        for i in range(1, len(text)):
            run = run + 1 if text[i] == text[i - 1] else 1
            longest = max(longest, run)
        return 1.0 - longest / len(text)
