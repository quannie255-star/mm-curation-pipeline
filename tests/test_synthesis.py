"""合成器测试：1→N 形状、溯源可归因、不改动入参、noop 丢弃、确定性。

## 为什么这些断言长这样
每条都对应2026-10-05 实跑时**真实抓到**的缺陷，不是设想的：

- `test_ids_globally_unique`：撞名会让去重算子把第二条当重复删掉，
  增强样本被**静默吃掉**，manifest 的n_synthesized 与实际留存量对不上。
- `test_noop_samples_are_dropped`：合成器对已合规样本正确地什么都不做，
  但照样产出就等于「合成量虚增一倍」，而报表还好看。
- `test_augment_does_not_mutate_input`：入参被改会让「同口径对照」失去意义。
- `test_synthesized_never_dirty`：增强样本带 dirty 标记 → 检出率口径不可比。
- `test_deterministic_same_seed`：不可复跑的门禁/对照实验一律不作数。
"""

from __future__ import annotations

import copy

import pytest

from mm_curation.operators.base import Sample
from mm_curation.synthesis import SynthesisPlan, available_synthesizers
from mm_curation.synthesis.base import build_synthesizers

TEXT_KINDS = ["text_deredup", "text_paraphrase", "text_truncate_repair"]


@pytest.fixture
def text_samples():
    """20 条中文文本样本（不依赖网络与真实数据）。"""
    return [
        Sample(
            id=f"news_{i:04d}",
            text="数据平台建设需要统一指标口径，并且链路可追溯，最终交付可复核。",
            modality="text_article",
        )
        for i in range(20)
    ]


def _plan(tmp_path, **kw):
    base = dict(
        augment_per_sample=2,
        coverage=0.5,
        seed=42,
        kinds={k: 1.0 for k in TEXT_KINDS},
    )
    base.update(kw)
    return SynthesisPlan(**base)


# ---------------------------------------------------------------------------
# 注册与计划形状
# ---------------------------------------------------------------------------


def test_all_registered_kinds_are_importable():
    kinds = available_synthesizers()
    assert "text_deredup" in kinds
    assert "text_paraphrase" in kinds
    # 2026-10-05：text_typo_fix 已删除——它对应一个本项目不存在的污染形态
    assert "text_typo_fix" not in kinds


def test_unknown_kind_raises():
    """未注册的 kind 必须报错，不能静默跳过。

    静默跳过的后果：「我配了 5 种增强」实际只跑 4种，
    而 manifest 里看不出来——数量对不上但没人报错。
    """
    with pytest.raises(ValueError, match="未注册的增强类型"):
        build_synthesizers({"no_such_kind": 1.0})


def test_zero_kinds_raises(tmp_path):
    """★ 校验顺序：空样本集先于 kinds 被检查。

    第一版这个函数忘了接 `tmp_path` 参数（pytest 把它当fixture 传进来），
    于是报的是 `TypeError` 而不是预期的 `ValueError`——
    **红灯指向错的地方，等于没有红灯。**
    """
    with pytest.raises(ValueError, match="kinds 为空"):
        SynthesisPlan(kinds={}).run(
            [Sample(id="a", text="x。", modality="text_article")], images_out=tmp_path
        )


def test_empty_samples_raises(tmp_path):
    with pytest.raises(ValueError, match="样本集为空"):
        SynthesisPlan(kinds={"text_deredup": 1.0}).run([], images_out=tmp_path)


@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        ({"augment_per_sample": 0}, "augment_per_sample"),
        ({"coverage": 0}, "coverage"),
        ({"coverage": 1.5}, "coverage"),
        ({"kinds": {}}, "kinds 为空"),
    ],
)
def test_invalid_params_raise(tmp_path, kw, msg):
    """参数校验必须在 run 里报错，不能等到下游才炸。

    注意：校验顺序是「先空列表、再 kinds、最后数值参数」，
    所以这里必须给一个**非空**样本集，否则会先撞上「样本集为空」。
    第一版我传了空列表，于是三个数值参数的用例全都撞在同一个错上——
    测试红了但原因不是被测的那件事。**红灯指向错的地方，等于没有红灯。**
    """
    s = [Sample(id="a", text="x。", modality="text_article")]
    with pytest.raises(ValueError, match=msg):
        SynthesisPlan(
            **{
                **dict(augment_per_sample=2, coverage=0.5, seed=1, kinds={"text_deredup": 1.0}),
                **kw,
            }
        ).run(copy.deepcopy(s), images_out=tmp_path)


# ---------------------------------------------------------------------------
# 1→N 与溯源
# ---------------------------------------------------------------------------


def test_one_to_n_expansion(tmp_path, text_samples):
    out, manifest = _plan(tmp_path).run(copy.deepcopy(text_samples), images_out=tmp_path / "img")
    assert len(out) == len(text_samples) + manifest["n_synthesized"]
    assert manifest["n_synthesized"] > 0, "计划必须真的产出增强样本"
    assert manifest["n_source"] == len(text_samples)
    assert manifest["n_covered"] == round(len(text_samples) * 0.5)


def test_ids_globally_unique(tmp_path, text_samples):
    """★ 撞名会让去重算子静默吃掉增强样本（2026-10-05 实跑抓到）。"""
    out, _ = _plan(tmp_path, augment_per_sample=3).run(
        copy.deepcopy(text_samples), images_out=tmp_path / "img"
    )
    ids = [s.id for s in out]
    assert len(ids) == len(set(ids)), "合成样本 id 撞名了：去重算子会把它们当重复删掉"


def test_provenance_triple_present(tmp_path, text_samples):
    """每条增强样本都必须能说清「谁做的、从哪来、是不是干净的」。"""
    out, _ = _plan(tmp_path).run(copy.deepcopy(text_samples), images_out=tmp_path / "img")
    synth = out[len(text_samples) :]
    assert synth
    known = {s.id for s in text_samples}
    for s in synth:
        assert s.labels["synthesized_by"] in available_synthesizers()
        assert s.labels["source_id"] in known, "source_id 必须指向真实存在的源样本"
        assert s.labels["clean"] is True


def test_synthesized_never_dirty(tmp_path, text_samples):
    """红线：增强样本绝不能带 dirty 标记，否则检出率口径不可比。"""
    out, _ = _plan(tmp_path).run(copy.deepcopy(text_samples), images_out=tmp_path / "img")
    assert all(not s.labels.get("dirty") for s in out)


def test_augment_does_not_mutate_input(tmp_path, text_samples):
    """入参不许被改——否则「原集 vs 增强集」无法做同口径对照。"""
    before = copy.deepcopy(text_samples)
    _plan(tmp_path).run(text_samples, images_out=tmp_path / "img")
    assert text_samples == before


# ---------------------------------------------------------------------------
# noop 丢弃（合成量不能虚增）
# ---------------------------------------------------------------------------


def test_noop_samples_are_dropped(tmp_path, text_samples):
    """★「什么都没改」的样本必须丢弃，否则合成量虚增一倍（实跑抓到）。

    这里**只用必然 noop 的增强器**（已合规文本：deredup 无叠字），
    而不用 paraphrase——paraphrase 会真的轮换分句，它不是 noop
    （第一版误以为三个都 noop，测试红了；也有第二版忘了接text_samples 参数，
    pytest 把 fixture 函数当样本传进去，报的是 TypeError 而非断言失败——
    **红灯指向错的地方，等于没有红灯**）。

    paraphrase 的有效性由test_paraphrase_is_not_noop 单独锁住，
    这样「丢 noop」与「不丢有效增强」两侧都有断言。
    """
    out, manifest = _plan(
        tmp_path, coverage=1.0, augment_per_sample=2, kinds={"text_deredup": 1.0}
    ).run(copy.deepcopy(text_samples), images_out=tmp_path / "img")
    assert manifest["n_noop"] == manifest["n_attempt"], "已合规输入经 deredup 应当全部 noop"
    assert manifest["n_synthesized"] == 0
    assert len(out) == len(text_samples), "不该产出任何没变化的增强样本"


def test_paraphrase_is_not_noop(tmp_path, text_samples):
    """反向对照：paraphrase 真的改了文本，所以它不该被当 noop 丢掉。

    上一条测试只锁 deredup；这一条确保「丢 noop」不是把有效增强也一起丢了。
    """
    out, manifest = _plan(
        tmp_path, coverage=1.0, augment_per_sample=1, kinds={"text_paraphrase": 1.0}
    ).run(copy.deepcopy(text_samples), images_out=tmp_path / "img")
    assert manifest["n_synthesized"] == len(text_samples)
    by = {s.id: s for s in text_samples}
    assert all(s.text != by[s.labels["source_id"]].text for s in out[len(text_samples) :])


def test_manifest_counts_are_self_consistent(tmp_path, text_samples):
    out, m = _plan(tmp_path).run(copy.deepcopy(text_samples), images_out=tmp_path / "img")
    assert m["n_attempt"] == m["n_synthesized"] + m["n_noop"]
    assert sum(m["counts"].values()) == m["n_synthesized"]
    assert all(v > 0 for v in m["counts"].values()), "counts 里不该有 0 项"


def test_changed_false_yields_no_output(tmp_path):
    """单点验证：已合规文本经truncate_repair 后不应产出样本。"""
    s = [Sample(id="a", text="已经完整了。", modality="text_article")]
    out, m = _plan(tmp_path, coverage=1.0, kinds={"text_truncate_repair": 1.0}).run(
        copy.deepcopy(s), images_out=tmp_path / "img"
    )
    assert m["n_synthesized"] == 0 and len(out) == 1


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


def test_deterministic_same_seed(tmp_path, text_samples):
    """同seed 跑两次：id序列与manifest 必须逐条一致。"""
    kw = dict(augment_per_sample=2, coverage=0.5, seed=7, kinds={k: 1.0 for k in TEXT_KINDS})
    a, ma = SynthesisPlan(**kw).run(copy.deepcopy(text_samples), images_out=tmp_path / "i1")
    b, mb = SynthesisPlan(**kw).run(copy.deepcopy(text_samples), images_out=tmp_path / "i2")
    assert [s.id for s in a] == [s.id for s in b]
    assert ma == mb


def test_different_seed_changes_ids(tmp_path, text_samples):
    """不同 seed 应产生不同的增强选择——否则 seed 形同虚设。"""
    kw = dict(augment_per_sample=1, coverage=1.0, kinds={k: 1.0 for k in TEXT_KINDS})
    a, _ = SynthesisPlan(seed=1, **kw).run(copy.deepcopy(text_samples), images_out=tmp_path / "i1")
    b, _ = SynthesisPlan(seed=2, **kw).run(copy.deepcopy(text_samples), images_out=tmp_path / "i2")
    assert [s.id for s in a] != [s.id for s in b]


# ---------------------------------------------------------------------------
# 各增强器的具体行为（逐条锁住，避免被"优化"掉）
# ---------------------------------------------------------------------------


def test_deredup_keeps_normal_repetition(tmp_path):
    """正常叠词必须保留，只压真正的刷字——否则会误伤「重要」「看看」。

    ★ 用例直接照抄污染器 `LowQualityText` 的 repeat 变体实现
    （`text[:8] + "哈"*30`，见 contamination/impl.py），
    而不是手写一个「看起来像」的形态：
    我第一版写的是 `"这很重要，哈" * 30`——那是**片段重复**不是叠字，
    于是测试红了，而实现其实是对的。
    **造一个项目里不存在的形态去测，测的就不是同一件事。**
    """
    import random

    from mm_curation.synthesis.base import SynthesisContext
    from mm_curation.synthesis.impl import DeRedup

    polluted = "数据平台建设需要" + "哈" * 30
    s = Sample(id="x", text=polluted, modality="text_article")
    out = DeRedup().apply(copy.deepcopy(s), 0, SynthesisContext([s], tmp_path, random.Random(0)))
    assert out.text == "数据平台建设需要" + "哈" * 4, "刷字应被压到阈值 4"
    assert out.meta["augment"]["changed"] is True


def test_deredup_keeps_normal_word_repetition(tmp_path):
    """两个字的正常叠词（重要 / 看看）不许被压掉——那是误伤。"""
    import random

    from mm_curation.synthesis.base import SynthesisContext
    from mm_curation.synthesis.impl import DeRedup

    s = Sample(id="x", text="这很重要，我们来看看。", modality="text_article")
    out = DeRedup().apply(copy.deepcopy(s), 0, SynthesisContext([s], tmp_path, random.Random(0)))
    assert out.text == "这很重要，我们来看看。"
    assert out.meta["augment"]["changed"] is False


def test_paraphrase_does_not_break_sentence(tmp_path):
    """★ 旋转分句时不能把句号夹到中间（实跑产出过 '可追溯。，数据平台建设'）。"""
    import random

    from mm_curation.synthesis.base import SynthesisContext
    from mm_curation.synthesis.impl import Paraphrase

    s = Sample(id="x", text="甲，乙，丙。", modality="text_article")
    out = Paraphrase().apply(copy.deepcopy(s), 0, SynthesisContext([s], tmp_path, random.Random(0)))
    assert "。，" not in out.text
    assert out.text.endswith("。"), "句末标点必须仍在句尾"
    # 分句顺序被旋转：乙丙甲（原：甲乙丙）
    assert "乙，丙，甲" in out.text
    # 词袋不变（这是「哈希变、语义不变」的根据）。
    # 只断言主体三字都在——连接词是**随机三选一**，
    # 第一版我把三个候选连接词全写进期望值，于是稳定红；
    # 那不是实现的问题，是我没意识到它随机。
    assert all(ch in out.text for ch in "甲乙丙")


def test_paraphrase_keeps_connection_word_single(tmp_path):
    """连接词只能有一个，多轮增强不许叠加成「该条目：…，具体而言，该条目：…」。

    ★ 断言数的是**连接词本身**而不是冒号：
    第一版断言 `text.count("：") == 1`，而三个候选连接词里
    `具体而言，` 用的是逗号 —— 于是稳定红。红灯指向了一个不存在的问题。
    """
    import random

    from mm_curation.synthesis.base import SynthesisContext
    from mm_curation.synthesis.impl import Paraphrase

    cur = "甲，乙，丙。"
    for i in range(4):
        ctx = SynthesisContext(
            [Sample(id="x", text=cur, modality="text_article")], tmp_path, random.Random(i)
        )
        cur = Paraphrase().apply(Sample(id=f"x{i}", text=cur, modality="text_article"), i, ctx).text
    n = sum(cur.count(c) for c in Paraphrase._CONNECTORS)
    assert n == 1, f"连接词叠加了 {n} 个：{cur!r}"


def test_paraphrase_single_clause_untouched(tmp_path):
    """只有一个分句时不硬造变化——changed 必须为 False。"""
    import random

    from mm_curation.synthesis.base import SynthesisContext
    from mm_curation.synthesis.impl import Paraphrase

    s = Sample(id="x", text="只有一句。", modality="text_article")
    out = Paraphrase().apply(copy.deepcopy(s), 0, SynthesisContext([s], tmp_path, random.Random(0)))
    assert out.meta["augment"]["changed"] is False
    assert out.text == "只有一句。"


def test_truncate_repair_only_adds_punctuation(tmp_path):
    """只补标点，绝不追加字词——追加就是编内容。"""
    import random

    from mm_curation.synthesis.base import SynthesisContext
    from mm_curation.synthesis.impl import TruncateRepair

    s = Sample(id="x", text="链路可追", modality="text_article")
    out = TruncateRepair().apply(
        copy.deepcopy(s), 0, SynthesisContext([s], tmp_path, random.Random(0))
    )
    assert out.text == "链路可追。"
    assert len(out.text) == len(s.text) + 1, "只允许多一个标点"
