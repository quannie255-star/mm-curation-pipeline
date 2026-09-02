"""V2 β 阶段验收测试（docs/design_tables.md β 验收四条的测试侧落点）。

B1 端到端：4 种文本污染 + 精确重复走完整文本漏斗，靶子算子逐级拦截，
ground truth 全召回、干净零误杀——污染器与算子的「对靶」由本测试钉死
（两者各自单测通过但互相对不上，是 β 集成阶段揪出的集成断层）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from curation_eval import ContaminationPlan, Sample

from mm_curation.operators.base import Sample as OpSample
from mm_curation.pipeline import OperatorSpec, PipelineConfig, run_funnel

_TEXT_OPS = [
    OperatorSpec(op="doc_length", params={"min": 30, "max": 50000}),
    OperatorSpec(op="chinese_ratio", params={"min": 0.3}),
    OperatorSpec(op="char_repetition", params={"min": 0.8}),
    OperatorSpec(op="line_repetition", params={"min": 0.8}),
    OperatorSpec(op="boilerplate", params={"min": 0.85}),
    OperatorSpec(op="pii_detect", params={"min": 0.9}),
    OperatorSpec(op="text_minhash", params={"threshold": 0.7}),
    OperatorSpec(op="perplexity", params={"min": 0.2, "batch_size": 8}),
]

# 干净集与受害集全部手写互异文本。教训（β 调试中实测三次）：任何
# 「模板+槽位」式生成器，字节 4-gram 相似度都由共享骨架主导（J≈0.6-0.7），
# 在去重眼里就是真近重复，合并是正确行为——测试数据必须真实多样。
_CLEAN_DOCS = [
    "长江中下游的梅雨季一般从六月中旬持续到七月上旬，降水集中且空气湿度极高。",
    "深度学习模型的泛化能力取决于训练数据分布与真实场景分布的一致程度。",
    "敦煌壁画中的飞天形象经历了北朝的西域风格到唐代丰腴飘逸的演变。",
    "咖啡风味曲线受烘焙升温速率影响极大，浅焙保留果酸而深焙偏向苦巧。",
    "蜂群崩溃失调症在北美与欧洲屡有报道，某些杀虫剂被列为疑似诱因。",
    "京杭大运河开凿始于春秋吴国，隋代全线贯通，元代截弯取直定型至今。",
    "量子纠错需要对物理比特做冗余编码，容错阈值是方案取舍的核心指标。",
    "海底光缆承担全球九成以上洲际数据流量，中继放大是其中的关键技术。",
    "宋代点茶讲究击拂成沫，斗茶以水痕先现者为负，茶筅做工尤为关键。",
    "新型电力系统需要大规模储能支撑，电化学与抽水蓄能形成互补格局。",
    "昆虫复眼由数千个小眼组成，每个小眼独立成像拼出镶嵌式的视觉。",
    "唐代长安城实行坊市制度，居住与商业分离，夜禁后坊门关闭不得通行。",
    "信鸽归航依赖地磁场感知、太阳方位与嗅觉地标的多重信息融合导航。",
    "火锅底料的牛油比例决定挂味程度，花椒与辣椒配比是各家的秘方。",
    "珊瑚白化的本质是共生虫黄藻排出后，珊瑚组织失去颜色并逐渐饿死。",
    "古希腊几何学经阿拉伯学者保存发展，到文艺复兴时期才回流欧洲。",
    "软件定义网络把控制面从转发设备剥离，由集中控制器统一下发流表。",
    "普洱茶的后期转化依赖微生物参与，仓储温湿度决定陈化速度与品质。",
    "极地冰芯里的气泡封存着远古大气成分，是重建古气候的关键样本。",
    "活字印刷术的西传路径存在陆路与海路两种说法，雕版与活字在东亚长期并存使用。",
]
_VICTIM_DOCS = [
    "恰当的提问比直接索要答案更能促进理解，追问应指向推理步骤而非结论本身。",
    "竹子整林同步开花后大面积枯死的物种策略仍存争议，或与鼠类种群暴发相关。",
    "城市热岛效应使城区夜间温度明显高于郊区，植被覆盖是主要的缓解手段。",
    "交响乐团的弦乐组通常占乐手总数一半以上，声部平衡依靠配器法实现。",
    "深海热液喷口周围的化能合成生态不依赖阳光，硫化氢氧化是能量起点。",
    "汉字激光照排让中文出版业跳过铅字时代，字形压缩算法是核心突破。",
    "候鸟迁徙路线的稳定依赖遗传与习得双重机制，幼鸟与经验个体结群飞行。",
    "章鱼的每条腕足都有独立神经元集群，切断后仍能完成部分反射动作。",
]
_CN = "零甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"


def _mock_scorer(monkeypatch):
    """假 GPT-2 评分器：一切文本 ppl=20（score≈0.714），漏斗畅通到最后一层。"""
    import mm_curation.operators.text_corpus as tc

    monkeypatch.setattr(tc, "get_scorer", lambda: lambda texts: [20.0 for _ in texts])


def test_b1_text_funnel_end_to_end(monkeypatch):
    _mock_scorer(monkeypatch)
    clean = [OpSample(id=f"c{i}", text=t) for i, t in enumerate(_CLEAN_DOCS)]
    victims = [OpSample(id=f"v{i}", text=t) for i, t in enumerate(_VICTIM_DOCS)]
    kinds = ["paragraph_repeat", "boilerplate_inject", "pii_inject", "whitespace_pad"]
    mixed_parts: list[Sample] = []
    for k, kind in enumerate(kinds):  # 每类恰好 2 条（单类计划，不靠抽样运气）
        pair = [victims[2 * k], victims[2 * k + 1]]
        mixed, manifest = ContaminationPlan(
            inject_rate=1.0,
            seed=11 + k,
            kinds={kind: 1.0},
        ).run(pair, Path("unused_images_beta"))
        assert manifest["counts"] == {kind: 2}
        mixed_parts.extend(mixed)
    injected = [s for s in mixed_parts if s.labels.get("dirty")]  # 尾部注入副本

    dups = [OpSample(id=f"dup{i}", text=clean[i].text) for i in range(3)]
    result = run_funnel(
        clean + mixed_parts + dups,
        PipelineConfig(
            name="beta_acceptance", raw_jsonl=Path("u"), output_dir=Path("u"), operators=_TEXT_OPS
        ),
    )

    # 零框架特例：8 级全量评分、无人被模态跳过（纯 text_article 数据集）
    assert [st.op for st in result.stats] == [
        "doc_length",
        "chinese_ratio",
        "char_repetition",
        "line_repetition",
        "boilerplate",
        "pii_detect",
        "text_minhash",
        "perplexity",
    ]
    assert all(st.skipped == 0 for st in result.stats)
    assert all(st.batch for st in result.stats[6:])  # 去重与困惑度是批量算子

    dropped_ids = {s.id for _, s in result.dropped}
    by_stage = {}
    for stage, s in result.dropped:
        by_stage.setdefault(stage, set()).add(s.id)

    # 靶子逐级拦截：4 种注入 + 3 条精确重复 = 11 条全召回，干净零误杀
    injected_ids = {s.id for s in injected}
    kind_of = {s.id: s.labels["dirty"] for s in injected}
    assert injected_ids | {f"dup{i}" for i in range(3)} <= dropped_ids
    for sid in injected_ids:
        kind = kind_of[sid]
        stage = {
            "paragraph_repeat": "line_repetition",
            "boilerplate_inject": "boilerplate",
            "pii_inject": "pii_detect",
            # whitespace_pad 的剩余有效长度与空白游程都在阈值边缘，
            # doc_length（有效长度）或 char_repetition（空白长游程）
            # 谁先接住都算数——漏斗口径下它必须被结构性算子拦截
            "whitespace_pad": "doc_length",
        }[kind]
        allowed = {stage} | ({"char_repetition"} if kind == "whitespace_pad" else set())
        assert any(sid in by_stage.get(st, set()) for st in allowed), (
            f"{kind} 未被靶子 {stage} 拦截"
        )
    assert {f"dup{i}" for i in range(3)} <= by_stage["text_minhash"]
    assert len(result.kept) == 28  # 20 干净 + 8 污染对象原稿
    assert not ({s.id for s in result.kept} & (injected_ids | {f"dup{i}" for i in range(3)}))


def test_b2_text_funnel_config_parses():
    """仓库内 text_funnel.yaml 必须可解析且算子全部已注册（防配置漂移）。"""
    import mm_curation.operators  # noqa: F401 触发算子注册
    from mm_curation.pipeline import PipelineConfig

    config = PipelineConfig.from_yaml("configs/text_funnel.yaml")
    assert [sp.op for sp in config.operators] == [
        "doc_length",
        "chinese_ratio",
        "char_repetition",
        "line_repetition",
        "boilerplate",
        "pii_detect",
        "text_minhash",
        "perplexity",
    ]


def test_b3_disjoint_modality_fail_fast(tmp_path):
    """文本算子配纯图文数据 → 启动即报错（fail-fast，不静默空转）。"""
    from PIL import Image

    p = tmp_path / "x.jpg"
    Image.new("RGB", (32, 32), (10, 10, 10)).save(p)
    image_samples = [OpSample(id=f"i{k}", text=f"图文样本{k}", image_path=str(p)) for k in range(3)]
    with pytest.raises(ValueError, match="完全不相交"):
        run_funnel(
            image_samples,
            PipelineConfig(
                name="bad",
                raw_jsonl=Path("u"),
                output_dir=Path("u"),
                operators=[OperatorSpec(op="doc_length", params={"min": 30})],
            ),
        )


def test_b4_single_source_guard_beta():
    """协议单一来源守卫（β 延续 α 的 A5）：主仓库不得本地定义协议类型。"""
    import re

    pattern = r"^class (Sample|Operator|BatchOperator|Executor|StageStat|FunnelResult)\b"
    for p in Path("src/mm_curation").rglob("*.py"):
        for line in p.read_text(encoding="utf-8").splitlines():
            assert not re.match(pattern, line), f"{p} 本地定义协议类型: {line}"
