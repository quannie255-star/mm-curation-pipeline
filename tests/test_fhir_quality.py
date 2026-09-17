"""医疗算子测试：每算子覆盖 正常通过/正常拒绝/边界值/错误输入 + 批量确定性。"""

from __future__ import annotations

import copy

from curation_eval import FHIRSample, Sample

from mm_curation.data.fhir_synth import LOINC_SYSTEM, generate_corpus
from mm_curation.operators.fhir_quality import (
    CodeValidityOp,
    PhiResidualOp,
    ReferentialIntegrityFhirOp,
    TemporalConsistencyOp,
    UnitNormalizationOp,
)

MIN = {"min": 1.0}
CORPUS = generate_corpus(seed=42, scale=0.1)  # 10/10/20/10 = 50 条


def _patient(**overrides):
    r = {
        "resourceType": "Patient",
        "id": "900001",
        "name": [{"family": "张", "given": ["*"]}],
        "birthDate": "1970-05-12",
        "address": [{"state": "某省", "city": "某市"}],
        "telecom": [{"system": "phone", "value": "+86-1**-****-****"}],
    }
    r.update(overrides)
    return r


def _observation(**overrides):
    r = {
        "resourceType": "Observation",
        "id": "900002",
        "status": "final",
        "code": {"coding": [{"system": LOINC_SYSTEM, "code": "8867-4"}]},
        "subject": {"reference": "Patient/000001"},
        "encounter": {"reference": "Encounter/000001"},
        "effectiveDateTime": "2026-02-01T08:00:00+00:00",
        "valueQuantity": {"value": 5.5, "unit": "mmol/L", "system": "http://unitsofmeasure.org"},
    }
    r.update(overrides)
    return r


# --- phi_residual -----------------------------------------------------------


def test_phi_clean_patient_passes():
    op = PhiResidualOp(**MIN)
    s = op(FHIRSample.from_resource(_patient()))
    assert s is not None and s.meta["score:phi_residual"] == 1.0


def test_phi_leaked_name_and_phone_dropped():
    op = PhiResidualOp(**MIN)
    dirty = _patient()
    dirty["name"][0]["given"] = ["伟"]
    dirty["telecom"].append({"system": "phone", "value": "13812345678"})
    assert op(FHIRSample.from_resource(dirty)) is None


def test_phi_boundary_identifier_ssn_and_email():
    op = PhiResidualOp(**MIN)
    ssn = _patient(
        identifier=[{"system": "http://hl7.org/fhir/sid/us-ssn", "value": "123-45-6789"}]
    )
    assert op(FHIRSample.from_resource(ssn)) is None
    mail = _patient(
        telecom=[{"system": "email", "value": "real.person@example.com"}]
    )
    assert op(FHIRSample.from_resource(mail)) is None


def test_phi_empty_subtree_resource_passes():
    """Encounter 无 PHI 目标子树：扫描字段为 0 → 放行。"""
    op = PhiResidualOp(**MIN)
    enc = {
        "resourceType": "Encounter",
        "id": "900003",
        "status": "finished",
        "subject": {"reference": "Patient/000001"},
        "period": {"start": "2026-02-01T08:00:00+00:00"},
    }
    s = op(FHIRSample.from_resource(enc))
    assert s is not None and s.meta["score:phi_residual"] == 1.0


def test_phi_non_fhir_input_scores_none_kept():
    s = PhiResidualOp(**MIN)(Sample(id="t1", text="普通文本"))
    assert s is not None and s.meta["score:phi_residual"] is None


# --- code_validity ----------------------------------------------------------


def test_code_clean_loinc_passes():
    op = CodeValidityOp(**MIN)
    s = op(FHIRSample.from_resource(_observation()))
    assert s is not None and s.meta["score:code_validity"] == 1.0


def test_code_missing_dot_dropped():
    op = CodeValidityOp(**MIN)
    bad = _observation(
        code={"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "E119"}]}
    )
    assert op(FHIRSample.from_resource(bad)) is None


def test_code_lowercase_and_out_of_table_dropped():
    op = CodeValidityOp(**MIN)
    lower = _observation(
        code={"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "e11.9"}]}
    )
    assert op(FHIRSample.from_resource(lower)) is None
    outsider = _observation(
        code={"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "E11.99"}]}
    )  # 合法 ICD 格式但不在码表内
    assert op(FHIRSample.from_resource(outsider)) is None


def test_code_unknown_system_not_judged():
    """system 不在码表内的 Coding 不越权评判（v3-ActCode 等）。"""
    op = CodeValidityOp(**MIN)
    enc = {
        "resourceType": "Encounter",
        "id": "900004",
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
    }
    s = op(FHIRSample.from_resource(enc))
    assert s is not None and s.meta["score:code_validity"] == 1.0


def test_code_non_fhir_input_scores_none_kept():
    s = CodeValidityOp(**MIN)(Sample(id="t2", text="普通文本"))
    assert s is not None and s.meta["score:code_validity"] is None


# --- unit_normalization -----------------------------------------------------


def test_unit_standard_passes():
    op = UnitNormalizationOp(**MIN)
    s = op(FHIRSample.from_resource(_observation()))
    assert s is not None and s.meta["score:unit_normalization"] == 1.0


def test_unit_case_violation_dropped():
    op = UnitNormalizationOp(**MIN)
    bad = _observation(valueQuantity={"value": 90, "unit": "mg/dl"})
    assert op(FHIRSample.from_resource(bad)) is None


def test_unit_missing_dropped():
    op = UnitNormalizationOp(**MIN)
    no_unit = _observation(valueQuantity={"value": 90})
    assert op(FHIRSample.from_resource(no_unit)) is None


def test_unit_absent_quantity_passes():
    """合法业务异常：无 valueQuantity 的 Observation 放行。"""
    op = UnitNormalizationOp(**MIN)
    sparse = _observation()
    del sparse["valueQuantity"]
    s = op(FHIRSample.from_resource(sparse))
    assert s is not None and s.meta["score:unit_normalization"] == 1.0


# --- temporal_consistency（批量）--------------------------------------------


def _run_temporal(samples):
    op = TemporalConsistencyOp(**MIN)
    survivors = op.run_batch(copy.deepcopy(samples))
    return op, {s.id: s for s in survivors}


def test_temporal_clean_corpus_all_pass():
    _, kept = _run_temporal(CORPUS)
    assert len(kept) == len(CORPUS)  # 干净语料时间一致性零违例（生成器保证）
    obs_scores = [
        s.meta["score:temporal_consistency"]
        for s in kept.values()
        if s.meta["fhir_resource_type"] == "Observation"
    ]
    assert obs_scores and all(v == 1.0 for v in obs_scores)


def test_temporal_inverted_dropped():
    inverted = FHIRSample.from_resource(_observation(effectiveDateTime="1960-01-01T00:00:00+00:00"))
    _, kept = _run_temporal(CORPUS + [inverted])
    assert inverted.id not in kept
    assert len(kept) == len(CORPUS)


def test_temporal_missing_reference_scores_none_kept():
    """边界：subject 指向不存在的 Patient → None 不误杀（断链归 referential 管）。"""
    orphan = FHIRSample.from_resource(
        _observation(subject={"reference": "Patient/888888"})
    )
    _, kept = _run_temporal(CORPUS + [orphan])
    assert orphan.id in kept
    assert kept[orphan.id].meta["score:temporal_consistency"] is None


def test_temporal_non_observation_passes_and_shuffle_deterministic():
    import random as _random

    outs = []
    for seed in (0, 1):
        items = CORPUS.copy()
        _random.Random(seed).shuffle(items)
        _, kept = _run_temporal(items)
        outs.append(sorted(kept))
    assert outs[0] == outs[1]  # 簇索引与输入序无关


# --- referential_integrity_fhir（批量）--------------------------------------


def _run_referential(samples):
    op = ReferentialIntegrityFhirOp(**MIN)
    survivors = op.run_batch(copy.deepcopy(samples))
    return op, {s.id: s for s in survivors}


def test_referential_clean_closure_all_pass():
    _, kept = _run_referential(CORPUS)
    assert len(kept) == len(CORPUS)  # 引用闭合（生成器保证）


def test_referential_broken_subject_dropped():
    broken = FHIRSample.from_resource(_observation(subject={"reference": "Patient/999999"}))
    _, kept = _run_referential(CORPUS + [broken])
    assert broken.id not in kept
    assert len(kept) == len(CORPUS)


def test_referential_patient_without_refs_passes():
    _, kept = _run_referential(CORPUS)
    patients = [
        s for s in kept.values() if s.meta["fhir_resource_type"] == "Patient"
    ]
    assert patients and all(s.meta["score:referential_integrity_fhir"] == 1.0 for s in patients)


def test_referential_malformed_reference_counts_broken():
    malformed = FHIRSample.from_resource(_observation(subject={"reference": "no-slash-ref"}))
    _, kept = _run_referential(CORPUS + [malformed])
    assert malformed.id not in kept


def test_referential_shuffle_deterministic():
    import random as _random

    outs = []
    for seed in (0, 1):
        items = CORPUS.copy()
        _random.Random(seed).shuffle(items)
        _, kept = _run_referential(items)
        outs.append(sorted(kept))
    assert outs[0] == outs[1]
