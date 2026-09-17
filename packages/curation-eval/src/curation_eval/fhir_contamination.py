"""FHIR 医疗污染器（V4 α）：向合成 FHIR 语料注入「合规脏数据」。

供体重抽（本模块的核心模式）：计划随机抽中的源样本未必带目标算子可攻击的
字段（如 phi 靶抽中了 Observation——PHI 长在 Patient 上）。此时从 ctx.pool
重抽一个合适供体（走 ctx.rng，同 seed 确定性），plan 分配的 id/labels 不变，
meta 三键随供体刷新。原始样本永远不被修改（注入即复制，协议约定）。

码表/姓名池与合成语料同源（mm_curation.data.fhir_synth）——本模块只依赖
字面 system URL 判断「受评判编码」，不 import 主仓库（包不反向依赖消费方）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from .contamination import Contaminator, Context, register

ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"
LOINC_SYSTEM = "http://loinc.org"
ATC_SYSTEM = "http://www.whocc.no/atc"
_SYSTEMS = (ICD10_SYSTEM, LOINC_SYSTEM, ATC_SYSTEM)

# 通用常见姓名样式（与 fhir_synth 池同型；仅注入用，不指向真实个体）
_FAMILIES = "张 王 李 赵 刘 陈 杨 黄 周 吴 徐 孙 马 朱 胡 郭 何 高 林 罗".split()
_GIVENS = "伟 芳 娜 敏 静 丽 强 磊 军 洋 勇 艳 杰 娟 涛 明 超 秀 兰 霞".split()


def _load(sample) -> dict:
    return json.loads(sample.text)


def _store(sample, resource: dict) -> None:
    sample.text = json.dumps(resource, sort_keys=True, ensure_ascii=False)
    sample.meta["fhir_resource_type"] = resource["resourceType"]
    last_updated = (resource.get("meta") or {}).get("lastUpdated")
    if last_updated:
        sample.meta["fhir_last_updated"] = last_updated


def _draw_donor(ctx: Context, predicate) -> dict:
    pool = [s for s in ctx.pool if predicate(_load(s))]
    if not pool:
        raise ValueError("污染器供体池为空（语料不满足注入前提）")
    return _load(pool[ctx.rng.randrange(len(pool))])


def _seq_of(sample_id: str) -> int:
    """plan 生成的 id 形如 `src::fhir_xxx{i}`——取尾部序号做确定性手机号。"""
    tail = sample_id.rsplit("::", 1)[-1]
    rev = ""
    for ch in reversed(tail):
        if ch.isdigit():
            rev = ch + rev
        elif rev:
            break
    return int(rev) if rev else 0


def _first_judged_coding(resource: dict) -> dict:
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("system") in _SYSTEMS and "code" in node:
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    raise ValueError("资源中无受评判编码")


def _patient_birth(ctx: Context, pid: str) -> datetime:
    for s in ctx.pool:
        r = _load(s)
        if r.get("resourceType") == "Patient" and r["id"] == pid:
            return datetime.fromisoformat(r["birthDate"])
    raise ValueError(f"供体患者不存在: {pid}")


@register("fhir_phi_leak")
class FhirPhiLeak(Contaminator):
    """PHI 未脱敏：脱敏名（given=["*"]）换真实样式姓名；半数变体追加完整手机号。"""

    def apply(self, sample, ctx: Context):
        resource = _load(sample)
        if resource.get("resourceType") != "Patient":
            resource = _draw_donor(ctx, lambda r: r.get("resourceType") == "Patient")
        name = resource["name"][0]
        name["family"] = _FAMILIES[ctx.rng.randrange(len(_FAMILIES))]
        name["given"] = [_GIVENS[ctx.rng.randrange(len(_GIVENS))]]
        if ctx.rng.random() < 0.5:
            seq = _seq_of(sample.id)
            resource.setdefault("telecom", []).append(
                {"system": "phone", "value": f"13{seq % 10}{seq:09d}"}
            )
        _store(sample, resource)
        return sample


def _mangle(code: str, kind: int) -> str:
    if kind == 0 and "." in code:
        return code.replace(".", "")
    if kind == 1 and any(ch.isalpha() for ch in code):
        return code[0].lower() + code[1:]
    last = code[-1]
    return code[:-1] + ("8" if last == "9" else "9")  # 表外同格式：恒可用的兜底


@register("fhir_code_invalid")
class FhirCodeInvalid(Contaminator):
    """编码格式破坏：去点（E11.9→E119）/ 首字母小写（e11.9）/ 换表外同格式码。"""

    def apply(self, sample, ctx: Context):
        resource = _load(sample)
        if not _has_judged(resource):
            resource = _draw_donor(ctx, _has_judged)
        coding = _first_judged_coding(resource)
        code = str(coding["code"])
        mangled = _mangle(code, ctx.rng.randrange(3))
        if mangled == code:  # 该变体对这类码无效（如纯数字码小写）→ 破格式兜底
            mangled = _mangle(code, 2)
        coding["code"] = mangled
        _store(sample, resource)
        return sample


def _has_judged(resource: dict) -> bool:
    try:
        return _first_judged_coding(resource) is not None
    except ValueError:
        return False


@register("fhir_time_inverted")
class FhirTimeInverted(Contaminator):
    """时间倒挂：Observation.effectiveDateTime 移到其 Patient.birthDate 之前。"""

    def apply(self, sample, ctx: Context):
        resource = _load(sample)

        def ok(r):
            return (
                r.get("resourceType") == "Observation"
                and "subject" in r
                and "effectiveDateTime" in r
            )

        if not ok(resource):
            resource = _draw_donor(ctx, ok)
        pid = resource["subject"]["reference"].rsplit("/", 1)[-1]
        birth = _patient_birth(ctx, pid)
        inverted = birth - timedelta(days=100 + ctx.rng.randrange(500))
        resource["effectiveDateTime"] = inverted.isoformat()
        _store(sample, resource)
        return sample


@register("fhir_ref_broken")
class FhirRefBroken(Contaminator):
    """引用断裂：subject/encounter 指向语料中不存在的资源 id（9999xx 段）。"""

    def apply(self, sample, ctx: Context):
        resource = _load(sample)
        if "subject" not in resource and "encounter" not in resource:
            resource = _draw_donor(
                ctx, lambda r: "subject" in r or "encounter" in r
            )
        key = "subject"
        if "encounter" in resource and (ctx.rng.random() < 0.3 or "subject" not in resource):
            key = "encounter"
        rtype = resource[key]["reference"].split("/")[0]
        resource[key]["reference"] = f"{rtype}/9999{_seq_of(sample.id) % 100:02d}"
        _store(sample, resource)
        return sample


@register("fhir_unit_off")
class FhirUnitOff(Contaminator):
    """单位不一致：UCUM 大小写破坏（mg/dL→mg/dl）或直接删除 unit。"""

    def apply(self, sample, ctx: Context):
        resource = _load(sample)
        vq = resource.get("valueQuantity")
        if not isinstance(vq, dict) or not isinstance(vq.get("unit"), str):
            resource = _draw_donor(ctx, lambda r: _has_unit(r))
            vq = resource["valueQuantity"]
        if ctx.rng.random() < 0.5 and vq["unit"] != vq["unit"].lower():
            vq["unit"] = vq["unit"].lower()
        else:
            del vq["unit"]
        _store(sample, resource)
        return sample


def _has_unit(resource: dict) -> bool:
    vq = resource.get("valueQuantity")
    return isinstance(vq, dict) and isinstance(vq.get("unit"), str)
