# 服务实测性能与降级承诺（SLA_README）

> 原则：**只写测过的数字，只承诺做到的降级**。本服务是单机研究/演示部署，
> 不虚构可用性百分比；生产化时按下方矩阵补齐。

## 实测性能（RTX 4060 Laptop / 索引 1,585 条 / 检测器 CNN 常驻）

| 操作 | 首次（含惰性加载） | 热 | 备注 |
|---|---|---|---|
| POST /api/search（文搜图） | ~7.6s | **67ms** | Chinese-CLIP 文本编码 + FlatIP 精确检索 |
| POST /api/search（图搜图） | 同上 | ~331ms | 含图像编码 |
| POST /api/ingest（质量门+判重） | ~15.6s | **136-179ms** | 7 算子评分 + CNN 推理 + 三层判重 |
| GET /metrics | — | <1ms | Prometheus 文本格式 |
| 离线：漏斗全量 2,106 条 | — | ~3min | GPU（CLIP 编码占大头） |
| 离线：索引构建 1,585 条 | — | ~1.5min | 图像批量编码 |

## 监控面（接 Prometheus/Grafana 即可）

- `mm_requests_total{path,status}`：请求计数（含 4xx/5xx 分布）
- `mm_request_latency_seconds_bucket`：search/ingest 延迟直方图
- `mm_business_total{kind=ingest_accepted|ingest_rejected|ingest_quality_flagged|ingest_duplicate}`：质量门漏斗
- 漂移：`drift_report`（PSI）——建议每批次跑一次，换源级漂移 >0.25 告警

## 降级策略矩阵

| 条件 | 行为 | 用户感知 |
|---|---|---|
| 检测器权重缺失 | 质量门去掉 wm_nsfw_cnn 分，其余照常 | 响应少一个分数键 |
| 索引未构建 | /api/search 404，/api/health 如实报 index_ready=false | 明确错误而非静默空结果 |
| 索引过期 | 告警日志 + 正常查询 | 结果可能滞后（日志可见） |
| 无 GPU | 全链路 CPU 回落 | 延迟上升（约 10-20x），功能不变 |
| 上游换源 | PSI 告警，人工确认后重校准阈值 | 入库暂停建议 |

## 已知限制（诚实清单）

1. ingest 去重状态为进程内存态：重启后重复检测窗口重开（修复路径：
   journal 追加 + 启动重放，见 ARCHITECTURE.md FMEA）
2. 单进程单worker：uvicorn 多副本时去重状态不共享（同上修复路径）
3. 未做鉴权/限流：内网研究部署假设
