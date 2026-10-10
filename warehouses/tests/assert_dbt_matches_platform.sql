-- ② 跨层一致性：dbt 算出的丢弃率必须与 Python 链路的上游值一致
--
-- 【这个测试回答的是本项目最大的风险】
-- 上游 Python 链路与这里的 SQL 层是**两套独立实现的口径**。
-- 一旦它们漂移，对外的「清洗有效（R@1 0.459 → 0.556）」就不再可信——
-- 因为那个结论建立在「清洗前后水位可比」上，而水位正是这里断言的量。
--
-- 所以这不是「检查 dbt 自己」，是**用 SQL 层给 Python 层做独立交叉校验**。
-- 删掉 warehouses/ 不影响上游；但留着，它就一直在替上游担保。
--
-- 【为什么用「差值 > 1e-9」而不是「= 0」】
-- 浮点比较用等号是经典的假红来源：两个 0.1 + 0.2 在浮点下就是不相等的。
-- 1e-9远小于任何有业务意义的差异（丢弃率差 1e-9 没有现实含义），
-- 却能吸收浮点噪声。这是「判据要匹配语义而不是子串」的又一个实例。

select
    'drop_rate drift between dbt layer and upstream view' as failure_reason,
    d.dataset,
    d.event_date,
    d.recomputed_drop_rate   as dbt_value,
    u.upstream_drop_rate     as upstream_value,
    abs(d.recomputed_drop_rate - u.upstream_drop_rate) as diff

from {{ ref('int_sample_verdict') }} as d

-- 直接回到source（上游视图），而不是引用任何中间模型——
-- 引用中间模型的话，中间模型自己错了就会被自己的输出掩盖。
inner join (
    select
        dataset,
        event_date,
        cast(n_dropped as double) / nullif(n_total, 0) as upstream_drop_rate
    from {{ source('platform', 'dws_dataset_day') }}
    where n_total > 0
) as u
    on  u.dataset   = d.dataset
    and u.event_date = d.event_date

where abs(d.recomputed_drop_rate - u.upstream_drop_rate) > 1e-9
