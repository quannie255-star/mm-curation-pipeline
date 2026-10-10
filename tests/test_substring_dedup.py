"""`substring_dedup` 的测试：算法正确性 + 跨篇语义 + 阈值行为。

为什么这组测试值得存在：跨文档精确子串去重的**语义很容易写歪**，
而且写歪了**不报错** —— 它只会安静地少删或多删数据。
本文件在实现过程中确实抓到了一个「两篇逐字相同却完全漏检」的真bug
（判据看的是「子串起终点是否跨篇」，而重复子串长度可能远小于单篇长度）。
所以这里的每条用例都对应一种**可能的歪法**。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages/curation-eval/src"))

import mm_curation.operators  # noqa: E402,F401  导入即注册
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.operators.substring_dedup import (  # noqa: E402
    SubstringDedup,
    build_suffix_array,
    find_long_repeated_cross,
    kasai_lcp,
)


def _naive_sa(s: str) -> list[int]:
    return sorted(range(len(s)), key=lambda i: s[i:])


def _naive_lcp(a: str, b: str) -> int:
    k = 0
    while k < len(a) and k < len(b) and a[k] == b[k]:
        k += 1
    return k


def mk(i: int, text: str) -> Sample:
    return Sample(id=f"s{i}", text=text, modality="text_article")


BOILER = "本作品采用知识共享许可协议禁止商业使用未经授权转载" * 4  # 100 字


# ───────────────────────── 算法层：与朴素实现对照 ─────────────────────────


@pytest.mark.parametrize("trial", range(12))
def test_suffix_array_matches_naive(trial: int) -> None:
    """后缀数组必须与「把所有后缀排序」的朴素实现逐位一致。"""
    rng = random.Random(1000 + trial)
    s = "".join(rng.choice("ab c") for _ in range(rng.randint(1, 40)))
    assert build_suffix_array(s) == _naive_sa(s)


@pytest.mark.parametrize("trial", range(12))
def test_lcp_matches_naive(trial: int) -> None:
    """Kasai 算法的 height 必须与逐对朴素求 LCP 一致。"""
    rng = random.Random(2000 + trial)
    s = "".join(rng.choice("ab") for _ in range(rng.randint(1, 45)))
    sa = build_suffix_array(s)
    height = kasai_lcp(s, sa)
    expect = [0] + [_naive_lcp(s[sa[i]:], s[sa[i - 1]:]) for i in range(1, len(s))]
    assert height == expect


def test_suffix_array_handles_empty_and_single() -> None:
    """边界：空串与单字符不许崩（空串是「分块降级路径」的常见输入）。"""
    assert build_suffix_array("") == []
    assert kasai_lcp("", []) == []
    assert build_suffix_array("a") == [0]
    assert kasai_lcp("a", [0]) == [0]


# ───────────────────────── 跨篇语义：每条对应一种歪法 ─────────────────────────


def test_detects_shared_boilerplate_across_docs() -> None:
    """两篇只共享一段 100 字许可证原话、其余不同 → 后者判重。"""
    samples = [
        mk(0, "开头甲" + BOILER + "结尾甲独有内容一二三四五六七八九十甲"),
        mk(1, "开头乙" + BOILER + "结尾乙独有内容甲乙丙丁戊己庚辛壬癸乙"),
        mk(2, "完全无关的一篇正文内容" * 8),
    ]
    kept = {s.id for s in SubstringDedup(min_chars=50).run_batch(samples)}
    assert kept == {"s0", "s2"}


def test_single_doc_self_repetition_is_not_cross_doc_dup() -> None:
    """单篇内部复读是 `char_repetition` 的职责，跨篇去重不得越界。

    这条判据若写错（只看「有没有长子串」），会把复读文档误杀。
    """
    samples = [
        mk(0, "abcabcabc" * 20 + "独特结尾文字xyz"),
        mk(1, "另一篇完全不同的正文内容ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    ]
    kept = {s.id for s in SubstringDedup(min_chars=30).run_batch(samples)}
    assert kept == {"s0", "s1"}


def test_identical_docs_are_deduped() -> None:
    """两篇逐字相同 → 只留第一篇。

    ⚠️ 这条是用例里最关键的一条：重复子串的长度可能**远小于**单篇长度，
    若判据写成「子串起点与终点是否落在不同文档」，这里会**完全漏检**
    （实现时真实发生过，见模块 docstring）。
    """
    dup = "完全一样的内容" * 20
    kept = {s.id for s in SubstringDedup(min_chars=30).run_batch([mk(0, dup), mk(1, dup)])}
    assert kept == {"s0"}


def test_three_docs_share_one_block() -> None:
    """三篇共享同一段 → 只留第一篇（先到先保留约定）。"""
    kept = {
        s.id
        for s in SubstringDedup(min_chars=50).run_batch(
            [mk(0, BOILER + "甲"), mk(1, BOILER + "乙"), mk(2, BOILER + "丙")]
        )
    }
    assert kept == {"s0"}


def test_docs_without_shared_block_all_kept() -> None:
    """没有共享 → 一条都不许删（防过度清洗）。"""
    samples = [mk(0, "第一篇讲天文与星座" * 6), mk(1, "第二篇讲化学与元素" * 6)]
    assert {s.id for s in SubstringDedup(min_chars=50).run_batch(samples)} == {"s0", "s1"}


def test_attribution_points_to_the_kept_doc() -> None:
    """被丢的样本必须写明「重复自谁」，否则no-silent-filter 判据无法成立。"""
    samples = [mk(0, "开头甲" + BOILER + "结尾甲"), mk(1, "开头乙" + BOILER + "结尾乙")]
    kept_ids = {s.id for s in SubstringDedup(min_chars=50).run_batch(samples)}
    dropped = [s for s in samples if s.id not in kept_ids]
    assert len(dropped) == 1
    meta = dropped[0].meta.get("dedup:substring_dedup")
    assert meta == {"duplicate_of": "s0"}


# ───────────────────────── 阈值行为（决策 C2 的前提）─────────────────────────


def test_threshold_is_monotonic_in_drop_count() -> None:
    """阈值越低 → 判重越多（单调性）。

    这条是「阈值可标定」的前提：若不单调，做多档对照实验没有意义
    —— 无法把「调高阈值」翻译成「更保守」。

    ⚠️ 判据方向：**阈值升高 → 丢弃数单调不增**，所以正确的断言是
    `drops == sorted(drops, reverse=True)`。
    第一版写成 `sorted(drops)`（升序）—— 于是判据在**实现完全正确**时变红。
    这是本项目的常见陷阱：**先问「正确实现下会不会误判」**。
    """
    a = "开头甲" + BOILER + "结尾甲独有内容一二三四五六七八九十甲"
    b = "开头乙" + BOILER + "结尾乙独有内容甲乙丙丁戊己庚辛壬癸乙"
    c = "开头丙" + BOILER + "结尾丙独有内容子丑寅卯辰巳午未申酉甲"

    drops = []
    for mc in (20, 40, 60, 80):
        samples = [mk(0, a), mk(1, b), mk(2, c)]
        drops.append(3 - len(SubstringDedup(min_chars=mc).run_batch(samples)))
    assert drops == sorted(drops, reverse=True), (
        f"阈值升高时丢弃数必须单调不增，实际 {drops}"
    )
    # 并且两端必须真的分开（恒定的序列说明阈值根本没起作用）
    assert drops[0] > drops[-1], f"阈值两端必须有差异，否则阈值是摆设：{drops}"


def test_threshold_above_shared_length_keeps_all() -> None:
    """阈值高于共享长度 → 不判重（说明判据真的在用阈值，不是恒真）。"""
    samples = [mk(0, "甲" + BOILER + "甲尾"), mk(1, "乙" + BOILER + "乙尾")]
    # BOILER 长度从常量**现取**，不硬编码 —— 硬编码会在改文案后静默失效
    assert len(SubstringDedup(min_chars=len(BOILER) + 50).run_batch(samples)) == 2
    # 反向对照：阈值低于共享长度时必须判重（否则上面那条可能是恒真）
    assert len(SubstringDedup(min_chars=len(BOILER) // 2).run_batch(samples)) == 1


# ───────────────────────── 不越界：既有 config 行为不变 ─────────────────────────


def test_operator_is_registered_with_expected_metadata() -> None:
    """注册元数据：只在text_article 上评、批级、非分片。"""
    from curation_eval.registry import get_operator_class

    meta = get_operator_class("substring_dedup").meta
    assert meta.modalities == frozenset({"text_article"})
    assert meta.shardable is False
    assert meta.superlinear is True


def test_short_docs_bypass_the_algorithm() -> None:
    """短于 2×min_chars 的文档不进拼接串（避免制造噪声匹配）。"""
    samples = [mk(0, "太短"), mk(1, "也太短")]
    assert {s.id for s in SubstringDedup(min_chars=200).run_batch(samples)} == {"s0", "s1"}


def test_find_long_repeated_cross_requires_two_docs() -> None:
    """单篇时不该报「跨篇重复」（分母为零的守卫）。"""
    one = "内容" * 100
    bounds = [(0, len(one))]
    assert find_long_repeated_cross(one + "", 10, bounds) == []
