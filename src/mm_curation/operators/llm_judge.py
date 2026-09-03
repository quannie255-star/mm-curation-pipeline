"""L3 LLM-judge 算子（V2 δ，ARCHITECTURE_V2 决策 7）。

架构位置：普通注册算子 + 独立推理边界——算子只说 OpenAI 兼容的
`/v1/chat/completions` 协议，服务端可插拔（本机 scripts/serve_judge.py 的
极简兼容层 / Linux 上的 vLLM / 云端任意兼容 API），换 provider 不改算子。

可信度协议：judge 不是真理，与 ground truth 的一致性（Cohen's kappa，
见 scripts/eval_judge.py）才是它的可信度证明。

成本意识：L3 只抽样裁决歧义区——sample_rate 确定性抽样（同 config 重跑
抽同一批）；服务不可用/解析失败默认保留不评判（on_error: skip），
L3 是增强不是阻塞。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from curation_eval import BatchOperator, CostClass, Sample, register_operator

from .text_corpus import _TEXT, _TEXT_FIELDS

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = (
    "你是大模型训练语料的质量审核员。对下面的文本按 0-10 打分"
    "（10=干净且信息量大，0=乱码/复读/广告/无意义），只输出 JSON：\n"
    '{"score": <0-10 整数>, "reason": "<=30字"}\n\n文本：\n'
)

_JSON_RE = re.compile(r"\{[^{}]*\}", re.S)


def _parse_score(content: str) -> float | None:
    """从回复中稳健地抽出第一个 JSON 对象的 score（失败返回 None）。"""
    m = _JSON_RE.search(content)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    score = obj.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None
    return float(score)


def _sampled(sample_id: str, rate: float, seed: int) -> bool:
    """确定性抽样：同 (seed, id) 永远同判定——可复现、可审计。"""
    if rate >= 1.0:
        return True
    digest = hashlib.sha1(f"{seed}:{sample_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF < rate


def _chat(base_url: str, model: str, api_key: str, text: str, timeout: float) -> str:
    """单次 OpenAI 兼容 chat 调用（标准库实现，不引入 SDK 依赖）。"""
    import urllib.error
    import urllib.request

    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": _JUDGE_PROMPT + text}],
            "temperature": 0.0,
            "max_tokens": 64,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    return body["choices"][0]["message"]["content"]


@register_operator(
    name="llm_judge",
    modalities=_TEXT,
    required_fields=_TEXT_FIELDS,
    cost_class=CostClass.LLM,  # 远程推理服务；成本-质量前沿的最高档
    shardable=True,  # 逐样本独立调用（批量仅为并发效率）
)
class LlmJudgeOp(BatchOperator):
    """LLM-as-judge 质量裁决（text_article 模态，LLM 档）。

    params:
      base_url   OpenAI 兼容端点（默认本机 serve_judge.py）
      model      模型名（服务端路由用）
      api_key    缺省取 env OPENAI_API_KEY（本地服务校验形同虚设，兼容云端）
      sample_rate 抽样率 0-1（默认 0.1；1.0 = 全评）
      seed       抽样种子（默认 7）
      min        通过阈值（score/10 归一化后；默认 0.5）
      max_workers 批内并发数（默认 4）
      timeout_s  单次调用超时（默认 30）
      on_error   skip（默认，保留不评判）| fail（服务异常直接抛）
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8100/v1",
        model: str = "judge",
        sample_rate: float = 0.1,
        seed: int = 7,
        min: float = 0.5,
        max_workers: int = 4,
        timeout_s: float = 30.0,
        on_error: str = "skip",
        **params,
    ):
        super().__init__(min=min, **params)
        import os

        self.base_url = base_url
        self.model = model
        self.api_key = os.environ.get("OPENAI_API_KEY", "local-judge")
        self.sample_rate = sample_rate
        self.seed = seed
        self.min = min
        self.max_workers = max_workers
        self.timeout_s = timeout_s
        if on_error not in ("skip", "fail"):
            raise ValueError(f"on_error 只支持 skip|fail，得到 {on_error!r}")
        self.on_error = on_error
        self.n_calls = 0
        self.n_errors = 0
        self.n_unparsed = 0
        self.total_latency = 0.0

    def score(self, sample: Sample) -> float | None:
        raise TypeError("LlmJudgeOp 是批量算子，请通过 run_batch() 调用")

    def _judge_one(self, sample: Sample) -> tuple[Sample, float | None]:
        """单样本裁决：返回 (样本, 归一化分数或 None)。异常按 on_error 语义。"""
        t0 = time.perf_counter()
        try:
            content = _chat(
                self.base_url, self.model, self.api_key, sample.text[:2000], self.timeout_s
            )
        except Exception as e:  # 网络/超时/协议错误一视同仁
            if self.on_error == "fail":
                raise
            self.n_errors += 1
            logger.warning("judge 调用失败（保留不评判）: %s", e)
            return sample, None
        finally:
            self.n_calls += 1
            self.total_latency += time.perf_counter() - t0
        raw = _parse_score(content)
        if raw is None:
            self.n_unparsed += 1
            return sample, None  # 解析失败同样保留不评判（分数缺失≠误杀）
        return sample, raw / 10.0

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        targets = [s for s in samples if _sampled(s.id, self.sample_rate, self.seed)]
        target_ids = {s.id for s in targets}
        passthrough = [s for s in samples if s.id not in target_ids]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            results = list(pool.map(self._judge_one, targets))
        kept = list(passthrough)
        for s, normalized in results:
            s.meta["score:llm_judge"] = normalized
            if normalized is None or normalized >= self.min:
                kept.append(s)
        if self.n_calls:
            logger.info(
                "llm_judge: %s 次调用（错误 %s / 解析失败 %s），平均 %.2fs",
                self.n_calls,
                self.n_errors,
                self.n_unparsed,
                self.total_latency / self.n_calls,
            )
        return kept

    def stats_snapshot(self) -> dict[str, Any]:
        """调用统计（成本-质量前沿分析的原始数据）。"""
        return {
            "n_calls": self.n_calls,
            "n_errors": self.n_errors,
            "n_unparsed": self.n_unparsed,
            "avg_latency_s": (self.total_latency / self.n_calls if self.n_calls else None),
        }
