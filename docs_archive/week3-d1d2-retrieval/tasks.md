# 任务清单 — Week3 D1-D2：FAISS 索引 + FastAPI 检索服务

> 层序声明（本模块，非通用模板）：
> **L1 索引层（index/）→ L2 服务层（serving/）→ L3 集成层（CLI/Makefile/静态资源）**
> 每任务 50-150 行，附 pytest 证据，层间等待用户确认。

| # | 任务 | 层 | 产物 | 预估行数 | 依赖 | 状态 |
|---|---|---|---|---|---|---|
| T1 | 索引构建器：编码→FAISS→落盘三件套 | L1 | `src/mm_curation/index/store.py` | ~100 | design 1.1/1.2 | ✅ 完成（6 测试绿，含 stale 判定） |
| T2 | 索引加载与查询：text/image → top-k | L1 | `src/mm_curation/index/searcher.py` | ~80 | T1 产物 | ✅ 完成（含 stale 告警、行数一致性校验） |
| T3 | L1 测试：searcher 查询测试（构建器测试已并入 T1） | L1 | `tests/test_index.py` 增补 | ~60 | T2 | ✅ 完成（55 测试全绿×2 次） |
| T4 | FastAPI 服务：路由/校验/统一响应/静态图 | L2 | `src/mm_curation/serving/api.py` | ~130 | T2 接口 + design 2.1 | ✅ 完成（query/image 二选一校验、404/422 语义、静态资源精确白名单防穿越） |
| T5 | L2 测试：TestClient + monkeypatch searcher | L2 | `tests/test_serving.py` | ~90 | T4 | ✅ 完成（5 测试：健康/清单/文搜/图搜/校验矩阵/白名单与穿越） |
| T6 | 构建脚本 + Makefile + 实际构建两个索引 | L3 | `scripts/build_index.py`, Makefile | ~60 | T1-T3 确认 | ✅ 完成（clean_v2=1585 / dirty_raw=2106，均 dim=512） |
| T7 | 手测归档：curl 示例 + 自测报告 | L3 | `docs/test_cases.md` | — | T6 | ✅ 完成（全部真实输出：热查询 67ms、图搜图自命中 1.000、错误语义 4/4） |

## 验收标准（模块级）
1. `make index-clean index-dirty` 产出两个可用索引
2. `make serve` 后：文搜图/图搜图返回正确 top-k，脏/净索引可切换
3. 全部测试绿（预计 46 → 55+）
