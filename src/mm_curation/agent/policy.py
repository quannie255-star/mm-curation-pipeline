"""Agent 编排层的决策策略。

## 为什么策略是纯函数、零 LLM（这是本模块最重要的一条设计）

本项目的核心纪律是「每道闸门可被第三方复跑」。
如果路由决策依赖 LLM，那么：

- 同输入两次跑出不同路径 → **无法回归测试**
- 出了问题无法定位 → 不知道是策略错还是模型抽风
- 面试被问「你那 Agent 是不是随机瞎选」→ 只能答「是」

所以 `policy.py` 里**没有一个 LLM 调用**。
LangGraph 负责编排（顺序、条件边、重试、状态），
策略负责决策（纯函数：状态 → 决策），两者职责严格分开。

**这不等于「Agent 不该用 LLM」**——真正的落点是：
凡是需要语义判断的环节（如「这条文本在讲什么」），由 LLM 承担；
而「要不要花钱去问 LLM」这个决策本身，用确定性规则。
这与生产系统里的分层判据一致：**决策要可复跑，判断才交给模型。**

## 决策输入是什么

只用**样本自身可观测的字段**（`sample.meta` 里已算好的score + 文本长度等），
不使用全局聚合量、不使用目标数据集的任何先验。
理由：路由决策必须能在**单样本级**做出，否则就没法分片，
而分片是本项目`shardable` 语义的另一半。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 规则算子的 score 在 meta 里的键名形如 `score:<op_name>`
SCORE_PREFIX = "score:"


@dataclass(frozen=True)
class TierDecision:
    """一次分层决策的结果。

    ## 为什么用frozen dataclass
    决策对象会被塞进 LangGraph 的状态里，而状态可能被浅拷贝传递。
    可变对象在这种链路下会出现「改了但没生效」或「两个版本互相污染」，
    而这两种bug 都极难定位。frozen 是最便宜的防御。
    """

    #: 要跑哪些成本档（按代价从低到高）。空 tuple = 直接放行。
    tiers: tuple[str, ...]
    #: 决策理由。**必须非空**——没有理由的决策无法审计，
    #: 而本项目每个数字都要能追问到底，一个没有理由的路由同样不行。
    reason: str
    #: 这一步省掉了多少成本档（用于 manifest 汇总）
    skipped_tiers: tuple[str, ...] = field(default=())

    @property
    def runs_expensive(self) -> bool:
        return "llm" in self.tiers


#: 各成本档的代价权重（相对 RULE）。
#: **这不是实测美元数**，而是「同一批样本上各档的大致耗时档位」的量化。
#: 用相对值而非绝对值，因为绝对值依机器而异，相对关系才是决策依据。
#: 之所以给 LLM 一个显著更高的权重（50），是因为它比其他档贵一个数量级以上
#: （真实调用要过网络 + 排队 + 生成），而不是 30倍。
COST_WEIGHT: dict[str, int] = {
    "rule": 1,
    "perceptual": 5,
    "model": 20,
    "llm": 50,
}

#: 成本档的执行顺序（便宜 → 贵）。**这个顺序必须是从低到高**：
#: Agent 的核心价值就是「先便宜的判，判不出问题就不花钱跑贵的」。
ALL_TIERS: tuple[str, ...] = ("rule", "perceptual", "model", "llm")


def estimate_cost(tiers: tuple[str, ...], n_samples: int) -> int:
    """给定要跑的成本档与样本数，返回相对代价。

    注意 `shardable=False` 的算子需要**全量可见性**：
    它们无论路由到哪里都必须至少跑一次，否则精确去重会失效。
    这一点由 `plan_for_corpus` 处理，而不是在这里——
    单样本决策看不到全体，强行在这里判断就是自欺。
    """
    return n_samples * sum(COST_WEIGHT.get(t, 1) for t in tiers)


def _rule_signals(sample: Any) -> dict[str, float | None]:
    """取样本身上已算好的规则档 score。

    这些 score 是漏斗**上游**已经写入 `sample.meta` 的——
    Agent 不重算，只读。**这是「不重复劳动」的关键**：
    若Agent 自己再跑一遍规则算子，成本账本就错了，
    而成本账本错了整个优化的结论也就错了。
    """
    meta = getattr(sample, "meta", None) or {}
    out: dict[str, float | None] = {}
    for key, val in meta.items():
        if key.startswith(SCORE_PREFIX) and isinstance(val, (int, float)):
            out[key[len(SCORE_PREFIX) :]] = float(val)
    return out


#: 「这条文本明显是结构化数据/代码/编号列表」的特征。
#: 命中就走规则层收尾，不必问 LLM —— 判据要**可解释**，
#: 所以用显式正则而不是「模型觉得像」。
_CODE_LIKE = re.compile(r"^[\s\W]*(\d+\s*[,，]\s*){4,}", re.M)


#: ── 为什么默认**不跳过 MODEL 档**（2026-10-05 实跑抓到的，不是设计时想到的）
#:
#: 我最初写过一个「生僻字占比 > 阈值 → 疑似乱码」的守卫，
#: 理由是实跑发现了一件反直觉的事：在真实维基语料上注入
#: 「字符级噪声」（把 50% 字符换成生僻汉字）后
#:
#:     规则档 keep 120 / 120（**一个都没拦住**）
#:     perplexity（MODEL 档）keep 2 / 120 → **贵档独拦 118 条**
#:
#: 也就是说：**规则算子对乱码几乎完全失效**，
#: 因为乱码字符大多仍落在 `chinese_ratio` 认可的汉字区间内、
#: `doc_length` 也照样够长。所以「规则分干净就跳过贵档」
#: 会恰好漏掉 MODEL 档唯一有增量价值的形态。
#:
#: ## 但那个守卫本身也被数据否决了（所以没有留在代码里）
#: 实测「生僻字占比」在 120 条真实维基正文上的分布：
#:
#:     干净  p50 = 0.41  max = 0.79
#:     乱码  p50 = 0.75min = 0.59
#:
#: **两个分布重叠**（干净的 max 高于乱码的 min），
#: 没有任何单一阈值能同时满足「不误伤干净文本」与「抓住乱码」。
#: 根因是现代汉语正文里大量专有名词（人名、地名、术语）
#: 本来就是生僻字——维基百科不是《现代汉语常用字表》的例句集。
#:
#: 结论：**不写这个启发式。**
#: 判据在正确实现下就会误判 = 比没有门禁更坏。
#: 宁可把优化范围收窄（下面这条），也不上自己已证明不可靠的判据。
#:
#: ## 因此：跳过哪一档是**显式参数**，不是策略自己猜的
#: `SKIPPABLE_TIERS` 写死「只允许跳llm」——
#: 因为 LLM 判官在本项目里没有第二个实例（注册表里只有 `llm_judge` 一个），
#: 且它对「规则分已经很干净」的文本边际价值最低；
#: 而 MODEL 档实测有 100%+ 的增量检出，**不许跳**。
#:
#: 将来若要放开 MODEL 档，前置条件是**先有一个经真实数据验证、
#: 在干净语料上零误伤的乱码判据**。这条写在代码里是为了让下一个人
#: 知道代价，而不是让他以为「忘了写」。
SKIPPABLE_TIERS: frozenset[str] = frozenset({"llm"})

#: 无论白名单怎么放开都**必须跑**的档。
#: 目前只有 `rule`：它是余量判据的输入来源，
#: 不跑它就没有任何依据做「跳过贵档」的判断。
#: 与 `SKIPPABLE_TIERS` 分开是因为两者语义相反——
#: 前者是「在有依据的前提下可以省」，后者是「省了就没有依据」。
_MANDATORY_TIERS: frozenset[str] = frozenset({"rule"})


def _after_skip(ceiling: tuple[str, ...]) -> tuple[str, ...]:
    """在 `ceiling` 里去掉「白名单允许跳」的档，并保持档位顺序。

    存在的理由是让「跳过 MODEL 档」这件事在代码里**不可能发生**，
    而不是靠每个调用点的自觉——第一版三处判据各写各的
    `t for t in ceiling if t != "rule"`，其中一处就会在
    `max_tier="perceptual"` 时把 MODEL 档跳掉。
    """
    return tuple(t for t in ceiling if t not in SKIPPABLE_TIERS)


def _decision(*, ceiling: tuple[str, ...], tiers: tuple[str, ...], reason: str) -> TierDecision:
    """构造 `TierDecision`，并**当场断言跳档白名单**。

    `skipped` 由 `ceiling - tiers` 自动推出，所以调用点无法
    「声明跳了什么」和「实际跳了什么」不一致。

    ## 为什么用断言而不是运行时降级
    跳档白名单是「省钱」与「不漏检」之间的取舍边界。
    越界时的正确行为是**让门禁红**，而不是悄悄多跑一档：
    悄悄多跑会让「Agent 省了多少」这个数字失真，
    而失真是无声的——数字照样好看，只是从此不再可信。

    ## `rule` 档不在白名单管辖内
    白名单管的是「哪些**贵**档可以省」，而 rule 档是所有决策的**前提**
    （余量判据读的就是它的 score）。把 rule 档写进白名单等于
    允许「不跑规则就下结论」——那不是省钱，是无依据。
    所以它由`_MANDATORY_TIERS` 单独挡住，**白名单开多大都跳不掉**。
    """
    skipped = tuple(t for t in ceiling if t not in tiers)
    illegal = sorted((set(skipped) - SKIPPABLE_TIERS) | (set(skipped) & _MANDATORY_TIERS))
    if illegal:
        raise AssertionError(
            f"试图跳过不允许跳的档 {illegal}；"
            f"白名单={sorted(SKIPPABLE_TIERS)}，"
            f"强制必跑={sorted(_MANDATORY_TIERS)}。"
            f"要放开请先在 SKIPPABLE_TIERS 上方写清实测依据。"
        )
    return TierDecision(tiers=tiers, reason=reason, skipped_tiers=skipped)


def normalized_margin(score: float, keep_min: float | None) -> float | None:
    """把「分数高于保留阈值多少」折算成 [0, 1] 的余量。

    返回 `None` 表示**这条 score 不可用于判定**（不是「判定为差」）。

    ## 为什么必须归一化，不能直接比绝对值（2026-10-05 实跑抓到的）
    第一版策略是「所有规则分都 >= 0.9 就放行」，实跑只省下 7.2%，
    179/193 条仍然跑满 LLM ——看着像门禁失效，其实是**口径用错了**。
    实测各算子 score 在同一批 kept 样本上的分布：

    - `doc_length`：p50 = **181**（它的 score 是文本长度）
    - `chinese_ratio`：p50 = **0.848**（中文占比）
    - `char_repetition`：p50 = 0.990

    用同一个 0.9 去判这三类，`doc_length` 永远过不了（它根本不是比例），
    其余两个又几乎恒过 —— 阈值等于没写。
    **这不是「阈值调错了」，是「把不同量纲的数放在同一把尺子上」。**

    ## 折算方式
    余量 = (score - keep_min) / (1 - keep_min)，即
    「从保留线走到满分（1.0）走了多远」。

    对 `chinese_ratio`（keep_min=0.3、score=0.848）得 0.78；
    对 `char_repetition`（keep_min=0.8、score=0.990）得 0.95。
    两者可比，且都远高于 margin_ok=0.5 —— 这才是「明显干净」。

    ## 什么时候返回 None
    `keep_min is None`（该算子没声明保留线，我们无从知道它的量纲），
    或 `keep_min >= 1`（量纲不是比例，如 `doc_length` 的 min=30）。
    这两种情况**必须报「不可判定」而不是猜一个值**：
    猜出来的数字会让「Agent 放行」与「漏斗放行」失去可比性，
    而那个可比性正是本模块的红线。
    """
    if keep_min is None or keep_min >= 1.0 or keep_min < 0.0:
        return None
    span = 1.0 - keep_min
    return (score - keep_min) / span


def decide_tiers(
    sample: Any,
    *,
    max_tier: str = "llm",
    keep_min_of_op: dict[str, float | None] | None = None,
    margin_ok: float = 0.5,
    llm_min_chars: int = 40,
    llm_max_chars: int = 4000,
    code_like_ends: bool = True,
) -> TierDecision:
    """决定单条样本要跑哪些成本档。

    参数全是**显式阈值**而不是从配置深挖——
    因为策略必须能被第三方独立复跑，而「阈值藏在三处配置里」
    就等于「第三个人复跑时要先猜你在哪调的」。

    ## `keep_min_of_op`：量纲对照表，不是「阈值表」
    它是 `算子名 → 该算子在漏斗里的保留阈值`，来源应当是
    `PipelineConfig.operators[].params['min']` —— 即**漏斗自己用的那个数**。
    引用同一个数而不是另抄一份，是为了让「Agent 放行」与
    「漏斗放行」用的是同一把尺子；抄一份就会漂移，而漂移的后果是
    Agent 省了钱却改了结论，且无法被察觉。

    传 `None`（默认）表示调用方没有这张表 → 全部判据失效 →
    **一律按上限跑**（不猜）。

    ## 决策顺序（顺序本身是有讲究的）
    1. 规则档**必跑**：它是其它档的前置（分数都已算在 meta 里）
    2. 所有**可判定**的规则分余量都很宽 → 到此为止
    3. 文本太短 / 太长 → LLM 判官价值低（短的是碎片，长的是大块文档）
    4. 明显是编号/结构化数据 → 语义判官几乎必然给低分，不问

    ## 放行的正确性论证（这是本模块唯一允许的放行理由）
    漏斗的保留条件是 `score >= keep_min`。而
    `normalized_margin(score, keep_min) >= margin_ok` 等价于

        score >= keep_min + margin_ok * (1 - keep_min)  >  keep_min

    即**离被丢弃还有一段明确的距离**。所以
    「Agent 放行」是「漏斗本来也会放行」的**严格子集**——
    省下的是「没必要再问一遍」，不会改变结论。
    margin_ok 越大越保守：0.5 表示离满分至少走了一半。
    """
    if max_tier not in ALL_TIERS:
        raise ValueError(f"max_tier 必须是 {ALL_TIERS} 之一，得到 {max_tier!r}")
    if not 0.0 < margin_ok < 1.0:
        raise ValueError(f"margin_ok 必须在 (0,1) 之间，得到 {margin_ok!r}")

    ceiling = ALL_TIERS[: ALL_TIERS.index(max_tier) + 1]
    text = getattr(sample, "text", "") or ""
    n_chars = len(text)

    # 第 1 条：规则档永远必跑——后面的判断依赖它的分数
    if ceiling == ("rule",):
        return _decision(ceiling=ceiling, tiers=ceiling, reason="上限就是 rule 档")

    signals = _rule_signals(sample)
    if not signals:
        # 没有分数就不猜。宁可多跑一档，也不靠「看起来干净」放行——
        # 那会让 Agent 的效果无法与静态基线对比。
        return _decision(
            ceiling=ceiling,
            tiers=ceiling,
            reason="样本身上没有任何规则 score，无法判定就按上限跑（不猜）",
        )

    thresholds = keep_min_of_op or {}
    judged: dict[str, float] = {}
    unjudgeable: list[str] = []
    for op, val in signals.items():
        margin = normalized_margin(float(val), thresholds.get(op))
        if margin is None:
            unjudgeable.append(op)
        else:
            judged[op] = margin

    # 第 2 条：所有可判定的余量都够宽 → 到此为止
    # 判据要**结构化**（只认可判定项），而不是对全部 score 取 min：
    # 后者会让一个「不可判定」把整个决策拖成跑满，
    # 于是 doc_length 这种非比例 score 一进来，Agent 就永远不省。
    if judged and all(m >= margin_ok for m in judged.values()):
        weakest = min(judged.items(), key=lambda kv: kv[1])
        extra = (
            f"（已排除不可判定的 {len(unjudgeable)} 项：{', '.join(sorted(unjudgeable))}）"
            if unjudgeable
            else ""
        )
        return _decision(
            ceiling=ceiling,
            # ★ 只跳**白名单里的**档，不跳「非 rule 的所有档」。
            #  上一版写的是 `t for t in ceiling if t != "rule"`，
            #  在 `max_tier="perceptual"` 时会跳过 MODEL 档——而 MODEL 档
            #  实测是唯一能拦住乱码的档（乱码 120 条里它独拦 118）。
            #  那等于「规则分干净就跳过唯一有增量价值的档」，
            #  正是上面那段实测结论要防的事。
            tiers=_after_skip(ceiling),
            reason=(
                f"{len(judged)} 项规则分的归一余量均 >= {margin_ok}"
                f"（最弱 {weakest[0]}={weakest[1]:.2f}），"
                f"只跑到 {max_tier} 为止的可跳档{extra}"
            ),
        )

    # 剩下要跑哪几档：从 perceptual 起，逐档往上到 max_tier
    expensive = tuple(t for t in ceiling if t != "rule")

    # 第 3 条：LLM 只对「中长度的自然文本」有价值
    if "llm" in expensive and (n_chars < llm_min_chars or n_chars > llm_max_chars):
        return _decision(
            ceiling=ceiling,
            tiers=_after_skip(ceiling),
            reason=(
                f"文本长度 {n_chars} 不在 LLM 有效区间 "
                f"[{llm_min_chars}, {llm_max_chars}]，跳过 llm 档"
            ),
        )

    # 第 4 条：结构化数据不必问语义判官
    if "llm" in expensive and code_like_ends and _CODE_LIKE.match(text):
        return _decision(
            ceiling=ceiling,
            tiers=_after_skip(ceiling),
            reason="文本是编号/结构化数据形态，语义判官几乎必然判低，跳过 llm 档",
        )

    if judged:
        worst = min(judged.items(), key=lambda kv: kv[1])
        why = f"最弱余量 {worst[0]}={worst[1]:.2f} < {margin_ok}"
    else:
        why = "没有任何可判定的规则分（缺量纲对照表或全部不可判定）"
    return _decision(ceiling=ceiling, tiers=ceiling, reason=f"{why}，跑满到 {max_tier}")


#: 强制全量运行的成本档。
#: 原因：这些算子声明了 `shardable=False`，即它们需要**全量可见性**
#: （精确去重、跨样本时序一致性、分组统计）。
#: 逐样本路由会让它们看到残缺的全体 → 结果与静态基线不一致，
#: 而「Agent 的结论要与基线可比」是这个模块的红线。
#:
#: 所以它们不进 `decide_tiers` 的逐样本决策，
#: 而是由 `plan_for_corpus` 在语料级统一安排。
GLOBAL_ONLY_TIERS: frozenset[str] = frozenset({"global"})


@dataclass(frozen=True)
class CorpusPlan:
    """语料级的执行计划：逐样本路由 + 全量算子的统一安排。"""

    #: 每条样本的决策（key = 样本 id）
    per_sample: dict[str, TierDecision]
    #: 必须全量跑的算子（shardable=False 那些）
    global_ops: tuple[str, ...]
    #: 基线相对代价（全量跑所有档）
    baseline_cost: int
    #: 计划相对代价
    planned_cost: int

    @property
    def saved_ratio(self) -> float:
        if self.baseline_cost <= 0:
            return 0.0
        return 1.0 - self.planned_cost / self.baseline_cost


def keep_min_table(config: Any) -> dict[str, float | None]:
    """从 `PipelineConfig` 抽出 `算子名 → 漏斗的保留阈值`。

    ## 为什么要从 config 抽，而不是让调用方传
    第一版是让调用方自己传 `keep_min_of_op`，实测立刻出问题：
    我手写的那张表把 `doc_length` 的 min 写成 0.9（记成了比例），
    而真实配置是 30 —— 于是一个长度型的 score 被当成
    「离满分只剩 7%」，**179/193 条样本全被判成不干净**。

    错因不是「我算错了」，是**同一份参数被抄了两遍**。
    所以这里只提供一条路：从漏斗自己的配置读，
    `PipelineConfig` 不变则这张表永远不变。
    """
    out: dict[str, float | None] = {}
    for spec in getattr(config, "operators", None) or ():
        params = getattr(spec, "params", None) or {}
        out[spec.op] = params.get("min")
    return out


def tier_table(metas: dict[str, Any]) -> dict[str, str]:
    """从 `OperatorMeta` 注册表抽 `算子名 → 成本档`。

    真实来源是 `OperatorMeta.cost_class`（既有权威声明），
    这里**不自己维护一张表** —— 自己维护的表会与注册表漂移，
    而漂移的后果是「Agent 按错误的档位估算成本」。
    """
    out: dict[str, str] = {}
    for name, meta in (metas or {}).items():
        cc = getattr(meta, "cost_class", None)
        out[name] = getattr(cc, "value", None) or str(cc)
    return out


def plan_for_corpus(
    samples: list[Any],
    *,
    max_tier: str = "llm",
    config: Any = None,
    keep_min_of_op: dict[str, float] | None = None,
    margin_ok: float = 0.5,
    llm_min_chars: int = 40,
    llm_max_chars: int = 4000,
) -> CorpusPlan:
    """把逐样本决策汇总成语料级计划。

    ## 量纲对照表的两条来源，只能给一条
    - `config`：从 `PipelineConfig`现抽（**首选**）
    - `keep_min_of_op`：直接给表（给 `RunState` 这类已落盘的场景用）

    两条都不给 → 全部判据失效 → **一律按上限跑**。
    这是刻意的保守默认值：**没有尺子就不量长度**。

    同时给两条时 `keep_min_of_op` 优先并**覆盖** `config` 抽出的表——
    之所以不是报错而是覆盖：图编排的状态是从落盘恢复的，
    落盘里的表才是「当时真正用的那把尺子」，
    而现读config 只能反映「现在的配置」。两者不一致时，
    **以落盘的为准**才符合「事后审计」的目的。
    """
    thresholds = dict(keep_min_of_op) if keep_min_of_op else None
    if thresholds is None and config is not None:
        thresholds = {k: v for k, v in keep_min_table(config).items() if v is not None} or None

    per_sample: dict[str, TierDecision] = {}
    for s in samples:
        per_sample[s.id] = decide_tiers(
            s,
            max_tier=max_tier,
            keep_min_of_op=thresholds,
            margin_ok=margin_ok,
            llm_min_chars=llm_min_chars,
            llm_max_chars=llm_max_chars,
        )

    ceiling = ALL_TIERS[: ALL_TIERS.index(max_tier) + 1]
    baseline_cost = estimate_cost(ceiling, len(samples))
    planned_cost = sum(estimate_cost(d.tiers, 1) for d in per_sample.values())

    global_ops = tuple(sorted(GLOBAL_ONLY_TIERS))
    return CorpusPlan(
        per_sample=per_sample,
        global_ops=global_ops,
        baseline_cost=baseline_cost,
        planned_cost=planned_cost,
    )
