"""Sample schema 测试：构造不变量、自动推断、序列化与 legacy 兼容。"""

from __future__ import annotations

import pytest
from curation_eval import MODALITY_FIELDS, Sample


def test_text_article_default_and_empty_text_allowed():
    s = Sample(id="t1")
    assert s.modality == "text_article" and s.text == "" and s.image_path is None
    # 空文本合法：空文本正是质量算子要抓的对象（构造只管结构不管质量）


def test_image_auto_infers_modality():
    s = Sample(id="i1", text="一只猫", image_path="img/1.jpg")
    assert s.modality == "image_caption"  # 有图自动推断，免漏标


def test_empty_id_rejected():
    with pytest.raises(ValueError, match="id"):
        Sample(id="", text="x")


def test_unknown_modality_rejected():
    with pytest.raises(ValueError, match="未知 modality"):
        Sample(id="x", modality="video_caption")


def test_image_caption_without_image_rejected():
    with pytest.raises(ValueError, match="image_path"):
        Sample(id="x", text="x", modality="image_caption")


def test_modality_fields_consistent():
    # image_caption 必然蕴含 text + image_path；text_article 只蕴含 text
    assert MODALITY_FIELDS["image_caption"] == frozenset({"text", "image_path"})
    assert MODALITY_FIELDS["text_article"] == frozenset({"text"})


def test_round_trip_and_meta_labels():
    s = Sample(
        id="s1",
        text="一只猫",
        image_path="img/1.jpg",
        meta={"score:x": 0.5},
        labels={"dirty": "watermark"},
    )
    s2 = Sample.from_dict(s.to_dict())
    assert s2 == s


def test_from_dict_legacy_caption_key():
    legacy = {
        "id": "s2",
        "image_path": "img/2.jpg",
        "caption": "老数据格式",
        "meta": {},
        "labels": {},
    }
    s = Sample.from_dict(legacy)
    assert s.text == "老数据格式" and s.modality == "image_caption"  # 推断生效


def test_from_dict_prefers_text_over_caption():
    s = Sample.from_dict({"id": "s3", "text": "新字段", "caption": "旧字段"})
    assert s.text == "新字段"


def test_from_dict_drops_unknown_keys():
    # dropped.jsonl 里带 dropped_by 追加键：静默丢弃而非 TypeError（v1 容错行为）
    s = Sample.from_dict({"id": "s4", "text": "x", "dropped_by": "blur"})
    assert s.id == "s4"
