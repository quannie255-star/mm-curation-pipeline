#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合成样本的「同口径可比」门禁。

## 这个脚本回答什么问题

ROADMAP 第三节第2 项的红线原文：

    合成样本必须走同一条漏斗，否则「我合成了一批数据」不可证伪。

「走了同一条漏斗」很容易做到（本脚本就是这么做的）。
真正难回答的是：**走了之后，数字可比吗？**
如果合成样本走漏斗得到的数字与真实脏数据差一个量级，
那两张表放在一起比较就是错的，而错在哪没人看得出来。

所以这里做**三臂对照**，三臂过**同一条漏斗、同一个阈值**：

    A 真实干净样本（原始维基正文）
    B 合成增强样本（SynthesisPlan 派生）
    C 污染注入样本（ContaminationPlan 注入）

## 三个数字的关系才是判据，单个数字不是

    背景误杀率  flag_rate(clean)  ≈ 数据集**自带**的异常率上界
    合成误杀率  flag_rate(synth)  ≈ 与背景同档 → 合成**没有引入新误杀**
    注入检出率  flag_rate(inject) ≫ 背景     → 漏斗**看得见**要抓的东西

三条同时成立才叫「同口径可比」。
只报其中一个都可能自圆其说：
- 只报 B 很低→ 可能只是漏斗瞎了
- 只报 C 很高  → 可能只是漏斗过杀，连干净集也一起杀

## ⚠️ 三条口径不许混（血泪，2026-10-05）

`flag_rate(clean)` **不是误杀率**。
它是这批维基语料**本来就有**的异常比例——
维基正文里确实混着广告模板、注音符号堆砌、超短条目、
段落复读。这些是真的该被拦的，所以它只能当**上界**，
不能当「算子错杀的量」。

同理 `flag_rate(synth)` 也不是「增强引入的噪声」：
合成样本来自同一个干净集，它被拦的原因与A 臂**同源**。

## 为什么污染 kind 只选文本类

`ContaminationPlan` 的 kinds 里图像类（blur / low_resolution / watermark /
near_duplicate_image / exact_duplicate / semantic_duplicate）
在纯文本样本上会直接抛
`AttributeError: 'NoneType' object has no attribute 'seek'`
——因为 `Sample.image_path` 是 None。

**这是既有缺口，不是本轮引入的**：
漏斗侧有模态门禁（`run_funnel` 会检查算子模态与数据集是否不相交），
而 `Contaminator` **没有 `modalities` 声明**，所以配错就是崩而不是拒。
本脚本绕开它（只选文本类 kind），并把这个缺口记在本文档里，
而不是假装它不存在。见 ROADMAP 第六节的「如实未做」。
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from mm_curation.contamination import ContaminationPlan  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline.config import OperatorSpec, PipelineConfig  # noqa: E402
from mm_curation.pipeline.runner import run_funnel  # noqa: E402
from mm_curation.synthesis import SynthesisPlan  # noqa: E402

CORPUS = REPO_ROOT / "data" / "raw" / "text_corpus.jsonl"

# 漏斗算子：只用**零外部依赖**的 L1 规则层。
# 为什么不加 text_minhash / perplexity：
#   - text_minhash 是 batch 算子，需要全量视角，分母与单样本臂不同口径
#   - perplexity 需要 GPT-2 zh 权重，本机拉不到 → 门禁必须能在离线跑
# 这个取舍是**口径选择**，不是「做不到」：L1 层已经能分出三臂的档位
# （见实测 1.83% / 1.27% / 43.67%），加L2 只是让绝对值变，不会改变关系。
FUNNEL_OPS: list[tuple[str, dict[str, Any]]] = [
    ("doc_length", {"min": 30, "max": 50000}),
    ("chinese_ratio", {"min": 0.3}),
    ("char_repetition", {"min": 0.8}),
    ("line_repetition", {"min": 0.8}),
    ("boilerplate", {"min": 0.8}),
    ("pii_detect", {"min": 0.9}),
]

# 合成手段：只用纯文本类，且都是**可逆或结构性**的（见 impl.py 的可逆性对照表）
SYNTH_KINDS = {"text_deredup": 1.0, "text_paraphrase": 1.0, "text_truncate_repair": 1.0}

# 污染 kind：只选真文本类（图像类在纯文本样本上会崩，见模块 docstring）
DIRTY_KINDS = {"low_quality_text": 1.0, "near_duplicate_text": 1.0}

#: 门禁默认参数。**门禁本体与变异基线必须用同一套**，
#: 所以提成常量让「不一致」在结构上就不可能发生。
#: 第一版变异基线单写 n=300，于是 dirty臂 = 150< MIN_ARM_SAMPLES → 基线红 →
#: 纪律生效、中止。那是判据在正常工作，但结论是：
#: 「变异基线跑另一套参数」本身就是一种假绿，变异结果没有说服力。
DEFAULT_N = 600
DEFAULT_SCAN = 3000

#: 各臂最小样本量。**不是拍脑袋的**：实跑两档对比得出——
#: n=60 时比率能差2 倍（干净 11.7%），n=394 时 1.27% 稳定到小数点后两位。
#: 200 落在「比率已经稳定」那一侧。
MIN_ARM_SAMPLES = 200


def load_clean(n: int, scan_limit: int, min_chars: int) -> list[Sample]:
    """从真实语料取 n 条正文。

    `scan_limit` 是**读多少行**，`n` 是**要多少条**——
    两者分开是因为语料里混着超短条目（<min_chars），
    只按 n 读会拿到不足量的样本，然后被误判成「数据变少了」。
    """
    out: list[Sample] = []
    with CORPUS.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if lineno > scan_limit or len(out) >= n:
                break
            row = json.loads(line)
            if len(row["text"]) < min_chars:
                continue
            out.append(Sample(id=row["id"], text=row["text"], modality="text_article"))
    if not out:
        raise SystemExit(f"[RED] 从 {CORPUS} 没读到可用样本（scan_limit={scan_limit}）")
    return out


def build_config(name: str) -> PipelineConfig:
    return PipelineConfig(
        name=name,
        raw_jsonl=CORPUS,
        output_dir=REPO_ROOT / "data" / f"tmp_synth_parity_{name}",
        operators=[OperatorSpec(op=op, params=params) for op, params in FUNNEL_OPS],
    )


def flag_rate(batch: list[Sample], config: PipelineConfig) -> dict[str, Any]:
    """跑漏斗，返回被拦比率与按算子拆分的明细。"""
    result = run_funnel(list(batch), config)
    dropped = len(result.dropped)
    by_op: dict[str, int] = {}
    for op_name, _sample in result.dropped:
        by_op[op_name] = by_op.get(op_name, 0) + 1
    return {
        "n": len(batch),
        "kept": len(result.kept),
        "dropped": dropped,
        "flag_rate": round(dropped / len(batch), 6) if batch else 0.0,
        "by_operator": dict(sorted(by_op.items())),
    }


def run_experiment(n: int, scan_limit: int, workdir: pathlib.Path) -> dict[str, Any]:
    clean = load_clean(n, scan_limit, min_chars=80)
    config = build_config("parity")

    synth_out, synth_mf = SynthesisPlan(
        augment_per_sample=2, coverage=0.5, seed=42, kinds=dict(SYNTH_KINDS)
    ).run(copy.deepcopy(clean), images_out=workdir / "img")
    synthesized = synth_out[len(clean) :]

    dirty_out, dirty_mf = ContaminationPlan(inject_rate=0.5, seed=7, kinds=dict(DIRTY_KINDS)).run(
        copy.deepcopy(clean), workdir / "con"
    )
    injected = dirty_out[len(clean) :]

    return {
        "config": {
            "operators": [op for op, _ in FUNNEL_OPS],
            "n_clean": len(clean),
            "synth_kinds": dict(SYNTH_KINDS),
            "dirty_kinds": dict(DIRTY_KINDS),
        },
        "arms": {
            "clean": flag_rate(clean, config),
            "synth": flag_rate(synthesized, config),
            "dirty": flag_rate(injected, config),
        },
        "manifests": {"synth": synth_mf, "dirty": dirty_mf},
    }


def judge(result: dict[str, Any]) -> tuple[bool, list[str]]:
    """三条关系全部成立才算同口径可比。

    判据形式是**关系**而不是绝对阈值：
    绝对阈值会随语料、算子版本漂移，而关系是本实验真正要证的命题。
    """
    arms = result["arms"]
    clean = arms["clean"]["flag_rate"]
    synth = arms["synth"]["flag_rate"]
    dirty = arms["dirty"]["flag_rate"]

    problems: list[str] = []

    # 关系 1：合成臂不得高于背景档（上界 = 背景 + 一个百分点）
    # 用百分点而不是倍数：低基数下倍数会放大噪声。
    if synth > clean + 0.01:
        problems.append(
            f"合成臂误杀 {synth:.2%} 高于真实干净臂{clean:.2%}+1pp → 增强引入了新误杀，这条不成立"
        )

    # 关系 2：注入检出率必须显著高于背景档（至少 5 倍）
    if clean > 0 and dirty < clean * 5:
        problems.append(
            f"注入检出 {dirty:.2%} 不到背景 {clean:.2%} 的 5 倍 "
            f"→ 漏斗看不见要抓的东西，这条实验没有区分能力"
        )
    if clean == 0.0 and dirty < 0.05:
        problems.append(f"背景为 0 但注入检出仅 {dirty:.2%} → 绝对量太低，样本量不足")

    # 关系 3：三臂样本量都要够，否则比率不可信（60 条时比率能差 2 倍）
    for name in ("clean", "synth", "dirty"):
        if arms[name]["n"] < MIN_ARM_SAMPLES:
            problems.append(
                f"{name} 臂仅 {arms[name]['n']} 条样本（需 >= {MIN_ARM_SAMPLES}），比率不可信"
            )

    return not problems, problems


# ── 文档一致性：这三个比率必须与 NARRATIVE.md 里写的逐字一致 ──
#
# ## 为什么不走 claims.json 的facade
# facade 的 `source` 只有三种形态（claim / baseline / derived），
# 而 claim 要指向一个**入库的** JSON 指针（`data/reports/*.json`）。
# 但 `data/reports/` 整棵被 .gitignore 忽略——
# 报告是本地产物，不入库。
#
# 所以把 claim 绑上去只有两条路：
#   ①硬绑→ CI 上那个文件不存在，门禁变成「无法校验」，**把「没门禁」写成「有门禁」**
#   ② `comparator: historical` → 同上，只是明说了而已
# 本项目N-3b 里0.575 就是这么变成「仍未注册」的。
#
# ## 那这道门靠什么成立
# 靠**本门禁自己**现算并比对文档——它是唯一能把「实测」和「文档」
# 对上的那道门，因为报告不入库，跨文件比对根本无从谈起。
# 代价：必须跑真实语料（本机约 40 秒），所以进不了「秒级门禁」那一档。
# 这个取舍是刻意的：**慢的门禁 > 假绿的门禁**。
DOC = REPO_ROOT / "docs" / "NARRATIVE.md"

#:臂名 → NARRATIVE.md 那张表里的**行首锚点**。
#:
#: ## 为什么用行首锚点而不是全文搜数字
#: 第一版 `check_doc` 用 `if literal not in text`。端到端验证时抓到它失效：
#: 我把表格里的 `43.67%` 改成 `51.23%`，门禁**仍然报绿**——
#: 因为正文里还有两处提到 43.67%（「实测 43.67% / 1.83% ≈ 24 倍」
#: 与「只有 43.67% 才叫检出率」），子串照样能找到。
#:
#: 这与本项目踩过的「静默失效」是同一类：**判据看起来在拦，实际拦不住**。
#: 子串检查在任何「同一个数字在文档里出现多次」的场景都会漏——
#: 而这恰恰是数字被引用的常态。
#:
#: 正解是**结构化定位**：只认「行首锚点匹配的那一行里的那个数字」。
DOC_ARMS = {
    "clean": "| A 真实干净",
    "synth": "| B 合成增强",
    "dirty": "| C 污染注入",
}


def check_doc(result: dict[str, Any]) -> list[str]:
    """NARRATIVE.md 那张表里每臂的比率必须等于本门禁现算的比率。

    比「文档↔ 注册表」更强的判据：**文档 ↔ 实跑**。
    注册表只保证「文档与注册表一致」，而注册表也可能过期；
    这里直接把实测值渲染后要求表格行里逐字出现。

    判据匹配的是**结构**（行首锚点 + 该行内唯一百分比）而不是子串。
    """
    if not DOC.exists():
        return [f"找不到 {DOC}，无法校验文档数字"]
    lines = DOC.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []

    for arm, anchor in DOC_ARMS.items():
        rate = result["arms"][arm]["flag_rate"]
        literal = f"{rate * 100:.2f}%"

        hits = [line for line in lines if line.startswith(anchor)]
        if len(hits) != 1:
            problems.append(
                f"{DOC} 里以 {anchor!r} 开头的行有 {len(hits)} 条（需恰好 1）"
                f"→ 无法定位 {arm} 臂的数字"
            )
            continue

        row = hits[0]
        # 该行里所有百分比，按出现顺序取**唯一值**。
        # 若有多个，说明行结构变了，判据不该猜——直接报出来。
        pcts = re.findall(r"(\d+\.\d+)%", row)
        if len(pcts) != 1:
            problems.append(
                f"{DOC} 的 {anchor!r} 行里有 {len(pcts)} 个百分比"
                f"（{pcts}），需恰好 1 → 表格结构变了，判据不猜"
            )
            continue
        if pcts[0] != literal.removesuffix("%"):
            problems.append(
                f"NARRATIVE.md 的 {anchor!r} 行写的是 {pcts[0]}%，"
                f"本轮实跑 = {rate:.4%}。文档数字必须现跑现写，不能手抄。"
            )

    return problems


def _mutate() -> int:
    """变异测试：证明上面三条判据真能拦红。

    顺序纪律：**先验基线，再看变异**。
    基线不绿时必须停止，而不是继续报数——
    否则「变异也红」会被当成「拦截成功」，实际上没有区分能力。
    """
    workdir = REPO_ROOT / "data" / "tmp_synth_parity_mut"
    base = run_experiment(n=DEFAULT_N, scan_limit=DEFAULT_SCAN, workdir=workdir)
    ok, problems = judge(base)
    doc_problems = check_doc(base)
    if not ok or doc_problems:
        print("[RED] 变异测试中止：基线本身就不绿，判据无区分能力", file=sys.stderr)
        for p in problems + doc_problems:
            print(f"       {p}", file=sys.stderr)
        return 2
    print(
        f"[GREEN] 基线绿：clean={base['arms']['clean']['flag_rate']:.2%} "
        f"synth={base['arms']['synth']['flag_rate']:.2%} "
        f"dirty={base['arms']['dirty']['flag_rate']:.2%}"
    )
    print("[GREEN] 文档一致性基线绿：NARRATIVE.md 里三个比率与实测逐字一致")

    results: list[bool] = []

    # 变异 1：合成臂涨到远超背景 → 关系 1 必须拦
    m = json.loads(json.dumps(base))
    m["arms"]["synth"]["flag_rate"] = m["arms"]["clean"]["flag_rate"] + 0.20
    results.append(not judge(m)[0])
    print(f"  [{'通过' if results[-1] else '未通过'}] 合成臂误杀飙升 →关系1")

    # 变异 2：注入检出跌到与背景同档 → 关系 2 必须拦
    m = json.loads(json.dumps(base))
    m["arms"]["dirty"]["flag_rate"] = m["arms"]["clean"]["flag_rate"] * 1.2
    results.append(not judge(m)[0])
    print(f"  [{'通过' if results[-1] else '未通过'}] 注入检出≈ 背景 → 关系 2")

    # 变异 3：某臂样本量掉到 30 条 → 关系 3 必须拦
    m = json.loads(json.dumps(base))
    m["arms"]["synth"]["n"] = 30
    results.append(not judge(m)[0])
    print(f"  [{'通过' if results[-1] else '未通过'}] 样本量不足 30 条 → 关系 3")

    # 变异 4：实测比率变了但文档没改 → 文档一致性必须拦
    #   这条是本模块**存在的主要理由**。前三条关系只保证「数字之间自洽」，
    #   而读者看的是文档——实测变了文档没变，说服力就来自那个过期数字。
    #
    #   ⚠️ 这里的判据形态与前三条**不同**，不能照抄：
    #     judge() 返回 bool（ok=True = 通过）→ 取反才是「拦住」
    #     check_doc() 返回**问题列表** → 本身就是「拦住了」
    #   第一版照抄前三条写成 `not check_doc(m)`，于是
    #   「真的拦住了」反而报成「未通过」——**变异测试把有效判据说成失效**。
    #   教训：判据的返回形态也是契约，抄别的判据时要看清它返回什么。
    m = json.loads(json.dumps(base))
    m["arms"]["dirty"]["flag_rate"] = 0.5123
    hit = bool(check_doc(m))
    results.append(hit)
    print(f"  [{'通过' if hit else '未通过'}] 实测漂移但文档未改→ 文档一致性")

    # 变异 5：实测**没变**（就等于基线）→ 文档一致性必须**放行**
    #   正向变异。第一版只有反向变异（证明能拦），没有正向变异（证明不误报）。
    #   一条只会拦红、永远不会绿的判据，和没有判据是一样的。
    hit_green = not check_doc(base)
    results.append(hit_green)
    print(f"  [{'通过' if hit_green else '未通过'}] 实测与文档一致 → 不该误报")

    passed = sum(results)
    print(f"\n变异测试：{passed}/{len(results)} 条被正确拦住")
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="合成样本同口径可比性门禁")
    parser.add_argument(
        "--n", type=int, default=DEFAULT_N, help=f"真实干净样本数（默认 {DEFAULT_N}）"
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=DEFAULT_SCAN,
        help=f"最多读多少行语料（默认 {DEFAULT_SCAN}）",
    )
    parser.add_argument("--mutate", action="store_true", help="变异测试：证明判据还能拦红")
    parser.add_argument("--json-out", default="", help="把报告写到该JSON 路径")
    args = parser.parse_args()

    if args.mutate:
        return _mutate()

    workdir = REPO_ROOT / "data" / "tmp_synth_parity"
    workdir.mkdir(parents=True, exist_ok=True)
    result = run_experiment(n=args.n, scan_limit=args.scan_limit, workdir=workdir)
    ok, problems = judge(result)
    doc_problems = check_doc(result)
    all_problems = problems + doc_problems

    for name in DOC_ARMS:
        arm = result["arms"][name]
        print(
            f"  {name:6s} n={arm['n']:<5d} kept={arm['kept']:<5d} "
            f"被拦={arm['dropped']:<5d} 比率={arm['flag_rate']:>7.2%}  {arm['by_operator']}"
        )

    if not all_problems:
        print("[GREEN] 三条口径关系全部成立：合成样本与真实脏数据同口径可比")
        print("[GREEN] NARRATIVE.md 里三个比率与本轮实测逐字一致")
    else:
        print("[RED] 同口径门禁未通过：", file=sys.stderr)
        for p in all_problems:
            print(f"       {p}", file=sys.stderr)

    if args.json_out:
        out = pathlib.Path(args.json_out)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告已写入 {out}")

    return 0 if not all_problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
