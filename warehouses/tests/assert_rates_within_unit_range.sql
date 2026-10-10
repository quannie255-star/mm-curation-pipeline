-- ① 值域断言：比率必须落在 [0,1]
--
-- 【为什么不用 dbt_utils.accepted_range】
-- 它需要 `dbt deps` 从 PyPI 拉包。本项目的要求是「本机跑完整」，
-- 凡是需要在装好 dbt 之后还能联网才能跑的东西，一律不用。
-- 代价是这类断言要手写 SQL；收益是**整个模块零外部包**，
-- 删掉 warehouses/ 目录即彻底回退，不留半拉子状态。
--
-- 【dbt singular test 的判定语义】
-- **返回 0 行 = 通过**，返回任何行 = 失败。
-- 所以写法都是「把不合法的行 SELECT 出来」，而不是「合法的行」——
-- 这个方向搞反的话，测试会永远通过而你以为它在拦东西。

-- 丢弃率不可能 <0 或 >1。超出即说明 n_dropped > n_total，那是口径 bug 不是真实数据
select
    'recomputed_drop_rate out of [0,1]' as failure_reason,
    dataset,
    event_date,
    recomputed_drop_rate as offending_value
from {{ ref('int_sample_verdict') }}
where recomputed_drop_rate < 0
   or recomputed_drop_rate > 1
union all
select
    'fct drop_rate out of [0,1]',
    dataset,
    event_date,
    drop_rate
from {{ ref('fct_sample_daily') }}
where drop_rate < 0
   or drop_rate > 1
