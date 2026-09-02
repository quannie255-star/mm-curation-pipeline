"""文本语料算子与文本污染器测试（V2 β：text_article 模态零特例验证）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from curation_eval import ContaminationPlan, Sample

from mm_curation.operators import build_operator


@pytest.fixture
def text_samples():
    return [
        Sample(id="t1", text="这是一段足够长的正常中文文本内容，用来测试文本质量算子的基础行为。"),
        Sample(id="t2", text="短文"),
    ]


def _doc(text: str, sid: str = "d") -> Sample:
    return Sample(id=sid, text=text)


def test_doc_length(text_samples):
    op = build_operator({"op": "doc_length", "params": {"min": 10}})
    assert op.score(text_samples[0]) == pytest.approx(len(text_samples[0].text))
    assert op(text_samples[1]) is None  # 短文被丢
    assert text_samples[1].meta["score:doc_length"] == 2.0


def test_line_repetition_flags_paragraph_repeat():
    op = build_operator({"op": "line_repetition"})
    clean = _doc("第一段内容\n第二段内容\n第三段内容", "clean")
    dirty = _doc("第一段内容\n第二段内容\n第二段内容\n第二段内容", "dirty")
    assert op.score(clean) == 1.0
    assert op.score(dirty) == pytest.approx(1.0 - 2 / 4)
    assert op.score(_doc("单行文本无换行")) == 1.0


def test_boilerplate_and_pii_detect():
    bp = build_operator({"op": "boilerplate"})
    clean = _doc("正文内容讲的是数据工程实践。")
    spam = _doc("扫码关注公众号领取福利，点击链接 www.promo-site.cn 立即抢购")
    assert bp.score(clean) == 1.0
    assert bp.score(spam) < 1.0

    pii = build_operator({"op": "pii_detect"})
    clean2 = _doc("联系方式已脱敏。")
    leak = _doc("联系人手机 13812345678，邮箱 a@b.com")
    assert pii.score(clean2) == 1.0
    assert pii.score(leak) == pytest.approx(1.0 - 0.68)  # 两类命中


def test_perplexity_with_fake_scorer(text_samples, monkeypatch):
    import mm_curation.operators.text_corpus as tc

    def fake_ppls(texts):
        return [20.0 if len(t) > 10 else 800.0 for t in texts]  # 长文干净/短文乱码

    monkeypatch.setattr(tc, "get_scorer", lambda: fake_ppls)
    op = build_operator({"op": "perplexity", "params": {"min": 0.2}})
    clean, garbage = text_samples
    kept = op.run_batch([clean, garbage])
    assert [s.id for s in kept] == ["t1"]  # 高困惑度（乱码）被丢
    assert garbage.meta["score:perplexity"] == pytest.approx(1 / (1 + 800 / 50))


@pytest.mark.parametrize(
    "kind,any_of",
    [
        ("paragraph_repeat", ["正文。"]),
        ("boilerplate_inject", ["公众号", "转载请注明出处", "www.", "免责声明", "下载 APP"]),
        ("pii_inject", ["138", "@", "证件号"]),
        ("whitespace_pad", ["\u3000", "\n\n\n"]),
    ],
)
def test_text_contaminator_each_kind(kind, any_of):
    """逐类型测试（计划级 4 次抽样不能保证覆盖全部类型）。模板池多选一，
    断言任一特征子串命中。"""
    body = "这是第{i}段正文。\n第二段描述内容完整。"
    samples = [Sample(id=f"s{i}", text=body.format(i=i)) for i in range(3)]
    mixed, manifest = ContaminationPlan(inject_rate=1.0, seed=9, kinds={kind: 1.0}).run(
        samples, Path("/tmp/unused_images_beta")
    )
    assert manifest["counts"] == {kind: 3}
    injected = mixed[3:]
    assert all(s.labels["dirty"] == kind for s in injected)
    assert all(any(sub in s.text for sub in any_of) for s in injected)
