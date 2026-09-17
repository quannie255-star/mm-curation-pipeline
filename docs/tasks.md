# tasks.md — 原子任务拆解（现行模块：V5 α 工业传感器增强包）

> 按 docs/AI_CODING_PROTOCOL.md 生成：设计表（design_tables.md V5 节）确认后拆任务，
> 逐任务交付「代码 + pytest 证据 + 状态报告」。V4 α 清单已归档 docs_archive/v4-alpha-fhir/。
> 增强包六件套：适配器（包）/ 领域算子（主仓）/ 领域污染器（包）/ 确定性语料（主仓）/
> 漏斗 config / 评测门禁——FHIR 包为参考实现，本包为第二实例。

## V5 α 层序声明

协议层（curation-eval 包）→ 数据逻辑层（语料/算子/污染器）→ CLI 脚本层 → 文档回写层
（下层依赖上层，不跨层跳写）。

## α1 协议层（packages/curation-eval）

- [x] schema.py：MODALITY_FIELDS 登记 `industrial_sensor: frozenset({"text"})`
- [x] 新模块 curation_eval/sensor.py：`SensorSample.from_payload / to_payload / parse`——
      一窗一 Sample（text = 窗口 payload canonical JSON），record_type ∈
      {reading_window, maintenance_event}，meta 提升八键；构造期 fail-fast
- [x] `__init__.py` 导出 SensorSample；版本 0.3.0 → 0.4.0
- [x] tests/test_sensor.py ≥6：roundtrip / meta 键与 modality / 未知模态仍拒 /
      from_dict 往返 / 执行器跳过语义 / 批量 id 排序确定性 / 错误输入

## α2 数据逻辑层

- [x] src/mm_curation/data/sensor_synth.py：3 类设备 × 6 设备 × 12 通道 × 100 窗
      ≈ 1200 读数窗 + 48 检修事件；AR(1)+噪声走 seed；工况 schedule（idle/run/
      changeover）；计划检修窗真实缺席；内嵌量程/单位表
- [x] src/mm_curation/operators/industrial_quality.py：sensor_stuck / sensor_range
      （单样本）+ sensor_drift / unit_consistency / fault_vs_maintenance（批量
      shardable=False，同工况比较，计划索引由事件样本构建）
- [x] OPERATOR_TARGETS 增五条主靶映射
- [x] 包侧 curation_eval/sensor_contamination.py：sensor_cal_offset / sensor_flatline /
      sensor_out_of_range / sensor_unit_swap / sensor_unplanned_silence（供体重抽，
      drift 供体限定基线窗之后）
- [x] tests/test_sensor_synth.py（seed 逐字节 / 构成与计划缺席 / 干净语料零模式命中）
- [x] tests/test_sensor_quality.py（5 算子 × ≥4 + 批量确定性）
- [x] tests/test_sensor_contamination.py（确定性 / 五类靶向 / 干净侧零误杀）

## α3 CLI 脚本层

- [x] configs/funnel_industrial.yaml（单样本在前批量在后）
- [x] scripts/eval_industrial.py（镜像 eval_fhir：operator_pr 报告 + 漏斗门禁
      召回 ≥90%/误杀 ≤5% exit code）+ Makefile eval-industrial
- [x] tests/test_sensor_eval.py（门禁函数 / 冒烟全链路）

## α4 文档回写层

- [x] docs/DOMAIN_PACKS.md：领域增强包规范（六件套 + 双参考实现 + 扩展步骤 + 红线）
- [x] RUNBOOK eval-industrial 段；DEV_PLAN 快照+日志；ROADMAP V5 α 节
- [x] 质量门全绿 → 实点基线回写 → commit + push
