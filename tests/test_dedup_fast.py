"""向量化文本去重（dedup_fast）单测：预聚类保真、LSH 捕获率、先到先保留。

背景：β 基准实测揪出两个静默正确性缺陷——
1) 超大 LSH 桶整桶跳过会连带牺牲桶内真重复（签名预聚类修）；
2) union-find 合并方向不固定会让"先到先保留"退化为随机保留（小索引做根修）。
本文件即这两个修复的回归防线。
"""

from __future__ import annotations

import random

import pytest

from mm_curation.dedup_fast import dedup_texts, exact_text_duplicates
from mm_curation.operators.base import Sample

_TPL = "模板人物{}（），军史研究者，1955年授衔。参考名录、勋章记录、传记资料汇编条目。"


def _build_corpus(seed: int = 3):
    """50 个同模板人物条目（家族簇压力）+ 150 篇常规文 + 注入 30 精确/30 近似。"""
    rng = random.Random(seed)

    def near(t: str) -> str:
        chars = list(t)
        if len(chars) >= 8:
            s = rng.randrange(len(chars) - 7)
            chars.insert(s, "".join(chars[s : s + 8]))
        for i in rng.sample(range(len(chars)), max(1, int(len(chars) * 0.03))):
            chars[i] = ""
        return "".join(chars)

    docs = [_TPL.format(f"姓名甲乙丙{chr(0x4E00 + i)}" * 3) for i in range(50)]
    docs += [
        f"普通文章{i}号：讨论数据工程主题{i % 7}的第{i}个实践细节，"
        + "内容段落" * 40
        + f"结尾{i}。"
        for i in range(150)
    ]
    samples = [Sample(id=f"d{i}", text=t) for i, t in enumerate(docs)]
    near_ids = {f"n{i}" for i in range(30)}
    samples += [Sample(id=f"n{i}", text=near(samples[i * 6].text)) for i in range(30)]
    exact_ids = {f"x{i}" for i in range(30)}
    samples += [Sample(id=f"x{i}", text=samples[i * 6].text) for i in range(30)]
    return samples, near_ids, exact_ids


_CORPUS = _build_corpus()


def test_exact_dup_survives_bucket_skip():
    """签名预聚类：模板簇撑爆 LSH 桶（max_bucket=50）时精确重复仍全召回。"""
    samples, _, exact_ids = _CORPUS
    r = dedup_texts(samples, max_bucket=50)
    dropped = set(r.duplicate_of)
    assert len(exact_ids & dropped) / len(exact_ids) == 1.0


def test_near_dup_recall():
    """默认参数（80 签名 8 band × 10 row）下注入近重复召回 >= 0.8。"""
    samples, near_ids, _ = _CORPUS
    r = dedup_texts(samples)
    dropped = set(r.duplicate_of)
    assert len(near_ids & dropped) / len(near_ids) >= 0.8


def test_first_occurrence_wins():
    """先到先保留：duplicate_of 的值（簇代表）必须在输入顺序上先于键。"""
    samples, _, _ = _CORPUS
    r = dedup_texts(samples)
    pos = {s.id: k for k, s in enumerate(samples)}
    bad = [d for d, src in r.duplicate_of.items() if pos[d] < pos[src]]
    assert not bad, f"随机保留回潮: {bad[:5]}"


def test_num_perm_must_divide_bands():
    samples, _, _ = _CORPUS
    with pytest.raises(ValueError, match="整除"):
        dedup_texts(samples[:10], num_perm=60, bands=8)


def test_exact_text_duplicates_md5_semantics():
    samples, _, _ = _CORPUS
    dup = exact_text_duplicates(samples)
    assert set(dup) == {f"x{i}" for i in range(30)}
    assert dup["x0"] == "d0"


# --- 合成样本 ↔ 声明源的合并豁免（J1 红线的去重侧保障，2026-10-09）---


def _synth(sid: str, source_id: str, body: str):
    return Sample(
        id=sid,
        text=body,
        labels={"synthesized_by": "paraphrase", "source_id": source_id, "clean": True},
    )


_BASE = (
    " alta velocidade 全网首发：某型号压缩机组在本月完成了连续三十天的平稳运行考核，"
    "各项振动与温度指标均处于设计裕度之内，运维团队据此更新了预测性维护计划。"
)


def test_compute_signatures_matches_dedup_internal():
    """拆分后的公共签名函数与 dedup_texts 内部路径同源：同 seed 逐行一致。"""
    import numpy as np

    from mm_curation.dedup_fast import compute_signatures

    samples = [Sample(id=f"s{i}", text=_TPL.format(i) + _BASE) for i in range(6)]
    sigs = compute_signatures(samples, num_perm=80, seed=42)
    assert sigs.shape == (6, 80)
    # 同 seed 重算一致（确定性契约）
    assert np.array_equal(sigs, compute_signatures(samples, num_perm=80, seed=42))
    # 不同 seed 不同签名
    assert not np.array_equal(sigs, compute_signatures(samples, num_perm=80, seed=43))


def test_synthetic_and_its_source_not_merged():
    """合成样本与其声明源必然高相似（否则合成失败）——合并会把增强对照洗掉。"""
    source = Sample(id="src1", text=_BASE + "附录：机组历史运行记录摘要。")
    synth = _synth("syn1", "src1", _BASE + "附录：机组历史运行记录摘要（改写版）。")
    unrelated = Sample(id="raw9", text="完全无关的另一篇正文，讲的是船舶涂层工艺验收规范。")
    res = dedup_texts([source, synth, unrelated], threshold=0.5)
    assert len(res.kept) == 3  # 全部保留：合成 ↔ 源 共存是特性
    assert res.duplicate_of == {}


def test_synthetic_vs_unrelated_raw_still_merged():
    """豁免只保护声明的源；合成样本与无关原样本高相似仍照常去重。"""
    source = Sample(id="src1", text="完全不同的源文本：某机组平稳运行考核与维护计划修订说明。")
    synth = _synth("syn1", "src1", _BASE + "附录：机组历史运行记录摘要（改写版）。")
    # 与 synth 高相似、与 source 无关：
    unrelated = Sample(id="raw9", text=_BASE + "附录：机组历史运行记录摘要。")
    res = dedup_texts([unrelated, source, synth], threshold=0.5)
    # synth ↔ raw9 高相似且 raw9 非其声明源 → 合并（先到先保留，raw9 为根）；
    # 其声明源 src1 文本不同，保留
    assert len(res.kept) == 2
    assert res.duplicate_of.get("syn1") == "raw9"
    assert "src1" not in res.duplicate_of


def test_two_synthetic_samples_still_deduped():
    """两个合成样本互相高相似：合成集内去重照常（豁免不豁免同类）。"""
    a = _synth("synA", "srcA", _BASE + "改写甲：考核结论为平稳运行。")
    b = _synth("synB", "srcB", _BASE + "改写乙：考核结论为平稳运行。")
    res = dedup_texts([a, b], threshold=0.5)
    assert len(res.kept) == 1
    assert set(res.duplicate_of) == {"synB"}


def test_protect_synthetic_off_restores_legacy_behavior():
    """opt-out 开关：关掉豁免后回到历史行为（合成对被合并）——证明默认开启是新语义。"""
    source = Sample(id="src1", text=_BASE + "附录：机组历史运行记录摘要。")
    synth = _synth("syn1", "src1", _BASE + "附录：机组历史运行记录摘要（改写版）。")
    res = dedup_texts([source, synth], threshold=0.5, protect_synthetic=False)
    assert len(res.kept) == 1  # 历史行为：高相似即合并
