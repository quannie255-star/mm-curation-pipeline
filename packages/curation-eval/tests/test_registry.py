"""注册表测试：元数据校验（fail-fast）、名字派生、重名拒绝。"""

from __future__ import annotations

import pytest
from curation_eval import (
    CostClass,
    OperatorMeta,
    available_operator_metas,
    get_operator_meta,
    register_operator,
)


def test_meta_rejects_unimplied_fields():
    # text_article 只蕴含 {text}，image_path 未被蕴含 → 注册时 ValueError
    with pytest.raises(ValueError, match="蕴含"):
        OperatorMeta(
            name="x",
            modalities=frozenset({"text_article"}),
            required_fields=frozenset({"image_path"}),
            cost_class=CostClass.RULE,
        )


def test_meta_rejects_unknown_modality_and_empty():
    with pytest.raises(ValueError, match="未知模态"):
        OperatorMeta(
            name="x",
            modalities=frozenset({"video"}),
            required_fields=frozenset({"text"}),
            cost_class=CostClass.RULE,
        )
    with pytest.raises(ValueError, match="不得为空"):
        OperatorMeta(
            name="x",
            modalities=frozenset(),
            required_fields=frozenset({"text"}),
            cost_class=CostClass.RULE,
        )


def test_register_derives_name_from_camel_case():
    @register_operator(
        modalities=frozenset({"image_caption"}),
        required_fields=frozenset({"text", "image_path"}),
        cost_class=CostClass.MODEL,
    )
    class ProbeWmNsfwCnnOp:  # 缩写连写难例：派生 probe_wm_nsfw_cnn
        pass

    # 注意：进程内注册表全局共享，主仓库已注册 wm_nsfw_cnn（重名会被拒绝——设计行为）
    assert ProbeWmNsfwCnnOp.name == "probe_wm_nsfw_cnn"
    assert ProbeWmNsfwCnnOp.meta.cost_class is CostClass.MODEL


def test_register_explicit_name_and_meta_attached():
    @register_operator(
        name="test_len_meta",
        modalities=frozenset({"text_article", "image_caption"}),
        required_fields=frozenset({"text"}),
        cost_class=CostClass.RULE,
    )
    class SomethingUnrelated:
        pass  # 注册机制不要求继承 Operator（框架只管机制，实例管实现）

    assert SomethingUnrelated.name == "test_len_meta"
    assert get_operator_meta("test_len_meta").required_fields == frozenset({"text"})
    assert "test_len_meta" in available_operator_metas()


def test_duplicate_name_rejected():
    @register_operator(
        name="dup_probe",
        modalities=frozenset({"text_article"}),
        required_fields=frozenset({"text"}),
        cost_class=CostClass.RULE,
    )
    class First:
        pass

    with pytest.raises(ValueError, match="冲突"):

        @register_operator(
            name="dup_probe",
            modalities=frozenset({"text_article"}),
            required_fields=frozenset({"text"}),
            cost_class=CostClass.RULE,
        )
        class Second:
            pass
