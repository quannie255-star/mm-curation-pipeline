"""数据集切分契约测试（本项目最容易被"看起来实现了"骗过的地方）。

⚠️ 全部判据都来自**一次真实 bug**：`assign_split` 第一版把 `sample_id`
   拼进了 hash basis，导致**分层完全失效但看起来是实现了的**
   （实测 25 个 symbol 里 21 个跨 split）—— 指标正常、报告照出、
   静默失效。这类 bug 必须由测试钉住，不能靠"读代码觉得对"。
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# E402: sys.path 注入必须在 import 前（这是仓库既有约定，见其他测试）
from mm_curation.dataset import (  # noqa: E402
    assign_split,
    estimate_jaccard,
    minhash_signature,
)

# ── 分层切分：同一分层键的样本必须同split ────────────────────────────────


def test_同一样本id_永远落进同一split():
    """确定性：可复现是数据系统的底线。"""
    a = [assign_split(f"s{i}", 0.1, 0.1) for i in range(200)]
    b = [assign_split(f"s{i}", 0.1, 0.1) for i in range(200)]
    assert a == b, "同输入两次调用结果不同 → 不可复现"


def test_分层键生效时同键必同split():
    """给了 key 就**只按 key 分**——这条是第一版真bug 的判据。

    第一版实现是 `basis = f"{key}::{sample_id}"`，每条样本各算自己的
    hash → 同一 symbol 的不同样本必然落到不同桶（实测 21/25 symbol 跨 split）。
    """
    by_key = defaultdict(set)
    for i in range(300):
        by_key["600276"].add(assign_split(f"s{i}", 0.1, 0.1, "600276"))
    assert len(by_key["600276"]) == 1, (
        f"同一 symbol 分到了多个 split：{by_key['600276']} —— 分层失效，泄漏的是实体而非样本"
    )


def test_分层键_none_时退化为按样本分():
    """反向判据：不传 key 就该按样本分（跨 split 是预期的）。

    ⚠️ 这条是为了防止**判据恒真**：如果有无 key 的行为一样，
    那上面那条测试就是恒真的（它只测"有 key"的分支）。
    """
    by_key = defaultdict(set)
    for i in range(300):
        by_key["600276"].add(assign_split(f"s{i}", 0.1, 0.1, None))
    assert len(by_key["600276"]) > 1, "传key=None 与传 key 行为相同 → 上面的分层测试可能是恒真的"


def test_切分比例大致符合配置():
    """比例判据不能太严（hash 分桶有波动），但也不能恒真。"""
    n = 4000
    c = defaultdict(int)
    for i in range(n):
        c[assign_split(f"s{i}", 0.1, 0.1)] += 1
    for split, target in (("val", 0.1), ("test", 0.1), ("train", 0.8)):
        got = c[split] / n
        assert abs(got - target) < 0.05, f"{split} 实际 {got:.3f}，目标 {target}"


# ── MinHash 签名与泄漏判定 ──────────────────────────────────────────────


def test_签名相同文本的相似度为1():
    a = minhash_signature("这是一段测试文本，用于验证签名的一致性。")
    assert estimate_jaccard(a, a) == 1.0


def test_签名长度恒定且不一致时raise而非静默返回0():
    """静默返回 0 会被读成「不相似」→ 漏检泄漏。必须 raise。

    ⚠️ 这条测试第一版写错了：假设「短文本 vs 长文本」会得到不同长度的签名。
    实际 `minhash_signature` 的 `num_perm` 是**固定参数**，签名长度恒为
    num_perm×8 字节，与文本长度无关 → 那个假设造出的输入根本触发不了
    异常，是**恒真的测试**（它没在测它声称在测的东西）。
    改为直接构造长度不同的两个签名字节串。
    """
    from mm_curation.dataset.build import estimate_jaccard as ej

    a = minhash_signature("文本")
    assert len(a) == 64 * 8, "签名长度应恒为 num_perm × 8"
    with pytest.raises(ValueError, match="签名长度"):
        ej(a, a[:-8])


def test_完全不同的文本相似度低():
    a = minhash_signature("今天股市行情不错，上证指数上涨。")
    b = minhash_signature("公司在本次交易中披露了回购股份的进展公告。")
    assert estimate_jaccard(a, b) < 0.5, "无关文本被判为高相似 → MinHash 失效"


# ── 产物契约：manifest 里不许出现占位 ──────────────────────────────────


def test_已产出manifest的必填字段非空且自洽():
    """检查真实产出的 manifest（若存在）。

    纪律：manifest 是唯一真相源，**不能有占位数字**。
    """
    mf = ROOT / "datasets" / "finance_news_v1" / "manifest.json"
    if not mf.exists():
        pytest.skip("尚未构建数据集")
    m = json.loads(mf.read_text(encoding="utf-8"))
    assert m["n_samples"] > 0, "manifest 的样本数为 0 —— 疑似占位"
    assert m["n_tokens"] > 0
    assert m["n_shards"] == len(m["shard_checksums"]), "shard 数与校验和条目不一致"
    assert m["n_chars"] >= m["n_tokens"], "字符数少于 token 数（中文不可能）"
    assert sum(m["splits"].values()) == m["n_samples"], (
        f"切分合计 {sum(m['splits'].values())} != 样本数 {m['n_samples']}"
    )
    # training_runs 必须是空 list 或真实结果，**不能是占位字典**
    for run in m["training_runs"]:
        assert "ppl" in run or "metric" in run, f"训练结果缺实测指标：{run}"
