# 手测用例 — Week3 D1-D2：FAISS 索引 + FastAPI 检索服务

> 以下全部为真实执行输出（2026-08-26，本机 RTX 4060）。
> 服务启动：`make serve`（等价 `uvicorn mm_curation.serving.api:app --app-dir src --port 8000`）
> Windows 下 curl 传中文 JSON 建议 `--data-binary @body.json`（shell 直传可能编码损坏）。

## 前置：构建索引

```bash
make index-clean   # -> {"name": "clean_v2", "n_items": 1585, "dim": 512}
make index-dirty   # -> {"name": "dirty_raw", "n_items": 2106, "dim": 512}
```

## 1. 健康检查

```bash
curl http://127.0.0.1:8000/api/health
```
```json
{"status": "ok", "index_ready": true}
```

## 2. 索引清单

```bash
curl http://127.0.0.1:8000/api/indexes
```
```json
[{"name": "clean_v2", "n_items": 1585, "built_at": "2026-08-26T09:02:26+00:00", "source": "...cleaned.jsonl"},
 {"name": "dirty_raw", "n_items": 2106, "built_at": "2026-08-26T09:03:50+00:00", "source": "...contaminated/samples.jsonl"}]
```

## 3. 文搜图（净索引，中文查询）

```bash
curl -X POST http://127.0.0.1:8000/api/search -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{"query": "一只狗在草地上奔跑", "index": "clean_v2", "top_k": 3}
EOF
```
实测（首次 7.6s 含模型惰性加载，热查询 67ms）：
```
0.443 COCO_train2014_000000067443 | 草地上有一只狗跳起来接飞盘。
0.430 COCO_train2014_000000071128 | 一只狗在沙滩上奔跑。
0.428 COCO_train2014_000000025401 | 一只小狗趴在田野上
```

## 4. 同一查询切到脏索引（清洗价值的直观展示）

```json
{"query": "一只狗在草地上奔跑", "index": "dirty_raw", "top_k": 3}
```
实测：
```
0.443 ...067443::blur293          | 一只斗牛犬衔着一只足球奔跑着。 | labels: {"dirty": "blur"}
0.443 ...067443                   | 草地上有一只狗跳起来接飞盘。   | labels: {}
0.430 ...071128::exact_duplicate215 | 一只狗在沙滩上奔跑。          | labels: {"dirty": "exact_duplicate"}
```
top3 中 2 条为脏数据，模糊图排在第一位——脏数据污染检索的直接证据
（量化对比见 Week3 D3 评测：Recall@K / MRR）。

## 5. 图搜图（base64）

```bash
B64=$(base64 -w0 data/raw/images/COCO_train2014_000000061732.jpg)
curl -X POST http://127.0.0.1:8000/api/search -H "Content-Type: application/json" \
  -d "{\"image\": \"$B64\", \"index\": \"clean_v2\", \"top_k\": 2}"
```
实测：`top1=COCO_train2014_000000061732 score=1.000`（自检索命中），
含图像编码 331ms。

## 6. 静态资源（白名单）

```bash
curl -o /dev/null -w "%{http_code} %{content_type}\n" \
  http://127.0.0.1:8000/static/data/raw/images/COCO_train2014_000000061732.jpg
# -> 200 image/jpeg（146KB）
curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/static/../../etc/passwd
# -> 404（路径穿越被精确白名单拦截）
```

## 7. 错误语义

| 请求 | 预期 | 实测 |
|---|---|---|
| `index: "nope"` | 404 | 404 ✅ |
| query/image 都缺 | 422 | 422 ✅ |
| `top_k: 0` | 422 | 422 ✅ |
| 非法 base64 image | 422 | 422 ✅ |

## 自动化回归

`pytest`（60 项）覆盖以上语义的 mock 版本；真实索引的端到端即本文档手测。
