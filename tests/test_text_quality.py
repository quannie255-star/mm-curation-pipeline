"""文本算子与配置解析的单元测试。"""

import pytest

from mm_curation.operators import Sample, build_operator
from mm_curation.operators.text_quality import (
    CharRepetitionOp,
    ChineseRatioOp,
    TextLengthOp,
)
from mm_curation.pipeline.config import PipelineConfig

# ---------- text_length ----------


def test_text_length_counts_stripped_chars():
    op = TextLengthOp(min=5, max=100)
    s = Sample(id="1", image_path="a.jpg", text="  一只狗在草地上  ")
    assert op.score(s) == 7
    assert op(s) is not None


def test_text_length_rejects_too_short():
    op = TextLengthOp(min=5)
    s = Sample(id="1", image_path="a.jpg", text="狗")
    assert op(s) is None


# ---------- chinese_ratio ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("一只狗在草地上奔跑", 1.0),
        ("dog on grass 一只狗", 3 / 16),
        ("!!!???...", 0.0),
        ("", 0.0),
    ],
)
def test_chinese_ratio(text, expected):
    op = ChineseRatioOp()
    s = Sample(id="1", image_path="a.jpg", text=text)
    assert op.score(s) == pytest.approx(expected)


def test_chinese_ratio_threshold():
    op = ChineseRatioOp(min=0.3)
    keep = Sample(id="1", image_path="a.jpg", text="图上有 cat 一只猫")
    drop = Sample(id="2", image_path="b.jpg", text="the quick brown fox")
    assert op(keep) is not None
    assert op(drop) is None


# ---------- char_repetition ----------


def test_char_repetition_flags_spam_text():
    op = CharRepetitionOp(min=0.8)
    spam = Sample(id="1", image_path="a.jpg", text="哈哈哈哈哈哈哈哈哈哈哈哈哈")
    normal = Sample(id="2", image_path="b.jpg", text="一只狗在草地上奔跑")
    assert op(spam) is None
    assert op(normal) is not None


# ---------- score 写入 meta ----------


def test_score_recorded_in_meta():
    op = TextLengthOp(min=1)
    s = Sample(id="1", image_path="a.jpg", text="你好世界")
    op(s)
    assert s.meta["score:text_length"] == 4.0


# ---------- registry & config ----------


def test_build_operator_unknown_name_raises():
    with pytest.raises(KeyError, match="未注册的算子"):
        build_operator({"op": "no_such_op"})


def test_config_from_yaml(tmp_path):
    cfg_file = tmp_path / "p.yaml"
    cfg_file.write_text(
        """
name: test_pipeline
dataset:
  raw_jsonl: data/raw/samples.jsonl
operators:
  - op: text_length
    params: {min: 5, max: 100}
output:
  dir: data/processed/test
""",
        encoding="utf-8",
    )
    cfg = PipelineConfig.from_yaml(cfg_file)
    assert cfg.name == "test_pipeline"
    assert len(cfg.operators) == 1
    built = cfg.operators[0].build()
    assert isinstance(built, TextLengthOp)


def test_config_rejects_unknown_operator(tmp_path):
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text(
        """
name: bad
dataset: {raw_jsonl: x.jsonl}
operators:
  - op: not_registered
output: {dir: out}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="未注册算子"):
        PipelineConfig.from_yaml(cfg_file)
