"""SDK 测试：算子基类语义、Executor map 语义、模态跳过、与串行循环等价。"""

from __future__ import annotations

import pytest
from curation_eval import (
    BatchOperator,
    CostClass,
    Executor,
    LocalSequentialExecutor,
    Operator,
    OperatorMeta,
    Sample,
)


class _LenOp(Operator):
    """测试用文本长度算子：双模态，score=字符数。"""

    name = "test_len"
    meta = OperatorMeta(
        name="test_len",
        modalities=frozenset({"text_article", "image_caption"}),
        required_fields=frozenset({"text"}),
        cost_class=CostClass.RULE,
    )

    def __init__(self, min_len: int = 3):
        super().__init__(min=min_len)

    def score(self, sample: Sample) -> float | None:
        return float(len(sample.text))


class _ImageOnlyLenOp(_LenOp):
    """仅图文模态：text_article 样本应被跳过。"""

    name = "test_len_img"
    meta = OperatorMeta(
        name="test_len_img",
        modalities=frozenset({"image_caption"}),
        required_fields=frozenset({"text", "image_path"}),
        cost_class=CostClass.RULE,
    )


class _DropEvenBatch(BatchOperator):
    """测试用批量算子：id 以偶数结尾的判重丢弃（模拟先到先保留）。"""

    name = "test_batch"
    meta = OperatorMeta(
        name="test_batch",
        modalities=frozenset({"image_caption"}),
        required_fields=frozenset({"text", "image_path"}),
        cost_class=CostClass.PERCEPTUAL,
        shardable=False,
        superlinear=True,
    )

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        return [s for s in samples if int(s.id) % 2 == 1]


def _mixed_batch() -> list[Sample]:
    """5 图文 + 5 纯文本，id 0-9（偶数 id 将被批量算子判重）。"""
    out = []
    for i in range(10):
        if i % 2 == 0:
            out.append(
                Sample(id=str(i), text=f"图文样本{i}号的描述文字", image_path=f"img/{i}.jpg")
            )
        else:
            out.append(Sample(id=str(i), text=f"纯文本样本{i}号的正文内容"))
    return out


def test_operator_threshold_and_meta_write():
    op = _LenOp(min_len=5)
    long_s = Sample(id="long", text="足够长的文本内容")
    short_s = Sample(id="short", text="短")
    assert op(long_s) is long_s
    assert op(short_s) is None  # 低于阈值丢弃
    assert long_s.meta["score:test_len"] == float(len("足够长的文本内容"))
    assert short_s.meta["score:test_len"] == 1.0  # 分数仍写入（报告依赖）
    assert op.keep(None) is True  # None = 无法计分，保留不误杀


def test_batch_operator_call_rejected():
    op = _DropEvenBatch()
    s = Sample(id="1", text="x", image_path="img/1.jpg")
    with pytest.raises(TypeError, match="run_batch"):
        op(s)
    with pytest.raises(TypeError, match="score"):
        op.score(s)


def test_mixed_funnel_modality_skips():
    executor = LocalSequentialExecutor()
    samples = _mixed_batch()
    result = executor.run([_ImageOnlyLenOp(min_len=5)], samples)

    stat = result.stats[0]
    assert stat.n_in == 10 and stat.skipped == 5  # 5 条纯文本被跳过（保留不评判）
    assert stat.n_out == 10 and stat.dropped == 0
    # 只有图文样本被打分
    assert sum(1 for s in result.kept if "score:test_len_img" in s.meta) == 5


def test_batch_modality_partition_preserves_order():
    executor = LocalSequentialExecutor()
    samples = _mixed_batch()
    result = executor.run([_DropEvenBatch()], samples)

    stat = result.stats[0]
    assert stat.batch and stat.n_in == 10
    # 图文样本全是偶数 id -> 全部判重丢弃（dropped=5）；纯文本不适用直通（skipped=5）
    assert [s.id for s in result.kept] == ["1", "3", "5", "7", "9"]
    assert stat.dropped == 5 and stat.skipped == 5
    assert len(result.dropped) == 5 and result.dropped[0][0] == "test_batch"


def test_executor_equivalent_to_serial_loop():
    """A4 验收：LocalSequentialExecutor 与手写串行循环结果完全一致。"""
    executor = LocalSequentialExecutor()
    samples = _mixed_batch()
    result = executor.run([_LenOp(min_len=5), _DropEvenBatch()], samples)

    # 手写串行参照：逐级复现同样语义
    op1, op2 = _LenOp(min_len=5), _DropEvenBatch()
    stage1, drop1 = [], []
    for s in samples:
        (stage1 if op1(s) is not None else drop1).append(s)
    stage2 = op2.run_batch(stage1)
    drop2 = [s for s in stage1 if s.id not in {x.id for x in stage2}]

    assert [s.id for s in result.kept] == [s.id for s in stage2]
    assert [(op, s.id) for op, s in result.dropped] == [("test_len", s.id) for s in drop1] + [
        ("test_batch", s.id) for s in drop2
    ]
    assert result.stats[0].dropped == len(drop1)
    assert result.stats[1].dropped == len(drop2)


def test_reduce_placeholder_not_implemented():
    executor = LocalSequentialExecutor()
    with pytest.raises(NotImplementedError, match="二期"):
        executor.reduce([[Sample(id="a")], [Sample(id="b")]])


def test_executor_is_abstract():
    with pytest.raises(TypeError):
        Executor()  # type: ignore[abstract]
