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
| 启动判官服务 | `python -X utf8 scripts/serve_judge.py` | 首次 +1GB 下载 | 监听 127.0.0.1:8100（Qwen2.5-0.5B-Instruct） |
| kappa 实验 | `python -X utf8 scripts/eval_judge.py --n 400` | ~10 分钟 | data/reports/judge_kappa.md：judge vs 脏标签 / judge vs L1 / L1 vs 标签 三 κ + 分歧样本 |
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

## 2. 演示（10 分钟，面试/展示）

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
