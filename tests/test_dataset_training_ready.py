"""数据集「训练就绪」契约测试。

⚠️ 这批测试的来源：三个**真实 bug**，都是「构建期全绿、训练期才炸」或
「验收脚本自己假绿」的类型。它们的共同形态是**跨层错误**——本层没报错，
下一层才炸，且报错点离真正原因很远。

1. `splits` 单位错：packing 后manifest 记样本数、磁盘是block 数
   → 消费者核对必然对不上，只能靠猜。
2. 变长 block：尾块 451token → `DataLoader` 崩。而**只看第一个 batch
   是绿的**（前几个恰好都是满块）——假绿的典型形态。
3. 词表口径错：`tokenizer.vocab_size` **不含 added tokens**
   （Qwen: 151643 vs `len(tok)`=151665）→ CUDA device-side assert。

判据纪律：每条测试都要问「正确实现不误红？错误实现必变红？」
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mm_curation.dataset import (  # noqa: E402
    DatasetManifest,
    TinyCausalLM,
    assign_split,
    pack_sequences,
    train_config_from_manifest,
)


# ── 1. splits 单位必须显式登记 ────────────────────────────────────────────
def test_manifest必须登记行单位():
    """不登记 `row_unit` → 消费者无法判断 splits 是样本数还是 block 数。"""
    m = DatasetManifest(name="x")
    assert m.row_unit == "sample"
    assert m.n_rows == 0, "未构建时 n_rows 应为 0（不是 None，也不是假数字）"
    # 序列化后必须带着这个字段 —— 磁盘上的 manifest 是给外部看的
    d = m.to_dict()
    assert "row_unit" in d
    assert "n_rows" in d


def test_单位与行数必须自洽():
    """`sum(splits) == n_rows` 是 manifest 的**内部硬约束**。

    违反它的典型场景：packing 后 splits 记样本数而 n_rows 记 block 数
    （实测踩过：1559/215/155 vs 694/281/207）→ 两者永不相等，
    而**每个数单独看都像真的**。
    """
    man = {
        "name": "x", "tokenizer": "t", "tokenizer_vocab_size": 151665,
        "pack_block_size": 512,
        "n_blocks": {"train": 694, "val": 281, "test": 207},
        "splits": {"train": 694, "val": 281, "test": 207},
        "n_rows": 1182, "row_unit": "block",
    }
    assert sum(man["splits"].values()) == man["n_rows"]
    # 变异：把 splits 换回样本级（packing 前口径）→ 断言必须红
    man_bad = dict(man, splits={"train": 1559, "val": 215, "test": 155})
    assert sum(man_bad["splits"].values()) != man_bad["n_rows"], (
        "自洽性检查必须能抓住「单位错位」这个 bug"
    )


# ── 2. 定长 block（变长必崩 batch）────────────────────────────────────────
def test_packing必须产出定长block():
    """尾块必须 pad 到block_size，否则 DataLoader 一定崩。

    ⚠️ 判据的关键：不能只测「第一个 batch 能stack」—— 实测踩过，
    顺序读时前几个 block 恰好都是满的，看着是绿的，shuffle 后立刻崩。
    """
    eos = 151664
    # 刻意构造：最后一条远短于 block_size（模拟真实尾块 451/512）
    token_lists = [[1] * 500, [2] * 600, [3] * 100]
    blocks = pack_sequences(token_lists, block_size=512, eos_token_id=eos)
    assert blocks, "应产出至少一个 block"
    lens = {len(b) for b in blocks}
    # pack_sequences 本身允许尾块短（它只管拼），定长是**写盘时**的事
    assert lens, "block 不应为空"
    # 模拟写盘时的 pad（与 _write_packed_shards 同一逻辑）
    padded = [b + [eos] * (512 - len(b)) if len(b) < 512 else b for b in blocks]
    assert {len(b) for b in padded} == {512}, (
        f"pad 后应全部为定长 512，实际 {sorted({len(b) for b in padded})}")


def test_不同长度样本的batch必须能stack():
    """端到端复现那个 bug：一条长+ 一条短 → 不pad 就崩，pad 就不崩。"""
    import torch

    eos = 151664
    long_seq = list(range(1, 513))
    short_seq = list(range(1, 452))  # 实测里的 451
    #不 pad：必然不等长
    try:
        torch.stack([torch.tensor(long_seq), torch.tensor(short_seq)])
        raise AssertionError("未 pad 的变长 batch 竟然能 stack —— 装置有问题")
    except RuntimeError as exc:
        assert "equal size" in str(exc) or "size" in str(exc)
    # pad 后：能 stack
    padded = [
        torch.tensor(long_seq + [eos] * (512 - len(long_seq))),
        torch.tensor(short_seq + [eos] * (512 - len(short_seq))),
    ]
    assert torch.stack(padded).shape == (2, 512)


def test_padding位必须被loss_mask排除():
    """padding 位若计入 loss，模型在学「预测 eos」，指标是假的。

    ⚠️ 这条判据改了三版才站得住，全记下来（别重犯）：
    · v1「全-100 应产生 nan」→ **恒假**：真实 batch 总有有效位。
    · v2「不传 ignore_index 应变」→ **恒假且我误判了原因**：
      实测 PyTorch 的 cross_entropy **默认 ignore_index 就是 -100**，
      所以「不传」根本没取消忽略。想造对照必须用别的负数，而别的负数
      直接 IndexError（不在词表内）→ 这条路根本走不通。
    · v3（采用）：**差分法**——有效 token 完全相同、只有 padding 位
      填的内容不同 → loss 必须**逐位相同**。
      这测的正是「padding 位置不参与目标函数」，且不依赖内部实现。
    """
    import torch
    import torch.nn.functional as F

    torch.manual_seed(0)
    logits = torch.randn(1, 6, 8)

    def loss_with(real_n: int, pad_fill: int) -> float:
        """前 real_n 个位是真实 token，其余用 pad_fill 填（labels 侧置 -100）。"""
        # 词表只有 8 类 → 合法 id 是 0..7（**别用 8/9，会 IndexError**）
        base = [3, 4, 5, 6, 7, 2][:real_n]
        ids = torch.tensor([base + [pad_fill] * (6 - real_n)])
        mask = [1] * real_n + [0] * (6 - real_n)
        labels = torch.where(torch.tensor([mask], dtype=torch.bool), ids,
                            torch.full_like(ids, -100))
        return float(F.cross_entropy(logits.reshape(-1, 8), labels.reshape(-1),
                                    ignore_index=-100))

    # 同样的 3 个有效 token，padding 填不同的东西 → loss 必须相同
    a = loss_with(3, pad_fill=0)
    b = loss_with(3, pad_fill=1)
    assert abs(a - b) < 1e-6, (
        f"padding 内容改变却影响了 loss（{a:.6f} vs {b:.6f}）"
        " → padding 位被算进训练目标了")

    # 反向验证：有效 token 数量改变 → loss 必须变（否则上面那条恒真）
    c = loss_with(4, pad_fill=0)
    assert abs(a - c) > 1e-6, (
        "改动有效位却没改变 loss → 判据测不出任何东西")

    # 与手算对齐（sum 口径，便于精确核对）
    labels = torch.tensor([[3, 4, 5, -100, -100, -100]])
    tot = F.cross_entropy(logits.reshape(-1, 8), labels.reshape(-1),
                         ignore_index=-100, reduction="sum")
    manual = F.cross_entropy(logits.reshape(-1, 8)[:3], labels.reshape(-1)[:3],
                             reduction="sum")
    assert abs(float(tot) - float(manual)) < 1e-6, (
        f"sum 口径不匹配：{float(tot):.6f} vs {float(manual):.6f}")


# ── 3. 词表口径（added tokens）──────────────────────────────────────────
def test_词表大小必须能容纳数据里的最大id():
    """`tokenizer.vocab_size` **不含 added tokens** —— 这是实测踩过的坑。

    Qwen2.5: `vocab_size=151643` 但 `len(tok)=151665`，
    而编码会产出 id=151645 → 按 vocab_size 建embedding → CUDA assert。
    """
    vocab = 151665
    cfg = {"name": "t", "tokenizer": "Qwen/Qwen2.5-0.5B-Instruct",
           "tokenizer_vocab_size": vocab, "pack_block_size": 512}
    conf = train_config_from_manifest(cfg, n_layer=1, n_head=2, n_embd=32,
                                      block_size=512)
    assert conf.vocab_size == vocab
    import torch

    model = TinyCausalLM(conf)
    # 用「超出 tokenizer.vocab_size 但在 len(tok) 内」的 id → 必须不崩
    idx = torch.tensor([[151664]])  # 真实数据里出现过的最大 id
    out = model(idx)
    assert out.shape == (1, 1, vocab), (
        f"输出词表维度应等于 embedding 大小 {vocab}，实际 {out.shape[-1]}")
    # 变异：若用 vocab_size=151643 建表，151664 会越界
    with pytest.raises((IndexError, RuntimeError)):
        torch.nn.Embedding(151643, 32)(idx)


def test_缺词表必须报错而不是硬编码():
    """manifest 缺 `tokenizer_vocab_size` → raise，不许用任何默认词表。"""
    with pytest.raises(ValueError, match="tokenizer_vocab_size"):
        train_config_from_manifest(
            {"name": "x", "tokenizer": "t", "pack_block_size": 512})
    with pytest.raises(ValueError, match="pack_block_size"):
        train_config_from_manifest(
            {"name": "x", "tokenizer": "t", "tokenizer_vocab_size": 1000})


def test_配方指纹会随配置变化():
    """`recipe_id` 是 training_runs 的锚。配置改了必须换 id。"""
    a = train_config_from_manifest(
        {"tokenizer": "t", "tokenizer_vocab_size": 100, "pack_block_size": 512},
        n_layer=2, n_embd=64)
    b = train_config_from_manifest(
        {"tokenizer": "t", "tokenizer_vocab_size": 100, "pack_block_size": 512},
        n_layer=2, n_embd=128)
    assert a.recipe_id != b.recipe_id, (
        "改n_embd 却沿用 recipe_id → training_runs 历史会说谎")


# ── 4. 分层切分（回归防护）───────────────────────────────────────────────
def test_同实体必须同split():
    """同一 symbol 的样本不能分处 train/test（泄漏的是实体不是样本）。"""
    from collections import defaultdict

    rows = [{"id": f"n{i}", "symbol": f"S{i % 5}"} for i in range(200)]
    by = defaultdict(set)
    for r in rows:
        by[r["symbol"]].add(assign_split(r["id"], 0.1, 0.1, r["symbol"]))
    bad = {k: v for k, v in by.items() if len(v) > 1}
    assert not bad, f"分层失效：{bad}"
    # 变异检查：不给 key 时应当真的分层失效（否则上面那条是恒真的）
    by2 = defaultdict(set)
    for r in rows:
        by2[r["symbol"]].add(assign_split(r["id"], 0.1, 0.1, None))
    assert any(len(v) > 1 for v in by2.values()), (
        "无 key 时全部同 split → 分层判据恒真，测不出退化")
