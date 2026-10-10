"""`text_quality_classifier` 的测试：结构特征的分档与「少一族就漏」。

为什么这组测试值得存在
--------------------
本模块的核心结论是「廉价 n-gram 分类器学的是**具体字符**、不是**形态**」，
而泛化完全靠**与字面量无关的结构特征**。这意味着：

* 少一族结构特征 → 某一类 held-out 损伤直接漏到 0.1 以下，
  而**门禁仍然绿**（因为其他指标没塌）。
* 所以必须对**每一族特征**单独断言它抓得住对应的损伤形态。

这不是预防性设计，是实测逼出来的（详见 `_struct_token` 与 `_augment` 的注释）：

===================  ==========================================
去掉的那一族       实测后果
===================  ==========================================
`INV` 隐形比例      unseen 零宽字符召回 0.020 → **0.930**
`REP` 最长游程       整段复读召回 0.180 → **0.845**
`LNU` 行级唯一度    同形态换字面量召回 0.500 → **0.100**
浮点直接写进 token  隐形特征权重恒 0（词表里没有那个 token）
===================  ==========================================
"""

from __future__ import annotations

import random

import pytest

from mm_curation.text_quality_classifier import (
    _INV_BUCKETS,
    A_SUBSTYLES,
    INVISIBLE_CHARS,
    STYLE_B,
    StyleGroup,
    _augment,
    apply_style,
    inv_bucket,
    invisible_ratio,
    line_uniqueness,
    max_repeat_run,
    repeat_ratio,
)

ZW = chr(0x200B)  # 零宽空格 —— 必须用码位，字面量会被工具链静默丢弃
WJ = chr(0x2060)  # word joiner


#: 测试基文本。**必须是多样内容** —— 曾用同一句×6，那本身就是复读样本，
#: 于是 `repeat_ratio` 测出 0.83，用它去验证「重复率无区分度」自然失败。
#: 判据的素材本身带偏，测出来的结论就是错的。
_BASE_PARTS = (
    "维基百科是一部自由的百科全书，任何人都可以编辑它。",
    "本作遵循知识共享许可协议，禁止商业使用与未经授权的转载。",
    "该条目介绍了一段历史事件的背景、经过与后续影响。",
    "编者按：本文内容仅供参考，不构成任何投资建议。",
    "数据来源为公开出版物，统计口径详见文末的附录说明。",
    "参考文献列出了该条目所引用的全部学术文献与档案编号。",
    "在电子设备中，此参数通常以毫秒为单位表示其持续时间。",
    "该物种在形态上与近缘种的区别主要体现在叶片边缘特征。",
)


def _real_corpus(limit: int) -> list[str]:
    """读真实语料（用于「判据必须在真实数据形态上验证」的那几条）。

    CI 干净检出不含 `data/raw/`，此时返回空列表 → 调用方 `pytest.skip`。
    **不造合成替代品** —— 合成文本的形态与真实语料不同，
    用它验证「判据无区分度」会得到假结论（实测踩过）。
    """
    import json  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    path = pathlib.Path(__file__).resolve().parents[1] / "data/raw/text_corpus.jsonl"
    if not path.exists():
        return []
    out: list[str] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line)["text"])
            except (json.JSONDecodeError, KeyError):
                continue
            if len(out) >= limit:
                break
    return out


def mk() -> str:
    return "".join(_BASE_PARTS)


# ---------------------------------------------------------------- INV 档位


def test_inv_bucket_is_ordered_and_monotonic() -> None:
    """档位必须**单调**：比例越大档位越高，否则模型学不到「越脏」的方向。"""
    prev = -1
    for r in (0.0, 0.0005, 0.002, 0.01, 0.03, 0.10):
        b = inv_bucket(r)
        assert b >= prev, f"档位非单调：ratio={r} → {b}< {prev}"
        prev = b
    assert inv_bucket(0.0) == 0
    assert inv_bucket(10.0) == len(_INV_BUCKETS), "极高比例应落在最高档"


def test_invisible_ratio_counts_all_invisible_chars() -> None:
    """`invisible_ratio` 必须认得**所有**不可见字符类别，
    否则换个码位就测不出来（实测踩过：`_ZERO_WIDTH` 被写成空串 → 恒 0）。"""
    for ch in INVISIBLE_CHARS:
        t = mk()[:100]
        assert invisible_ratio(t + ch * 10) > 0, f"未识别不可见字符 {ord(ch):#x}"


def test_augment_emits_bucket_tokens_not_floats() -> None:
    """⚠️ 反回归：档位 token 必须是**类别**而不是浮点数字串。

    `⟦INV 0.0234⟧` 这种写法在 char n-gram 下 tokenize 成数字串，
    推理时词表里根本没有那个 token → 权重恒 0 且**不报错**。
    这是真实的静默失效（实测：unseen 零宽字符召回卡在 0.020）。

    断言方式：把前置的结构 token 逐个拆出来，检查
    ①每token 都是 `⟦族名+纯数字⟧` 的形态；②**没有任何小数点**。
    """
    head = _augment([mk() + ZW * 30])[0].split(" ", 1)[0]
    # 形如 ⟦INV4⟧⟦REP3⟧⟦RAT2⟧⟦LNU0⟧⟦LEN1⟧
    import re  # noqa: PLC0415

    toks = re.findall(r"⟦[^⟧]*⟧", head)
    assert len(toks) >= 5, f"结构 token 族数不足: {toks}"
    for tk in toks:
        body = tk.removeprefix("⟦").removesuffix("⟧")
        assert re.fullmatch(r"[A-Z]+\d+", body), (
            f"结构 token 必须是「族名+纯数字」，实际 {body!r}—— "
            "出现小数点即退化成浮点 token（静默失效）"
        )
    assert "." not in head, f"结构 token 里出现小数点: {head!r}"
    families = {re.fullmatch(r"[A-Z]+", tk.removeprefix("⟦").removesuffix("⟧")[:-1]).group()
                for tk in toks}
    assert {"INV", "REP", "RAT", "LNU", "LEN"} <= families, f"缺结构族: {families}"


def test_augment_distinguishes_clean_from_invisible() -> None:
    """干净样本与非零隐形样本必须落在**不同**档位。"""
    clean = _augment([mk()])[0]
    dirty = _augment([mk() + ZW * 40])[0]
    assert clean.split(" ", 1)[0] != dirty.split(" ", 1)[0]


# ---------------------------------------------------------------- 结构量


def test_max_repeat_run_separates_char_repeat_from_clean() -> None:
    """字符复读 → 长游程；干净正文 → 1~3。"""
    clean = max_repeat_run(mk())
    assert clean <= 3, f"干净正文游程应很短，实际 {clean}"
    dirty = max_repeat_run(mk()[:100] + "甲" * 200)
    assert dirty > 20, f"字符复读游程应很长，实际 {dirty}"


def test_line_uniqueness_separates_para_repeat_from_clean() -> None:
    """⚠️ 反回归：3-gram 抓不到**整段复读**，行级唯一度才抓得到。

    实测：段级复读的 3-gram 游程仍是 1~3（与干净相同），
    但行级唯一度趋近 1/N。这条测试锁住的就是那个差异。
    """
    clean = line_uniqueness(mk())          # 单行 → 1.0
    para = "\n".join(["甲段"] * 10)
    assert clean == 1.0
    assert line_uniqueness(para) < 0.2


def test_repeat_ratio_alone_is_not_sufficient_on_short_text() -> None:
    """⚠️ **反判据**测试：3-gram 重复率在本项目语料上**没有区分度**。

    实测（真实语料 300 条）：干净样本重复率中位 **0.271**，
    段级复读（para_repeat）注入后中位 **0.281**，
    **62% 的损伤样本落在干净样本的 p05~p95 区间内** —— 分布几乎重合。

    这条测试**故意断言重叠存在**（用真实语料而非合成文本）。
    它的作用是：让未来任何人想「只用重复率」时，立刻看到为什么不够
    （必须配 `max_repeat_run` 与 `line_uniqueness`）。

    ⚠️ 判据的素材必须用**真实语料**：曾经用「同一句 ×6」当基文本，
    那是复读样本本身（重复率 0.83），测出来的结论自然是错的 ——
    **判据的素材带偏，结论就全错**，而且不报错。
    """
    corpus = _real_corpus(300)
    if len(corpus) < 50:
        pytest.skip("真实语料不可用（CI 干净检出不含 data/raw）")

    clean = sorted(repeat_ratio(t) for t in corpus)
    lo, hi = clean[int(0.05 * len(clean))], clean[int(0.95 * len(clean))]

    st = A_SUBSTYLES[0]
    one = StyleGroup(
        name="P", kinds=("para_repeat",), filler_chars=st.filler_chars,
        repeat_range=st.repeat_range, keep_ratio=st.keep_ratio,
    )
    dirty = [repeat_ratio(apply_style(t, one, random.Random(i)))
             for i, t in enumerate(corpus[:60])]
    overlap = sum(1 for v in dirty if lo <= v <= hi) / len(dirty)
    assert overlap > 0.5, (
        f"段级复读只有 {overlap:.0%} 落在干净分布内 —— "
        "「重复率无区分度」的结论可能已失效，需重新核实后再改判据"
    )


# ---------------------------------------------------------------- 注入生效


@pytest.mark.parametrize(
    "style",
    [*A_SUBSTYLES, STYLE_B],
    ids=lambda s: s.name,
)
def test_every_style_actually_modifies_text(style: StyleGroup) -> None:
    """每种损伤都必须真的改文本 —— 静默返回原文 = 假样本。

    （实测踩过：`_ZERO_WIDTH = ""` 让zero_width 注入函数
    原样返回，而训练照跑，指标照出，全错。）
    """
    out = apply_style(mk(), style, random.Random(7))
    assert out != mk(), f"风格 {style.name} 的注入没有任何效果"


def test_zero_width_style_injects_real_characters() -> None:
    """B 组的零宽字符必须真的注入（`INVISIBLE_CHARS` 要认得它）。"""
    one = StyleGroup(
        name="B", kinds=("zero_width",), filler_chars=STYLE_B.filler_chars,
        repeat_range=STYLE_B.repeat_range, keep_ratio=STYLE_B.keep_ratio,
    )
    out = apply_style(mk(), one, random.Random(3))
    assert out.count(ZW) > 0, "零宽字符没注入"
    assert invisible_ratio(out) > 0


def test_wordjoiner_style_differs_from_zero_width_char() -> None:
    """A5（训练侧）用的不可见字符必须**不同于** B 组的。

    否则「教不变量、验迁移」就退化成了「教答案」——
    泛化检查会立刻失去意义（B 组不再是 held-out）。
    """
    assert WJ in INVISIBLE_CHARS
    assert WJ != ZW
    one = StyleGroup(
        name="A5", kinds=("wordjoiner_spam",), filler_chars=("壬", "癸"),
        repeat_range=(4, 7), keep_ratio=(0.3, 0.5),
    )
    out = apply_style(mk(), one, random.Random(3))
    assert out.count(WJ) > 0
    assert out.count(ZW) == 0, "训练侧不应出现 B 组的零宽字符"


# ---------------------------------------------------------------- 分组纪律


def test_b_style_damage_kinds_never_appear_in_training() -> None:
    """B 组损伤**不得**出现在任何 A 子风格里 —— 否则 held-out 是假的。"""
    train_kinds = {k for s in A_SUBSTYLES for k in s.kinds}
    assert not (set(STYLE_B.kinds) & train_kinds), (
        f"B 组损伤 {set(STYLE_B.kinds) & train_kinds} 出现在训练集里"
    )


def test_training_styles_produce_nonconstant_struct_features() -> None:
    """⚠️ 反回归：训练侧必须出现过**非零档位**。

    若所有训练样本的档位都是 0，该特征就是常数、模型学不到任何东西
    （实测：A 组全是可见损伤 → `⟦INV0⟧` 恒定 → 隐形特征完全失效）。
    """
    from mm_curation.text_quality_classifier import _struct_token  # noqa: PLC0415

    seen: set[str] = set()
    for style in A_SUBSTYLES:
        for kind in style.kinds:
            one = StyleGroup(
                name=style.name, kinds=(kind,), filler_chars=style.filler_chars,
                repeat_range=style.repeat_range, keep_ratio=style.keep_ratio,
            )
            for seed in range(6):
                out = apply_style(mk(), one, random.Random(seed))
                seen.add(_struct_token(out).strip())
                if len(seen) > 4:
                    break
    assert len(seen) > 1, f"训练侧结构特征恒定（只有 {seen}）→ 该族特征学不到东西"
