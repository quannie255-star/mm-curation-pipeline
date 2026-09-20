"""改写通道协议测试（V6 α）：与「打分→阈值」并列的前置阶段。

覆盖：注册/元数据校验、模态透传语义、留痕、丢弃、多级顺序、空输入。
"""

from __future__ import annotations

import pytest
from curation_eval import (
    Sample,
    Transformer,
    TransformerMeta,
    TransformResult,
    available_transformers,
    register_transformer,
    run_pre_stages,
)


@register_transformer(name="_t_upper", modalities=("text_article",))
class _Upper(Transformer):
    """测试用：转大写（确定性、可观测）。"""

    def transform(self, sample: Sample) -> TransformResult:
        upper = sample.text.upper()
        if upper == sample.text:
            return TransformResult.unchanged(sample)
        orig_len = len(sample.text)
        sample.text = upper
        return TransformResult.replaced(sample, {"rule": "upper", "orig_len": orig_len})


@register_transformer(name="_t_blank", modalities=("text_article",))
class _Blank(Transformer):
    """测试用：纯空白 → 不可用。"""

    def transform(self, sample: Sample) -> TransformResult:
        if sample.text.strip():
            return TransformResult.unchanged(sample)
        return TransformResult.unusable({"reason": "blank"})


@register_transformer(name="_t_tag", modalities=("text_article",))
class _Tag(Transformer):
    """测试用：追加标记（验证多级链顺序）。"""

    def transform(self, sample: Sample) -> TransformResult:
        sample.text = sample.text + "|tag"
        return TransformResult.replaced(sample, {"rule": "tag"})


def _s(sid: str, text: str = "abc", modality: str = "text_article") -> Sample:
    return Sample(id=sid, text=text, modality=modality)


def test_registration_visible_in_registry():
    metas = available_transformers()
    assert "_t_upper" in metas
    assert metas["_t_upper"].modalities == frozenset({"text_article"})
    assert metas["_t_upper"].required_fields == frozenset({"text"})
    # 装饰器把元数据注入类属性（与算子注册表同款）
    assert _Upper.meta.name == "_t_upper"
    assert _Upper.name == "_t_upper"


def test_unknown_modality_rejected():
    with pytest.raises(ValueError, match="未知模态"):
        TransformerMeta(name="x", modalities=frozenset({"nope"}))


def test_empty_modalities_rejected():
    with pytest.raises(ValueError, match="不得为空"):
        TransformerMeta(name="x", modalities=frozenset())


def test_required_fields_must_be_implied_by_modalities():
    with pytest.raises(ValueError, match="依赖字段"):
        TransformerMeta(
            name="x",
            modalities=frozenset({"text_article"}),
            required_fields=frozenset({"image_path"}),
        )


def test_duplicate_name_rejected():
    with pytest.raises(ValueError, match="名冲突"):

        @register_transformer(name="_t_upper", modalities=("text_article",))
        class _Dup(Transformer):  # pragma: no cover - 装饰期即失败
            def transform(self, sample: Sample) -> TransformResult:
                return TransformResult.unchanged(sample)


def test_result_constructors_require_log():
    """「没留痕的改写不可审计」——replaced/unusable 强制非空 log。"""
    s = _s("a")
    with pytest.raises(ValueError, match="非空 log"):
        TransformResult.replaced(s, {})
    with pytest.raises(ValueError, match="非空 log"):
        TransformResult.unusable({})
    assert TransformResult.unchanged(s).changed is False


def test_modality_mismatch_passes_through_untouched():
    """结构性模态（fhir_resource）不被自然语言改写器碰到。"""
    fhir = Sample(id="f1", text="{}", modality="fhir_resource")
    out = run_pre_stages([_Upper()], [fhir])
    assert out.samples[0].text == "{}"
    assert out.stats[0].skipped == 1
    assert out.stats[0].changed == 0
    assert out.stats[0].n_out == 1
    assert "transform:_t_upper" not in out.samples[0].meta


def test_log_written_only_when_changed():
    a, b = _s("a", "abc"), _s("b", "ABC")
    out = run_pre_stages([_Upper()], [a, b])
    assert out.samples[0].text == "ABC"
    assert out.samples[0].meta["transform:_t_upper"]["rule"] == "upper"
    assert "transform:_t_upper" not in out.samples[1].meta  # 未命中不写日志
    assert out.stats[0].changed == 1


def test_unusable_sample_dropped_and_recorded():
    out = run_pre_stages([_Blank()], [_s("a", "   "), _s("b", "ok")])
    assert [s.id for s in out.samples] == ["b"]
    assert out.stats[0].dropped == 1
    assert out.dropped[0][0] == "_t_blank"
    assert out.dropped[0][1].id == "a"
    assert out.dropped[0][1].meta["transform:_t_blank"]["outcome"] == "unusable"


def test_stages_apply_in_order():
    out = run_pre_stages([_Upper(), _Tag()], [_s("a", "abc")])
    assert out.samples[0].text == "ABC|tag"
    assert [st.stage for st in out.stats] == ["_t_upper", "_t_tag"]


def test_empty_input_is_safe():
    out = run_pre_stages([_Upper()], [])
    assert out.samples == []
    assert out.stats[0].n_in == 0
    assert out.stats[0].pass_rate == 0.0


def test_dunder_call_returns_sample_only():
    assert _Upper()(_s("a", "abc")).text == "ABC"


def test_applies_to_without_meta_covers_all_modalities():
    """无元数据的 v1 风格改写器 = 全模态适用（与算子约定一致）。"""

    class _NoMeta(Transformer):
        def transform(self, sample: Sample) -> TransformResult:
            return TransformResult.unchanged(sample)

    fhir = Sample(id="f1", text="{}", modality="fhir_resource")
    assert _NoMeta().applies_to(fhir) is True
