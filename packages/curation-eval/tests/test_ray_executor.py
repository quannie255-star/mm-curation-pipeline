"""Ray 分布式执行器测试：与 LocalSequentialExecutor 的等价性（γ3 验收口径）。

等价 = kept 集按 id 相等 + 每级 StageStat 数字相等 + kept 样本逐 id 分数相等
（ray 不保序，行序不承诺）。CI 无 ray 时 importorskip 自动跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ray = pytest.importorskip("ray")

from curation_eval import (  # noqa: E402
    BatchOperator,
    CostClass,
    LocalSequentialExecutor,
    Operator,
    OperatorMeta,
    RayDistributedExecutor,
    Sample,
)


class _LenOp(Operator):
    """双模态文本长度算子（单样本，RULE 档）。"""

    name = "ray_len"
    meta = OperatorMeta(
        name="ray_len",
        modalities=frozenset({"text_article", "image_caption"}),
        required_fields=frozenset({"text"}),
        cost_class=CostClass.RULE,
    )

    def __init__(self, min_len: int = 5):
        super().__init__(min=min_len)

    def score(self, sample: Sample) -> float | None:
        return float(len(sample.text))


class _TextOnlyLenOp(_LenOp):
    """仅纯文本模态：图文样本应被跳过（保留不评判）。"""

    name = "ray_len_text"
    meta = OperatorMeta(
        name="ray_len_text",
        modalities=frozenset({"text_article"}),
        required_fields=frozenset({"text"}),
        cost_class=CostClass.RULE,
    )


class _ScoreBatch(BatchOperator):
    """shardable=True 批量算子：逐样本独立计分（batch 仅为效率包装）。"""

    name = "ray_batch_shardable"
    meta = OperatorMeta(
        name="ray_batch_shardable",
        modalities=frozenset({"text_article"}),
        required_fields=frozenset({"text"}),
        cost_class=CostClass.MODEL,
        shardable=True,
    )

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        kept = []
        for s in samples:
            s.meta["score:ray_batch_shardable"] = float(len(s.text) % 7)
            if len(s.text) % 7 >= 3:
                kept.append(s)
        return kept


class _KeepOddBatch(BatchOperator):
    """shardable=False 批量算子：奇数 id 保留（全量视角，模拟去重）。"""

    name = "ray_batch_global"
    meta = OperatorMeta(
        name="ray_batch_global",
        modalities=frozenset({"text_article"}),
        required_fields=frozenset({"text"}),
        cost_class=CostClass.PERCEPTUAL,
        shardable=False,
        superlinear=True,
    )

    def run_batch(self, samples: list[Sample]) -> list[Sample]:
        return [s for s in samples if int(s.id) % 2 == 1]


@pytest.fixture(scope="module")
def ray_exe():
    # driver 的 sys.path 不传播给 worker；测试里的 toy 算子类按引用序列化，
    # worker 需要 tests 目录可导入（curation_eval 本体是 editable 安装，无需处理）
    tests_dir = str(Path(__file__).resolve().parent)
    exe = RayDistributedExecutor(
        num_cpus=2,
        object_store_memory=800_000_000,
        runtime_env={"env_vars": {"PYTHONPATH": tests_dir}},
    )
    yield exe
    ray.shutdown()


def _mixed_samples(n: int = 40) -> list[Sample]:
    """n 条混合模态样本：偶数 id 图文（长度交替长短），奇数 id 纯文本。"""
    out = []
    for i in range(n):
        if i % 2 == 0:
            out.append(Sample(id=str(i), text="长" * (i + 1), image_path=f"img/{i}.jpg"))
        else:
            out.append(Sample(id=str(i), text="短" if i % 4 == 1 else "中中中中中中"))
    return out


def _assert_equivalent(local, ray_res, ops) -> None:
    assert {s.id for s in local.kept} == {s.id for s in ray_res.kept}
    assert len(local.stats) == len(ray_res.stats) == len(ops)
    for op, ls, rs in zip(ops, local.stats, ray_res.stats):
        for field in ("n_in", "n_out", "dropped", "skipped"):
            assert getattr(ls, field) == getattr(rs, field), (op.name, field)
        assert ls.batch == rs.batch
    local_scores = {s.id: dict(s.meta) for s in local.kept}
    for s in ray_res.kept:
        for key, val in s.meta.items():
            assert local_scores[s.id][key] == val, (s.id, key)


def test_ray_equivalent_single_sample_op(ray_exe):
    samples = _mixed_samples()
    local = LocalSequentialExecutor().run([_TextOnlyLenOp(min_len=5)], samples)
    ray_res = ray_exe.run([_TextOnlyLenOp(min_len=5)], _mixed_samples())
    _assert_equivalent(local, ray_res, [_TextOnlyLenOp(min_len=5)])
    assert ray_res.stats[0].skipped == 20  # 图文样本全被跳过


def test_ray_equivalent_shardable_batch_op(ray_exe):
    local = LocalSequentialExecutor().run([_ScoreBatch()], _mixed_samples())
    ray_res = ray_exe.run([_ScoreBatch()], _mixed_samples())
    _assert_equivalent(local, ray_res, [_ScoreBatch()])
    assert ray_res.stats[0].batch is True


def test_ray_equivalent_global_batch_op(ray_exe):
    """shardable=False：汇聚单点执行，dropped/skipped 与本地一致。"""
    local = LocalSequentialExecutor().run([_KeepOddBatch()], _mixed_samples())
    ray_res = ray_exe.run([_KeepOddBatch()], _mixed_samples())
    _assert_equivalent(local, ray_res, [_KeepOddBatch()])
    # 20 条奇数 id 文本保留 + 20 条偶数 id 图文直通（skipped）
    assert ray_res.stats[0].n_out == 40 and ray_res.stats[0].skipped == 20
    assert ray_res.stats[0].dropped == 0


def test_ray_equivalent_full_funnel(ray_exe):
    """三级混合漏斗（单样本 + shardable 批量 + 全局批量）整体等价。"""
    ops = [_TextOnlyLenOp(min_len=5), _ScoreBatch(), _KeepOddBatch()]
    local = LocalSequentialExecutor().run(ops, _mixed_samples())
    ray_res = ray_exe.run(ops, _mixed_samples())
    _assert_equivalent(local, ray_res, ops)


def test_ray_requires_package(monkeypatch):
    """未安装 ray 时实例化报可操作的 ImportError（CI 零依赖路径）。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ray":
            raise ImportError("No module named 'ray'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="pip install"):
        RayDistributedExecutor()
