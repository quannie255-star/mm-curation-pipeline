"""FHIR R4 资源 ↔ Sample 双向适配器（V4 α：医疗模态零特例接入）。

扁平化约定（设计表 V4 α 决策点 1）：资源 JSON 规范化序列化（sort_keys、
ensure_ascii=False）进 sample.text——单一事实源；资源类型/FHIR 版本/最后更新时间
提升为 meta 三键。MODALITY_FIELDS 登记 fhir_resource 后，注册表校验、执行器
（含批量算子的模态切分）与算子级评测器零改动即可处理医疗样本。

roundtrip 保真：from_resource(to_resource(s)) 与 s 逐字段相等——id/meta 都从
资源确定性推导，序列化键序固定，不存在信息丢失路径。
"""

from __future__ import annotations

import json
from typing import Any

from .schema import Sample

MODALITY = "fhir_resource"

# 本适配器承诺 roundtrip 的资源类型（评测语料与污染器的定义域）。
KNOWN_RESOURCE_TYPES = frozenset(
    {"Patient", "Observation", "Encounter", "MedicationRequest"}
)


class FHIRSample:
    """FHIR 资源与扁平 Sample 之间的双向映射（纯函数式，无实例状态）。"""

    modality = MODALITY

    @staticmethod
    def from_resource(resource: dict[str, Any], *, fhir_version: str = "R4") -> Sample:
        if not isinstance(resource, dict):
            raise ValueError(f"FHIR 资源应为 dict，得到 {type(resource).__name__}")
        rtype = resource.get("resourceType")
        if not rtype or rtype not in KNOWN_RESOURCE_TYPES:
            raise ValueError(
                f"未知或缺失 resourceType: {rtype!r}，已知: {sorted(KNOWN_RESOURCE_TYPES)}"
            )
        rid = resource.get("id")
        if not rid:
            raise ValueError(f"{rtype} 资源缺少逻辑 id")
        meta: dict[str, Any] = {
            "fhir_resource_type": rtype,
            "fhir_version": fhir_version,
        }
        last_updated = (resource.get("meta") or {}).get("lastUpdated")
        if last_updated:
            meta["fhir_last_updated"] = last_updated
        return Sample(
            id=f"{rtype[:3].lower()}_{rid}",
            text=json.dumps(resource, sort_keys=True, ensure_ascii=False),
            modality=MODALITY,
            meta=meta,
        )

    @staticmethod
    def to_resource(sample: Sample) -> dict[str, Any]:
        if sample.modality != MODALITY:
            raise ValueError(f"modality={sample.modality!r} 不是 {MODALITY}，拒绝解析")
        resource = json.loads(sample.text)
        if not isinstance(resource, dict):
            raise ValueError("sample.text 不是 FHIR 资源 JSON 对象")
        return resource

    @staticmethod
    def parse(sample: Sample) -> dict[str, Any]:
        """算子侧别名：语义同 to_resource（解析失败抛 ValueError）。"""
        return FHIRSample.to_resource(sample)
