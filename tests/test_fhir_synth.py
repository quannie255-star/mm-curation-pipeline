"""合成 FHIR 语料测试：确定性逐字节、构成/引用闭合、PHI 基线零命中。"""

from __future__ import annotations

import json
import re

from mm_curation.data.fhir_synth import (
    N_ENCOUNTER,
    N_MEDICATION,
    N_OBSERVATION,
    N_PATIENT,
    generate_corpus,
)

MOBILE_RE = re.compile(r"1[3-9]\d{9}")
SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")
ID_CARD_RE = re.compile(r"\d{17}[\dXx]")


def _corpus_dicts(samples):
    return [json.loads(s.text) for s in samples]


def _by_type(samples, rtype):
    return [s for s in samples if s.meta["fhir_resource_type"] == rtype]


def test_seed_determinism_byte_identical():
    a = [s.to_dict() for s in generate_corpus(seed=7, scale=0.1)]
    b = [s.to_dict() for s in generate_corpus(seed=7, scale=0.1)]
    assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)
    c = [s.to_dict() for s in generate_corpus(seed=8, scale=0.1)]
    assert json.dumps(a, ensure_ascii=False) != json.dumps(c, ensure_ascii=False)


def test_composition_and_sample_ids():
    samples = generate_corpus(seed=42)
    assert len(samples) == N_PATIENT + N_ENCOUNTER + N_OBSERVATION + N_MEDICATION
    assert len(_by_type(samples, "Patient")) == N_PATIENT
    assert len(_by_type(samples, "Encounter")) == N_ENCOUNTER
    assert len(_by_type(samples, "Observation")) == N_OBSERVATION
    assert len(_by_type(samples, "MedicationRequest")) == N_MEDICATION
    assert all(s.modality == "fhir_resource" for s in samples)


def test_reference_closure():
    """全部 subject/encounter 引用指向存在的资源——干净侧误杀=0 的前提。"""
    resources = _corpus_dicts(generate_corpus(seed=42))
    existing = {(r["resourceType"], r["id"]) for r in resources}
    refs = []
    for r in resources:
        for key in ("subject", "encounter"):
            if key in r:
                refs.append(r[key]["reference"])
    assert refs, "语料应包含跨资源引用"
    for ref in refs:
        rtype, rid = ref.split("/")
        assert (rtype, rid) in existing, f"断链引用: {ref}"


def test_clean_corpus_zero_phi_patterns_and_temporal_consistent():
    """干净语料：零 PHI 模式命中 + Observation 时间全部晚于 birthDate 且在周期内。"""
    from datetime import datetime

    samples = generate_corpus(seed=42)
    resources = _corpus_dicts(samples)
    for r in resources:
        blob = json.dumps(r, ensure_ascii=False)
        assert not MOBILE_RE.search(blob), f"手机号模式命中: {r['id']}"
        assert not SSN_RE.search(blob)
        assert not ID_CARD_RE.search(blob)
        if r["resourceType"] == "Patient":
            assert r["name"][0]["given"] == ["*"]  # 脱敏基线
    birth = {
        r["id"]: datetime.fromisoformat(r["birthDate"])
        for r in resources
        if r["resourceType"] == "Patient"
    }
    periods = {
        r["id"]: r.get("period", {})
        for r in resources
        if r["resourceType"] == "Encounter"
    }
    n_checked = 0
    for r in resources:
        if r["resourceType"] != "Observation":
            continue
        eff = datetime.fromisoformat(r["effectiveDateTime"])
        pid = r["subject"]["reference"].split("/")[1]
        assert eff.date() > birth[pid].date(), f"时间早于出生: obs {r['id']}"
        period = periods[r["encounter"]["reference"].split("/")[1]]
        assert eff >= datetime.fromisoformat(period["start"])
        if "end" in period:
            assert eff <= datetime.fromisoformat(period["end"])
        n_checked += 1
    assert n_checked == N_OBSERVATION


def test_business_anomalies_present():
    """合法业务异常存在（缺 valueQuantity / 无 end / 无 telecom），防算子作弊。"""
    resources = _corpus_dicts(generate_corpus(seed=42))
    obs = [r for r in resources if r["resourceType"] == "Observation"]
    encs = [r for r in resources if r["resourceType"] == "Encounter"]
    pats = [r for r in resources if r["resourceType"] == "Patient"]
    assert any("valueQuantity" not in r for r in obs)
    assert any("end" not in r["period"] for r in encs)
    assert any("telecom" not in r for r in pats)
