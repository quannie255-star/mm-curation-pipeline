# 设计表 — Week3 D1-D2：FAISS 向量索引 + FastAPI 检索服务

> 按协议生成；实现前需用户确认。对应 ROADMAP Week3 D1-D2。

## 1. 数据结构表

### 1.1 IndexManifest（索引清单，JSON）

| 字段 | 类型 | 说明 |
|---|---|---|
| name | str | 索引名，如 `clean_v2` / `dirty_raw`（对比实验的两侧） |
| source_jsonl | str | 构建来源样本文件（data/processed/.../cleaned.jsonl 或 contaminated） |
| n_items | int | 索引样本数 |
| dim | int | 向量维度（Chinese-CLIP = 512） |
| metric | str | 相似度度量，固定 `cosine`（向量已归一化，内积即余弦） |
| faiss_index | str | FAISS 索引文件相对路径 |
| image_store | str | 样元数据 jsonl 路径（id/图片路径/caption，按索引行号对齐） |
| built_at | str | 构建时间 ISO8601 |

### 1.2 ImageStore 行（样本元数据，jsonl，与 FAISS 行号严格对齐）

| 字段 | 类型 | 说明 |
|---|---|---|
| row | int | FAISS 行号（对齐键） |
| id | str | 样本 id（COCO_train2014_... 或注入 id） |
| image_path | str | 相对仓库根的路径（服务返回静态文件用） |
| caption | str | 中文 caption |
| labels | dict | ground truth（净索引应为空） |

### 1.3 SearchRequest / SearchResponse（API DTO）

| 字段 | 类型 | 校验 | 说明 |
|---|---|---|---|
| query | str | 非空，长度 1-256 | 文搜图的查询文本 |
| image | str(base64) | 与 query 二选一 | 图搜图的图像编码 |
| top_k | int | 1-100，默认 10 | 返回条数 |
| index | str | 白名单校验 | 索引名（clean_v2 / dirty_raw） |
| results[].row/id/score/image_path/caption | — | — | score 为余弦相似度 [-1,1]，降序 |
| took_ms | float | — | 服务端耗时 |

## 2. 接口约定表

### 2.1 FastAPI（serving/）

| URL | Method | 入参 | 出参 | 校验规则 |
|---|---|---|---|---|
| /api/search | POST | SearchRequest | SearchResponse | query/image 二选一否则 422；index 不在清单白名单 → 404；top_k 越界 → 422 |
| /api/indexes | GET | — | [{name, n_items, built_at, source}] | — |
| /api/health | GET | — | {status, index_ready} | 无索引时 200 但 index_ready=false |
| /static/{path} | GET | 文件路径 | 图像文件 | 只允许 manifest 内已登记目录，防路径穿越 |

### 2.2 CLI（scripts/build_index.py）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| --input | str | data/processed/cn_flickr_curation_v2/cleaned.jsonl | 样本来源 |
| --name | str | clean_v2 | 索引名 |
| --out | str | data/indexes | 输出目录 |
| 退出码 | 0 成功 / 1 输入缺失 / 2 编码失败 | | |

产物：`data/indexes/<name>/{faiss.index, store.jsonl, manifest.json}`

## 3. 流转表

### 3.1 索引构建状态

| 当前状态 | 触发事件 | 下一状态 | 约束 |
|---|---|---|---|
| (无) | build_index 启动 | encoding | 输入 jsonl 存在且非空 |
| encoding | 全部向量编码完成 | building | 向量数 == 样本数，dim 一致 |
| building | FAISS IndexFlatIP 构建并落盘 | ready | manifest 写入成功 |
| ready | 上游 cleaned.jsonl 重新生成 | stale | manifest.source_jsonl 的 mtime > built_at |
| stale | 重新执行 build_index | ready | — |

### 3.2 样本生命周期（与 DAG 依赖对齐）

```
raw(下载) → contaminated(污染) → funnel:* (逐级判定)
  ├─ kept → indexed(入净索引) → 可检索
  └─ dropped(stage 标注) → 评测原料
```

### 3.3 Week3 剩余任务依赖（D3-D5 预告，本期不实现）

`build clean 索引 + build dirty 索引 → eval/retrieval（Recall@K/MRR 对比）→ sampling/ → Airflow 串接`
