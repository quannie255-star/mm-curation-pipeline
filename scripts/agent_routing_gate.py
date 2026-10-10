"""Agent 路由 vs 静态基线：真实语料上的对照门禁。

## 这个门禁要回答的问题只有一个
**「Agent 省了钱，而且效果没掉」——两件事必须同时成立。**

只报节省是自欺：那说明不了代价。
只报效果是保守：说明不了这个模块的价值。

## 为什么必须现跑、不能用夹具
第一版策略的判据是「所有规则分 >= 0.9」，在夹具上**看不出问题**
（夹具里只有一个算子，score天然在 0.85 左右，恒过）；
只有真实语料里`doc_length`（score 是长度，p50=181）混进来，
才会暴露「不同量纲放在同一把尺子上」这个错误。
所以本门禁**必须读真实 text_corpus.jsonl**，用 `data/raw/` 里的真数据。

## 三条判据的关系（都是相对关系，不是绝对阈值）
1. 节省必须**实质性**：saved_ratio >= MIN_SAVED_RATIO
2. 效果必须**持平**：Agent 路由后的保留率与全量跑逐位一致
3. LLM 调用必须**真的下降**：否则「自适应路由」没发生

第2 条是红线：Agent 只能省成本，不能掉质量。
掉了就必须报实际差值，而不是「效果因人而异」。
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from mm_curation.agent import (  # noqa: E402
    ALL_TIERS,
    COST_WEIGHT,
    DecisionRecord,
    compress,
    keep_min_table,
    plan_for_corpus,
    run_state,
)
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline.config import OperatorSpec, PipelineConfig  # noqa: E402
from mm_curation.pipeline.runner import run_funnel  # noqa: E402

#: 门禁默认参数。**变异基线与门禁本体必须共用**——
#: 「变异基线跑另一套参数」本身就是一种假绿（parity 门禁踩过）。
DEFAULT_N = 300
DEFAULT_SCAN = 2000

#: 各档最小样本量。与 parity 门禁同值。
#: **它是实跑比出来的**：n=60 时比率能差2 倍，n=300 时稳定到小数点后两位。
MIN_ARM_SAMPLES = 200

#: 最低节省比例。定0.5 的理由：低于一半等于「路由基本没起作用」，
#: 而那说明判据口径又错了（本轮第一次实跑只有 7.2%）。
MIN_SAVED_RATIO = 0.5

#: 保留率的允许绝对偏差。
#: **必须是 0**：Agent 的放行条件是「漏斗本来也会放行」的严格子集，
#: 所以保留率应当**逐条一致**，不是「差不多」。
#: 放宽容差等于允许悄悄掉质量——而那正是本模块的红线。
KEEP_RATE_TOLERANCE = 0.0

#: 记忆压缩比上限（常驻 token / 全量 token）。
#: 定 0.1 的理由：常驻上下文只应含分桶摘要，量级上与样本数无关；
#: 实跑是 6 / 293 条理由 ≈ 0.001，离0.1 有两个数量级余量，
#: 所以 0.1 不会误伤正常实现，只拦得住「其实没压缩」那种。
MAX_COMPRESSION_RATIO = 0.1

#: 漏斗算子。**刻意混入异量纲的 doc_length**——
#: 少了它，量纲判据的错误在门禁里就测不出来（见模块 docstring）。
#: ── 规则档（L1）。**只做命名与类型零外部依赖的那批**。
RULE_OPERATORS = (
    ("doc_length", {"min": 30, "max": 50000}),
    ("chinese_ratio", {"min": 0.3}),
    ("char_repetition", {"min": 0.8}),
    ("line_repetition", {"min": 0.8}),
    ("boilerplate", {"min": 0.8}),
    ("pii_detect", {"min": 0.9}),
)

#: ── 贵档（MODEL）。**基线臂必须真的跑它，否则「效果持平」是拿自己比自己。**
#:
#: 第一版这里只放规则档，于是「基线臂」与「Agent 臂」跑的算子集合**完全相同**，
#: 而 `decide_tiers` 无论输入如何至少返回 `("rule",)`，
#: 所以 `only_in_agent` 与 `only_in_baseline` **恒为 0**、
#: `kept_rate` 恒等于 baseline——那条红线结构上永远不可能失败。
#: 变异测试 9/9 全拦也照样绿：
#: **变异测试只验「判据能否被数据结构触发」，无法发现「判据的前提本身恒成立」。**
#:
#: 加入真贵档后，Agent 跳过它就可能真的漏掉样本，红线才具备区分能力。
#:
#: 为什么选 `perplexity`（实测 2026-10-07，本机可跑、有真实区分力）：
#: - `clip_alignment` / `wm_nsfw_cnn` / `phash_near` 需要 `image_path`，
#:   纯文本样本上直接崩（`NoneType.read`）
#: - `blur` 是单样本接口（没有 `run_batch`），本模块按批调用
#: - `semantic_dedup` 需要 embedding 模型
#: → `perplexity` 是唯一「真能跑 + 真有区分力 + 纯文本」的 MODEL 档算子。
EXPENSIVE_OPERATORS = (("perplexity", {"min": 0.2}),)

#: 基线臂 = 规则档 + 贵档（= 真实 Agent 要对标的那条线）
OPERATORS = RULE_OPERATORS + EXPENSIVE_OPERATORS

#: 污染臂用的注入手段（与`synthesis_parity_gate.py` 同款，保持两处可比）
DIRTY_KINDS = {"low_quality_text": 1.0, "near_duplicate_text": 1.0}

DOC = REPO_ROOT / "docs" / "NARRATIVE.md"


def load_real_samples(n: int, scan_limit: int, workdir: pathlib.Path) -> list[Sample]:
    """从真实维基语料取 n 条文本样本。

    刻意不造假样本：这条门禁的全部价值就在于
    「真实数据的量纲分布与脏数据形态」，换成就没了。
    """
    corpus = REPO_ROOT / "data" / "raw" / "text_corpus.jsonl"
    if not corpus.exists():
        raise FileNotFoundError(
            f"找不到真实语料 {corpus}。本门禁不能用夹具替代——量纲判据的错误只有真实数据才暴露。"
        )
    out: list[Sample] = []
    with corpus.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= scan_limit or len(out) >= n:
                break
            d = json.loads(line)
            if len(d.get("text", "")) < 80:
                continue
            out.append(Sample(id=d["id"], text=d["text"], modality="text_article"))
    return out


def build_config(
    workdir: pathlib.Path, operators: tuple[tuple[str, dict[str, object]], ...] | None = None
) -> PipelineConfig:
    ops = OPERATORS if operators is None else operators
    return PipelineConfig(
        name="agent_gate",
        raw_jsonl=REPO_ROOT / "data" / "raw" / "text_corpus.jsonl",
        output_dir=workdir,
        operators=[OperatorSpec(op=o, params=dict(p)) for o, p in ops],
    )


def _run_dirty_arm(
    *,
    samples: list[Sample],
    rule_cfg: PipelineConfig,
    full_cfg: PipelineConfig,
    workdir: pathlib.Path,
    margin_ok: float,
) -> dict[str, object]:
    """污染臂：脏数据上「跳过贵档」到底漏没漏。

    ## 为什么必须有这一臂
    第一版红线只在**干净**语料上比，而实测：
    - 干净 150 条：`perplexity` kept 150/150（一个都不拦，score p50=0.85）
    - 污染 150 条：`perplexity` kept 127，而规则档只 kept 80

    也就是说 **MODEL 档在这批语料上比 L1 规则档更宽松**（净增拦截是负的）。
    这既说明「贵档不一定更严」，也说明：
    **在干净数据上，红线永远不可能变红** —— 那不是红线的功劳，是数据没有脏东西。

    所以脏数据上必须单独量一次「Agent 跳过贵档漏掉了多少条」，
    那才是这条红线真正的检验。
    """
    import copy

    from mm_curation.contamination import ContaminationPlan

    tmp = workdir / "dirty_arm"
    seeded, cmf = ContaminationPlan(inject_rate=1.0, seed=7, kinds=dict(DIRTY_KINDS)).run(
        copy.deepcopy(samples), tmp / "con"
    )
    dirty = seeded[len(samples) :]
    if not dirty:
        return {"n": 0, "note": "污染臂没产出样本（本机缺依赖或语料不足）"}

    rule_res = run_funnel(list(dirty), rule_cfg)
    rule_ids = {s.id for s in rule_res.kept}
    full_res = run_funnel(list(dirty), full_cfg)
    full_ids = {s.id for s in full_res.kept}

    # 规则档放行、但含贵档的漏斗会拦的样本 —— 跳过贵档就等于漏掉它们
    missed = rule_ids - full_ids

    # Agent 在这批脏数据上会怎么路由（只看规则档的分数）
    dirty_plan = plan_for_corpus(list(rule_res.kept), config=rule_cfg, margin_ok=margin_ok)

    # ★ 真正的漏检量。第一版写的是 `rule_ids - agent_ids`，**那是恒真的**：
    #   plan 本来就是对 `rule_res.kept` 做的，`decide_tiers` 又至少返回
    #   ("rule",)，于是 `agent_ids ⊇ rule_ids` 恒成立 → 差集恒空 → 门禁永不红。
    #   这类「恒真判据」比缺门禁更危险：它给你一个漂亮的满分。
    #
    #   正确问法：**Agent 声称跳过了贵档的那些样本里，有多少条是贵档本来会拦的。**
    #   即 `missed ∩ skipped`。这个量可能非零，也必须非零——
    #   省成本不能以漏检脏数据为代价。
    #
    #   「贵档」= 除 rule 外的全部档，**不写死档名**：
    #   档位集合变了（加了新档 / 改了 cost_class）这里自动跟着变。
    expensive_tiers = set(ALL_TIERS) - {ALL_TIERS[0]}
    skipped_ids = {
        sid for sid, d in dirty_plan.per_sample.items() if set(d.skipped_tiers) & expensive_tiers
    }
    agent_missed = missed & skipped_ids
    return {
        "n": len(dirty),
        "rule_kept": len(rule_ids),
        "full_kept": len(full_ids),
        "agent_kept": len(dirty_plan.per_sample),
        # Agent 声称跳过贵档的条数（分母：判断「跳了却没漏」是不是侥幸）
        "agent_skipped_expensive": len(skipped_ids),
        # 贵档相对规则档的**净增拦截**。负数=贵档更宽松（本机实测就是负的）。
        "expensive_extra_catch": len(rule_ids) - len(full_ids),
        "costly_only": len(missed),
        "agent_missed": len(agent_missed),
        "injected_counts": cmf["counts"],
    }


def run_experiment(
    *, n: int, scan_limit: int, workdir: pathlib.Path, margin_ok: float = 0.5
) -> dict[str, object]:
    """跑一次对照，返回结构化结果（不做判定——判定与实跑必须分开）。

    ## 三臂设计（第一版只有两臂，且其中一臂是恒真的）
    - **基线臂**：规则档 **+ 贵档** 全量跑（= Agent 要对标的那条线）
    - **Agent 臂**：按样本状态路由（可能跳过贵档）
    - **污染臂**：真实脏数据，用来检查红线在「有脏东西时」会不会变红

    污染臂不能省：第一版只在**干净**语料上比，而干净语料里
    `perplexity` 一个都拦不下（kept 150/150），
    于是「跳过贵档」在干净数据上永远是安全的 —— 那条红线依然恒真。
    **判据必须在它可能失败的数据上验证。**
    """
    samples = load_real_samples(n, scan_limit, workdir)
    if not samples:
        return {"error": f"语料里不足 {n} 条可用样本", "arms": {}}

    config = build_config(workdir)
    rule_only_cfg = build_config(workdir, operators=RULE_OPERATORS)

    # ── 基线臂：静态漏斗全量跑，含贵档
    base = run_funnel(list(samples), config)
    kept_ids = {s.id for s in base.kept}

    # ── 规则档单独跑一遍：Agent 只看得到这些分数，它的放行依据就是它们
    rule_only = run_funnel(list(samples), rule_only_cfg)
    rule_kept_ids = {s.id for s in rule_only.kept}

    # ── Agent 臂：按样本状态路由
    plan = plan_for_corpus(list(rule_only.kept), config=rule_only_cfg, margin_ok=margin_ok)

    # ★ Agent 的输入集必须**恰好**是规则档放行的那批。
    #   若误把「含贵档的基线放行集」喂给 plan，plan 就会读到
    #   `perplexity` 的分数——那等于偷看答案，红线立刻失去意义。
    #   （这一条第一版没有，于是「省了 96% 成本」曾经可能只是
    #   「它其实读过贵档分数」。）
    #   判据用**集合相等**，不用 `rule_only.kept` 自比——那恒真。
    routing_input_ids = set(plan.per_sample)
    routing_input_ok = routing_input_ids == rule_kept_ids

    n_llm = sum(1 for d in plan.per_sample.values() if d.runs_expensive)
    dist = collections.Counter("+".join(d.tiers) for d in plan.per_sample.values())
    skipped = collections.Counter(t for d in plan.per_sample.values() for t in d.skipped_tiers)

    # ★ 效果对照：Agent 的放行集合 vs **含贵档的**基线放行集合。
    #   两者不同就说明「跳过贵档」真的漏掉了东西 —— 这条现在**可能变红**了。
    agent_kept_ids = {sid for sid, d in plan.per_sample.items() if d.tiers}
    only_base = kept_ids - agent_kept_ids
    only_agent = agent_kept_ids - kept_ids

    mem = compress(
        [
            DecisionRecord(
                sample_id=sid,
                tiers=d.tiers,
                reason=d.reason,
                evidence="",
            )
            for sid, d in plan.per_sample.items()
        ],
        skipped=dict(skipped),
    )

    # 复跑一致性：同输入再算一次，决策必须逐条一致
    plan2 = plan_for_corpus(list(rule_only.kept), config=rule_only_cfg, margin_ok=margin_ok)
    reproducible = {k: v.tiers for k, v in plan.per_sample.items()} == {
        k: v.tiers for k, v in plan2.per_sample.items()
    }

    state = run_state(
        run_id="gate",
        samples=list(rule_only.kept),
        config=rule_only_cfg,
        margin_ok=margin_ok,
    )

    # ── 污染臂：红线唯一可能被触发的地方。
    # 实测（2026-10-07）：干净语料上 perplexity kept 150/150 —— 一个都不拦，
    #   所以在干净数据上「跳过贵档」永远安全，那条红线毫无区分能力。
    #   加脏数据之后，「规则档放行而贵档要拦」的样本才可能出现。
    dirty_arm = _run_dirty_arm(
        samples=samples,
        rule_cfg=rule_only_cfg,
        full_cfg=config,
        workdir=workdir,
        margin_ok=margin_ok,
    )

    return {
        "n_scanned": len(samples),
        "arms": {
            "baseline": {
                "n": len(kept_ids),
                "kept": len(kept_ids),
                "kept_rate": len(kept_ids) / len(samples),
                "flag_rate": 1 - len(kept_ids) / len(samples),
            },
            "agent": {
                "n": len(plan.per_sample),
                "kept": len(agent_kept_ids),
                "kept_rate": len(agent_kept_ids) / len(samples),
                "flag_rate": 1 - len(agent_kept_ids) / len(samples),
                "n_llm_calls": n_llm,
                "only_in_baseline": len(only_base),
                "only_in_agent": len(only_agent),
                "decision_dist": dict(sorted(dist.items())),
                "skipped_tiers": dict(sorted(skipped.items())),
                "reproducible": reproducible,
                "routing_input_ok": routing_input_ok,
                "n_rule_kept": len(rule_kept_ids),
            },
            "dirty": dirty_arm,
        },
        "cost": {
            "baseline": plan.baseline_cost,
            "planned": plan.planned_cost,
            "saved_ratio": plan.saved_ratio,
        },
        "threshold_table": {
            k: v for k, v in sorted(keep_min_table(config).items()) if v is not None
        },
        "margin_ok": margin_ok,
        "memory": {
            "n_records": mem.n_records,
            # 两个口径必须都报：常驻（压缩后）与全量（审计）。
            # 只报全量会让人以为「压缩了 5000 token」，而那全是逐条理由。
            "summary_tokens": mem.summary_tokens(),
            "full_context_tokens": mem.full_context_tokens(),
            "compression_ratio": mem.compression_ratio(),
            "rendered_lines": len(mem.render_summary().splitlines()),
        },
        "state_manifest": {
            "n_samples": len(state.samples),
            "n_plans": len(state.plans),
            "has_threshold_table": bool(state.keep_min_of_op),
        },
        "cost_weights": dict(COST_WEIGHT),
    }


def judge(result: dict[str, object]) -> tuple[bool, list[str]]:
    """把实跑结果判成红/绿。**只看关系，不看绝对阈值的漂亮程度。**"""
    problems: list[str] = []
    arms = result.get("arms") or {}
    if not arms:
        return False, [str(result.get("error", "实验没跑出结果"))]

    base = arms["baseline"]
    agent = arms["agent"]
    cost = result["cost"]

    # 关系 1：效果持平（红线）
    #
    # ★ 这一条第一版是**恒真**的（两臂跑同一批算子 + decide_tiers 至少返回
    #   ("rule",)），现在基线臂含真贵档 `perplexity`，它**可能**变红了。
    if agent["n"] < MIN_ARM_SAMPLES:
        problems.append(f"Agent 臂只有 {agent['n']} 条样本（需 >= {MIN_ARM_SAMPLES}），比率不可信")
    delta = abs(agent["kept_rate"] - base["kept_rate"])
    if delta > KEEP_RATE_TOLERANCE:
        problems.append(
            f"★效果没持平：保留率 基线={base['kept_rate']:.4f} "
            f"Agent={agent['kept_rate']:.4f}（差 {delta:.4f} > {KEEP_RATE_TOLERANCE}）。"
            f"另有 {agent['only_in_agent']} 条 Agent 放行但漏斗不keep、"
            f"{agent['only_in_baseline']} 条漏斗 keep 但 Agent 没经手。"
        )

    # 关系 1c：★ Agent 不得偷看贵档分数
    #
    # 路由的**全部意义**在于「只用便宜档的分数决定要不要跑贵的」。
    # 若喂给 plan 的样本集里混进了「已含贵档分数」的样本，
    # 那么「省下 96% 成本」就只是账面数字——它其实读过答案。
    # 第一版没有这条判据，于是这个作弊可以静默通过。
    if not agent.get("routing_input_ok", False):
        problems.append(
            f"★Agent 的路由输入集不等于规则档放行集"
            f"（plan 收了 {agent['n']} 条，规则档放行 {agent['n_rule_kept']} 条）——"
            f"它可能读到了贵档分数，那「省成本」就不成立了。"
        )

    # 关系 1b：★ 污染臂上的漏检（这条才是红线的真实检验）
    #
    # 干净语料上贵档一个都不拦（实测 kept 150/150），
    # 所以关系 1 在干净数据上恒绿。必须在脏数据上量：
    # 「规则档放行、但贵档要拦」的样本里，Agent 漏掉了几条。
    #
    # 判据用「漏检数 == 0」而不是比率：脏臂样本量小，比率的分辨率不够，
    # 而且漏检 1 条就是漏检，**没有「基本持平」这回事**。
    dirty = arms.get("dirty") or {}
    if dirty.get("n"):
        if dirty["agent_missed"] > 0:
            problems.append(
                f"★脏数据上漏检{dirty['agent_missed']} 条："
                f"规则档放行 {dirty['rule_kept']} 条、含贵档只放行 {dirty['full_kept']} 条，"
                f"其中贵档独拦 {dirty['costly_only']} 条；"
                f"Agent 声称跳过贵档 {dirty['agent_skipped_expensive']} 条，"
                f"其中 {dirty['agent_missed']} 条本该被贵档拦下。"
                f"省成本不能以漏检为代价。"
            )
        elif dirty["agent_skipped_expensive"] == 0:
            # 「没漏检」有两种可能：不跳，或跳了但那些样本本来就没被贵档拦。
            # 后者才是真省；前者说明白名单收得太紧，这条门禁在偷懒。
            problems.append(
                "污染臂上Agent 一条贵档都没跳（跳过 0 条）——"
                "那么「没漏检」是因为没省钱，不是因为判据有效。"
                "这条红线在当前配置下不构成证据。"
            )
    else:
        # 没有脏臂数据就不能声称红线被验证过——**如实报，不静默跳过**
        problems.append(
            "污染臂没跑出样本，红线未在脏数据上验证。"
            "干净语料上贵档一个都不拦，那条关系 1 在此处恒真，不能当作「效果持平」的证据。"
        )

    # 关系 2：节省必须实质性
    if cost["saved_ratio"] < MIN_SAVED_RATIO:
        problems.append(
            f"只省了 {cost['saved_ratio']:.1%}（需 >= {MIN_SAVED_RATIO:.0%}）——"
            "省得这么少通常不是「优化保守」，而是判据口径又错了"
        )

    # 关系 3：LLM 调用真的下降
    if agent["n_llm_calls"] >= base["n"]:
        problems.append(f"LLM 调用没下降：基线 {base['n']} → 路由后 {agent['n_llm_calls']}")

    # 关系 4：决策可复现
    if not agent["reproducible"]:
        problems.append("同输入两次跑出的决策不一致——「可复跑」纪律被破坏")

    # 关系 5：落盘必须带着量纲表（事后审计要能回答「按什么判的」）
    if not result["state_manifest"]["has_threshold_table"]:
        problems.append("RunState 落盘里没有量纲对照表，事后无法复现当时的判据")

    # 关系 6：记忆压缩必须真的与规模解耦。
    # 这条判据来自一个真实失效：`summary_tokens()` 的 docstring 声称
    # 「不会随样本量线性爆炸」，实现却把每条 reason 都算进去
    # （293 条 -> 6 token 的摘要被算成 5206 token）。
    # **看起来实现了压缩，实际没有** —— 所以必须用比值判，不能只看「比原文小」。
    mem = result["memory"]
    if mem["compression_ratio"] >= MAX_COMPRESSION_RATIO:
        problems.append(
            f"记忆压缩比 {mem['compression_ratio']:.4f} >= "
            f"{MAX_COMPRESSION_RATIO}：常驻上下文没有与样本量解耦。"
            f"（常驻 {mem['summary_tokens']} / 全量 {mem['full_context_tokens']} token）"
        )

    return not problems, problems


def check_doc(result: dict[str, object]) -> list[str]:
    """NARRATIVE.md 里写的数字必须等于本门禁现算的。

    比「文档 ↔ 注册表」更强：这是**文档 ↔ 实跑**。
    注册表只保证文档与注册表一致，而注册表也可能过期。
    """
    if not DOC.exists():
        return [f"找不到 {DOC}，无法校验文档数字"]
    lines = DOC.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []
    arms = result["arms"]
    checks = (
        ("baseline", arms["baseline"]["kept_rate"], "静态基线"),
        ("agent", arms["agent"]["kept_rate"], "Agent 路由"),
    )
    for key, value, label in checks:
        literal = f"{value * 100:.2f}%"
        hit = [(i + 1, ln) for i, ln in enumerate(lines) if label in ln and literal in ln]
        if not hit:
            problems.append(
                f"NARRATIVE.md 里找不到 {label} 的保留率 {literal}"
                f"（本轮实跑 = {value:.4%}）。文档数字必须现跑现写，不能手抄。"
            )
    saved = f"{result['cost']['saved_ratio'] * 100:.1f}%"
    if not any("Agent" in ln and saved in ln for ln in lines):
        problems.append(
            f"NARRATIVE.md 里找不到 Agent 的节省比例 {saved}"
            f"（本轮实跑 = {result['cost']['saved_ratio']:.4%}）"
        )
    return problems


def _mutate() -> int:
    print("== 变异测试（在内存里做，真实文件只读）==")
    workdir = REPO_ROOT / "data" / "tmp_agent_gate_mut"
    base = run_experiment(n=DEFAULT_N, scan_limit=DEFAULT_SCAN, workdir=workdir)
    ok, problems = judge(base)
    if not ok:
        print("[RED] 变异测试中止：基线本身就不绿，判据无区分能力", file=sys.stderr)
        for p in problems:
            print(f"       {p}", file=sys.stderr)
        return 2
    print(
        f"[GREEN] 基线绿：saved={base['cost']['saved_ratio']:.1%} "
        f"kept 基线={base['arms']['baseline']['kept_rate']:.4f} "
        f"Agent={base['arms']['agent']['kept_rate']:.4f} "
        f"llm={base['arms']['agent']['n_llm_calls']}"
    )
    print()

    results: list[bool] = []

    def mutate_and_judge(fn, label: str) -> None:
        m = json.loads(json.dumps(base))
        fn(m)
        hit = not judge(m)[0]
        results.append(hit)
        print(f"  [{'通过' if hit else '未通过'}] {label}")

    # 变异 1：Agent 多keep 了漏斗不keep 的样本 → 关系 1 必须拦
    mutate_and_judge(
        lambda m: m["arms"]["agent"].update(
            {"kept_rate": m["arms"]["agent"]["kept_rate"] + 0.05, "only_in_agent": 7}
        ),
        "Agent 放行了漏斗不 keep 的样本 → 效果持平",
    )

    # 变异 2：节省掉到 3% → 关系 2 必须拦
    mutate_and_judge(
        lambda m: m["cost"].update({"saved_ratio": 0.03}),
        "节省只有 3% → 实质性节省",
    )

    # 变异 3：LLM 调用没下降 → 关系 3 必须拦
    mutate_and_judge(
        lambda m: m["arms"]["agent"].update({"n_llm_calls": m["arms"]["baseline"]["n"]}),
        "LLM 调用没下降 → 自适应路由",
    )

    # 变异 4：决策不可复现 → 关系 4 必须拦
    mutate_and_judge(
        lambda m: m["arms"]["agent"].update({"reproducible": False}),
        "同输入决策不一致 → 可复跑",
    )

    # 变异 5：样本量不足 → 必须拦
    mutate_and_judge(
        lambda m: m["arms"]["agent"].update({"n": 30}),
        "Agent 臂只有 30 条 → 样本量下限",
    )

    # 变异 6：落盘丢了量纲表 → 关系 5 必须拦
    mutate_and_judge(
        lambda m: m["state_manifest"].update({"has_threshold_table": False}),
        "落盘缺量纲表 → 可审计",
    )

    # 变异 7：记忆压缩没解耦（常驻 ≈ 全量）→ 关系 6 必须拦。
    #   这条模拟的就是实测那个失效：summary_tokens 把逐条理由都算进去。
    mutate_and_judge(
        lambda m: m["memory"].update({"compression_ratio": 0.87}),
        "常驻≈全量 → 记忆压缩",
    )

    # 变异 8：文档数字漂移 → 文档一致性必须拦
    m = json.loads(json.dumps(base))
    m["cost"]["saved_ratio"] = 0.8888
    hit = bool(check_doc(m))
    results.append(hit)
    print(f"  [{'通过' if hit else '未通过'}] 实测漂移但文档未改 → 文档一致性")

    # ★ 正向变异：实测与文档一致时**必须放行**。
    #   只加会拦红的变异是不够的——一条永远红的判据等于没有判据。
    ok_pos, probs = judge(base)
    results.append(ok_pos)
    print(f"  [{'通过' if ok_pos else '未通过'}] 原样基线 → 文档一致性放行（正向）")

    passed = sum(results)
    print(f"\n变异测试：{passed}/{len(results)} 条被正确处理")
    return 0 if passed == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=DEFAULT_N, help=f"真实样本数（默认 {DEFAULT_N}）")
    ap.add_argument(
        "--scan-limit",
        type=int,
        default=DEFAULT_SCAN,
        help=f"最多读多少行语料（默认 {DEFAULT_SCAN}）",
    )
    ap.add_argument("--json-out", default="", help="把结果写到该 JSON 路径")
    ap.add_argument("--mutate", action="store_true", help="变异测试")
    ap.add_argument("--no-doc", action="store_true", help="跳过文档一致性校验（诊断用）")
    args = ap.parse_args()

    if args.mutate:
        return _mutate()

    workdir = REPO_ROOT / "data" / "tmp_agent_gate"
    result = run_experiment(n=args.n, scan_limit=args.scan_limit, workdir=workdir)
    ok, problems = judge(result)
    doc_problems = [] if args.no_doc else check_doc(result)
    all_problems = problems + doc_problems
    ok = ok and not doc_problems

    arms = result["arms"]
    print(f"真实样本 {result['n_scanned']} 条→ 漏斗 kept {arms['baseline']['kept']}")
    print(f"  量纲表（来自 PipelineConfig，不是手抄）: {result['threshold_table']}")
    print(
        f"  静态基线 kept_rate = {arms['baseline']['kept_rate']:.2%}"
        f"（被拦 {arms['baseline']['flag_rate']:.2%}）"
    )
    print(
        f"  Agent 路由 kept_rate = {arms['agent']['kept_rate']:.2%}"
        f"  LLM 调用 {arms['baseline']['n']} → {arms['agent']['n_llm_calls']}"
    )
    print(f"  决策分布: {arms['agent']['decision_dist']}")
    print(f"  跳过的档: {arms['agent']['skipped_tiers']}")
    print(
        f"  成本: 基线 {result['cost']['baseline']} → 计划 {result['cost']['planned']}"
        f"（省 {result['cost']['saved_ratio']:.2%}）"
    )
    print(
        f"  记忆: {result['memory']['n_records']} 条决策压成 "
        f"{result['memory']['rendered_lines']} 行摘要"
        f"（常驻 {result['memory']['summary_tokens']} token，"
        f"全量 {result['memory']['full_context_tokens']} token，"
        f"压缩比 {result['memory']['compression_ratio']:.4f}）"
    )
    print(f"  决策可复现: {arms['agent']['reproducible']}")

    if ok:
        print("[GREEN] 省了钱且效果逐位持平，Agent 路由成立")
    else:
        print("[RED] 对照不成立：", file=sys.stderr)
        for p in all_problems:
            print(f"       {p}", file=sys.stderr)

    if args.json_out:
        out = pathlib.Path(args.json_out)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告已写入 {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
