# 数仓建模层（dbt 模块）

> **独立模块。删掉整个 `warehouses/` 目录 + `scripts/dbt_gate.py`，本项目回到「纯 Python 链路」，
> 无残留、无副作用。** 下面会说明为什么它可以被这样删掉。

## 它补的是什么

原项目有完整的数据链路（清洗漏斗 / 湖分区 / 契约闸门），但**全链路没有一条 SQL**：
`.sql` 文件 0 个，表都是 Python 建视图。这个模块用[dbt](https://www.getdbt.com/)
把上游产出的视图重写成标准数仓分层—— 目的是让项目同时是**数据工程**和**数据开发**两个方向都能讲的东西。

| 层 | 模型 | 数仓考点 |
|---|---|---|
| staging | `stg_samples` | 命名规范化、类型收敛、**只SELECT 不WHERE** |
| intermediate | `int_device_history` | **拉链表（SCD Type 2）**、point-in-time join |
| intermediate | `int_sample_verdict` | **窗口函数**（NTILE / 组内最差·均值·累计占比） |
| marts | `fct_sample_daily` | **星型模型**、事实表粒度声明 |
| marts | `incr_dataset_day_agg` | **增量模型**（`is_incremental()` + delete+insert 幂等） |

质量测试即门禁：**5 个模型 + 33 个测试（28 个 schema generic + 5 个跨层 singular）= 38 个 dbt 节点**，
2026-10-05 在真实 prod 湖上 `dbt build` 全绿（38 PASS / 0 ERROR / 0 WARN）。

>数字是实点的，不是估的。复现命令见下一节的 `--json-out`，它会写出节点状态分布。

## 怎么跑（本机，无需外部服务）

```bash
# 走门禁脚本（推荐）：静态预检 -> 真跑 dbt -> 结构化判定，CI 可直接读退出码
python -X utf8 scripts/dbt_gate.py
python -X utf8 scripts/dbt_gate.py --lint-only    # 只跑静态预检（秒级，不连库）
python -X utf8 scripts/dbt_gate.py --mutate# 变异测试：证明门禁还能拦红
python -X utf8 scripts/dbt_gate.py --full-refresh # 强制全量重建

# 或者直接调 dbt（dbt 装在独立 venv，不污染项目所在的 Python 环境）
MM_WAREHOUSE_DB="data/envs/prod/data/warehouse/platform.duckdb" \
  "C:/Users/10393/.workbuddy/binaries/python/envs/mmwh2/Scripts/dbt.exe" \
  build --full-refresh --project-dir warehouses
```

**dbt 版本必须成对匹配**：`dbt-core==1.10.15` + `dbt-duckdb==1.10.0`。
错配的症状不是「装不上」，而是**装上了但一条命令都跑不了**——
`dbt --version` 报 `duckdb 1.11.0 - Not compatible!`，`dbt build` 报
`ModuleNotFoundError: No module named 'dbt.cli.main'`。

**装包必须用全新空venv**：在已有 dbt 的 venv 上重装会触发 pip 卸载旧版本，
而卸载要走 `shutil.rmtree` —— 在带删除护栏的环境里会被拦成`SystemExit(1)`。
装法：

```bash
"C:/Program Files/Python311/python.exe" -m venv \
  "C:/Users/10393/.workbuddy/binaries/python/envs/mmwh2"
"C:/Users/10393/.workbuddy/binaries/python/envs/mmwh2/Scripts/python.exe" \
  -m pip install "dbt-core==1.10.15" "dbt-duckdb==1.10.0"
```

零外部服务：目标引擎是 DuckDB（项目本来就在用），不需要数据库服务器。

## 写这个模块时踩过的坑（都已变成门禁）

这些都是**本层工具全绿、下一层才炸**的类型——不留门禁就会重犯：

| 坑| 症状 | 现在的门禁 |
|---|---|---|
| SQL 注释里写中文破折号后跟ASCII 词 | `Parser Error: syntax error at or near "varchar"` | 约定只用 `--`；注释加编码检查 |
| 文件里出现 **U+FFFD 替换字符** | 到处都正常（它**能正常解码**），只有人眼看得见 | `scripts/encoding_hygiene.py` |
| `config(...)` 块内写 `--` 注释 | `invalid syntax for function call expression`，只指 `config(` 那行 | `dbt_gate.py --lint-only` |
| 注释里写 `{{ this }}` 字面量 | 同上（会**被求值**） | `dbt_gate.py --lint-only` |
| YAML 行尾注释**缺空格**（`- x # c`写成 `- x# c`） | 不报语法错，只是清单里多个脏值→ 门禁永远红 | `dbt_gate.py --lint-only` |
| `accepted_values` 的 `arguments`写成裸 list | 编译成 `not in ()` → `Parser Error` | `dbt_gate.py --lint-only` |
| `arguments:` 块内写 YAML 注释 | dbt 把该块原样拼进 SQL → `Parser Error` | `dbt_gate.py --lint-only` |
| `not like 'maint\_%'` 想转义下划线 | **DuckDB 的 LIKE 默认无转义符** → 条件恒不匹配，**静默失效** | 改用 `starts_with()` |

后三类都是「**判据自己写错**」而不是「数据错」，各自都被变异测试覆盖
（当前 5/5 拦红，见 `--mutate`）。

## 为什么它可以被整目录删除

1. **只读**：所有 source 指向 `platform.duckdb` 里的**视图**，一行数据都不回写。
2. **不改协议**：`Sample` / `Operator` 签名、既有 4 份 config 一个字没动。
3. **不复用现有代码**：SQL 独立写。复用会让「删掉它」不再是干净的回退——
   Python 侧的依赖、注册表、门禁都要跟着动。
   **代价是同一份口径写了两遍**（上游算一次、SQL 里再算一次）。
   这个代价是故意付的，由`tests/assert_dbt_matches_platform.sql` 持续校验两套口径不漂移。
4. **依赖隔离**：dbt 装在独立 venv（`~/.workbuddy/binaries/python/envs/mmwh2`），
   不进项目环境。
5. **零外部包**：不用 `dbt_utils`（它需要 `dbt deps` 联网拉），
   值域断言一律写成纯 SQL 的 singular test —— 所以「装好 dbt 之后还能离线跑通」。
6. **不碰真实数据**：`scripts/dbt_gate.py --mutate` 每条变异都用**独立的库副本**，
   绝不让变异写到 `data/envs/prod/`（我踩过：手工探针把真库的增量表写成
   275 行含 2 行 NULL 日期，而 dbt 不报警，只有 `--full-refresh` 才修得回来）。

## 诚实边界（三处，必须自己先说破）

### ① 拉链表：**结构齐备，但没有真实变化**

上游 `dim_device` 有 `valid_from` / `valid_to` 两列（拉链表结构），
但实测「有多版本的设备数 = **0**」—— 本项目跑过的历史里设备属性从没变过。

所以准确说法是：**「拉链结构已就位、point-in-time 查询可跑，但未经真实变化检验」**，
不是「已实现缓慢变化维」。断言 `assert_scd2_integrity.sql` 今天必然通过，
因为它还没被真实数据考验过。

要让拉链真正生效，得让上游跑一次设备属性变更 —— 那是数据接入的活，不是本模块能造出来的。

### ② 规模：8 万行，跑不出倾斜

`ods_samples` 80201 行 / 1137 分区。**这个量级下不会有数据倾斜**，
所以本项目没有任何倾斜处理经验。窗口函数与增量模型在这里验证的是
**写法与机制正确**，不是性能结论。

### ③ 增量模型：机制已验证，规模未验证

`incr_dataset_day_agg` 用了 `is_incremental()` + `delete+insert` 幂等策略，
但 275 行的表上「增量」与「全量」差别可忽略。
**真正的判据是机制**：重跑同一天不产生重复行（delete+insert 按 unique_key 覆盖）。

## 与上游的口径关系（最重要的一条）

**这个模块不重算清洗判决，只回答「分布如何」。**

清洗判决的唯一来源是 Python 链路（`dws_dataset_day.n_kept / n_total`）。
如果这里自己判一遍，两套口径会漂移，而漂移会同时污染
「清洗到底有没有用」这个对外结论（R@1 0.459 → 0.556 的来源）。

所以 `tests/assert_dbt_matches_platform.sql` 每次都做交叉校验：
**dbt 算出的丢弃率必须与上游视图一致，差值 > 1e-9 即红。**
留着它，它就一直在替上游担保。
