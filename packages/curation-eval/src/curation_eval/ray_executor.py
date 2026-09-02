"""Ray 分布式执行器（V2 γ：同一算子图，第二种运行时）。

蓝图：ARCHITECTURE_V2 决策 2 方案 B——本地零依赖 + Ray 懒加载。import 本模块
不需要装 ray；只有实例化 RayDistributedExecutor 时才 import（缺失时报可操作的
安装指引）。算子在两种运行时下的语义映射见 docs/design_tables.md γ 决策点 2。

传输协议（ray.data 实测钉死，γ0 spike）：
- Sample 经 from_items 进 Dataset 后存入 pyarrow 的 pickle 兜底列；
  map_batches(batch_format="numpy") 收到 {"item": ndarray[Sample]}，元素是
  完整 Sample 对象（cloudpickle 保真，无需二次反序列化）
- ray≥2.5 禁止 map_batches 返回裸 list；返回 dict 的值必须等长（块的两个
  column）且元素类型 list/ndarray——统一返回 {"item": kept+dropped,
  "stage": ["kept"…/"dropped"…]}，skipped 由 driver 按模态重算（passthrough
  必然全部保留，语义等价）；take_all 取回行同为 {"item": obj, "stage": str}

语义口径：
- 单样本算子：actor 池批间并行；模态不匹配保留不评判（计 skipped）
- BatchOperator shardable=True：按块 run_batch（batch 仅为效率包装，分片语义
  不变）；shardable=False：汇聚单点执行（协议的 reduce/shuffle 属二期）
- 每级 materialize（take_all + 重建 Dataset）：StageStat 可观测性与 FunnelResult
  的 dropped 收集要求全级视图；流式优化属后续
- 行序不承诺（ray 不保序）；「先到先保留」的簇代表选择依赖输入序，跨运行时
  逐位一致不是目标，集合等价才是（γ3 验收口径）
"""

from __future__ import annotations

from typing import Sequence

from .schema import Sample
from .sdk import (
    BatchOperator,
    Executor,
    FunnelResult,
    Operator,
    StageStat,
    _score_stats,
    run_batch_mixed_modality,
)


class _RayMapSingle:
    """单样本算子的 map_batches 包装（模态跳过；kept/dropped 以 tag 列区分）。"""

    def __init__(self, op: Operator):
        self.op = op

    def __call__(self, batch: dict) -> dict:
        meta = self.op.meta
        kept: list[Sample] = []
        dropped: list[Sample] = []
        for s in batch["item"]:
            if meta is not None and s.modality not in meta.modalities:
                kept.append(s)  # 保留不评判
                continue
            (kept if self.op(s) is not None else dropped).append(s)
        return _tagged(kept, dropped)


class _RayMapBatchShard:
    """shardable 批量算子的 map_batches 包装（按块 run_batch）。"""

    def __init__(self, op: BatchOperator):
        self.op = op

    def __call__(self, batch: dict) -> dict:
        survivors, dropped, _ = run_batch_mixed_modality(self.op, list(batch["item"]))
        return _tagged(survivors, dropped)


def _tagged(kept: list[Sample], dropped: list[Sample]) -> dict:
    """map_batches 输出协议：对象列 + 等长 tag 列（列必须等长）。"""
    return {"item": kept + dropped, "stage": ["kept"] * len(kept) + ["dropped"] * len(dropped)}


class RayDistributedExecutor(Executor):
    """ray.data 后端的 Executor 协议实现（与 LocalSequentialExecutor 同语义）。

    不装 ray 时实例化报 ImportError（安装指引）；ray.init 参数可透传，
    默认 include_dashboard=False。
    """

    def __init__(
        self, num_cpus: int | None = None, object_store_memory: int | None = None, **ray_init_kwargs
    ):
        try:
            import ray
        except ImportError as e:  # pragma: no cover - 环境相关
            raise ImportError(
                "RayDistributedExecutor 需要 ray：pip install curation-eval[ray] "
                "（或 pip install ray）。不装 ray 可继续用 LocalSequentialExecutor。"
            ) from e
        self._ray = ray
        if not ray.is_initialized():
            ray.init(
                num_cpus=num_cpus,
                object_store_memory=object_store_memory,
                include_dashboard=False,
                **ray_init_kwargs,
            )

    # -- 内部：单级执行（返回新 Dataset 与该级统计） --------------------

    def _stage(self, op: Operator, ds) -> tuple[object, StageStat, list]:
        n_in = ds.count()
        # 无元数据的 v1 风格算子无 shardable 声明，按最安全的全量汇聚处理
        if isinstance(op, BatchOperator) and (op.meta is None or not op.meta.shardable):
            # 全量视角算子：汇聚单点执行（与 Local 同一共享语义函数）
            items = [row["item"] for row in ds.take_all()]
            survivors, dropped, skipped = run_batch_mixed_modality(op, items)
            new_ds = self._ray.data.from_items(survivors)
            stat = StageStat(
                op=op.name,
                n_in=n_in,
                n_out=len(survivors),
                dropped=len(dropped),
                skipped=skipped,
                batch=True,
            )
            return new_ds, stat, dropped

        wrapper = _RayMapBatchShard if isinstance(op, BatchOperator) else _RayMapSingle
        mapped = ds.map_batches(wrapper, fn_constructor_kwargs={"op": op}, batch_format="numpy")
        kept: list[Sample] = []
        dropped: list[Sample] = []
        for row in mapped.take_all():
            # take_all 返回逐行 dict：row = {"item": Sample, "stage": "kept"|"dropped"}
            (kept if row["stage"] == "kept" else dropped).append(row["item"])
        # passthrough（模态不匹配）必然全部保留 → skipped 可由 kept 重算
        skipped = sum(
            1 for s in kept if op.meta is not None and s.modality not in op.meta.modalities
        )
        new_ds = self._ray.data.from_items(kept)
        if isinstance(op, BatchOperator):
            stat = StageStat(
                op=op.name,
                n_in=n_in,
                n_out=len(kept),
                dropped=len(dropped),
                skipped=skipped,
                batch=True,
            )
        else:
            smin, p50, smax = _score_stats(kept + dropped, op.name)
            stat = StageStat(
                op=op.name,
                n_in=n_in,
                n_out=len(kept),
                dropped=len(dropped),
                skipped=skipped,
                score_min=smin,
                score_p50=p50,
                score_max=smax,
            )
        return new_ds, stat, dropped

    def run(self, ops: Sequence[Operator], samples: list[Sample]) -> FunnelResult:
        result = FunnelResult()
        ds = self._ray.data.from_items(list(samples))
        for op in ops:
            ds, stat, dropped = self._stage(op, ds)
            result.stats.append(stat)
            result.dropped.extend((op.name, s) for s in dropped)
        result.kept = [row["item"] for row in ds.take_all()]
        return result
