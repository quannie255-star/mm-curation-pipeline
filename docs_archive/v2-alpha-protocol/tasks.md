# 任务清单 — V2 α 阶段：协议收口（curation-eval 0.2 + 主仓库适配）

> 设计见 docs/design_tables.md；验收标准 A1-A7 为模块级完成定义。
> 预估总量 ~45h。执行顺序即依赖顺序；T2/T4 可并行。

## curation-eval 侧（包先行）

- [x] T1 schema.py：Sample（text/image_path/modality/meta/labels + 构造不变量 + to/from_dict legacy 兼容）与 MODALITY_FIELDS；单测覆盖推断/校验/往返/legacy（~4h，无依赖）
- [x] T2 registry.py：CostClass/OperatorMeta/register_operator 装饰器 + 注册校验（字段蕴含/重名/空模态）；单测（~5h，依赖 T1 的 MODALITY_FIELDS）
- [x] T3 sdk.py：Operator/BatchOperator（score 语义 + min/max keep）+ Executor ABC（reduce/shuffle 占位 NotImplementedError）+ LocalSequentialExecutor + 单测（~5h，依赖 T1/T2）
- [x] T4 contamination 迁移到 Sample（plan.run 输入输出 Sample，dict 入参移除）+ 包内单测更新（~4h，依赖 T1）
- [x] T5 包收口：version 0.2.0、README 协议段重写、包测试全绿、变更记录（~2h，依赖 T1-T4）

## 主仓库适配（同一周内完成，决策 ② 的版本承诺）

- [x] T6 升级安装 curation-eval 0.2.0；operators/base.py 改纯 re-export（Sample/Operator/BatchOperator/注册装饰器），旧定义删除（~2h，依赖 T5）
- [x] T7 caption→text 原子重命名：operators/contamination/data/serving/eval/sampling/scripts/tests 全量；from_dict legacy 键验证（~6h，依赖 T6）
- [x] T8 全部 12 个算子注册补元数据（modalities/required_fields/cost_class/shardable/superlinear/input_signal）+ 注册校验全过（~4h，依赖 T7）
- [x] T9 runner 接 LocalSequentialExecutor + 模态跳过计数 + 配置 fail-fast 校验（引用了模态不匹配算子的配置在启动时报错）（~4h，依赖 T3/T8）
- [x] T10 α 验收测试：A1-A5 逐条落测试（10 样本混合漏斗端到端为主场景）（~5h，依赖 T9）
- [x] T11 legacy 数据对账：data/interim jsonl（caption 键）加载跑漏斗，召回/误杀与基线差 ≤1pp（A6）（~2h，依赖 T10）
- [x] T12 cost_model 读注册表元数据替代硬编码 superlinear 集合（A7）；双仓库 CI 绿；ARCHITECTURE_V2/FAQ/RUNBOOK 同步；tag v0.2（~3h，依赖 T11）

## 完成定义（DoD）

- [x] 双仓库 CI 全绿（主仓库 105+，包 6+新增）
- [x] A1-A7 全部有对应测试或守卫
- [x] 主仓库无任何协议类型本地定义（A5 grep 守卫通过）
- [x] legacy 数据指标对账 ≤1pp


> ✅ α 全部完成（2026-09-02）：112 主仓库 + 29 包测试绿；legacy 对账精确一致；全漏斗端到端 99.8%/2.16%。
