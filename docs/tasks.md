# tasks.md — 原子任务拆解（现行模块：V4 α 医疗模态协议扩展）

> 按 docs/AI_CODING_PROTOCOL.md 生成：设计表（design_tables.md V4 节）确认后拆任务，
> 逐任务交付「代码 + pytest 证据 + 状态报告」。历史模块任务清单验收后归档 docs_archive/
> （θ 已归档 docs_archive/v3-theta-studio/）。β/γ 阶段任务在 α 收官后另拆。

## V4 α 层序声明

协议层（curation-eval 包）→ 数据逻辑层（语料/算子/污染器）→ CLI 脚本层 → 文档回写层
（下层依赖上层，不跨层跳写）。

## α1 协议层（packages/curation-eval）

- [x] schema.py：MODALITY_FIELDS 登记 `fhir_resource: frozenset({"text"})`（既有登记点一行扩展）
- [x] 新模块 curation_eval/fhir.py：`FHIRSample.from_resource / to_resource / parse`，
      text = canonical JSON（sort_keys、ensure_ascii=False），meta 三键
      （fhir_resource_type / fhir_version / fhir_last_updated）；构造期 fail-fast
- [x] `__init__.py` 导出 FHIRSample；版本 0.2.0 → 0.3.0
- [x] tests/test_fhir.py ≥6：roundtrip 保真 / meta 三键与 modality / 未知模态仍拒 /
      from_dict 序列化往返 / 混合模态执行器跳过语义 / 批量算子 id 排序确定性 /
      错误输入 ValueError

## α2 数据逻辑层（主仓）

- [x] src/mm_curation/data/fhir_synth.py：`generate_corpus(seed, scale)`——
      P100/O200/E100/M100（scale 缩放），内嵌假名池 40 + ICD-10 30 + LOINC 15 +
      ATC 10 + UCUM 10 静态表；引用闭合；约 8% 合法业务异常；同 seed 逐字节一致
- [x] src/mm_curation/operators/fhir_quality.py：phi_residual / code_validity /
      unit_normalization（单样本）+ temporal_consistency / referential_integrity_fhir
      （批量 shardable=False）；operators/__init__.py 接线
- [x] OPERATOR_TARGETS 增五条主靶映射（eval/operator_pr.py）
- [x] 包侧 curation_eval/fhir_contamination.py：fhir_phi_leak / fhir_code_invalid /
      fhir_time_inverted / fhir_ref_broken / fhir_unit_off（供体重抽模式；
      动码时发现仓库双污染器注册表并存——V1 主仓套仅剩 contaminate.py 消费方，
      按 β 文本污染器先例改投包侧 V2 协议套，ContaminationPlan 零改动）
- [x] tests/test_fhir_synth.py（seed 逐字节 / 构成与引用闭合 / 干净语料零 PHI 模式）
- [x] tests/test_fhir_quality.py：5 算子 × ≥4（通过/拒绝/边界/错误输入）+
      批量确定性
- [x] tests/test_fhir_contamination.py（同 seed 确定性 / 五类靶向命中对应算子 /
      原始样本不被修改）

## α3 CLI 脚本层

- [x] configs/funnel_fhir.yaml（五算子链，单样本在前批量在后）
- [x] scripts/eval_fhir.py：语料生成 → ContaminationPlan 注入 → evaluate_all 独立 P/R
      （operator_pr 格式）→ 漏斗串联门禁（总体故障召回 ≥0.90 且误杀 ≤0.05，
      跌破 exit 1，--no-gate 观测）→ data/reports/operator_pr_fhir.{json,md}
- [x] Makefile 增 eval-fhir 目标
- [x] tests/test_fhir_eval.py（小规模冒烟全绿落盘 / 门禁函数劣化判红）

## α4 文档回写层

- [x] RUNBOOK eval-fhir 段（裸命令，Git Bash 无 make）
- [x] DEV_PLAN 状态快照 + 日志；ROADMAP 进度表 V4 α 行
- [x] ENGINEERING_NOTES（扁平化决策：第三模态零框架特例的话术）
- [x] 质量门全绿（ruff + 主仓 + 包）→ 实点基线回写 → commit + push
