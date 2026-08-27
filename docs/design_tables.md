# 设计表 — Phase2 P1：水印/NSFW 合成数据自训检测器

> 目标：把"合规占位实现"升级为真模型，堵住"NSFW 是假的"这一最明显追问。
> 灵魂设计：**风格错开的训练/测试划分**——训练与测试用不同的水印风格参数，
> 检测器学到的是"图上有叠加文字/广告版式"的概念而非记住具体水印，
> 直接回应"合成训+合成测=循环论证"的质疑。

## 1. 数据结构表

### 1.1 风格组（防循环论证的核心）

| 参数 | 组 A（训练+同风格测试） | 组 B（泛化测试，训练不可见） |
|---|---|---|
| 水印布局 | 斜向平铺（复用现有污染器逻辑） | 单角落大字 / 顶部横幅 |
| 透明度 | 0.35-0.55 | 0.20-0.35 |
| 字体 | msyh | simhei / simsun |
| 水印文本 | 文本池 1（6 个域名样例） | 文本池 2（全新 6 个域名） |
| NSFW 广告版式 | 矩形色块 + 居中文字 | 渐变背景 + 角落文字 + 边框 |

### 1.2 DetectorSample（生成数据清单，jsonl）

| 字段 | 类型 | 说明 |
|---|---|---|
| image_path | str | 生成图（干净底图 + 合成污染） |
| label | int | 0=clean / 1=watermark / 2=ad_nsfw |
| style_group | str | A / B |
| gen_params | dict | 生成参数快照（可追溯） |

### 1.3 模型与算子

| 项 | 值 |
|---|---|
| 架构 | torchvision MobileNetV3-Small（ImageNet 预训练，3 分类头） |
| 体积 | ~10MB（`models/detector/wm_nsfw_cnn.pt`，gitignore 已覆盖 *.pt） |
| 算子名 | `wm_nsfw_cnn`（BatchOperator：批量推理，与 clip_alignment 同形态） |
| score 语义 | 1 - max(P(watermark), P(ad))，越高越好（项目统一约定） |

## 2. 接口约定表

### 2.1 模块（detector/）

| 函数 | 入参 | 出参 | 说明 |
|---|---|---|---|
| render_watermark(img, params) | PIL 图 + 参数 | PIL 图 | 参数化渲染（布局/字体/透明度/文本） |
| render_ad(size, params) | 尺寸 + 参数 | PIL 图 | 合成广告占位图 |
| generate_dataset(clean_images, out_dir, n_per_class, group) | 风格组 | DetectorSample 清单 | 确定性（seed） |
| build_model() / load_detector() | — | model | 单例缓存（同 clip_encoder 模式） |

### 2.2 CLI（scripts/train_detector.py）

| 参数 | 默认 | 说明 |
|---|---|---|
| --epochs / --batch-size | 3 / 64 | 训练超参 |
| --out | models/detector/wm_nsfw_cnn.pt | 模型产物 |
| 退出码 | 0/1/2 | 0 成功 / 1 底图缺失 / 2 训练异常 |

### 2.3 报告（data/reports/detector_eval.md）

同风格测试（记忆对照） vs 风格错开测试（泛化）的 3 类混淆矩阵与 P/R；
差距即泛化损耗——两个数字都报告，不藏。

## 3. 流转表与报备决策

### 3.1 流水

```
干净底图(1620) → 风格组 A/B 分别生成 (3 类 × N) 
→ A 训练 MobileNetV3（冻结 backbone 前 N-1 层微调头 + 全量微调各评）
→ A 留出测试（记忆）+ B 全量测试（泛化）→ 报告
→ 注册算子 wm_nsfw_cnn → 全量脏集独立 P/R（复用 operator_pr 框架）
→ 与 clip_alignment 重叠分析（此前 0.38 顺带压住 nsfw——检测器是替代还是互补）
```

### 3.2 需门审确认的决策

1. **报备：重构 contamination/impl.py 的 Watermark**——其渲染逻辑抽到
   detector/synth.py 参数化共用（消除双实现漂移），污染器行为不变（回归测试保证）
2. 三分类单模型（clean/wm/ad）而非两个二分类器：广告图与水印图特征差异大，
   单模型特征共享 + 一次前向，推理成本低
3. MobileNetV3-Small ImageNet 预训练起点（需装 torchvision~0.20.1 cu121）：
   泛化靠预训练特征，从零训小 CNN 在风格错开下大概率塌
4. 规模：训练 3×1200 / 测试 A 组 3×150 + B 组 3×400（生成秒级，成本可忽略）
