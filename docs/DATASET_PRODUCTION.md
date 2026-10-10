# 训练数据生产系统 · 使用与设计说明

> 一句话：把清洗后的 JSONL 变成**能直接训练的数据集**，并用**真训一次**来证明它值不值得训。
>
> 与「质量决策系统」的分界：决策系统报「保留率 97.67% + 判决书」；
> 生产系统报「用 recipe R 训出 val loss P，吞吐 T tok/s」，并且**跑不掉**。

---

## 1. 快速上手

```bash
# 1) 清洗（漏斗）
python scripts/run_pipeline.py --config configs/news_zh_funnel.yaml

# 2) 建数据集（tokenize → 切分 → 泄漏检查 → packing → Parquet + manifest）
python scripts/build_dataset.py \
    --input data/processed/news_zh_funnel/cleaned.jsonl \
    --name news_zh_v2 --pack-block-size 512 --shard-rows 1000 \
    --funnel-config configs/news_zh_funnel.yaml

# 3) 验收（能不能在项目之外被消费）
python scripts/verify_dataset_consumable.py news_zh_v2

# 4) 真训一次（结果回填 manifest）
python scripts/train_from_dataset.py --dataset news_zh_v2 \
    --steps 300 --batch-size 4 --precision bf16 --writeback
```

产物目录：

```
datasets/news_zh_v2/
├── manifest.json        # 唯一真相源：规模/谱系/切分/校验和/训练结果
└── shards/
    ├── train-00000.parquet   # block 级：block_id, input_ids, loss_mask,
    ├── val-00000.parquet     #            n_tokens, n_real_tokens, split
    └── test-00000.parquet
```

---

## 2. 外部怎么用（不碰本项目代码）

```python
import datasets
from torch.utils.data import DataLoader

# 标准 HF 入口。⚠️ 必须按 split 显式传 data_files：
#    传裸目录在 datasets 5.x 会报 FileNotFoundError。
ds = datasets.load_dataset("parquet", data_files={
    "train": ["datasets/news_zh_v2/shards/train-00000.parquet"],
    "val":   ["datasets/news_zh_v2/shards/val-00000.parquet"],
})
print(ds["train"][0]["input_ids"][:8])
```

**已经是定长 block，直接 `DataLoader(batch_size=B)` 即可，不需要自己 pad。**

`loss_mask` 标 padding 位（0=不参与 loss）。用 `transformers` 时：

```python
def collate(ex):
    ids = torch.tensor([e["input_ids"] for e in ex], dtype=torch.long)
    m = torch.tensor([e["loss_mask"] for e in ex], dtype=torch.bool)
    return {"input_ids": ids, "labels": torch.where(m, ids, -100)}
```

---

## 3. manifest 字段分组（都是本次实测写入，无占位）

| 组 | 字段 | 口径说明 |
|---|---|---|
| **规模** | `n_samples` / `n_tokens` / `n_chars` | **语料口径**（不含 padding、不含 block 边界） |
| | `compression_chars_per_token` | `n_chars / n_tokens` |
| **行单位** | `row_unit` | `"sample"` 或 `"block"` —— **`splits` 的单位由它决定** |
| | `splits` / `n_rows` | packing 时是 **block 数**；恒有 `sum(splits) == n_rows` |
| | `n_blocks` | 仅 packing 时有值 |
| **padding** | `n_real_tokens_total` / `n_padding_tokens_total` / `padding_pct` | 恒有 `real + pad == n_rows × pack_block_size` |
| **谱系** | `source_files` / `funnel_config` / `funnel_ops` | 从 config 现读，不手填 |
| | `tokenizer` / `tokenizer_vocab_size` / `tokenizer_base_vocab_size` | 见下方⚠️ |
| **质量** | `leakage_check` | md5 + MinHash 双检（复用 `benchmarks/` 的口径） |
| | `n_truncated` | 被截断的样本数——**截断量必须可见** |
| **完整性** | `shard_checksums` / `n_shards` | 每个 shard 的 md5 |
| **训练结果** | `training_runs[]` | 每条含 `recipe_id` + 配置 + val loss + 吞吐 + 数据集指纹 |

### ⚠️ `tokenizer_vocab_size` 是 `len(tokenizer)`，不是 `tokenizer.vocab_size`

Qwen2.5 的 `vocab_size = 151643`，但 `len(tokenizer) = 151665` ——
**added special tokens 不计入 `vocab_size`，却会被编码产出**。
按 `vocab_size` 建 embedding → 数据里出现 `id=151645` → CUDA device-side assert
（报错在 `optimizer.step()`，离真正原因隔了十万八千里）。

`tokenizer_base_vocab_size` 留档原值，差值 > 0 就说明该 tokenizer 有 added tokens。

---

## 4. 踩过的坑（全部有测试锁定）

| # | 坑 | 症状 | 修法 | 锁定处 |
|---|---|---|---|---|
| 1 | **`splits` 单位错位** | manifest 记样本数、磁盘是 block 数 | 新增 `row_unit` + `n_rows` | `test_manifest必须登记行单位` |
| 2 | **变长 block 崩 batch** | `stack expects each tensor to be equal size, but got [451] and [512]` | 写盘 pad 到定长 + `loss_mask` | `test_packing必须产出定长block` |
| 3 | **只测第一个 batch = 假绿** | 顺序读时前几个 block 恰好满的 | 验收**遍历全部 batch** | `test_不同长度样本的batch必须能stack` |
| 4 | **词表口径错** | CUDA device-side assert | `len(tokenizer)` + 构建期越界门禁 | `test_词表大小必须能容纳数据里的最大id` |
| 5 | **产物不可重复构建** | 重建后目录混着上次的 shard（179行 vs 163 样本） | `__init__` 就清旧 shard | 验收的行数核对 |
| 6 | **分层切分失效** | 同一 symbol 分处 train/test | hash basis 只用 key，不用 id | `test_同实体必须同split` |
| 7 | **空 split** | `load_dataset` 直接 raise | 声明空 split 是错的，必须三切分都有 | 构建期跳过空split |
| 8 | **round-trip 0%** | `uer/gpt2-chinese` 解码在汉字间插空格 → 语料被静默改写 | 选型改Qwen（99.96% 逐字节） | `tokenizer_benchmark.json` |

---

## 5. 训练配方

```bash
# bf16：约快 4 倍、显存减半（需 CUDA）
python scripts/train_from_dataset.py --dataset news_zh_v2 \
    --steps 300 --batch-size 4 --precision bf16 --n-layer 4 --n-embd 256 --writeback
```

### 容量公式：**batch_size 的上限由词表决定，不由模型深度决定**

```
logits 显存 ≈ B × T × V × dtype_bytes   （+ 梯度同量 → 约 2 倍）
```

中文 `V ≈ 15 万`（Qwen 151665），英文约 5 万 —— **差 3 倍显存**。

实测（本机 RTX 4060 Laptop 8GB，block=512，n_embd=128）：

| 精度 | batch | 峰值显存 | ms/步 |
|---|---|---|---|
| fp32 | 2 | 2.76 GB | 172 |
| fp32 | 8 | **10.26 GB（超卡）** | 4652 |
| bf16 | 4 | ~2.4 GB | ~700 |
| bf16 | 8 | 4.0 GB | 1172 |

**调 batch 前先算这个公式**，别靠 OOM 试错（OOM 报在 `optimizer.step()`，
不会告诉你真正原因是词表太大）。

### bf16 的坑

`autocast` 只降**计算**精度，**loss 必须显式 `.float()`**。
在 bf16 logits 上直接算交叉熵 → 数值不稳，loss 卡在某个值不降，**且不报任何错**。

---

## 6. 验收（`verify_dataset_consumable.py`）

五关，**任何一关红就不算可交付**：

1. `pyarrow.parquet` 直读 + ZSTD 压缩生效 + 行数 == manifest `n_rows`
2. `datasets.load_dataset`（HF 标准入口）+ 切分与 manifest 一致 + 列名符合 `row_unit`
3. `torch.DataLoader` **遍历全部 batch** + 定长校验 + `loss_mask` 与 manifest 口径对齐
4. 泄漏结论与 manifest 自洽
5. 每个 shard 的 md5 匹配

第3 关的「遍历全部」是踩过坑才加的：只看第一个 batch 时前几个恰好是满 block，
**看着是绿的**，shuffle 后立刻炸。

---

## 7. 与 G1 三臂对照实验的区别（刻意分开两条路径）

| | `train_from_dataset.py`（本文档） | `eval_training_utility.py`（G1） |
|---|---|---|
| 读什么 | **manifest + Parquet** | JSONL 语料 |
| 数据加载 | 标准 HF 入口 | 自己的 `load_corpus()` |
| 目的 | 证明「**这个数据集能被训练**」 | 三臂等量/等步数/噪声地板对照 |
| 回答 | 生产链是否打通 | 清洗**是否有效** |

**为什么刻意不合并**：如果数据集不可用这件事被对照实验的复杂逻辑包住，
它就不会暴露。两条路径各自独立地回答各自的问题。
---

## 8. 清洗 vs 未清洗的对照（`compare_cleaned_vs_raw.py`）

回答「清洗让这个数据集**更好**了吗」—— 这是生产系统必须回答的问题。

### ⚠️ 第一版对照被我否掉了

直接比两份训练报告的 val loss：清洗 6.4477 vs 未清洗 6.4469（差 0.0008）。
**这个比较无效**，两个硬伤：

1. **held-out 不同**（281 vs 274 block，两臂各自的 val 来自不同样本集合）
   → 唯一变量不是「清洗与否」，是**两个不同的尺子**。
2. **无噪声地板** → 0.0008 算不算差异无从判断。

### 现在的设计

| 要素 | 做法 |
|---|---|
| held-out | **清洗组的 test split**（207 block），两臂都**不训练它** |
| seed | 两臂**共用同一批 seed** → **配对设计** |
| 判据 | **配对符号检验**（每对 seed 的差是否同号） |
| 结果 | `data/reports/cleaned_vs_raw.json` |

### 为什么不用「max−min噪声地板」

第一版用「同臂跨 seed 的 max−min」当地板，实测 **Δ/地板 = 1.009** ——
判据刚好越过门槛就报「清洗更好」，这跟噪声没区别，属**假阳性**。两条硬伤：

- `max−min` 受单个极值支配，n=3 时极不稳定；
- 忽略了两臂**共用 seed** 的配对设计 —— 配对能消掉绝大部分 seed 效应，
  用独立样本的比较方式等于主动扔掉信息。

配对设计下唯一可看的量是「每对 seed 的差是否同号」，问题变成一个**符号检验**。
判据还要**诚实标注需要多少个同号对**才能达到 p<0.05（双尾，需 ≥6 个），
而不是看到 3/3 同号就报阳性。

### 结果（8 seed 配对，26 分钟）

| 量 | 值 |
|---|---|
| 清洗臂 held-out loss | **6.5033** |
| 未清洗臂 held-out loss | **6.5247** |
| Δ(未清洗 − 清洗) | **+0.0214** |
| 配对差同号 | **8/8 为正**（p = **0.0078** 双尾） |
| Cohen dz | 2.12 |
| 判定 | **清洗更好**（统计上成立） |

### ⚠️ dz=2.12 不等于「效应很大」

配对差的**绝对值只有 0.02**，能检出是因为**8 个差全部同号且方差极小**
（sd = 0.0101）。dz 大只说明「方向稳定」，不说明「幅度可观」。
对外表述应是「**方向确定，幅度小**」，不是「效果显著所以收益大」。

### ⚠️ 混淆变量：两臂训练量不同

清洗臂 1929 样本 / 1182 block，未清洗臂 1960 样本 / 1178 block。
「唯一变量只有清洗」这句话**字面上不成立** —— 清洗的结果本身就是样本数变了。
好在方向对结论有利：未清洗臂**样本更多**（多 31 条）却loss **更高**，
所以差异不能用「数据量」解释。但这句话必须写出来，不能装作没有。

### 判据被抓到的两个真缺陷

抽成纯函数 `paired_sign_test()` 后被自己的测试抓住：

1. **方向会反**：`elif k == 0: "...**清洗更好**"`，其中 `k = min(n_pos, n_neg)`。
   **全负时 `n_pos = 0`，`k` 也是 0** → 「清洗更差」被报成「清洗更好」。
   判据不能由「有没有相反符号」决定结论，必须由**符号方向**决定。
2. **`n_needed_for_p05` 差一格**：日志说「至少需 5 个同号」，
   但 5 个同号时 p = 0.0625 **仍不显著**，要 6 个。日志会骗读者。

两个变异（方向写死 / 去掉 p<0.05 门槛）都被 `tests/test_compare_criterion.py` 拦住。

### 未清洗组的数据问题（对照的直接证据）

`news_zh_v1_raw`（未过漏斗）的 minhash检出**3 对泄漏**：

- 1 对 Jaccard **1.0000** —— 同一篇新闻在语料里出现两次（转载）
- 2 对 Jaccard **0.8281** —— 同一新闻的不同来源版本

清洗组 `news_zh_v2` 泄漏 **0 对**（md5 + minhash 双检）。
这是「漏斗做了什么」最直接的量化，不依赖任何训练指标。

### 诚实边界

清洗丢弃量只有 **1.6%**（1960 → 1929，25 条转载重复），
所以这个实验回答的是「**这批数据**清洗有没有效」，
**不是**「清洗一般有没有效」。效应量本就小，样本量不足时**不能报「清洗更好」**。
