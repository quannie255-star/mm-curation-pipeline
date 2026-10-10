-- ③ 拉链表的完整性：当前有效版本必须唯一，且有效期不得倒挂
--
-- 【为什么这两条断言是拉链表的生命线】
-- 拉链表一旦破了，通常以两种方式静默出错：
--   ① 一个 device_sk 有两条「当前有效」记录 → point-in-time join 时行数翻倍，
--      所有下游聚合悄悄翻倍，**没有任何报错**。
--   ② valid_to < valid_from（有效期倒挂）→ 该版本在时间轴上不存在，
--      查历史时会「跳到」这个版本，而不是返回空。
-- 两者都不会让流水线变红，只会让报表变错—— 所以必须在这里拦住。
--
-- 【本项目当前的真实状态】
-- 上游 dim_device 有 valid_from/valid_to 结构，但**当前 0 个设备有多版本**，
-- 所以第一条断言今天必然通过（每个设备恰好一条当前版本）。
-- **这意味着它还没有被真实数据检验过** —— 见int_device_history.sql 的诚实边界。
-- 断言写在这里是为了「等真实变化发生的那天，它已经在拦了」，
-- 而不是等出事后再补门禁。

-- ① 当前有效版本唯一性：每个 device_sk 最多一条 valid_to = 9999-12-31
select
    'multiple current versions in SCD2 dimension' as failure_reason,
    device_sk,
    count(*)as n_current_rows
from {{ ref('int_device_history') }}
where valid_to = date '9999-12-31'
group by device_sk
having count(*) > 1

union all

-- ② 有效期不得倒挂
select
    'valid_to earlier than valid_from' as failure_reason,
    device_sk,
    valid_from
from {{ ref('int_device_history') }}
where valid_to < valid_from
