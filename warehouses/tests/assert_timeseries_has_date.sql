-- assert_timeseries_has_date.sql
--
-- 【本文件的三条断言改过两轮，理由都留在下面】
-- 记录改判过程本身就是门禁的一部分：下一个人改判据前会先看到
-- 「上一版错在哪」，比只留一句结论有用得多。
--
-- ┌ 第一版：想写「event_date 全非空」（not_null）
-- │  被 prod 真实数据否掉：2171 行（image_funnel 2106 + fhir_funnel 65）无日期。
-- └ 不是脏数据，是那两类数据集**本身没有时间维度**。给它加 not_null
--    等于把数据的固有语义当成质量缺陷，处置会滑向「填假日期让门禁绿」。
--
-- ┌ 第二版：分两类断言
-- │  ① has_time_dimension=true  → event_date 非空
-- │  ② has_time_dimension=false → event_date 必须为空
-- └ **②被否掉**：实点发现 text_article 类（news_corpus 2066 / finance_funnel 214 /
--    text_funnel 3 = 2283 行）**有日期但没有采样率**，这是正常的内容型数据。
--    「有日期」不等于「时序」——所以②的前提根本不成立。
--    若保留②，它会一直红，而红的原因是判据错，等于在训练大家忽略红灯。
--
-- ┌ 第三版（当前）：只在「时序」这一侧断言，不对非时序侧做要求
-- │  ① 时序测量样本 → event_date 非空
-- │  ② 时序测量样本 → sampling_hz 非空且为正
-- └ 为什么不对称：时序样本「没有采样率」一定是数据问题（可行动）；
--    而非时序样本「有采样率」反倒是异常，但真实库里目前不存在这种情况，
--    断言它属于**为不存在的场景写判据** —— 判据腐烂的常见来源之一。
--    真出现时（新增一个既无日期又有 hz 的数据集），① 会先红。
--
-- 【has_time_dimension 的口径】
-- 定义为 `modality = 'industrial_sensor'`（上游已有字段），**不是**用
-- 「event_date 是否为 null」反推 —— 那是循环论证（用结果定义原因）。
-- 实点确认它与「有无采样率」完全等价：
--   industrial_sensor 75747行（全有 hz）→ text_article 2283 / image_caption 2106 / fhir_resource 65（全无 hz）
--
-- 【maint_ 维护记录，2026-10-05 实点发现】
-- metropt3 有 3 行 sample_id 以 'maint_' 开头（如
-- maint_metropt3_labelled_df_20200430T120000），is_kept=1、无 dropped_by，
-- 是**维护/标注记录而非测量样本** —— 有合法日期但本来就没有采样率。
--
-- ⚠️ 这里踩过一个**静默失效**的坑（值得记住）：
-- 排除条件最初写 `not like 'maint\_%'`，想的是「转义下划线」。
-- 但 DuckDB 的 LIKE **默认不把反斜杠当转义符**，实测
--   'maint_abc' like 'maint\_%'  →  false（匹配不上任何东西）
-- 于是 `not like` 恒为 true，排除**静默失效**，断言照常红。
-- 若当时只看「报错行里确实有那3 行 maint_」，很容易误判成「排除没写对」而反复调转义。
-- 改用 `not starts_with(sample_id, 'maint_')` —— 绕开转义，无歧义。
--
-- ★ 教训：带转义的模式匹配是「静默失效」的典型 —— 它不报错，只是永远不匹配。
--   任何这类条件，都要单独用一条查询验证「它到底匹配到了几行」。

select
    dataset,
    '时序数据集的测量样本 event_date 为空' as failure_reason,
    count(*) as n_bad_rows
from {{ ref('stg_samples') }}
where has_time_dimension
  and event_date is null
  and not starts_with(sample_id, 'maint_')
group by dataset

union all

select
    dataset,
    '时序数据集的测量样本 sampling_hz 缺失或非正（maint_ 维护记录已排除）'
        as failure_reason,
    count(*) as n_bad_rows
from {{ ref('stg_samples') }}
where has_time_dimension
  and (sampling_hz is null or sampling_hz <= 0)
  and not starts_with(sample_id, 'maint_')
group by dataset