"""归一化层测试（V6 α）：七条规则 + 留痕 + 聚合 + 改写器接入。

关键回归锚点：
- #65 现场（空白膨胀冒充低中文占比）的规则级复现
- #44 陷阱（U+2028 会让 splitlines 错切 JSONL）
- 「同形态多含义」：结构化载荷模态**不得**被自然语言改写器碰到
"""

from __future__ import annotations

import pytest
from curation_eval import run_pre_stages

from mm_curation.normalize import (
    NATURAL_LANGUAGE_MODALITIES,
    RULES,
    TextNormalizeTransformer,
    aggregate,
    normalize_text,
    whitespace_ratio,
)
from mm_curation.operators.base import Sample


def test_rules_catalog_complete_and_unknown_rejected():
    assert RULES == (
        "nfc",
        "line_separators",
        "newline",
        "zero_width",
        "control_chars",
        "whitespace_collapse",
        "strip",
    )
    with pytest.raises(ValueError, match="未知归一化规则"):
        normalize_text("x", rules=("no_such_rule",))


def test_carriage_return_normalized():
    """#65 现场实测的 `\\r` 残留（891 篇命中 newline 规则）。"""
    out = normalize_text("第一行\r\n第二行\r第三行")
    assert out.text == "第一行\n第二行\n第三行"
    assert "newline" in out.rules_applied


def test_whitespace_inflation_collapsed():
    """#65 现场：3000 个空格把「汉字/全文长度」的分母撑爆。

    归一化后残留的 4 个空白字符是**刻意保留的段落边界**（两个 `\\n\\n`），
    不是没清干净——段落结构是语言证据。
    """
    text = "中文新闻标题\n\n" + "中文正文。" * 10 + " " * 3000 + "\r\r\n尾部"
    out = normalize_text(text)
    assert whitespace_ratio(text) > 0.9
    assert out.chars_removed >= 3000
    assert out.whitespace_ratio_after < 0.1
    assert out.text.count("\n\n") == 2  # 段落边界保留
    assert set(out.rules_applied) >= {"newline", "whitespace_collapse"}


def test_paragraph_boundary_preserved():
    """段落边界是语言证据，必须保留；超过两个换行才折叠。"""
    assert normalize_text("甲\n\n乙").text == "甲\n\n乙"
    assert normalize_text("甲\n\n\n\n乙").text == "甲\n\n乙"


def test_line_separator_u2028_and_splitlines_trap():
    """笔记 #44：U+2028 会让 `splitlines()` 把一行 JSONL 切成两行。"""
    raw = "甲\u2028乙"
    assert len(raw.splitlines()) == 2  # 陷阱本身（项目禁用 splitlines 的原因）
    out = normalize_text(raw)
    assert out.text == "甲\n乙"


def test_zero_width_and_control_chars_removed():
    assert normalize_text("甲\u200b\u200c乙").text == "甲乙"
    assert normalize_text("甲\x07乙").text == "甲乙"
    # 制表符保留到折叠阶段 → 变成空格（`a\tb` 不该塌成 `ab`）
    assert normalize_text("a\tb").text == "a b"


def test_fullwidth_and_inline_spaces_collapsed():
    assert normalize_text("甲\u3000\u3000乙").text == "甲 乙"
    assert normalize_text("甲   乙").text == "甲 乙"


def test_nfc_composes_decomposed_sequence():
    out = normalize_text("e\u0301")
    assert out.text == "\u00e9"
    assert "nfc" in out.rules_applied


def test_clean_text_reports_no_rules_applied():
    """诚实留痕：规则跑过但没改东西，就不记它。"""
    out = normalize_text("完全干净的中文文本。\n\n第二段。")
    assert out.rules_applied == ()
    assert out.changed is False
    assert out.chars_removed == 0


def test_subset_rules_only_applies_subset():
    out = normalize_text("a\r\nb", rules=("whitespace_collapse",))
    assert out.text == "a\r\nb"  # 没跑 newline 规则
    assert out.rules_applied == ()


def test_outcome_log_fields():
    out = normalize_text("a  b")
    log = out.to_log()
    assert set(log) == {
        "rules_applied",
        "orig_len",
        "new_len",
        "chars_removed",
        "whitespace_ratio_before",
        "whitespace_ratio_after",
    }
    assert log["rules_applied"] == ["whitespace_collapse"]
    assert log["orig_len"] == 4 and log["new_len"] == 3


def test_whitespace_ratio_edges():
    assert whitespace_ratio("") == 0.0
    assert whitespace_ratio("    ") == 1.0
    assert whitespace_ratio("中") == 0.0


def test_aggregate_counts_and_medians():
    agg = aggregate([normalize_text(t) for t in ("a  b", "干净文本", "x\r\ny")])
    d = agg.to_dict()
    assert d["n"] == 3
    assert d["n_changed"] == 2
    # 规则正交：`x\r\ny` 只算 newline 的功劳，不算 whitespace_collapse
    assert d["rule_counts"]["whitespace_collapse"] == 1
    assert d["rule_counts"]["newline"] == 1
    assert 0.0 <= d["whitespace_ratio_before_p50"] <= 1.0
    assert d["chars_removed_total"] == 2  # "a  b"->1, "x\r\ny"->1


# --- 改写器接入 ---------------------------------------------------------


def test_transformer_declares_only_natural_language_modalities():
    assert NATURAL_LANGUAGE_MODALITIES == frozenset({"text_article", "image_caption"})
    assert TextNormalizeTransformer.meta.modalities == NATURAL_LANGUAGE_MODALITIES


def test_structured_modalities_pass_through_untouched():
    """同形态多含义：JSON 载荷里的双空格是数据，不是噪声。"""
    fhir = Sample(id="f1", text='{"given": ["John  Smith"]}', modality="fhir_resource")
    sensor = Sample(id="s1", text='{"readings": [1,  2]}', modality="industrial_sensor")
    out = run_pre_stages([TextNormalizeTransformer()], [fhir, sensor])
    assert out.samples[0].text == '{"given": ["John  Smith"]}'
    assert out.samples[1].text == '{"readings": [1,  2]}'
    assert out.stats[0].skipped == 2
    assert out.stats[0].changed == 0


def test_transformer_unchanged_for_clean_text():
    clean = Sample(id="c1", text="干净的新闻正文。\n\n第二段。")
    out = run_pre_stages([TextNormalizeTransformer()], [clean])
    assert out.samples[0].text == clean.text
    assert out.stats[0].changed == 0
    assert "transform:text_normalize" not in out.samples[0].meta


def test_transformer_writes_meta_log_on_change():
    s = Sample(id="d1", text="a  b\r\nc")
    out = run_pre_stages([TextNormalizeTransformer()], [s])
    log = out.samples[0].meta["transform:text_normalize"]
    assert log["outcome"] == "replaced"
    assert set(log["rules_applied"]) >= {"newline", "whitespace_collapse"}
    assert out.stats[0].changed == 1


def test_transformer_marks_whitespace_only_sample_unusable():
    s = Sample(id="e1", text="   \r\n\t  ")
    out = run_pre_stages([TextNormalizeTransformer()], [s])
    assert out.samples == []
    assert out.stats[0].dropped == 1
    assert out.dropped[0][1].meta["transform:text_normalize"]["reason"] == "empty_after_normalize"


def test_transformer_rule_subset_limits_effect():
    s = Sample(id="f1", text="a\r\nb  c")
    out = run_pre_stages([TextNormalizeTransformer(rules=("whitespace_collapse",))], [s])
    assert out.samples[0].text == "a\r\nb c"
