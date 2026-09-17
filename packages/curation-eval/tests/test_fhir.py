"""FHIR 适配器测试：roundtrip 保真、模态登记、执行器零特例、错误输入。"""

from __future__ import annotations

import json

import pytest
from curation_eval import (
    MODALITY_FIELDS,
    BatchOperator,
    CostClass,
    FHIRSample,
    LocalSequentialExecutor,
    Operator,
    OperatorMeta,
    Sample,
)

PATIENT = {
    "resourceType": "Patient",
    "id": "000001",
    "name": [{"family": "张", "given": ["*"]}],
    "birthDate": "1970-05-12",
    "meta": {"lastUpdated": "2026-01-01T00:01:00+00:00"},
}


def test_roundtrip_fidelity():
    s = FHIRSample.from_resource(PATIENT)
    s2 = FHIRSample.from_resource(FHIRSample.to_resource(s))
    assert s2 == s  # id/text/meta 全部确定性推导，逐字段相等
    assert json.loads(s.text) == PATIENT


def test_flat_mapping_and_meta_keys():
    s = FHIRSample.from_resource(PATIENT)
    assert s.id == "pat_000001"
    assert s.modality == "fhir_resource"
    assert s.meta["fhir_resource_type"] == "Patient"
    assert s.meta["fhir_version"] == "R4"
    assert s.meta["fhir_last_updated"] == "2026-01-01T00:01:00+00:00"
    assert MODALITY_FIELDS["fhir_resource"] == frozenset({"text"})


def test_serialization_roundtrip_via_from_dict():
    s = FHIRSample.from_resource(PATIENT)
    assert Sample.from_dict(s.to_dict()) == s  # 落盘 JSONL 后读回无损


def test_unknown_modality_still_rejected():
    with pytest.raises(ValueError, match="未知 modality"):
        Sample(id="x", modality="hl7_v2")  # 登记是新模态的唯一入口


def test_executor_skips_fhir_for_text_ops():
    """混合模态漏斗：文本算子对 fhir 样本保留不评判（计 skipped），零特例。"""

    class _CjkRatioOp(Operator):
        name = "test_cjk"
        meta = OperatorMeta(
            name="test_cjk",
            modalities=frozenset({"text_article"}),
            required_fields=frozenset({"text"}),
            cost_class=CostClass.RULE,
        )

        def score(self, sample: Sample) -> float | None:
            return 0.9 if sample.text else 0.0

    obs = FHIRSample.from_resource(
        {
            "resourceType": "Observation",
            "id": "000001",
            "status": "final",
            "effectiveDateTime": "2026-02-01T08:00:00+00:00",
        }
    )
    text = Sample(id="t1", text="正文")
    result = LocalSequentialExecutor().run([_CjkRatioOp()], [obs, text])
    assert [s.id for s in result.kept] == ["obs_000001", "t1"]  # 都保留
    stat = result.stats[0]
    assert stat.skipped == 1 and stat.dropped == 0
    assert "score:test_cjk" not in obs.meta  # 未评判不写分


def test_batch_operator_id_ordering_determinism():
    """批量算子的簇代表选择只依赖 id、不依赖输入序（γ3 教训的 fhir 版）。"""

    class _KeepFirstPerType(BatchOperator):
        name = "test_keep_first"
        meta = OperatorMeta(
            name="test_keep_first",
            modalities=frozenset({"fhir_resource"}),
            required_fields=frozenset({"text"}),
            cost_class=CostClass.RULE,
            shardable=False,
        )

        def run_batch(self, samples: list[Sample]) -> list[Sample]:
            seen: dict[str, str] = {}  # resourceType -> 首见 id（依赖输入序）
            drop: set[str] = set()
            for s in samples:  # 执行器已按 id 规范化排序
                rtype = FHIRSample.to_resource(s)["resourceType"]
                if rtype in seen:
                    drop.add(s.id)
                else:
                    seen[rtype] = s.id
            return [s for s in samples if s.id not in drop]

    pat_a = FHIRSample.from_resource({**PATIENT, "id": "a1"})
    pat_b = FHIRSample.from_resource({**PATIENT, "id": "b2"})
    result = LocalSequentialExecutor().run([_KeepFirstPerType()], [pat_b, pat_a])
    assert [s.id for s in result.kept] == ["pat_a1"]  # 字典序最小者胜，与输入序无关


def test_error_inputs_rejected():
    with pytest.raises(ValueError, match="resourceType"):
        FHIRSample.from_resource({"id": "1"})
    with pytest.raises(ValueError, match="resourceType"):
        FHIRSample.from_resource({"resourceType": "Coverage", "id": "1"})
    with pytest.raises(ValueError, match="id"):
        FHIRSample.from_resource({"resourceType": "Patient"})
    with pytest.raises(ValueError, match="不是 fhir_resource"):
        FHIRSample.parse(Sample(id="t", text="正文"))
