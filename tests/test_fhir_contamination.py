"""医疗污染器测试：确定性、原始样本不可变、五类注入靶向命中对应算子、干净侧零误杀。"""

from __future__ import annotations

from pathlib import Path

from curation_eval import ContaminationPlan, Sample

from mm_curation.data.fhir_synth import generate_corpus
from mm_curation.operators.fhir_quality import (
    CodeValidityOp,
    PhiResidualOp,
    ReferentialIntegrityFhirOp,
    TemporalConsistencyOp,
    UnitNormalizationOp,
)

KINDS = {
    "fhir_phi_leak": 1.0,
    "fhir_code_invalid": 1.0,
    "fhir_time_inverted": 1.0,
    "fhir_ref_broken": 1.0,
    "fhir_unit_off": 1.0,
}
SINGLE_OPS = {
    "fhir_phi_leak": PhiResidualOp(min=1.0),
    "fhir_code_invalid": CodeValidityOp(min=1.0),
    "fhir_unit_off": UnitNormalizationOp(min=1.0),
}
BATCH_OPS = {
    "fhir_time_inverted": TemporalConsistencyOp(min=1.0),
    "fhir_ref_broken": ReferentialIntegrityFhirOp(min=1.0),
}
CORPUS = generate_corpus(seed=42, scale=0.1)


def _contaminate(seed: int):
    plan = ContaminationPlan(inject_rate=0.6, seed=seed, kinds=dict(KINDS))
    mixed, manifest = plan.run(CORPUS, Path("data/tmp_fhir_images"))
    return mixed, manifest


def test_plan_deterministic_same_seed():
    a, ma = _contaminate(42)
    b, mb = _contaminate(42)
    assert [s.to_dict() for s in a] == [s.to_dict() for s in b]
    assert ma == mb
    assert ma["counts"] and sum(ma["counts"].values()) == ma["n_injected"]


def test_originals_never_modified():
    mixed, _ = _contaminate(42)
    assert [s.to_dict() for s in mixed[: len(CORPUS)]] == [s.to_dict() for s in CORPUS]
    injected = mixed[len(CORPUS):]
    assert injected and all(s.labels.get("dirty") in KINDS for s in injected)


def test_five_kinds_present_and_hit_primary_operator():
    mixed, manifest = _contaminate(42)
    injected = mixed[len(CORPUS):]
    assert set(manifest["counts"]) == set(KINDS)  # inject_rate 足够大 + 等权 → 五类齐

    for kind, op in SINGLE_OPS.items():
        targets = [s for s in injected if s.labels["dirty"] == kind]
        assert targets, f"{kind} 无注入样本"
        missed = [s.id for s in targets if op(Sample.from_dict(s.to_dict())) is not None]
        assert not missed, f"{kind} 被主靶算子漏检: {missed}"

    survivors = {s.id for s in BATCH_OPS["fhir_time_inverted"].run_batch(mixed)} & {
        s.id for s in BATCH_OPS["fhir_ref_broken"].run_batch(mixed)
    }
    for kind in BATCH_OPS:
        targets = [s for s in injected if s.labels["dirty"] == kind]
        missed = [s.id for s in targets if s.id in survivors]
        assert not missed, f"{kind} 未被批量主靶拦截: {missed}"


def test_clean_corpus_zero_false_kill_all_ops():
    """干净语料在全部五个算子上零误杀（注入不改动原始样本的前提下可归因）。"""
    for op in SINGLE_OPS.values():
        assert all(op(Sample.from_dict(s.to_dict())) is not None for s in CORPUS)
    for op in BATCH_OPS.values():
        assert len(op.run_batch(CORPUS)) == len(CORPUS)
