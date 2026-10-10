-- assert_funnel_no_time_dimension.sql
--
-- 【这条测试抓的是本轮修掉的真bug】
-- incr_dataset_day_agg 用 delete+insert 增量、unique_key = (dataset, event_date)。
-- 上游 dws_dataset_day 里有 2 行 event_date 为 NULL（fhir_funnel / image_funnel
-- 的汇总行）。delete+insert 定位旧行靠 unique_key，而**SQL 里 NULL 匹配不上 NULL**：
--   → 旧行删不掉 → 每次重跑再插一遍 → 行数与 drop_rate 静默翻倍。
-- 这类缺陷在「每行都有日期」的夹具库里**永远测不出来**，
-- 是跑真实数据才暴露的（2026-10-05）。
--
-- 【为什么放在这里而不是当注释】
-- 修法是「日粒度表只收有日期的行」。但**人一定会忘**：
-- 半年后有人为了「让漏斗型数据也进日表」而放宽那条 where，
-- 本测试会立刻红并指出是哪两个数据集 —— 这才是门禁该做的事。
--
-- 【断言方向】正向：日粒度表里不允许出现空 event_date。
-- 反向（漏斗数据集确实没有日粒度）由本测试的姊妹测试
-- assert_timeseries_has_date.sql 覆盖，两条一起把语义钉死。

select
    dataset,
    '日粒度表里出现 event_date 为空的行 —— unique_key 会匹配失败导致重跑重复累加'
        as failure_reason,
    count(*) as n_bad_rows
from {{ ref('incr_dataset_day_agg') }}
where event_date is null
group by dataset