# RUNBOOK：完整跑一遍（Windows / Git Bash，无需 make）

> 两个本机踩过的坑先说清楚：
> 1. **`.venv` 不可搬迁**：venv 创建时把当时的绝对路径烧进启动器，项目目录
>    改名（本项目曾从中文名目录迁来）后 `.venv` 里的 pytest.exe 等全部失效，
>    报 `Fatal error in launcher ... ??????`。本机可用环境一直是**系统 Python
>    （3.11）+ 用户目录 site-packages**——先 `deactivate`，直接用 `python`。
> 2. **Git Bash 没有 make**：所有命令给等价 python 直跑版（Linux/macOS 用
>    Makefile 目标等价替换即可）。

## 0. 环境（一次性）

```bash
deactivate 2>/dev/null          # 退出坏的 venv（如有）
python -V                       # Python 3.11.x
python -m pytest -q             # 健康检查：105 passed
```

依赖缺失时：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
GPU 依赖：`pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121`

## 0.1 Git 推送排障（两个独立故障，别混为一谈）

本机推送踩过两个**成因完全不同**的坑，现象都是「卡住/报错」，但修法无关：

### 故障 A：SSL 证书校验失败（报错快，有错误信息）

```
fatal: unable to access 'https://github.com/.../':
SSL certificate problem: unable to get local issuer certificate
```

修法——改用 Windows 系统证书库校验（本仓库已固化此配置）：

```bash
git config http.sslBackend schannel
```

原因：代理/VPN 接管的网络路径下，Git 自带的 OpenSSL CA bundle 常缺中间证书；
schannel 走系统证书库即可正常验证。**注意这与是否信任证书无关，别用
`http.sslVerify false` 绕过——那是把校验整个关掉。**

### 故障 B：凭据助手挂起（无输出，直到超时，最容易被误判为"网络不好"）

```
$ git push origin main
（光标卡住，无任何输出，几分钟后被 timeout 杀掉，exit 124）
```

**根因链**（三层，缺一不可理解）：

1. Git for Windows 在 **system 层**默认配 `credential.helper = helper-selector`。
   本机它解析不到已存的凭据（凭据管理器里确有 `git:https://github.com` 条目）。
2. 拿不到凭据后，git 回退到**终端交互式询问** username/password；非交互会话里
   stdin 无输入 → 永久阻塞。这就是"无输出地挂住"。
3. `-c credential.helper=wincred` **不是替换而是追加**——system 层的
   `helper-selector` 仍排在前面先执行，所以单加这个参数无效，看起来就像"修了但没用"。

修法——在 **global 层用空值先重置列表**，再指定 wincred（空值会清空已累积的
助手列表，包括 system 层那条）：

```bash
git config --global credential.helper ""        # 空值 = 重置助手列表
git config --global --add credential.helper wincred
```

验证（两步都应秒回，不该有任何卡顿）：

```bash
printf 'protocol=https\nhost=github.com\n\n' | git credential fill   # 应输出 username/password
git push --dry-run origin main
```

> 排查心法：`git ls-remote origin main` **秒回**就说明网络与 SSL 都正常，
> 那么 push 挂住必然是凭据环节（故障 B），不是网络。这个一秒的判断能省掉
> 半小时的无效重试。

## 0.2 Git 对象库灾难恢复（2026-09-20 实测走通）

**先分清「健康」的真假信号**——这是本次事故最贵的一课：

| 命令 | 它到底在干什么 | 能否当健康检查 |
|---|---|---|
| `git rev-parse HEAD` | 只读 `.git/refs/heads/main` 这 41 字节**文件**，不碰对象库 | ❌ 假的。对象全丢也照常返回 |
| `git status` | 读 index + 工作区，对象缺失时也可能看似正常 | ❌ 不可靠 |
| `git cat-file -t HEAD` | 真的去对象库取这个对象 | ✅ |
| `git fsck --full --strict` | 全库可达性与对象完整性 | ✅ 唯一权威 |

**症状**：`git count-objects -v` 的 `count` 归零而 `in-pack` 只剩旧历史、
`git fsck` 报 `missing blob` 与 `invalid sha1 pointer`。**工作区文件通常完好**
——丢的只是 `.git/objects`。

**恢复（从远端重建对象，全程不需要 `github.com` 的 git 协议）**：

```bash
# 1. 取远端提交清单（api.github.com 稳定；github.com 可能被代理挡 502）
#    curl https://api.github.com/repos/<owner>/<repo>/commits?sha=main&per_page=100
# 2. 找一个「本地仍完好」的祖先提交做协商锚点（缺失段的前一个）
#    git cat-file -t <anchor>   # 必须返回 commit
# 3. 空临时裸库 + 播种本地旧 pack + 把 ref 指到该锚点
git init --bare /tmp/recover_bare.git
cp .git/objects/pack/*.pack .git/objects/pack/*.idx /tmp/recover_bare.git/objects/pack/
git -C /tmp/recover_bare.git update-ref refs/heads/main <anchor>
# 4. 只拉增量（服务器发缺失段）
git -C /tmp/recover_bare.git -c http.version=HTTP/1.1 \
    -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=999999 \
    fetch --no-tags https://github.com/<owner>/<repo>.git main
# 5. 对象按内容寻址 → 拷回真实仓库是纯增量，不可能改坏已有对象
cp /tmp/recover_bare.git/objects/pack/pack-* .git/objects/pack/
git multi-pack-index write && git commit-graph write --reachable
```

**两条必须知道的坑**：

1. **别在真实仓库里直接 `git fetch`**——`refs/heads/main` 已经指向丢失的提交，
   协商阶段 git 会认为「我已拥有它」从而**什么都不下载**（negotiation trap）。
   必须在一个**没有该 ref 的裸库**里 fetch，并把 ref 指到一个已知完好的祖先。
2. **`github.com` 只是慢/不稳，不等于不可用**。本次实测：`codeload` 的 tarball
   被限速到 ~22KB/s（9.2MB 要 **418 秒**，首块就要等 46 秒，极易误判为「卡死」）；
   而 git 协议走增量只有 **625KB / 20 秒**。**用「超时」当「失败」的证据之前，
   先量一次吞吐**——判标是字节/秒，不是「等了多久」。
   **`codeload` 忽略 `Range` 头**（实测带 `Range: bytes=0-65535` 仍返回 `200` +
   全量 `Content-Length: 9218131` 且无 `Content-Range`），所以「分片下载绕开限速」
   这条路**不存在**——一次都别试。

**验收（逐位一致，不接受近似）**：

```bash
git cat-file -t HEAD                      # 必须 commit
git rev-list --count HEAD                 # 必须等于远端提交数
git fsck --full --strict                  # 必须 rc=0 且零异常行
git log --oneline -3                      # 提交信息应是原文
```

**预防（比恢复便宜一万倍）**：对象是**唯一不可再生**的资产——工作区文件丢了能重写，
对象丢了只能靠网络。所以：

- **动 `objects/` 之前先备份**：`git bundle create /tmp/repo.bundle --all`
  或 `cp -r .git /tmp/git-backup`。
- **不要用 `git prune --expire now` / `git gc --prune=now` 来「清垃圾」**：
  它删的是「按当前可达性判定的不可达对象」，一旦判错**没有回滚**
  （对象就是一个个内容寻址的裸文件）。85MB 是磁盘问题，不是正确性问题。
- 定期 `git repack -a -d`：对象进了 pack 至少有 `multi-pack-index` 一层结构保护。

## 1. 完整复现（按管道顺序，每步有验收数字）

| 步骤 | 命令（make-free） | 耗时 | 验收 |
|---|---|---|---|
| 数据准备 | `python scripts/download_dataset.py` 然后 `python scripts/contaminate.py --config configs/contamination.default.yaml` | ~2 分钟 | contaminated = 2,106 条（1,620+486） |
| 清洗漏斗 | `python -X utf8 scripts/run_pipeline.py` | ~3 分钟 GPU | 2106→1585；召回 100%/误杀 2.16% |
| 双索引 | `python scripts/build_index.py --name clean_v2 --input data/processed/cn_flickr_curation_v2/cleaned.jsonl`<br>`python scripts/build_index.py --name dirty_raw --input data/interim/contaminated/samples.jsonl` | ~3 分钟 GPU | 两个 manifest 生成 |
| 灵魂实验 | `python -X utf8 scripts/eval_retrieval.py` | ~2 分钟 | R@1 0.459→0.556（+21%） |
| 算子 P/R | `python scripts/eval_operators.py` | ~4 分钟 | data/reports/operator_pr.md |
| 阈值扫描 | `python scripts/threshold_scan.py` | ~5 分钟 | 5 张 PNG 曲线 |
| 采样对比 | `python scripts/eval_sampling.py` | ~2 分钟 | budget=1000 R@1 +24% |
| 消融 | `python scripts/eval_ablation.py` | ~3 分钟 | 去重组 R@1 -0.017 |
| 随机删 baseline | `python -X utf8 scripts/eval_random_drop_baseline.py` | ~3 分钟 | 漏斗 0.575 vs 随机 0.447，净贡献 +0.128 |
| 多 seed 门禁 | `python -X utf8 scripts/eval_fhir.py --seeds 42,7,2026` | ~20 秒 | 最差口径召回/误杀入报告 stability 字段 |
| 检测器 | `python -X utf8 scripts/train_detector.py` | ~4 分钟 GPU | testA 98.2%/testB 87.3% |
| CLIP 微调 | `python -X utf8 scripts/finetune_clip.py` | ~20 分钟 GPU | clean_ft 0.688 vs dirty_ft 0.636 |
| 成本核算 | `python -X utf8 scripts/cost_model.py` | ~2 分钟 | cost_model.md 四维表 |

> Windows 注意：产出中文的脚本加 `-X utf8`；指标随污染 seed 有 ±1pp 正常浮动。
> 模型权重在 `models/`（gitignore）：新机器先跑任意 CLIP 命令触发下载，
> 再 `python scripts/convert_clip_weights.py`（详见 FAQ）。
> 推送报 `SSL certificate problem: unable to get local issuer certificate` 时：
> `git config http.sslBackend schannel`（改用 Windows 系统证书库校验，本仓库
> 已固化此配置）——代理/VPN 接管的网络路径下 OpenSSL 自带 CA bundle 常缺
> 中间证书，schannel 走系统证书库即可正常验证。

## 1.5 文本语料实例（V2 β，`text_article` 模态全流程）

| 步骤 | 命令 | 耗时 | 验收 |
|---|---|---|---|
| 语料下载 | `python -X utf8 scripts/download_text_corpus.py` | ~2 分钟 | data/raw/text_corpus.jsonl 302,002 篇（维基 zh） |
| 去重基准 | `python -X utf8 scripts/text_dedup_benchmark.py` | ~3 分钟 | data/reports/text_dedup_benchmark.md：10 万档 exact 召回 1.0 / near 0.97 / 21s |
| GPT-2 训练对比 | `python -X utf8 scripts/finetune_gpt2.py` | ~1.5 小时 GPU | data/reports/finetune_text_eval.md：dirty_ft 的 held-out ppl 比 clean_ft 高 >5%（默认剂量 100%，四种真损伤） |
| 文本漏斗 | `python -X utf8 scripts/run_pipeline.py --config configs/text_funnel.yaml` | ~2 小时（10 万档约 40 分钟） | 302,002 → 181,980（保留 60.3%）；text_minhash 合并 88,272 / perplexity 拦 303 |

### 1.5.1 真实脏数据试跑（新闻语料，2026-09-06）

```bash
python -X utf8 scripts/run_pipeline.py --config configs/text_funnel.yaml   --input data/raw/news_corpus.jsonl    # 2066 篇真实爬取新闻
```

实测 **2066 → 2031（保留 98.3%）**：chinese_ratio 拦 5（1 篇 GBK 乱码 + 4 篇
低占比边缘案例）、text_minhash 合并 29 条真实转载、perplexity 拦 1（人名连串）。
同语料在 chinese_ratio 语义修复（去空白后计占比，#65）之前只保留 61.5%——
爬虫空白膨胀曾把 773 篇正常新闻拖过阈值。注：β 维基漏斗数字（60.3%）为旧
语义产物；干净文本空白占比 ~1.4%，语义修正对该口径影响 <2pp，方向只减误杀。

> 文本算子/污染器的 GPT-2 权重走本地 safetensors：首次使用会自动从
> HF 缓存转换（`mm_curation/gpt2_weights.py` 的 `ensure_local_gpt2()`）；
> 缓存为空时按报错提示先 `snapshot_download('uer/gpt2-chinese-cluecorpussmall',
> endpoint='https://hf-mirror.com')`。

## 1.7 Ray 双运行时（V2 γ）

同一份 YAML 配置，`runtime: local`（默认，串行）与 `runtime: ray` 两种执行器
（`RayDistributedExecutor`，ray 懒加载：`pip install curation-eval[ray]` 或
`pip install ray`，不装 ray 零影响）。等价性口径与确定性约定见
docs/design_tables.md γ 决策点 3。

| 步骤 | 命令 | 耗时 | 验收 |
|---|---|---|---|
| 双运行时基准 | `python -X utf8 scripts/ray_funnel_benchmark.py --n 100000` | ~4 分钟 | data/reports/ray_funnel_benchmark.md：kept 集相等 + StageStat 相等 + 逐 id 分数相等；10 万档 local 21s / ray 92s（单机不追求更快，价值在横向扩展） |

注意：driver 的 sys.path 不传播给 ray worker——脚本方式使用时把 `src` 放进
worker 的 PYTHONPATH（`ray.init(runtime_env={"env_vars": {"PYTHONPATH": ...}})`）；
包以 editable 方式安装则 worker 可直接解析 `curation_eval`/`mm_curation`。

## 1.8 L3 LLM-judge（V2 δ）

三层漏斗的最后一层：judge 是普通注册算子，走 OpenAI 兼容协议，服务端可插拔
（本机 serve_judge.py / Linux 上 vLLM / 云端 API，算子零改动）。judge 的可信度
用 Cohen's kappa 对 ground truth 结算——它不是真理，一致性才是卖点。

| 步骤 | 命令 | 耗时 | 验收 |
|---|---|---|---|
| 启动判官服务 | `python -X utf8 scripts/serve_judge.py` | 首次 +1GB 下载 | 监听 127.0.0.1:8100（Qwen2.5-0.5B-Instruct；`--model Qwen/Qwen2.5-1.5B-Instruct` 换底座，8GB 显存实测可用） |
| kappa 实验 | `python -X utf8 scripts/eval_judge.py --n 400` | ~10 分钟 | data/reports/judge_kappa.md：judge vs 脏标签 / judge vs L1 / L1 vs 标签 三 κ + 分歧样本 |
| kappa 实验（1.5B 判官） | `python -X utf8 scripts/eval_judge.py --base-url http://127.0.0.1:8100/v1 --workers 2 --timeout 120` | ~45 分钟 | 1.5B 单次判分 6-24s，4 并发会撞 30s 默认超时——降并发 + 加超时；κ 0.5B ≈0 → 1.5B 0.247（V3 ι，报告跑前备份为 judge_kappa_0p5b.json） |
| L3 漏斗 | `python -X utf8 scripts/run_pipeline.py --config configs/text_funnel_llm.yaml` | 同漏斗 + 抽样调用 | L1+去重+困惑度后 judge 抽 10% 终审；on_error: skip 服务挂不死漏斗 |

设计要点：确定性抽样（同 config 重跑抽同一批）；解析失败/服务异常 → 保留
不评判（score=None），L3 是增强不是阻塞；成本口径见 `judge_stats_snapshot()`。

## 1.9 数据 CI 门禁（V2 ε）

数据质量门禁：与代码 CI（单测验证「逻辑对」）互补——本门禁在合成带标注语料上
**真跑去重实现**，质量数字低于门限即红。GitHub Actions 每周自动跑
（`.github/workflows/data-ci.yml`），本地一键验证：

```bash
python scripts/data_ci_benchmark.py          # 绿：exact 1.0 / near 0.954 / 误杀 0（0.5s）
python scripts/data_ci_benchmark.py --threshold 0.95   # 演示劣化变红（exit 1）
```

门限：exact ≥0.99 / near ≥0.90 / base 误杀率 ≤1%。合成语料 seed 固定可复现；
损伤强度按 α 校准方法论标定（删 1 词 + 邻位交换 → J∈[0.86,0.92]——损伤与
门限是耦合参数，先标定生成器再定门限，门禁测的是去重实现不是生成器）。

## 1.10 个人微调平台·首战「专属数据判官」（V3 ζ）

四步闭环：获取 → 清洗（复用 V2 漏斗）→ 冻结 benchmark → LoRA 微调 → κ 出数。

| 步骤 | 命令 | 耗时 | 验收 |
|---|---|---|---|
| 数据获取 | `python -X utf8 scripts/fetch_news_corpus.py --max-docs 2000` | ~35 分钟 | data/raw/news_corpus.jsonl（robots 合规/限速/幂等；当前 705 篇） |
| 冻结 benchmark | `python -X utf8 scripts/build_judge_benchmark.py` | ~1 分钟 | benchmarks/judge_news_v1/（300 条 150/150，manifest 含 seed 隔离与泄漏检查） |
| LoRA 微调 | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python -X utf8 scripts/finetune_judge_lora.py --n-clean 500 --n-dirty 500 --epochs 3 --batch 4` | ~70 分钟（8GB 本机） | adapter 落 models/judge_lora_v1 + 实验 ledger runs/experiments.jsonl |
| 出成绩表 | `python -X utf8 scripts/run_judge_benchmark.py --adapter models/judge_lora_v1` | ~3 分钟 | **通用 κ=-0.024 → 微调 κ=+0.560（P=0.706/R=0.960，解析率 100%）** |

工程红线（两次阴性训练换来的，见笔记 #58-59）：训练与推理必须同一
chat template 协议；**completion 必须完整落在窗口内**（prompt 按
completion 长度预留预算截断）——否则 loss 曲线健康、任务被静默替换。
训练/推理窗口不一致会引入视野错位，两侧统一 640。

## 1.11 偏好闭环·首战（V3 η-a）

三步：偏好数据构造（persona-oracle）→ DPO 双判官 → 冻结偏好 benchmark 出分歧率。

| 步骤 | 命令 | 耗时 | 验收 |
|---|---|---|---|
| 偏好数据+冻结题 | `python -X utf8 scripts/build_pref_data.py` | ~1 分钟 | data/interim/pref_dpo.jsonl 880 三元组 + benchmarks/pref_news_v1 150 题（结构性排除 judge 训练/评测源） |
| DPO 双判官 | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python -X utf8 scripts/finetune_judge_dpo.py --persona PA --out models/judge_pref_PA`（PB 同理） | 各 ~17 分钟（8GB 本机） | adapter ×2 + 实验 ledger |
| 出钱表 | `python -X utf8 scripts/run_pref_benchmark.py` | ~8 分钟 | **PA 命中率 0.933 / PB 命中率 0.867（线 ≥0.75）；分歧率 0.783（线 ≥40%）；通用基线 0.48/0.40** |

工程红线（一次学崩换来的，见笔记 #60-61）：DPO 的 chosen/rejected 必须是
**最小对**（唯一差异在判别维度上）——差异面在模板文本上时梯度全被模板吸收；
评测解析器只抓判别字段（别要求完整 JSON），max_new_tokens 留足闭括号余量。

## 1.12 抽取忠实性判官（V3 η-b，首轮未达标·如实记录）

| 步骤 | 命令 | 说明 |
|---|---|---|
| 数据+冻结题 | `python -X utf8 scripts/build_ext_data.py` | 250 三元组 + ext_news_v1 80 题（三损伤分层，结构性排除全部既有占用） |
| DPO 训练 | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python -X utf8 scripts/finetune_judge_dpo.py --persona EXT --data data/interim/ext_dpo.jsonl --out models/judge_ext_v1 --max-prompt 1120 --max-completion 48 --max-length 1168 --epochs 2` | 0.5B 本机 ~55 分钟 |
| 出数 | `python -X utf8 scripts/run_pref_benchmark.py --benchmark benchmarks/ext_news_v1 --adapters EXT=models/judge_ext_v1 --max-length 1700` | 见下 |

**首轮结果（未达标）**：微调判官 number_swap 0.53 / omit 0.50 / hallucinate
0.41 ≈ 通用基线（0.47/0.41/0.41）——验收线 ≥0.75 未过。根因诊断：抽取忠实性
要求逐条事实回原文对齐（真阅读理解），超出 0.5B 能力下限（对照 η-a 偏好裁决
靠浅层特征可达 0.93）。**业务结论见判官能力矩阵（笔记 #62）**：忠实性判官
需 1.5B+ 基座，重训选项=租卡或 Qwen2.5-1.5B 量化。

**1.5B 追加实测（SFT 退化路径）**：1.5B 上 DPO 的 ref 显存翻倍触发 WDDM
回落（258s/step），按设计表降级 SFT（梯度检查点+batch1+grad-accum 16）。
结果 0.59/0.48/0.52——仍 ≈ 随机。**能力悬崖在 1.5B 之后**：忠实性判官需
7B+（租卡）或任务重设计（分解式逐条判官）。矩阵固化在
benchmarks/capability_matrix.json（平台前端直接渲染）。

## 1.13 判官成本核算 + 平台控制台（业务层）

```bash
python -X utf8 scripts/judge_cost_report.py      # 成本选型表
make platform                                     # 或 streamlit run scripts/platform_app.py
```

**成本表（每万条，口径见报告头）**：本机 0.5B 判官 ¥0.11-0.32 / 云 API
¥26 / 人工 ¥2500——本机小判官比 API 便宜约 235 倍、比人工便宜约 23000 倍，
但质量以能力矩阵为准（成本优势只对 ✅ 任务有效）。

**平台控制台四页签**：判官能力矩阵（冻结 benchmark 实测渲染）/ 成本选型
计算器（参数可调实时算钱）/ A/B 偏好标注（真人选择→协议文件，η-a
persona-oracle 的真人化入口）/ 命令速查。

### 1.9.1 图像去重门禁（ε 补强，2026-09-03）

同一方法论扩到图像模态，锁 md5_exact / phash_near（data-ci.yml 同工作流自动跑）：

```bash
python -X utf8 scripts/data_ci_image_benchmark.py             # 绿：exact 1.0 / near 0.994 / 误杀 0（~50s）
python -X utf8 scripts/data_ci_image_benchmark.py --calibrate # 打印 near 距离分布（改损伤后先标定）
python -X utf8 scripts/data_ci_image_benchmark.py --crop 0.80,0.90   # 劣化注入：near 0.152 → 红（exit 1）
```

门限：exact ≥0.99 / near ≥0.97 / 误杀 ≤1%（near 生成器实测 0.994，留 2.4pp
劣化余量）。标定过程三收窄（裁剪 4~10% → 1~5%）：块状合成图对裁剪比真实
照片敏感（裁剪移动 8px 块网格），V1 参数下 15.6% 样本超阈——按预案修生成器
而非放水门限。

### 1.9.2 阈值回归门（ε2，2026-09-03）

锁「整条阈值敏感性曲线」而非单点数字：threshold_scan 曲线对冻结基线
`configs/threshold_baseline.json` 逐点比对（recall 偏移 >0.05 / 误杀偏移
>0.02 即红），防上游**换库版本/换实现**静默漂移（单测绿、单点门禁过、
曲线先变）。锁轻依赖双主算子 minhash_lsh / phash_near（blur 要 opencv、
clip/semantic 要编码器，不入 CI），data-ci.yml 自动跑：

```bash
python -X utf8 scripts/threshold_regression_gate.py                   # 绿：2 算子逐点比对通过（~13s）
python -X utf8 scripts/threshold_regression_gate.py --update-baseline # 重生成冻结基线（diff 需人工审查）
```

维护约定：升级 imagehash/datasketch 后若门禁红，先看基线 `meta.env` 版本
归因——确认是环境升级而非实现劣化后，`--update-baseline` 显式接受新曲线；
扫描区间/生产默认与 `scripts/threshold_scan.py` 共用 THRESHOLD_SPECS
（单一定义源），改区间必须重新生成基线。

## 1.14 偏好判官工坊（V3 θ，2026-09-06）

大众入口（推荐）：`make studio`（= `streamlit run scripts/judge_studio.py`）——
五步向导：①导入（粘贴/上传/一键示例语料）→ ②点击标注（甲/乙/都不合格，
建议 ≥150 对，最低 80）→ ③一键训练（subprocess 现有 DPO 脚本，日志实时）→
④评测出分（冻结 benchmark + 通用基线对比）→ ⑤试用（贴任意一对，判官裁决）。
训练/评测期间勿刷新页面（Streamlit 单线程阻塞）。

命令行等价（全部可独立复现）：

```bash
# 真人标注（向导②步落盘 data/annot/pref_labels_v2.jsonl，全文+变体元数据）
python -X utf8 scripts/build_user_pref_data.py --labels data/annot/pref_labels_v2.jsonl
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True   python -X utf8 scripts/finetune_judge_dpo.py   --persona USER --data data/interim/pref_user_dpo.jsonl --out models/judge_pref_USER
python -X utf8 scripts/run_pref_benchmark.py   --benchmark benchmarks/pref_user_v1 --adapters USER=models/judge_pref_USER --generic

# 模拟用户（流程验收账，与真人分文件分账；seed 53 完全可复现，产物不入库）
python -X utf8 scripts/build_oracle_labels.py --n-docs 250
python -X utf8 scripts/build_user_pref_data.py --labels data/annot/pref_labels_oracle.jsonl

# 学习曲线实验（50/100/200 点击各训一次；--min-pairs 放宽下限）
python -X utf8 scripts/build_user_pref_data.py --labels <v2/oracle>   --limit 50 --min-pairs 30 --out-dpo data/interim/pref_user_lc50.jsonl   --out-benchmark benchmarks/pref_user_lc50
```

**模拟用户验收（2026-09-06 实测，冻结考卷 77 题 = 62 main + 15 对照）**：
通用基线 main 0.532 / 对照 0.40 → 550 条标注（536 三元组）训出判官
**main 0.839（+30.6pp）/ 对照 0.80**。学习曲线警示：188 main 对同配方
只有 0.532（≈通用，未学会）——**最少标注量 ≈ 500 对**是当前证据下的
产品参数；向导建议量已按此设定，MIN_PAIRS=80 只是流程下限而非达标线。

纪律：真人标注/其 benchmark 属个人数据，默认不入库；评测报告按 benchmark 名
落盘（pref_alignment_<name>.json），向导不会覆盖 η-a 的报告。示例语料经
`load_news_corpus_excluded()` 结构性排除 judge/pref/ext 全部既有占用。
追加标注不换考卷：`build_user_pref_data.py --freeze-eval-from
benchmarks/pref_user_v1/items.jsonl`（冻结 main 题 source_id 强制留评测，
其余全进训练）。

## 1.15 规模拐点：local vs Ray 交叉曲线（V3 κ，2026-09-15）

回答「多大才该开 Ray」：语料 10 万 → 100 万档梯度，双运行时各一遍，
每档等价性三口径（kept 集 / StageStat / 逐 id 分数）全等。

| 步骤 | 命令 | 耗时 | 验收 |
|---|---|---|---|
| 扩量语料（独立文件，勿混入 β 基线） | `python -X utf8 scripts/download_text_corpus.py --out data/raw/text_corpus_1m.jsonl --docs 1000000` | ~10 分钟 | 1,001,764 篇；text_sources.SHARDS 已扩到全 6 分片 |
| 梯度双跑（每档一条，串行执行） | `python -X utf8 scripts/ray_funnel_benchmark.py --n 300000 --corpus data/raw/text_corpus_1m.jsonl --out scale_crossover/n300k`（另 `--n 100000` 默认落 γ3 路径；`--n 1000000`） | 100k ~6 分钟 / 300k ~14 分钟 / 1M ~90 分钟 | data/reports/scale_crossover/n*.json |
| 汇总曲线 | `python -X utf8 scripts/scale_crossover_report.py` | 秒级 | data/reports/scale_crossover.{md,png} |

**实测结论（单机 8 逻辑核）**：local 全区间胜——100k 113.6s/251.6s（2.21×）、
300k 355.8s/490.5s（1.38×）、1M 1605.5s/3749.8s（2.34×）；Ray 无回本点，
1M 档反升源于内存压力。**Ray 的回本条件是多机横向扩展，不在单机加大 n。**
测量纪律：梯子各档串行、与 GPU 任务分时——计时实验与任何后台负载并跑
即作废（本会话实测：同机有 judge 评测并行时 100k local 从 24s 膨胀到 114s）。

## 1.16 SemDeDup 语义剪枝采样（V3 λ，2026-09-15）

池向量聚类 → 簇内冗余剔除 → 分层补足（`SemanticPruneSampler`，faiss 单依赖）。
向量直接从 clean_v2 索引 reconstruct（图像塔嵌入 = 检索指标自己的空间）。

| 步骤 | 命令 | 耗时 | 验收 |
|---|---|---|---|
| 消融评测 | `python -X utf8 scripts/eval_sampling.py --budgets 1200 1000 800 --prune-fracs 0.1 0.2 0.3 --out data/reports/sampling_semde_dup.json` | ~2 分钟 | 报告 + 对照表；ε=0.3 剪穿池子时 n_indexed < budget 属预期，如实呈现 |
| 单测 | `python -X utf8 -m pytest tests/test_sampling.py -q` | 秒级 | 16 条全绿（含离群剔除/预算守恒/可复现/无向量保守保留） |

**实测结论（诚实阴性）**：semde_dup 在 9 个组合中仅 1200 档 ε=0.1 R@10 微胜
stratified（0.731 vs 0.706），R@1/MRR 全部不敌且 ε 越大越差。归因：本池是
漏斗清洗后的产物，语义冗余已被去重四件套在上游吃掉——「下游无冗余可剪」
反向证明上游去重质量。详见 data/reports/sampling_semde_dup.md + 笔记 #66。

## 1.17 OPS 日常运维飞轮（ops-flywheel，2026-09-15 起）

30 天数据飞轮一期（R0-R4，PRD 见 docs/OPS_PRD.md，设计表见 design_tables.md
OPS w1 节）：每日定时采集 A 股结构化行情（findata）+ 个股新闻文本（akshare），
双管道加工后出一份日报。ops 壳只编排不实现——findata 侧复用其
`scripts/daily_pipeline.py`（采集→巡检→推送→归档）。

| 动作 | 命令 | 说明 |
|---|---|---|
| 每日运维（单入口） | `python -X utf8 scripts/ops_daily.py` | findata_daily → fetch_text → funnel → audit → report；非零即停但日报必出 |
| 调试（不跑 findata） | `python -X utf8 scripts/ops_daily.py --skip-findata` | 未配 findata / 只验证文本链路 |
| 冒烟 | `python -X utf8 scripts/ops_daily.py --dry-run` | 只打印步骤与产物检查，零副作用 |
| 手动采新闻 | `python -X utf8 scripts/fetch_finance_news.py --symbols 600519` | 幂等增量；全部 symbol 失败才 exit 1 |
| 漏斗（金融新闻） | `python -X utf8 scripts/run_pipeline.py --config configs/text_funnel_finance.yaml` | 全量重跑（全局去重视角）；阈值待真实数据轮校准 |
| 定时任务 | `python -X utf8 scripts/ops_install_schedule.py`（预览）→ `--arm`（注册） | 每日 20:00；**R8 环境冻结完成前不要 --arm**；`--disarm` 删除 |
| 单测 | `python -X utf8 -m pytest tests/test_ops_daily.py -q` | 8 条全离线 |

产物与路径：日报 `data/reports/daily/YYYY-MM-DD.md`（顶部「今日异常」三行置顶）；
台账 `data/ops/stats.jsonl`（追加式，预期带告警 = 当日新增 < 近 7 天中位数 50%）；
逐步日志 `data/ops/logs/`；新闻语料 `data/raw/finance_news/news_corpus.jsonl`。
前置：findata 仓库在 `FINDATA_PATH`（缺省桌面 `FinData-Agent`，需已建 .venv）；
akshare 已装（系统 Python 实测 1.18.35）。首次联网真跑验收：
`fetch_finance_news.py --symbols 600519` 落 ≥1 条 → `ops_daily.py --skip-findata` 出首份日报。

**驾驶舱（前端）**：`streamlit run scripts/ops_dashboard.py` —— 五页签：
今日日报（异常置顶标红）/ 趋势（新增/保留率/磁盘曲线）/ 丢弃审计（dropped_by
柱状 + 抽样）/ 新闻语料（按股票筛选）/ 手动运行（触发 ops_daily 实时日志 tail，
judge_studio 同款模式）。零额外采集，只读 ops_daily 落盘产物。

**首跑实录（2026-09-15，完整链路 exit 0，耗时 600.6s）**：findata 采集入库
（stock_daily 28,477 行 / valuation 28,400 / index 3,420，25 标的）+ 巡检
信号 9 / 告警 7 / 健康分 72；文本新闻 25 只采集 214 条；漏斗 214 → 163
（保留率 76.2%）。**第一天 audit 就抓到金融域校准问题**：chinese_ratio 丢弃
46 条（21.5%），抽样全是数字密集的行情快报——已进 docs/ops_backlog.md 首条；
磁盘水位告警同步触发（85.3% > 80%）。

## 1.18 医疗 FHIR 模态评测（V4 α，2026-09-17）

第三个模态 `fhir_resource` 零框架特例接入：FHIR 资源经 `FHIRSample`
（curation-eval 包 v0.3.0）展平为 Sample（text = 资源 canonical JSON），
5 个医疗算子 + 5 类合规污染器 + 确定性合成语料，执行器/评测器零改动。

| 步骤 | 命令（make-free） | 耗时 | 验收 |
|---|---|---|---|
| 评测+门禁 | `python -X utf8 scripts/eval_fhir.py` | ~5 秒 | 500 条语料 + 150 注入；五算子主靶 recall 100%/误杀 0；漏斗召回 100% ≥90%、误杀 0% ≤5% → **PASSED exit 0** |
| 冒烟档 | `python -X utf8 scripts/eval_fhir.py --scale 0.1 --seed 7` | ~1 秒 | 50 条语料，报告落盘门禁绿 |
| 观测模式 | `python -X utf8 scripts/eval_fhir.py --no-gate` | ~5 秒 | 只出报告不设退出码 |
| 漏斗串联 | `python -X utf8 scripts/run_pipeline.py --config configs/funnel_fhir.yaml` | ~1 秒 | data/processed/fhir_funnel |

- 报告：`data/reports/operator_pr_fhir.{json,md}`（格式与 operator_pr 一致 + 漏斗门禁段）。
- 确定性：同 `--seed` 语料逐字节一致；码表（ICD-10 30/LOINC 15/ATC 10）、UCUM
  单位表、假名池内嵌于 `src/mm_curation/data/fhir_synth.py`，与算子/污染器同源。
- 诚实边界：语料全部程序生成，无任何真实患者数据；姓名池为通用常见姓名样式。
- `make eval-fhir` 等价于第一条命令。

## 1.19 工业传感器模态评测（V5 α，2026-09-17）

第四模态 `industrial_sensor` 零框架特例接入：一窗一 Sample（通道×256 读数），
SensorSample 适配器（包 v0.4.0），5 个工业算子 + 5 类污染器 + 确定性合成语料。
领域增强包规范（六件套/扩展步骤/红线）见 docs/DOMAIN_PACKS.md。

| 步骤 | 命令（make-free） | 耗时 | 验收 |
|---|---|---|---|
| 评测+门禁 | `python -X utf8 scripts/eval_industrial.py` | ~6 秒 | 1056 窗 + 317 注入；五算子主靶 recall 100%；漏斗召回 100% ≥90%、误杀 1.04% ≤5% → **PASSED exit 0** |
| 冒烟档 | `python -X utf8 scripts/eval_industrial.py --scale 0.1 --seed 7` | ~2 秒 | 报告落盘门禁绿 |
| 漏斗串联 | `python -X utf8 scripts/run_pipeline.py --config configs/funnel_industrial.yaml --input data/raw/sensor_synth/corpus.jsonl` | ~2 秒 | data/processed/industrial_funnel |

- 报告：`data/reports/operator_pr_industrial.{json,md}`。
- 核心算子故事：「计划检修静默（合法）vs 采集链路故障（异常）」判别——检修计划事件
  进样本流建索引，业务事件源不外置（金融「停牌 vs 采集失败」的工业映射）。
- 口径提醒：独立评测下批量算子（drift）的误杀会被其他类型灾难注入污染（报告有注），
  漏斗串联门禁才是端到端承诺口径。

## 1.20 前置改写通道 + 归一化层 + 判决书（V6 α，2026-09-20）

漏斗框架原本只有一条通道（`Operator`：打分 → 阈值），**没有「改写样本」的通道**。
V6 α 新增与算子**并列**的 `Transformer` 协议（包 v0.5.0），把归一化放在**所有打分之前**，
并把每一次裁决落成可审计的判决书记录。

| 步骤 | 命令（make-free） | 耗时 | 验收 |
|---|---|---|---|
| A/B 对照（规则档，纯 CPU） | `python -X utf8 scripts/normalize_ablation.py` | ~30 秒 | 2066 篇双跑；报告 `data/reports/normalize_ablation.{json,md}`；同码双跑**逐字节相同** |
| A/B 对照（含 perplexity） | `python -X utf8 scripts/normalize_ablation.py --allow-model --report-name normalize_ablation_with_model` | ~3 分钟 | 8 算子档；另存报告名避免覆盖 |
| 冒烟（前 300 篇） | `python -X utf8 scripts/normalize_ablation.py --limit 300 --report-name abl_smoke` | ~5 秒 | 不覆盖正式报告 |
| 判决台账 | 由上面第一步自动落盘 | — | `data/reports/normalize_ablation/B/verdict.jsonl`（另含 A 口径与 `manifest.json`） |

**读数（2026-09-20 实点）**：7 个规则档算子中**只有 `text_minhash` 判决变化**
（29 → 30，Δ=+1），存活 2032 → 2031；`chinese_ratio` Δ=0（#65 的单点补丁已覆盖它）。
归一化改写 976/2066 篇、净删 55.8% 字符。**空白占比 p50 几乎不动（1.98%→1.94%）而
p90 差一个数量级（79.89%→8.09%）**，空白占比 >50% 的篇目 849 → 0——语料是双峰的，
归一化的作用面在长尾而不在中位数。**漏斗的拦截数几乎看不见它，进下游的字符总量看得见。**

**三条硬规矩**（踩过才知道）：
1. **台账行数必须等于各滤级进入数之和**。判决书是追加式的，报告是整文件聚合的——
   两者相撞会让报告**不改一行代码就漂移**。脚本已加不变量断言，不等即 `SystemExit`。
   重跑前脚本会**截断** `verdict.jsonl`（不是删目录：沙箱的批量删除护栏在本轮累计
   删除超阈值后会拦掉后续所有删除操作）。
2. **逐规则归因必须是纯的**。七条规则各管一段：`newline` 独占 `\r`；`control_chars`
   不删 `\r`（删了会合并相邻行）且留着 `\t` 让空白折叠把它变**空格**；
   `whitespace_collapse` 只处理行内空白。职责重叠 = 每个逐规则数字都不可信。
3. **归一化只声明自然语言模态**（`text_article` / `image_caption`）。`fhir_resource`
   的 `text` 承载 canonical JSON，`"given": ["John  Smith"]` 里的双空格是数据不是噪声。

## 2. 演示（10 分钟，面试/展示）

**统一入口（V5 β 起，V6 α 扩到八页签）**：`streamlit run scripts/showcase_app.py`
——平台总览（四模态门禁卡 + 算子总数 + 测试基线）/ 图文 / 文本 / 医疗 FHIR / 工业传感器
（后两个支持**现场重跑门禁**，~6 秒）/ 证据链（R@1、ppl、消融、采样）/
**清洗过程**（逐级水位对照 + 判决台账，可筛滤芯与判决）/ **阈值沙盘**（选滤芯 + 方向 +
丢弃预算滑块 → 从分数分布反推门限）。报告缺失时页面直接给生成命令与耗时。

> 两个新页签的数据源是 §1.20 的 A/B 对照实验——**没跑过那个脚本时它们会显示
> 生成命令**（`data/reports/` 不入库，CI 上走的就是这条降级路径）。
> 阈值沙盘对**批量算子**（如 `text_minhash`）会显式说明「不适用」：批量算子按集合
> 裁决、不产生逐样本分数，没有分布就没有阈值的「门」。
> 反推出来的是**丢弃预算**（愿意最多删多少），**不是误杀率**——误杀率要有人工复核
> label 才算得出来，界面两处分开标注，不混口径。

专题深潜（存量应用，门户侧边栏有指引）：

```bash
# 终端 1：检索服务
python -m uvicorn mm_curation.serving.api:app --app-dir src --host 127.0.0.1 --port 8000
# 终端 2：四 tab 界面
python -m streamlit run scripts/streamlit_app.py
```

必演示三件事（http://localhost:8000/docs Swagger 直接点）：
1. `/api/search` 同一查询"一只狗在草地上奔跑"切换 clean_v2 / dirty_raw——
   脏索引 top3 里 2 条脏数据、模糊图排第一
2. `/api/ingest` 同图发两次 → 第二次 `is_duplicate: true`；低质图看 `quality.flags`
3. `/metrics` → Prometheus 计数 + 延迟直方图 + 质量门漏斗

## 3. 清洁 venv（可选，彻底修复搬迁问题）

```bash
python -m venv .venv --clear
source .venv/Scripts/activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```
（约 3GB 下载；不重建则继续用系统 Python，本项目全部功能等价。）
