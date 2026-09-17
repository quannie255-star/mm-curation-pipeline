"""医疗 FHIR 模态质量算子（V4 α：phi_residual / code_validity / unit_normalization /
temporal_consistency / referential_integrity_fhir）。

输入约定：样本全部经 FHIRSample 适配器展平（text = 资源 canonical JSON）；
结构化内容从 text 解析，解析失败（非 fhir 样本/损坏 JSON）计 None——无法计分
保留并记录，与协议语义一致。批量两算子假设输入已按模态过滤（漏斗经
run_batch_mixed_modality 预过滤），与 text_minhash 同约定。

score 语义（协议统一）：越高越好；min=1.0 即「任一违规即丢」。码表/单位表与
合成语料同源（mm_curation.data.fhir_synth），换表两边一起换。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from curation_eval import CostClass, FHIRSample, register_operator

from ..data.fhir_synth import (
    ATC_CODES,
    ATC_SYSTEM,
    ICD10_CODES,
    ICD10_SYSTEM,
    LOINC_CODES,
    LOINC_SYSTEM,
    UCUM_UNITS,
)
from .base import BatchOperator, Operator, Sample

_UTC = timezone.utc

# PHI 模式族：大陆手机号 / 身份证号 / 美 SSN / 邮箱（叶值级检索，非全文）
_PHI_PATTERNS = (
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"\d{17}[\dXx]"),
    re.compile(r"\d{3}-\d{2}-\d{4}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
_CJK = ("\u4e00", "\u9fff")

# PHI 扫描的目标键：命叶值，只看这些子树（全资源扫描会把 id/日期误当证据）
_PHI_KEYS = frozenset({"name", "given", "family", "address", "telecom", "identifier", "contact"})

_CODE_TABLES = {
    ICD10_SYSTEM: (re.compile(r"[A-TV-Z]\d{2}(\.\d{1,4})?"), frozenset(ICD10_CODES)),
    LOINC_SYSTEM: (re.compile(r"\d{1,5}-\d"), frozenset(LOINC_CODES)),
    # ATC 五级码形如 A10BA02（L NN LL NN）
    ATC_SYSTEM: (re.compile(r"[A-Z]\d{2}[A-Z]{2}\d{2}"), frozenset(ATC_CODES)),
}


def _parse(sample: Sample) -> dict | None:
    try:
        return FHIRSample.parse(sample)
    except ValueError:
        return None


def _collect_strings(node, keys: frozenset, hit: str | None = None, out: list | None = None):
    """收集目标键子树下的叶字符串，返回 (叶键, 值) 列表。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        for k, v in node.items():
            child = k if k in keys else hit
            _collect_strings(v, keys, child, out)
    elif isinstance(node, list):
        for item in node:
            _collect_strings(item, keys, hit, out)
    elif isinstance(node, str) and hit is not None:
        out.append((hit, node))
    return out


def _find_all(node, pred, out: list | None = None) -> list:
    """深度优先收集满足 pred 的节点（coding 字典 / valueQuantity 字典）。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        if pred(node):
            out.append(node)
        for v in node.values():
            _find_all(v, pred, out)
    elif isinstance(node, list):
        for item in node:
            _find_all(item, pred, out)
    return out


def _find_quantities(node, out: list | None = None) -> list:
    """按 FHIR 语义精确定位 valueQuantity 字典（缺 unit 的也是 quantity）。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        vq = node.get("valueQuantity")
        if isinstance(vq, dict):
            out.append(vq)
        for v in node.values():
            _find_quantities(v, out)
    elif isinstance(node, list):
        for item in node:
            _find_quantities(item, out)
    return out


def _is_cjk(text: str) -> bool:
    return any(_CJK[0] <= ch <= _CJK[1] for ch in text)


@register_operator(
    name="phi_residual",
    modalities=frozenset({"fhir_resource"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class PhiResidualOp(Operator):
    """PHI 残留检测：name/address/telecom/identifier/contact 子树的叶值做
    模式匹配（手机号/身份证/SSN/邮箱）+ given 未脱敏检查（脱敏基线为 "*"，
    出现中文即视为未脱敏真实姓名）。score = 1 - 命中叶值占比，min=1.0 即
    有 PHI 就丢；目标子树为空的资源放行。"""

    def score(self, sample: Sample) -> float | None:
        resource = _parse(sample)
        if resource is None:
            return None
        leaves = _collect_strings(resource, _PHI_KEYS)
        if not leaves:
            return 1.0
        hits = 0
        for key, value in leaves:
            if any(p.search(value) for p in _PHI_PATTERNS):
                hits += 1
            elif key == "given" and _is_cjk(value):
                hits += 1
        return 1.0 - hits / len(leaves)


@register_operator(
    name="code_validity",
    modalities=frozenset({"fhir_resource"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class CodeValidityOp(Operator):
    """编码有效性：全部 Coding 按 system 查内嵌码表（ICD-10/LOINC/ATC），
    同时校验标准格式与码表成员；system 不在码表内的 Coding 不评判（语料外
    词汇不越权）。score = 合法 Coding 占比；无 Coding = 1.0。"""

    def score(self, sample: Sample) -> float | None:
        resource = _parse(sample)
        if resource is None:
            return None
        codings = _find_all(resource, lambda n: "system" in n and "code" in n)
        judged = [c for c in codings if c.get("system") in _CODE_TABLES]
        if not judged:
            return 1.0
        valid = 0
        for coding in judged:
            pattern, table = _CODE_TABLES[coding["system"]]
            code = str(coding.get("code", ""))
            if pattern.fullmatch(code) and code in table:
                valid += 1
        return valid / len(judged)


@register_operator(
    name="unit_normalization",
    modalities=frozenset({"fhir_resource"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class UnitNormalizationOp(Operator):
    """单位标准化：valueQuantity.unit 是否为标准 UCUM 单位（大小写敏感，
    mg/dl ≠ mg/dL；缺 unit 视为违规——语料约定 valueQuantity 必带单位）。
    score = 标准 unit 占比；无 valueQuantity = 1.0（合法业务异常不误杀）。"""

    def score(self, sample: Sample) -> float | None:
        resource = _parse(sample)
        if resource is None:
            return None
        quantities = _find_quantities(resource)
        if not quantities:
            return 1.0
        standard = sum(
            1 for q in quantities if isinstance(q.get("unit"), str) and q["unit"] in UCUM_UNITS
        )
        return standard / len(quantities)


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # birthDate 等纯日期推导值无时区——统一按 UTC 参与比较
        dt = dt.replace(tzinfo=_UTC)
    return dt


@register_operator(
    name="temporal_consistency",
    modalities=frozenset({"fhir_resource"}),
    required_fields={"text"},
    cost_class=CostClass.RULE,
    shardable=False,  # 跨资源：需要 Patient birthDate / Encounter 周期索引
)
class TemporalConsistencyOp(BatchOperator):
    """时间一致性（批量）：Observation.effectiveDateTime 需晚于其 Patient 的
    birthDate、落在 Encounter 周期内。引用缺失/无法解析计 None 不误杀——
    断链责任在 referential_integrity_fhir，本算子只对可核验的时间做裁决；
    非 Observation 或无时间检查项 = 1.0。"""

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("temporal_consistency 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        birth: dict[str, str] = {}
        periods: dict[str, dict] = {}
        for s in samples:
            r = _parse(s)
            if r is None:
                continue
            if r.get("resourceType") == "Patient":
                birth[r["id"]] = r.get("birthDate")
            elif r.get("resourceType") == "Encounter" and "period" in r:
                periods[r["id"]] = r["period"]

        survivors = []
        for s in samples:
            r = _parse(s)
            checks = self._checks(r, birth, periods) if r is not None else None
            score: float | None
            if checks is None:
                score = None
            elif not checks:
                score = 1.0
            else:
                score = sum(checks) / len(checks)
            s.meta[f"score:{self.name}"] = score
            if self.keep(score):
                survivors.append(s)
        return survivors

    @staticmethod
    def _checks(resource: dict, birth: dict[str, str], periods: dict[str, dict]):
        """返回检查项列表；None = 无法计分（引用缺失）。空列表 = 无需检查。"""
        if resource.get("resourceType") != "Observation":
            return []
        effective = _parse_dt(resource.get("effectiveDateTime"))
        if effective is None:
            return None
        subject = resource.get("subject", {}).get("reference", "")
        sid = subject.rsplit("/", 1)[-1] if "/" in subject else ""
        if not sid or sid not in birth:
            return None  # 患者断链：不是时间问题，交 referential 裁决
        birth_dt = _parse_dt(birth[sid])
        if birth_dt is None:
            return None
        checks = [effective.date() > birth_dt.date()]
        encounter = resource.get("encounter", {}).get("reference", "")
        eid = encounter.rsplit("/", 1)[-1] if "/" in encounter else ""
        if eid:
            period = periods.get(eid)
            if period is None:
                return None  # 就诊断链：同上
            start = _parse_dt(period.get("start"))
            if start is not None:
                checks.append(effective >= start)
            end = _parse_dt(period.get("end"))
            if end is not None:
                checks.append(effective <= end)
        return checks


@register_operator(
    name="referential_integrity_fhir",
    modalities=frozenset({"fhir_resource"}),
    required_fields={"text"},
    cost_class=CostClass.RULE,
    shardable=False,  # 全局视角：需要全量资源 id 集
)
class ReferentialIntegrityFhirOp(BatchOperator):
    """引用完整性（批量）：全部 reference 字段（Patient/xxx、Encounter/xxx）
    需指向集内存在的资源。score = 可解析引用占比；无引用资源 = 1.0。"""

    def score(self, sample: Sample) -> float | None:  # pragma: no cover - 批量算子
        raise TypeError("referential_integrity_fhir 是批量算子，请通过 run_batch 调用")

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        existing: set[tuple[str, str]] = set()
        parsed: list[tuple[Sample, dict | None]] = []
        for s in samples:
            r = _parse(s)
            parsed.append((s, r))
            if r is not None:
                existing.add((r.get("resourceType"), r.get("id")))

        survivors = []
        for s, r in parsed:
            if r is None:
                s.meta[f"score:{self.name}"] = None
                survivors.append(s)
                continue
            refs = []
            _collect_refs(r, refs)
            if not refs:
                score = 1.0
            else:
                resolved = sum(1 for ref in refs if tuple(ref) in existing)
                score = resolved / len(refs)
            s.meta[f"score:{self.name}"] = score
            if self.keep(score):
                survivors.append(s)
        return survivors


def _collect_refs(node, out: list) -> None:
    """收集全部 reference 叶值，解析为 (resourceType, id)；格式非法计断链。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference" and isinstance(v, str):
                parts = v.rsplit("/", 1)
                out.append(tuple(parts) if len(parts) == 2 and all(parts) else ("", v))
            else:
                _collect_refs(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, out)
