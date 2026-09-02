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
