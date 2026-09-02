"""文本语料质量算子（V2 β，text_article 模态——框架通用化的第一批文本算子）。

与图文算子同一注册表、同一 score 语义（越高越好）、同一 meta 分数约定。
成本分级：doc_length/line_repetition/boilerplate/pii_detect 为 RULE，
perplexity 为 MODEL（GPT-2 zh 前向推理）。
"""

from __future__ import annotations

import re
from typing import Optional

from curation_eval import CostClass, register_operator

from .base import BatchOperator, Operator, Sample

_TEXT = frozenset({"text_article"})
_TEXT_FIELDS = frozenset({"text"})

# 广告/版权/导航模板句（真实网文高频 boilerplate 的代表样例）
_BOILERPLATE_PATTERNS = (
    r"扫码关注.{0,6}公众号",
    r"点击(链接|原文).{0,10}(抢购|下载|查看)",
    r"转载请注明出处",
    r"免责声明",
    r"www\.[\w-]+\.c[no]",
    r"下载 APP",
)
# PII 模式（命中即标记；识别目标是模式而非具体号码）
_PII_PATTERNS = (
    r"1[3-9]\d{9}",  # 手机号
    r"[\w.+-]+@[\w-]+\.[\w.]+",  # 邮箱
    r"\d{17}[\dXx]",  # 身份证
)


@register_operator(
    name="doc_length",
    modalities=_TEXT,
    required_fields=_TEXT_FIELDS,
    cost_class=CostClass.RULE,
)
class DocLengthOp(Operator):
    """文档有效字符数（去除全部空白——空白对中文语料无信息量，
    内部插空白类污染只有全去才能接住）。太短的正文无训练价值，
    太长多为拼接页。"""

    def score(self, sample: Sample) -> Optional[float]:
        return float(len("".join(sample.text.split())))


@register_operator(
    name="line_repetition",
    modalities=_TEXT,
    required_fields=_TEXT_FIELDS,
    cost_class=CostClass.RULE,
)
class LineRepetitionOp(Operator):
    """行级重复率：出现 >1 次的行占总行数比例。score = 1 - 重复率。
    攻击模板句复制/段落复读（paragraph_repeat 类污染）。"""

    def score(self, sample: Sample) -> Optional[float]:
        lines = [ln.strip() for ln in sample.text.split("\n") if ln.strip()]
        if len(lines) < 2:
            return 1.0
        counts: dict[str, int] = {}
        for ln in lines:
            counts[ln] = counts.get(ln, 0) + 1
        repeated = sum(c - 1 for c in counts.values() if c > 1)
        return 1.0 - repeated / len(lines)


@register_operator(
    name="boilerplate",
    modalities=_TEXT,
    required_fields=_TEXT_FIELDS,
    cost_class=CostClass.RULE,
)
class BoilerplateOp(Operator):
    """广告/版权/导航模板句命中。score = 1 - 0.2*命中数（下限 0）。"""

    def score(self, sample: Sample) -> Optional[float]:
        hits = sum(len(re.findall(p, sample.text)) for p in _BOILERPLATE_PATTERNS)
        if hits == 0:
            return 1.0
        return max(0.0, 1.0 - 0.2 * hits)


@register_operator(
    name="pii_detect",
    modalities=_TEXT,
    required_fields=_TEXT_FIELDS,
    cost_class=CostClass.RULE,
)
class PiiDetectOp(Operator):
    """PII（手机号/邮箱/证件号）命中。score = 1 - 0.34*命中类数（下限 0）。
    生产建议对命中样本做脱敏而非直接丢弃——此处按漏斗约定二值过滤。"""

    def score(self, sample: Sample) -> Optional[float]:
        hit_classes = sum(1 for p in _PII_PATTERNS if re.search(p, sample.text))
        if hit_classes == 0:
            return 1.0
        return max(0.0, 1.0 - 0.34 * hit_classes)


@register_operator(
    name="perplexity",
    modalities=_TEXT,
    required_fields=_TEXT_FIELDS,
    cost_class=CostClass.MODEL,
    shardable=True,  # 逐样本独立推理（批量仅为 GPU 效率）
)
class PerplexityOp(BatchOperator):
    """GPT-2 zh 困惑度（text_article 模态的质量信号，MODEL 档）。

    score = 1 / (1 + ppl/50)：干净文本 ppl ~10-100 → score 0.33-0.83；
    乱码/字符噪声 ppl 数百以上 → score 趋近 0。默认 min=0.2（约 ppl>200 丢弃），
    校准依据见 β 报告。长文截断到前 256 token（成本控制，抓乱码足够）。
    推理经 get_scorer() 单例（测试 monkeypatch 同一入口）。
    """

    def __init__(self, min: float = 0.2, batch_size: int = 64, **params):
        super().__init__(min=min, batch_size=batch_size, **params)
        self.min = min
        self.batch_size = batch_size

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        kept = []
        for start in range(0, len(samples), self.batch_size):
            chunk = samples[start : start + self.batch_size]
            ppls = get_scorer()(s.text for s in chunk)
            for s, ppl in zip(chunk, ppls):
                score = float(1.0 / (1.0 + ppl / 50.0))
                s.meta["score:perplexity"] = score
                if score >= self.min:
                    kept.append(s)
        return kept


@register_operator(
    name="text_minhash",
    modalities=_TEXT,
    required_fields=_TEXT_FIELDS,
    cost_class=CostClass.RULE,  # 哈希级成本（numpy 向量化签名，无模型）
    shardable=False,  # 全量签名 + LSH 桶视角
)
class TextMinhashDedupOp(BatchOperator):
    """文本近似去重（dedup_fast：向量化 MinHash-LSH，10 万文档 ~30s）。

    与图像漏斗的 minhash_lsh 同语义（先到先保留、threshold 为 Jaccard
    阈值），但实现走向量化 fast path——逐文档 datasketch 签名在长文本上
    是小时级，基准实测结论见 data/reports/text_dedup_benchmark.md。
    """

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        from ..dedup_fast import dedup_texts

        result = dedup_texts(samples, threshold=self.params.get("threshold", 0.7))
        by_id = {s.id: s for s in samples}
        for dup_id, src_id in result.duplicate_of.items():
            dup = by_id[dup_id]
            dup.meta["dedup:text_minhash"] = {
                "duplicate_of": src_id,
                "est_jaccard": result.est_jaccard.get(dup_id),
            }
        return result.kept


_SCORER = None


def get_scorer():
    """GPT-2 zh 困惑度评分器（惰性单例：tokenizer + 模型 + device）。"""
    global _SCORER
    if _SCORER is None:
        import os

        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        from ..gpt2_weights import ensure_local_gpt2

        device = "cuda" if torch.cuda.is_available() else "cpu"
        local = str(ensure_local_gpt2())  # .bin 权重墙：必须走本地 safetensors
        tok = AutoTokenizer.from_pretrained(local)
        model = AutoModelForCausalLM.from_pretrained(local).to(device).eval()
        _SCORER = _Gpt2Scorer(tok, model, device)
    return _SCORER


class _Gpt2Scorer:
    def __init__(self, tok, model, device):
        self.tok, self.model, self.device = tok, model, device

    def __call__(self, texts) -> list[float]:
        import torch

        out = []
        for text in texts:
            inputs = self.tok(text[:2000], return_tensors="pt", truncation=True, max_length=256).to(
                self.device
            )
            with torch.no_grad():
                loss = self.model(**inputs, labels=inputs["input_ids"]).loss
            out.append(float(torch.exp(loss)))
        return out
