"""业务场景 → 清洗配方 的映射层（前端的核心资产）。

这一层存在的理由：**让不懂技术的人不必碰 YAML**。
产品经理选「图文语料」，而不是自己去拼 `minhash_lsh` 和
`perplexity` 的阈值 —— 但映射关系仍然必须是**可核对的事实**，
不是拍脑袋的字符串。

因此本模块遵守三条硬规矩：

1. **算子名必须真实注册** —— `recipe.py` 里用断言卡住，
   删了/改名了算子这里立刻报错，而不是运行到一半才炸。
2. **阈值有出处** —— 每个非平凡阈值都注明标定依据
   （来自 `configs/*.yaml` 实跑或 `docs/ROADMAP.md` 校准记录）。
3. **不新增机器可读的第三份口径** —— 配方只是把既有 YAML
   的算子组合**按业务名重述**；真实执行仍走 `PipelineConfig`，
   阈值、顺序、算子行为全由既有链路决定。

⚠️ 判据纪律：本模块**不许有一个「看起来在拦」的占位检查**。
每个场景的 `required_fields` 会**真的**被拿去校验用户上传的文件
（见 `webapp/checkers.py`），校验不过就报错，不放行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 模态标识 = curation_eval 注册表里的 modalities 值
MODALITY_IMAGE_CAPTION = "image_caption"
MODALITY_TEXT_ARTICLE = "text_article"
MODALITY_FHIR = "fhir_resource"
MODALITY_SENSOR = "industrial_sensor"


@dataclass(frozen=True)
class Recipe:
    """一个业务场景的清洗配方。"""

    key: str
    title: str  # 卡片上显示的名字
    modality: str
    blurb: str  # 一句话说明「这个场景解决什么问题」
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    operators: tuple[dict, ...]
    produces: str  # 产出什么类型的数据集
    sample_hint: str  # 给用户的样例文件格式说明
    cost_note: str = ""
    # 已知会误伤/需要注意的点（诚实提示，不藏）
    caveats: tuple[str, ...] = field(default_factory=tuple)

    def spec_to_yaml(self, raw_path: str, out_dir: str) -> dict:
        """导出成PipelineConfig.from_yaml 认识的形状。"""
        return {
            "name": f"studio_{self.key}",
            "description": f"{self.title}：{self.blurb}",
            "dataset": {"raw_jsonl": raw_path},
            "output": {"dir": out_dir},
            "operators": [{"op": o, "params": p} for o, p in self.operators],
        }


# ── 算子的中文说明（**必填**）────────────────────────────────────────
# ⭐ 为什么单独维护这张表：算子注册表里只有英文名（`text_minhash`），
# 产品经理看不懂 —— 实测前端第一版直接把 op 名当标签渲染，等于没做产品化。
# 这里给每个算子一句「人话」，前端显示它、文档显示它。
#
# ⭐**这张表必须与配方同步**：assert_recipes_valid() 会检查
# 「配方里用到的每个算子都在表里，且表里没有多余的」（两个方向都查）。
# 只查一个方向的话，删掉一行说明不会报错 → 又变成静默失效。
OPERATOR_ZH: dict[str, str] = {
    # 文本
    "doc_length": "太短或太长的没信息量片段",
    "text_length": "文本长度是否在合理区间",
    "char_repetition": "字符多样性（单字/单句复读机）",
    "line_repetition": "行级重复（同一行反复出现）",
    "boilerplate": "模板套话（「综上所述」这类填充句）",
    "chinese_ratio": "中文字符占比（防止混入乱码或其他语种）",
    "pii_detect": "个人隐私信息（姓名/手机/身份证/邮箱）",
    "perplexity": "语言模型困惑度（机械感强的文本）",
    "md5_exact": "完全一样的重复内容",
    "text_minhash": "近似重复（转载、改写、同稿多发）",
    # 图片 / 图文
    "resolution": "图片分辨率是否够用",
    "aspect_ratio": "图片长宽比是否畸变",
    "phash_near": "视觉上几乎相同的重复图片",
    "clip_alignment": "图片和文字说的是不是一回事",
    # 医疗合规
    "phi_residual": "医疗记录里的个人信息残留",
    "code_validity": "疾病/药品编码是否有效（ICD/LOINC/ATC）",
    "unit_normalization": "数值单位是否规范（UCUM）",
    "referential_integrity_fhir": "跨资源引用是否对得上（患者/就诊记录）",
    "temporal_consistency": "时间线是否自洽（就诊时间倒流）",
    # 工业时序
    "sensor_range": "传感器读数是否超出物理量程",
    "sensor_stuck": "传感器卡死（读数长时间不变）",
    "sensor_drift": "传感器漂移（缓慢偏移）",
    "sensor_multivariate": "多变量相关性异常（互相矛盾）",
    "fault_vs_maintenance": "真故障与计划内维护的区分",
    "unit_consistency": "单位一致性（工业模态专用）",
    # 通用
    "minhash_lsh": "大规模近似去重（LSH 加速）",
}
COST_ZH = {
    "rule": "规则",
    "perceptual": "感知",
    "model": "模型",
    "llm": "大模型",
}


# ── 四个场景 ────────────────────────────────────────────────────────
# 阈值出处写在注释里；configs/news_zh_funnel.yaml 与
# configs/pipeline.example.yaml 是真实跑通过的两份。

RECIPES: tuple[Recipe, ...] = (
    Recipe(
        key="image_caption",
        title="图文语料（图片 + 配文）",
        modality=MODALITY_IMAGE_CAPTION,
        blurb="从网页爬来的图文对，常见脏数据是：图糊了、图和文字对不上、同一张图重复出现。",
        required_fields=("image_path", "text"),
        optional_fields=("id", "labels"),
        # 顺序原则 = 成本递增（configs/pipeline.example.yaml 注释）：
        # 便宜的规则先筛，尽早缩小昂贵算子（CLIP/去重）的输入规模。
        operators=(
            # --- 图像规则（需 image_path）---
            ("resolution", {"min": 100}),  # 干净集最小 109px，100 不误杀
            ("aspect_ratio", {"min": 0.25}),  # 砍极端横幅；干净集 p10=0.7
            # --- 文本规则（需 text）---
            ("text_length", {"min": 5, "max": 100}),
            ("chinese_ratio", {"min": 0.3}),
            ("char_repetition", {"min": 0.8}),
            # --- 去重（批量，作用于当前存活集）---
            ("md5_exact", {}),
            ("phash_near", {"threshold": 12}),  # 召回 84% / 误杀 0.3%（真实数据扫描）
            ("minhash_lsh", {"threshold": 0.65, "min_len": 8}),  # 召回 94% / 误杀 0.7%
            # --- 贵算子放最后 ---
            ("clip_alignment", {"min": 0.38}),  # 图文错配召回 96% / 误杀 0.2%
        ),
        produces="图文对数据集（image_path + text），可用于 CLIP/多模态 SFT",
        sample_hint='每行一个 JSON：{"image_path": "images/001.jpg", "text": "配文"}',
        cost_note="含 CLIP 图文对齐推理，需 GPU；无 GPU 会明显变慢（会明确告知）",
        caveats=(
            "去重是全局操作：会跨文件合并相似样本，不是单文件内去重",
            "clip_alignment 对「文字是描述而非配文」的数据容易误杀（实测最易误杀的一类）",
        ),
    ),
    Recipe(
        key="text_article",
        title="纯文本语料（文章/新闻/文档）",
        modality=MODALITY_TEXT_ARTICLE,
        blurb=(
            "长文本语料，常见脏数据是：太短太长、重复段落、模板页脚、"
            "个人隐私信息、以及互相转载的重复稿。"
        ),
        required_fields=("text",),
        optional_fields=("id", "title", "labels"),
        # 直接对应 configs/news_zh_funnel.yaml（实跑过：1960 → 1929 条）
        operators=(
            ("doc_length", {"min": 30, "max": 50000}),
            ("chinese_ratio", {"min": 0.3}),
            ("char_repetition", {"min": 0.8}),
            ("line_repetition", {"min": 0.8}),
            ("boilerplate", {"min": 0.8}),
            ("pii_detect", {"min": 0.9}),
            ("text_minhash", {"threshold": 0.7}),
            ("perplexity", {"min": 0.2, "batch_size": 64}),
        ),
        produces="纯文本数据集（text 字段），已按 train/val/test 切分并打包成定长 block",
        sample_hint='每行一个 JSON：{"text": "文章正文……"}',
        cost_note="perplexity 走语言模型算困惑度，首次运行会下载模型（几百 MB）",
        caveats=(
            "chinese_ratio 会把**英文语料**当成脏数据扔掉 —— 处理英文请改用其它配方",
            "pii_detect 是保守策略：宁可漏检也不误伤，隐私合规场景请人工复核",
            "实测：MinHash 阈值区（0.55–0.95）的误判率约 7%，加签名位数可降到更低",
            "**整段复读的文本可能漏检**：字符级重复算子对整段复制不敏感"
            "（实测复读得 0.998 vs 正常 0.974，阈值 0.8 分不开）",
        ),
    ),
    Recipe(
        key="fhir_resource",
        title="医疗健康数据（FHIR 资源）",
        modality=MODALITY_FHIR,
        blurb=(
            "医院/保险理赔的 FHIR JSON 资源，脏数据多是：单位写错、数值不合生理范围、时间线对不上。"
        ),
        required_fields=("text",),
        optional_fields=("resource_type", "id", "labels"),
        # 逐条对齐 configs/funnel_fhir.yaml（真实跑过）。
        # ⚠️ `unit_consistency` **不属于本模态**（它只支持 industrial_sensor），
        #    本配方的单位检查由 `unit_normalization`（UCUM 成员资格）承担
        #    —— 这条是被 recipes.assert_recipes_valid() 抓出来的，别再改回去。
        operators=(
            # 合规类：存在即违规 → min=1.0 一票否决
            ("phi_residual", {"min": 1.0}),  # PHI 残留（姓名/手机/证件/邮箱）
            ("code_validity", {"min": 1.0}),  # ICD-10/LOINC/ATC 编码有效性
            ("unit_normalization", {"min": 1.0}),  # valueQuantity 单位 ∈ UCUM
            # 跨资源批量（shardable=False，必须单点）
            ("temporal_consistency", {"min": 1.0}),  # 时间线冲突
            ("referential_integrity_fhir", {"min": 1.0}),  # subject/encounter 引用闭合
        ),
        produces="结构化医疗文本语料",
        sample_hint="每行一个 FHIR 资源的 JSON（含 resourceType / value 等）",
        cost_note="referential_integrity 需要全量资源做交叉核对，规模大时明显变慢",
        caveats=(
            "referential_integrity_fhir 是**不可分片**算子：必须单点核对，不能并行",
            "误杀医疗数据的代价远高于脏数据本身，请务必先小批量试跑看丢弃明细",
        ),
    ),
    Recipe(
        key="industrial_sensor",
        title="工业时序数据（传感器读数）",
        modality=MODALITY_SENSOR,
        blurb="设备传感器的时间序列，脏数据是：卡死在某个值、飘出物理范围、漂移、该报的故障没报。",
        required_fields=("text",),
        optional_fields=("series_id", "timestamp", "labels"),
        # 对应 configs/funnel_industrial.yaml
        operators=(
            ("sensor_range", {}),
            ("sensor_stuck", {}),
            ("sensor_drift", {}),
            ("sensor_multivariate", {}),
            ("unit_consistency", {}),
            ("fault_vs_maintenance", {}),
        ),
        produces="时序语料（可用于时序模型训练 / 异常检测）",
        sample_hint=(
            '每行一个采样点：{"series_id": "dev01", "timestamp": "...", "text": "温度=82.3"}'
        ),
        cost_note="6 个算子里 5 个**必须单点**（时序算子要跨采样点看全局视野），不能并行",
        caveats=(
            "本场景的数据量越大越慢（跨序列全局视野），十万级采样点请预留数十分钟",
            "该模态在分布式下几乎无法加速 —— 这是时序语义的要求，不是实现缺陷",
        ),
    ),
)

BY_KEY = {r.key: r for r in RECIPES}


def get_recipe(key: str) -> Recipe:
    if key not in BY_KEY:
        raise KeyError(f"未知场景 {key!r}，可选：{sorted(BY_KEY)}")
    return BY_KEY[key]


# ── 自检：算子名必须真实注册 ────────────────────────────────────────
# 这一段在 import 时就执行。有人删了/改名算子，这里立刻炸，
# 而不是等用户点了「开始清洗」才在运行中途失败。


def assert_recipes_valid() -> None:
    """校验所有配方引用的算子都真实注册、字段与模态自洽。

    存在的意义：配方是**手写的算子名清单**，
    而注册表才是真相源。两边不一致时必须**响亮地失败**。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from curation_eval.registry import available_operator_metas

    import mm_curation.operators  # noqa: F401  —— 触发 import 副作用注册

    metas = available_operator_metas()
    if not metas:
        raise RuntimeError(
            "算子注册表为空 —— 大概率是忘了 import mm_curation.operators"
            "（注册靠 import 副作用）。这是实测踩过的坑。"
        )
    problems: list[str] = []
    for r in RECIPES:
        seen: set[str] = set()
        for op, params in r.operators:
            if op not in metas:
                problems.append(f"[{r.key}] 引用未注册算子 {op!r}")
                continue
            if op in seen:
                problems.append(f"[{r.key}] 算子 {op!r} 重复出现（漏斗会跑两遍，白花钱）")
            seen.add(op)
            meta = metas[op]
            # 模态自洽：算子支持的模态必须包含本场景模态
            if meta.modalities and r.modality not in meta.modalities:
                problems.append(
                    f"[{r.key}] 算子 {op!r} 不支持模态 {r.modality!r}"
                    f"（它支持 {list(meta.modalities)}）"
                )
            # 字段自洽：算子需要的字段必须被场景声明为 required/optional
            if meta.required_fields:
                declared = set(r.required_fields) | set(r.optional_fields)
                missing = [f for f in meta.required_fields if f not in declared]
                if missing:
                    problems.append(f"[{r.key}] 算子 {op!r} 需要字段 {missing}，但场景没声明")
            if not isinstance(params, dict):
                problems.append(f"[{r.key}] 算子 {op!r} 的 params 必须是 dict")
        if not r.required_fields:
            problems.append(f"[{r.key}] 必须声明至少一个 required_fields，否则无法校验上传文件")

    # ── 中文说明表必须与配方**双向**同步 ──
    # ⚠️ 只查「配方用到的都有说明」不够：删掉一行说明不会报错，
    # 前端就会少一个中文标签（静默失效）。反向也要查。
    used_ops = {op for r in RECIPES for op, _ in r.operators}
    for op in sorted(used_ops - set(OPERATOR_ZH)):
        problems.append(f"算子 {op!r} 没有中文说明 —— 前端只能显示英文名，外行看不懂")
    for op in sorted(set(OPERATOR_ZH) - used_ops):
        problems.append(f"OPERATOR_ZH 里的 {op!r} 没有任何配方在用（过时条目，删掉）")
    # 代价档也要能翻译
    for r in RECIPES:
        for op, _ in r.operators:
            meta = metas.get(op)
            cost = getattr(meta, "cost_class", None)
            key = getattr(cost, "value", cost)
            if key not in COST_ZH:
                problems.append(f"算子 {op!r} 的代价档 {key!r} 没有中文标签")

    if problems:
        raise AssertionError("场景配方与算子注册表不一致：\n  - " + "\n  - ".join(problems))


assert_recipes_valid()
