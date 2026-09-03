"""LlmJudgeOp 测试：FakeClient 零网络——解析/阈值/确定性抽样/失败语义。"""

from __future__ import annotations

import pytest
from curation_eval import Sample

from mm_curation.operators.llm_judge import LlmJudgeOp, _parse_score, _sampled

# ---------- 解析 ----------


def test_parse_score_variants():
    assert _parse_score('{"score": 8, "reason": "ok"}') == 8.0
    assert _parse_score('好的，评分如下 {"score": 3, "reason": "含广告"} 完毕') == 3.0
    assert _parse_score('{"score": 8.5, "reason": "x"}') == 8.5
    assert _parse_score("没有 json") is None
    assert _parse_score('{"score": "高", "reason": "x"}') is None  # 非数值
    assert _parse_score('{"reason": "缺 score"}') is None
    assert _parse_score('{"score": true}') is None  # bool 不算数值


# ---------- 确定性抽样 ----------


def test_sampling_deterministic_and_bounded():
    ids = [f"doc{i}" for i in range(2000)]
    a = {i for i in ids if _sampled(i, 0.1, seed=7)}
    b = {i for i in ids if _sampled(i, 0.1, seed=7)}
    assert a == b  # 同参数同批
    c = {i for i in ids if _sampled(i, 0.1, seed=99)}
    assert a != c  # 不同种子不同批
    rate = len(a) / len(ids)
    assert 0.05 < rate < 0.16  # 抽样率落在合理区间
    assert all(_sampled(i, 1.0, seed=1) for i in ids)  # rate=1 全评


# ---------- 算子行为（FakeClient） ----------


@pytest.fixture
def fake_chat(monkeypatch):
    """按文本内容返回可预测的评分；记录收到的 prompt。"""
    calls = []

    def _fake(base_url, model, api_key, text, timeout):
        calls.append(text)
        if "复读复读复读" in text:
            return '{"score": 1, "reason": "严重复读"}'
        return '{"score": 8, "reason": "正常"}'

    monkeypatch.setattr("mm_curation.operators.llm_judge._chat", _fake)
    return calls


def _op(**params) -> LlmJudgeOp:
    return LlmJudgeOp(base_url="http://fake/v1", model="fake", sample_rate=1.0, **params)


def test_judge_scores_and_threshold(fake_chat):
    op = _op(min=0.5)
    good = Sample(id="g1", text="这是一段正常文本")
    bad = Sample(id="b1", text="复读复读复读复读复读复读复读")
    kept = op.run_batch([good, bad])
    assert [s.id for s in kept] == ["g1"]  # 1/10=0.1 < 0.5 被扔
    assert good.meta["score:llm_judge"] == 0.8
    assert bad.meta["score:llm_judge"] == 0.1  # 分数仍写入（报告依赖）
    assert len(fake_chat) == 2
    assert op.stats_snapshot()["n_calls"] == 2


def test_unparseable_kept_with_none_score(monkeypatch):
    monkeypatch.setattr(
        "mm_curation.operators.llm_judge._chat", lambda *a: "模型开始胡言乱语没有json"
    )
    op = _op()
    s = Sample(id="u1", text="文本")
    assert op.run_batch([s]) == [s]  # 保留不评判
    assert s.meta["score:llm_judge"] is None
    assert op.stats_snapshot()["n_unparsed"] == 1


def test_service_error_semantics(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("judge 服务挂了")

    monkeypatch.setattr("mm_curation.operators.llm_judge._chat", boom)
    s = Sample(id="e1", text="文本")
    kept = _op(on_error="skip").run_batch([s])
    assert kept == [s] and s.meta["score:llm_judge"] is None
    with pytest.raises(ConnectionError):
        _op(on_error="fail").run_batch([Sample(id="e2", text="文本")])


def test_sample_rate_passthrough(fake_chat):
    op = LlmJudgeOp(base_url="http://fake/v1", sample_rate=0.0)
    samples = [Sample(id=f"p{i}", text="正常文本") for i in range(5)]
    kept = op.run_batch(samples)
    assert len(kept) == 5 and fake_chat == []  # 零抽样：全部直通零调用
    assert all("score:llm_judge" not in s.meta for s in kept)


def test_invalid_on_error_rejected():
    with pytest.raises(ValueError, match="on_error"):
        _op(on_error="retry")
