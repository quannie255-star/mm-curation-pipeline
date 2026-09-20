# 新手操作手册：从"下载数据"到"数据清洗"（零基础版）

> 写给第一次接触本项目、不懂代码的个人使用者。**所有命令都在本机实测过，贴出的
> 输出是真实运行结果**（2026-09-18，Windows + Git Bash + 系统 Python 3.11）。
> 一句话理解本项目：**它是一台装在你电脑上的"数据净水厂"——脏数据进去，干净数据
> 出来，还附一份体检报告，告诉你每一滴水是被哪道滤芯拦下的。**

---

## 第 0 步 · 先看一眼它在干什么（0 下载 / 0 GPU / 30 秒）

```bash
cd /c/Users/10393/Desktop/mm-curation-pipeline
"C:/Program Files/Python311/python.exe" -m streamlit run scripts/showcase_app.py
```

浏览器自动打开 `http://localhost:8501`。你会看到六个页签：
**总览 / 图文数据 / 文本数据 / 医疗数据 / 工业传感器 / 效果证据**。

第一件事：点「医疗数据」页签 → 按「重跑门禁」。它会**当场真跑一遍质检**（约 1.5 秒），
不是放的截图。这就是本项目的核心动作：拿一批"标准答案已知"的脏数据，验证清洗能不能
全部抓住、又不冤枉干净数据。

---

## 最重要的一句话：你不需要下载任何东西就能开始

真实世界的脏数据**没有标准答案**（没人能告诉你这 1000 条里哪几条是坏的），
所以本项目自带**合成语料**：程序现场造一批带标准答案的假数据来当考卷。
因此下面三条路线，按你的目的直接选：

| 你想干什么 | 走哪条路线 | 要联网吗 | 要 GPU 吗 | 耗时 |
|---|---|---|---|---|
| 只想看看它怎么工作 | **A** 跑合成领域门禁 | 不用 | 不用 | 2 秒 |
| 有自己的文本数据要洗 | **B** 喂自己的 jsonl | 不用 | 不用 | 秒级 |
| 跑官方图文全流程（看完整演示） | **C** 下载 COCO-CN | 要（走 hf-mirror 镜像） | 图像 L2 要 | 首次十几分钟 |

---

## 路线 A · 「我只想看看它怎么工作」

跑这两行（复制粘贴即可，都在本仓目录下执行）：

```bash
"C:/Program Files/Python311/python.exe" -X utf8 scripts/eval_fhir.py
"C:/Program Files/Python311/python.exe" -X utf8 scripts/eval_industrial.py
```

实测输出（医疗，真实截取）：

```
医疗 FHIR P/R: fhir_r4_quality（650 条全集, 5 算子）
算子                            扔   误杀 precision    主靶recall
phi_residual                 29    0    100.0%        100%
code_validity                25    0    100.0%        100%
unit_normalization           22    0    100.0%        100%
temporal_consistency         38    0    100.0%        100%
referential_integrity_fhir   36    0    100.0%        100%
漏斗门禁（3 seeds 最差口径）: 召回 100.0%（门限 ≥90%）, 误杀 0.00%（门限 ≤5%）→ PASSED
```

怎么读这张表：
- **扔** = 这个算子拦下了多少条脏数据；**误杀** = 冤枉了多少条干净数据（越低越好）；
- **主靶 recall 100%** = 那种坏数据一条没漏；
- 最后一行是**门禁**：不达标它就会以非零状态退出（相当于红灯），所以 CI 能自动拦。

---

## 路线 B · 「我有自己的文本数据要洗」（推荐，免下载免 GPU）

这是本手册的重点。三步走，全程不需要联网。

### 第 1 步 · 把数据变成一行一条的 jsonl

只需要两个字段：`id`（唯一字符串）和 `text`（正文）。新建 `my_data.jsonl`：

```json
{"id": "s1", "text": "数据质量是模型效果的上限。清洗后的语料更干净，检索精度更高，这是本项目要证明的第一件事。"}
{"id": "s2", "text": "太短了"}
{"id": "s3", "text": "哈哈哈啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊"}
{"id": "s4", "text": "数据质量是模型效果的上限。清洗后的语料更干净，检索精度更高，这是本项目要证明的第一件事。"}
{"id": "s5", "text": "请联系 admin@example.com 或拨打 13800138000 获取完整语料清单与清洗配置说明文档。"}
```

> 转 jsonl 的土办法：Excel 另存 CSV → 用下面这行 Python 一键转（把 `你的.csv` 换掉）：
> ```bash
> "C:/Program Files/Python311/python.exe" -X utf8 -c "import csv,json;rows=list(csv.DictReader(open('你的.csv',encoding='utf-8-sig')));open('my_data.jsonl','w',encoding='utf-8').write(chr(10).join(json.dumps({'id':str(i),'text':r.get('正文') or r.get('text') or ''},ensure_ascii=False) for i,r in enumerate(rows)))"
> ```

### 第 2 步 · 抄一份清洗配方（配置文件）

```bash
cp configs/text_funnel.yaml my_funnel.yaml
```

打开 `my_funnel.yaml` 改两处、删一处：

```yaml
dataset:
  raw_jsonl: my_data.jsonl          # ← 改成你的文件路径
output:
  dir: my_out                       # ← 改成你想输出到哪
operators:
  # ... 上面 7 级保留 ...
  # 删掉文件末尾这三行（它需要 GPU，见"常见坑"第 1 条）：
  # - op: perplexity
  #   params: {min: 0.2, batch_size: 64}
```

留下的 7 道滤芯各管一件事（这就是"清洗规则"的全部，不需要你写代码）：

| 滤芯 | 管什么 | 参数 |
|---|---|---|
| `doc_length` | 太短/太长 | min 30 字 |
| `chinese_ratio` | 中文占比过低（乱码、纯英文） | min 0.3 |
| `char_repetition` | 字符复读（"啊啊啊啊…"） | min 0.8 |
| `line_repetition` | 段落级复读 | min 0.8 |
| `boilerplate` | 广告/导航模板句 | min 0.8 |
| `pii_detect` | 手机号/邮箱/证件号 | min 0.9 |
| `text_minhash` | 近似重复（转载、洗稿） | threshold 0.7 |

### 第 3 步 · 跑

```bash
"C:/Program Files/Python311/python.exe" -X utf8 scripts/run_pipeline.py --input my_data.jsonl --config my_funnel.yaml
```

实测输出（上面那 5 条样例，6 → 1）：

```
漏斗: zh_wiki_text_curation  (6 -> 1)
stage                n_in   n_out  drop   pass%   score_p50
doc_length              6       5      1   83.3%   62.00
chinese_ratio           5       4      1   80.0%   0.92
char_repetition         4       3      1   75.0%   0.97
line_repetition         3       3      0  100.0%   1.00
boilerplate             3       3      0  100.0%   1.00
pii_detect              3       2      1   66.7%   1.00
text_minhash            2       1      1   50.0%   —
```

**怎么读**：`drop` 就是这道滤芯扔掉了几条，`pass%` 是存活率。从上往下读，能看出
"谁把谁拦了"。

### 你得到了什么（在 `my_out/` 里）

| 文件 | 内容 | 用途 |
|---|---|---|
| `cleaned.jsonl` | 干净数据（本例 1 条） | 直接拿去训练/入库 |
| `dropped.jsonl` | 被扔掉的数据，每行多一个 `dropped_by` 字段 | **审计**：谁扔的、为什么扔 |
| `funnel_stats.json` | 每一级的进出数量与分数分布 | 接监控/画图 |
| `report.md` | 人类可读的漏斗报告 | 发给同事/写进周报 |

`dropped.jsonl` 实测长这样——每一行都能追责：

```
s2 <- doc_length       | 太短了
s3 <- char_repetition  | 哈哈哈啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊
s5 <- pii_detect       | 请联系 admin@example.com 或拨打 13
s4 <- text_minhash     | 数据质量是模型效果的上限。清洗后的语料更干净…
```

> 注意 `s4`：它和 `s1` 内容完全相同，被 `text_minhash` 当重复留一删一。
> 这就是"去重"在干什么——**保留一份，其余进 dropped 而不是消失**，随时可回查。

---

## 路线 C · 「我要跑官方图文全流程」（要联网 + GPU）

⚠️ Git Bash 里**没有 `make`**（Makefile 用不了），所以下面给的是等价 python 命令。

```bash
# ① 下载数据（COCO-CN 中文图文对，自动走 hf-mirror 国内镜像）
"C:/Program Files/Python311/python.exe" scripts/download_dataset.py --limit 200   # 先小样冒烟
"C:/Program Files/Python311/python.exe" scripts/download_dataset.py               # 全量约 1.6k 对

# ② 注入 10 类可控脏数据 → 得到"带标准答案"的 2106 条
"C:/Program Files/Python311/python.exe" scripts/contaminate.py --config configs/contamination.default.yaml

# ③ 清洗（这一步就是"数据清洗"本体）
"C:/Program Files/Python311/python.exe" scripts/run_pipeline.py --config configs/pipeline.example.yaml

# ④ 体检：每个算子单独算 precision/recall
"C:/Program Files/Python311/python.exe" scripts/eval_operators.py

# ⑤ 建索引 + 对比实验（本项目最有说服力的一步）
"C:/Program Files/Python311/python.exe" scripts/build_index.py --name dirty_raw --input data/interim/contaminated/samples.jsonl --out data/indexes
"C:/Program Files/Python311/python.exe" scripts/build_index.py --name clean_v2 --input data/processed/cn_flickr_curation_v2/cleaned.jsonl --out data/indexes
"C:/Program Files/Python311/python.exe" scripts/eval_retrieval.py
```

结果：脏索引 Recall@1 = 0.459 → 清洗后 0.556（**+21%**）。
这就是"清洗到底值不值"的答案——不是感觉，是同一批查询在两个索引上的实测差。

---

## 常见坑（三个最常撞到的）

**1. `ModuleNotFoundError: No module named 'torch'`**
本机 Python 环境在 2026-09-03 被整体重置过，训练栈没装回来。两个选择：
- 省事：按路线 B 删掉需要它的算子（`perplexity` / CLIP 系列 / 各种 `finetune_*`）；
- 装回来：`"C:/Program Files/Python311/python.exe" -m pip install torch -i https://pypi.tuna.tsinghua.edu.cn/simple`（CUDA 版见 Makefile 的 `install-gpu`）。
**哪些命令需要它**：文本漏斗最后一层困惑度、图像漏斗的 L2（CLIP）、所有 finetune / 门禁里带模型的评测。

**2. `make: command not found`**
Git Bash 不带 make。把 Makefile 里对应目标的那行命令抄出来直接跑（本手册路线 C 已全部给好）。

**3. 中文乱码 / 默认 python 报缺 numpy**
- 产出中文的脚本一律加 `-X utf8`；
- **务必用系统 Python** `"C:/Program Files/Python311/python.exe"`——直接敲 `python`
  可能是另一个环境（无 numpy/torch）。

---

## 你能用这个项目做什么（七个真实用法）

1. **洗自己的文本数据**——一条命令进、四个产物出，不需要写代码（路线 B）。
2. **拿到一份可交付的质检报告**——`report.md`，每道滤芯的进出量与存活率。
3. **精确知道"谁被谁扔了"**——`dropped.jsonl` 带 `dropped_by`，可人工抽检、可申诉回捞。
4. **给自己的清洗规则打分（而不是凭感觉调参）**——本项目的"污染器"能给任意数据集
   注入带标注的脏数据，于是你的规则也能算出 precision/recall；
   再加 `threshold_scan.py` 扫出"召回-误杀"曲线，用拐点定阈值。
5. **训一个"懂你"的判官（零代码）**——`streamlit run scripts/judge_studio.py`：
   导入数据 → 点约 500 次二选一 → 一键训练 → 出成绩。实测：通用模型 0.532 → 你的判官 0.839。
6. **每天自动清洗 + 巡检（运维飞轮）**——`scripts/ops_daily.py` 串起"采集 → 巡检 →
   清洗 → 日报"，`scripts/ops_dashboard.py` 是驾驶舱；金融新闻域已实跑过真实线上数据。
7. **接一个自己的领域**——按 `docs/DOMAIN_PACKS.md` 的六件套（适配器/算子/污染器/
   合成语料/配置/门禁），最薄的领域包一个下午能跑通；医疗、工业就是这么加的。

---

## 它不做什么（别抱不切实际的期待）

- **不是云端 SaaS**，没有注册登录和网页版：它装在你电脑上，你自己跑。
- **医疗/工业那两个漂亮数字来自合成语料**（程序造的假数据），真实院内/现场数据未验证——
  这是项目自己写在文档里的边界，不是藏着不说。
- **不做前端产品**：Streamlit 页面只是演示入口，不是给终端用户的产品界面。
- **不做实盘交易、不做插件市场**（明确的 Out 范围）。
- 本机现在**没有 GPU 训练栈**：`finetune_*` 类命令要先装回 torch。

---

## 下一步去哪

| 你想… | 看这个文件 |
|---|---|
| 完整命令与排障 | `docs/RUNBOOK.md` |
| 每个漂亮数字的出处与分母 | `docs/PROOF_CHAIN.md`（还有数字自动校验 `scripts/verify_claims.py`） |
| 这个项目的产品定位与能力边界 | `README.md` + `docs/ROADMAP.md` |
| 面试/汇报叙事 | `docs/INTERVIEW.md` + `docs/RESUME.md` |
| 加一个新领域 | `docs/DOMAIN_PACKS.md` |
