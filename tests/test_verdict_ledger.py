"""判决书测试（V6 α）：schema v1 + 落盘 + 聚合 + 算子包装 + 漏斗集成。

关键红线：**不传 ledger 时既有行为逐位不变**（旁路产物）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mm_curation.operators.base import Sample
from mm_curation.pipeline import OperatorSpec, PipelineConfig, run_funnel
from mm_curation.verdict import (
    RULE_BATCH_DROP_NO_SCORE,
    RULE_BATCH_DROP_SCORE_IN_RANGE,
    RULE_NO_SCORE_KEPT,
    RULE_SCORE_ABOVE_MAX,
    RULE_SCORE_BELOW_MIN,
    RULE_WITHIN_THRESHOLD,
    VERDICT_SCHEMA_VERSION,
    VerdictLedger,
    build_verdict,
    derive_rule,
    fingerprint_sample,
    read_verdicts,
    threshold_of,
    verdict_stats,
    wrap_for_verdict,
)
from mm_curation.verdict.recording import RecordingBatchOperator, RecordingOperator

# --- 指纹与判据 ---------------------------------------------------------


def test_fingerprint_is_content_addressed():
    a = Sample(id="id-a", text="同样的内容")
    b = Sample(id="id-b", text="同样的内容")
    c = Sample(id="id-a", text="不同的内容")
    assert fingerprint_sample(a) == fingerprint_sample(b)  # id 不入指纹
    assert fingerprint_sample(a) != fingerprint_sample(c)
    assert fingerprint_sample(a).startswith("sha256:")


def test_fingerprint_distinguishes_modality():
    text = "{}"
    assert fingerprint_sample(
        Sample(id="1", text=text, modality="fhir_resource")
    ) != fingerprint_sample(Sample(id="2", text=text, modality="text_article"))


def test_threshold_of_extracts_only_min_max():
    assert threshold_of({"min": 0.3, "max": 9, "batch_size": 64, "threshold": 0.7}) == {
        "min": 0.3,
        "max": 9,
    }
    assert threshold_of({}) == {}


@pytest.mark.parametrize(
    ("score", "threshold", "dropped", "expected"),
    [
        (0.1, {"min": 0.3}, True, RULE_SCORE_BELOW_MIN),
        (0.9, {"max": 0.3}, True, RULE_SCORE_ABOVE_MAX),
        (0.5, {"min": 0.3}, False, RULE_WITHIN_THRESHOLD),
        (None, {}, False, RULE_NO_SCORE_KEPT),
        (None, {}, True, RULE_BATCH_DROP_NO_SCORE),
        (0.5, {"min": 0.3}, True, RULE_BATCH_DROP_SCORE_IN_RANGE),
    ],
)
def test_derive_rule_branches(score, threshold, dropped, expected):
    assert derive_rule(score, threshold, dropped) == expected


# --- 构造 ---------------------------------------------------------------


def _scored(sid: str, score: float, text: str = "内容") -> Sample:
    s = Sample(id=sid, text=text)
    s.meta["score:doc_length"] = score
    return s


def test_build_verdict_drop_has_full_evidence_chain():
    s = _scored("s1", 12.0)
    v = build_verdict(
        run_id="r1", seq=1, op="doc_length", sample=s, dropped=True,
        params={"min": 30, "max": 50000},
    )
    assert v.v == VERDICT_SCHEMA_VERSION
    assert v.decision == "drop"
    assert v.rule == RULE_SCORE_BELOW_MIN
    assert v.threshold == {"min": 30, "max": 50000}
    # evidence 是「判据真正用到的量」——分数 + 门限
    assert v.evidence["score"] == 12.0
    assert v.evidence["min"] == 30
    assert v.input_fingerprint == fingerprint_sample(s)
    assert v.prov["wasGeneratedBy"] == "op:doc_length"


def test_build_verdict_merges_operator_explain():
    s = _scored("s1", 0.14)
    v = build_verdict(
        run_id="r1",
        seq=2,
        op="doc_length",
        sample=s,
        dropped=True,
        params={"min": 0.3},
        extra_evidence={"chars_han": 430, "chars_total": 3001},
    )
    assert v.evidence["chars_han"] == 430
    assert v.evidence["score"] == 0.14


def test_build_verdict_carries_transform_log():
    s = _scored("s1", 100.0)
    s.meta["transform:text_normalize"] = {"rules_applied": ["whitespace_collapse"]}
    v = build_verdict(run_id="r1", seq=1, op="doc_length", sample=s, dropped=False)
    assert v.transform == {"transform:text_normalize": {"rules_applied": ["whitespace_collapse"]}}


def test_build_verdict_keep_without_transform_is_null():
    v = build_verdict(run_id="r1", seq=1, op="doc_length", sample=_scored("s", 1.0), dropped=False)
    assert v.transform is None
    assert v.review is None


# --- 落盘与读取 ---------------------------------------------------------


def test_ledger_writes_jsonl_and_manifest(tmp_path: Path):
    led = VerdictLedger(tmp_path, run_id="run-x", config_name="cfg")
    for i in range(3):
        led.record(
            build_verdict(
                run_id="run-x", seq=1, op="doc_length",
                sample=_scored(f"s{i}", 1.0), dropped=False,
            )
        )
    led.close(extra={"n_kept": 3})
    assert led.n_written == 3

    rows = read_verdicts(tmp_path / "verdict.jsonl")
    assert len(rows) == 3
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "run-x"
    assert manifest["n_verdicts"] == 3
    assert manifest["n_kept"] == 3
    assert manifest["v"] == VERDICT_SCHEMA_VERSION


def test_ledger_rejects_run_id_mismatch(tmp_path: Path):
    led = VerdictLedger(tmp_path, run_id="run-x")
    bad = build_verdict(run_id="run-y", seq=1, op="op", sample=_scored("s", 1.0), dropped=False)
    with pytest.raises(ValueError, match="不一致"):
        led.record(bad)


def test_ledger_rejects_write_after_close(tmp_path: Path):
    led = VerdictLedger(tmp_path, run_id="r")
    led.close()
    with pytest.raises(RuntimeError, match="已 close"):
        led.record(
            build_verdict(run_id="r", seq=1, op="op", sample=_scored("s", 1.0), dropped=False)
        )


def test_ledger_requires_run_id(tmp_path: Path):
    with pytest.raises(ValueError, match="run_id"):
        VerdictLedger(tmp_path, run_id="")


def test_iter_verdicts_skips_blank_lines(tmp_path: Path):
    p = tmp_path / "verdict.jsonl"
    p.write_text('{"op": "a"}\n\n{"op": "b"}\n', encoding="utf-8")
    assert [r["op"] for r in read_verdicts(p)] == ["a", "b"]
    assert read_verdicts(tmp_path / "missing.jsonl") == []


def test_verdict_stats_aggregation():
    rows = [
        {"op": "doc_length", "seq": 1, "decision": "drop", "rule": RULE_SCORE_BELOW_MIN},
        {"op": "doc_length", "seq": 1, "decision": "keep", "rule": RULE_WITHIN_THRESHOLD},
        {"op": "text_minhash", "seq": 2, "decision": "drop", "rule": RULE_BATCH_DROP_NO_SCORE},
    ]
    st = verdict_stats(rows)
    assert st["n_total"] == 3
    assert st["n_drop"] == 2
    assert st["by_op"]["doc_length"] == {"n": 2, "drop": 1, "keep": 1, "seq": 1}
    assert st["by_rule"][RULE_BATCH_DROP_NO_SCORE] == 1


# --- 算子包装 -----------------------------------------------------------


class _FakeOp:
    name = "fake"
    meta = None

    def __init__(self, params=None):
        self.params = params or {}

    def score(self, sample):
        return float(len(sample.text))

    def keep(self, score):
        return score >= self.params.get("min", 0)

    def __call__(self, sample):
        score = self.score(sample)
        sample.meta[f"score:{self.name}"] = score
        return sample if self.keep(score) else None


def test_wrap_preserves_name_meta_and_params(tmp_path: Path):
    op = _FakeOp({"min": 3})
    wrapped = wrap_for_verdict(op, seq=1, ledger=VerdictLedger(tmp_path, run_id="r"))
    assert wrapped.name == "fake"
    assert wrapped.params == {"min": 3}
    assert wrapped.meta is None
    assert isinstance(wrapped, RecordingOperator)


def test_recording_operator_records_keep_and_drop(tmp_path: Path):
    led = VerdictLedger(tmp_path, run_id="r")
    wrapped = wrap_for_verdict(_FakeOp({"min": 3}), seq=1, ledger=led)
    wrapped(Sample(id="long", text="abcd"))
    wrapped(Sample(id="short", text="a"))
    led.close()
    rows = read_verdicts(tmp_path / "verdict.jsonl")
    assert [(r["decision"], r["rule"]) for r in rows] == [
        ("keep", RULE_WITHIN_THRESHOLD),
        ("drop", RULE_SCORE_BELOW_MIN),
    ]


def test_recording_batch_operator_records_by_id_diff(tmp_path: Path):
    from curation_eval import BatchOperator

    class _FakeBatch(BatchOperator):
        name = "fbatch"
        meta = None

        def __init__(self):
            self.params = {}

        def run_batch(self, samples):
            return [s for s in samples if s.id != "dup"]

    led = VerdictLedger(tmp_path, run_id="r")
    wrapped = wrap_for_verdict(_FakeBatch(), seq=2, ledger=led)
    assert isinstance(wrapped, RecordingBatchOperator)
    wrapped.run_batch([Sample(id="a"), Sample(id="dup"), Sample(id="b")])
    led.close()
    rows = read_verdicts(tmp_path / "verdict.jsonl")
    dropped = [r for r in rows if r["decision"] == "drop"]
    assert len(dropped) == 1
    assert dropped[0]["rule"] == RULE_BATCH_DROP_NO_SCORE
    assert dropped[0]["seq"] == 2


# --- 漏斗集成 -----------------------------------------------------------


def _cfg(ops):
    return PipelineConfig(
        name="t",
        raw_jsonl=Path("unused.jsonl"),
        output_dir=Path("unused_out"),
        operators=[OperatorSpec(op=name, params=params or {}) for name, params in ops],
    )


INFLATED = "中文新闻标题\n\n" + "中文正文。" * 10 + " " * 3000 + "\r\r\n尾部"


def test_run_funnel_without_ledger_leaves_no_trace(tmp_path: Path):
    """红线：旁路产物——不传 ledger 就不该产生任何落盘、不产生前置阶段统计。"""
    missing = tmp_path / "verdicts"
    result = run_funnel(
        [Sample(id="a", text=INFLATED), Sample(id="b", text="干干净净的中文新闻正文。" * 3)],
        _cfg([("doc_length", {"min": 30}), ("char_repetition", {"min": 0.8})]),
    )
    assert not missing.exists()
    assert result.transform_stats == []
    assert result.transform_dropped == []


def test_run_funnel_with_ledger_records_every_stage(tmp_path: Path):
    led = VerdictLedger(tmp_path, run_id="e2e")
    result = run_funnel(
        [Sample(id="a", text=INFLATED), Sample(id="b", text="干干净净的中文新闻正文。" * 3)],
        _cfg([("doc_length", {"min": 30}), ("char_repetition", {"min": 0.8})]),
        verdict_ledger=led,
    )
    rows = read_verdicts(tmp_path / "verdict.jsonl")
    # 2 样本 × 2 级
    assert len(rows) == 4
    assert len(result.kept) == 1
    assert {r["op"] for r in rows} == {"doc_length", "char_repetition"}
    assert sorted(r["seq"] for r in rows) == [1, 1, 2, 2]


def test_pre_stages_rescue_whitespace_inflated_document(tmp_path: Path):
    """归一化的直接价值：被空白膨胀骗过的算子不再误杀（实测 char_repetition）。"""
    from mm_curation.normalize import TextNormalizeTransformer

    cfg = _cfg([("char_repetition", {"min": 0.8})])
    samples = [Sample(id="a", text=INFLATED)]

    without = run_funnel([Sample(id="a", text=INFLATED)], cfg)
    with_pre = run_funnel(
        samples, cfg, pre_stages=[TextNormalizeTransformer()], verdict_ledger=None
    )

    assert len(without.kept) == 0  # 3000 个连续空格被判成「水文本」
    assert len(with_pre.kept) == 1  # 归一化后不再误杀
    assert with_pre.transform_stats[0].changed == 1


def test_funnel_result_keeps_dropped_and_transform_dropped_separate(tmp_path: Path):
    """前置阶段丢弃与算子丢弃分账——归因不混。"""
    from mm_curation.normalize import TextNormalizeTransformer

    result = run_funnel(
        [Sample(id="blank", text="   \r\n  "), Sample(id="ok", text="正常的中文新闻正文。" * 3)],
        _cfg([("doc_length", {"min": 30})]),
        pre_stages=[TextNormalizeTransformer()],
    )
    assert [t[0] for t in result.transform_dropped] == ["text_normalize"]
    assert result.dropped == []
    assert [s.id for s in result.kept] == ["ok"]
