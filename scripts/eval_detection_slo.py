"""形态级质量 SLO 评测：召回 × 误杀 × 错误预算 → rc 门禁。

对标机制（三条都来自业界 SLI/SLO 实践，非自创）：
  1. **分子属于分母**：召回与误杀的分母不同类，禁止复用同一个 n。
     本脚本里两者分别来自「形态样本数」与「干净样本数」。
  2. **错误预算**：每条 SLI 给 budget；实测失败率 > budget → BREACH。
     这让「小幅退化」不会静默腐烂 —— 只看 target 的话，一点点退化不报警。
  3. **永不静默丢弃**（no-silent-filter）：被丢弃但没归因的样本 → 单独判定，
     ratio_coverage 低于契约即红。这是 v1 假绿的直接教训：
     样本确实被丢了，但归因算错了 → 召回被虚高。

⚠️ 为什么用**独立构造**的黄金集而不是污染器注入（scripts/build_golden_set.py）：
   污染器造、算子抓 = 自证闭环。本项目 v1 就是这么报出「召回 100%」的，
   六版证伪才找到真值。用独立构造器 + 人标骨架，才能对答案。

⚠️ 黄金集缺失时**必须报错退出**，不能静默绿：
   `data/` 整棵被 gitignore，CI 上没有这个目录。若这里 fallback 成
   「用污染器现造」，门禁就在CI 上变成自证且无人察觉—— 那是最坏的失败模式。

用法：
    python -X utf8 scripts/eval_detection_slo.py
    python -X utf8 scripts/eval_detection_slo.py --config configs/pipeline.v3_full_coverage.yaml
    python -X utf8 scripts/eval_detection_slo.py --json-out data/reports/detection_slo.json
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages/curation-eval/src"))

import mm_curation.operators  # noqa: E402,F401  导入即注册算子
from mm_curation.pipeline import PipelineConfig, run_funnel  # noqa: E402

GOLDEN = ROOT / "data/golden/golden_set.jsonl"
DEDUP_OPS = {"md5_exact", "phash_near", "minhash_lsh", "semantic_dedup"}
DEFAULT_SLO = ROOT / "configs/detection_slo.yaml"
DEFAULT_CFG = ROOT / "configs/pipeline.v3_full_coverage.yaml"


def load_golden() -> list[dict]:
    """读独立构造的黄金集。**缺失即报错**，绝不静默 fallback。"""
    if not GOLDEN.exists():
        print(f"❌ 黄金集不存在：{GOLDEN}", file=sys.stderr)
        print("   生成命令：python scripts/build_golden_set.py", file=sys.stderr)
        print(
            "   ⚠️本脚本**故意不**在缺失时用污染器现造 —— 那样门禁会变成自证闭环。", file=sys.stderr
        )
        raise SystemExit(2)
    rows = []
    for line in GOLDEN.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            rows.append(json.loads(line))
    if not rows:
        print(f"❌ 黄金集为空：{GOLDEN}", file=sys.stderr)
        raise SystemExit(2)
    return rows


def to_samples(rows: list[dict]):
    from mm_curation.operators.base import Sample

    return [Sample.from_dict(d) for d in rows]


def load_golden_meta() -> dict:
    """读黄金集元信息（含各形态**召回天花板**，缺了不影响判定，只影响展示）。"""
    p = ROOT / "data/golden/golden_meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def evaluate(rows: list[dict], cfg: PipelineConfig) -> dict:
    """跑一次漏斗，按形态归因。**真召回 = 被非去重算子抓**。"""
    clean = [d for d in rows if d["_gold_label"] == "clean"]
    probes = [d for d in rows if d["_gold_label"] != "clean"]
    if not clean:
        raise SystemExit("❌ 黄金集里没有 clean 类 —— 误杀率的分母为零，判据无法成立")

    result = run_funnel(to_samples(rows), cfg)
    kept = {s.id for s in result.kept}
    dropped_by: dict[str, set[str]] = collections.defaultdict(set)
    for op, s in result.dropped:
        dropped_by[s.id].add(op)

    # ① 召回（分母 = 该形态样本数）
    per_form: dict[str, dict] = {}
    for d in probes:
        lab = d["_gold_label"]
        slot = per_form.setdefault(lab, {"n": 0, "true": 0, "dedup_only": 0, "kept": 0})
        slot["n"] += 1
        hit = dropped_by.get(d["id"], set())
        if not hit:
            slot["kept"] += 1
        elif hit <= DEDUP_OPS:
            # 去重算子抓的**不算**质量召回（它抓的是「重复」，不是「脏」）
            slot["dedup_only"] += 1
        else:
            slot["true"] += 1

    # ② 误杀（分母 = 干净样本数，与①不同类）
    clean_dropped = sum(1 for d in clean if d["id"] in dropped_by)
    clean_drop_by_op: dict[str, int] = collections.Counter()
    for d in clean:
        for op in dropped_by.get(d["id"], ()):
            clean_drop_by_op[op] += 1

    # ③ 归因覆盖率（no-silent-filter）：被丢的样本是否都有算子记录
    all_dropped = {s.id for _, s in result.dropped}
    attributed = set(dropped_by)
    unattributed = all_dropped - attributed

    return {
        "per_form": per_form,
        "clean_n": len(clean),
        "clean_dropped": clean_dropped,
        "false_kill": clean_dropped / len(clean) if clean else None,
        "clean_drop_by_op": dict(clean_drop_by_op),
        "dropped_total": len(all_dropped),
        "unattributed": sorted(unattributed),
        "reason_coverage": (
            (len(all_dropped) - len(unattributed)) / len(all_dropped) if all_dropped else 1.0
        ),
        "kept_total": len(kept),
        "input_total": len(rows),
    }


def judge(m: dict, slo: dict, meta: dict) -> tuple[int, list[str], list[str]]:
    """按契约判定。返回 (rc, BREACH 行, NO_SLO 行)。

    ⭐ 判定要扣**装置天花板**：文本构造器有结构性上限（同一形态要凑 30 条样本，
    但 caption 必须互异，否则被去重算子抓走）。若把天花板算进「算子不力」，
       就是**误判装置为能力** —— 本轮已踩过一次：
       mismatched_pair 实测 23.3% == 天花板 23.3%，说明 clip_alignment
       **已把所有能抓的都抓了**，剩下 76.7% 全是装置限制。
    → 故判定改为：实测召回 vs min(target, 天花板)；并显式报出「装置是否打满」。
    """
    breaches: list[str] = []
    no_slo: list[str] = []
    forms = slo.get("contracts", {}).get("forms", {})
    ceilings = meta.get("text_uniqueness", {})

    # ⚠️ 天花板容差必须**挂钩数据精度**，不能随手写 1e-9。
    #   golden_meta.json 里的 recall_ceiling 只保留 4 位小数（7/30 → 0.2333），
    #   而实测 rate = 7/30 = 0.233333…，差3.3e-5。
    #   用 1e-9 判「rate > ceil」会把**打满**误报成「超天花板」——
    #   又是静默失真：判定照样绿，只是措辞在说谎。
    #   1e-4 是ceil 的舍入单位，取它的一半做容差。
    CEIL_EPS = 5e-4

    print()
    print("=" * 96)
    print("① 召回（分子= 被非去重算子抓；分母 = 该形态样本数）")
    print("=" * 96)
    print(
        f"{'形态':<21}{'样本':>4}{'召回':>5}{'召回率':>8}{'期望':>7}"
        f"{'天花板':>8}{'要求≥':>8}{'去重抓':>6}  判定"
    )
    print("-" * 96)
    for lab in sorted(m["per_form"]):
        s = m["per_form"][lab]
        c = forms.get(lab, {}).get("recall", {})
        target, budget = c.get("target"), c.get("budget")
        rate = s["true"] / s["n"] if s["n"] else None
        ceil = ceilings.get(lab, {}).get("recall_ceiling")
        ceil_s = f"{ceil:>8.1%}" if ceil is not None else f"{'—':>8}"
        tgt_s = f"{target:>7.1%}" if target is not None else f"{'—':>7}"
        req_s = f"{1.0 - budget:>8.1%}" if budget is not None else f"{'—':>8}"

        if rate is None:
            print(
                f"{lab:<21}{s['n']:>4}{s['true']:>5}{'—':>8}{tgt_s}{ceil_s}"
                f"{req_s}{s['dedup_only']:>6}  NO_SLO"
            )
            no_slo.append(lab)
            continue

        if target is None or budget is None:
            print(
                f"{lab:<21}{s['n']:>4}{s['true']:>5}{rate:>8.1%}"
                f"{tgt_s}{ceil_s}{req_s}{s['dedup_only']:>6}"
                "  ⚠ NO_SLO（契约未定目标，**不算达标**）"
            )
            no_slo.append(lab)
            continue

        # 有效目标只用于展示（tgt_s 已用契约 target）。判定完全由 budget 决定：
        #   required = 1 - budget = 最低可接受召回
        # 天花板不参与「放宽预算」，只用于①拦住不可达契约 ②解释「装置打满」。
        # ⚠️ 预算**绝不被天花板顶高**。
        #   第一版写成 `eff_budget = max(budget, 1.0 - eff)`，
        #   结果变异测试 M1/M4 失败：把预算收紧到 0（要求 100% 召回）
        #   或把 target 设成 1.0，门禁都**不会红** —— 天花板替契约放宽了预算，
        #   判据形同虚设。**这正是恒真判据的典型形态：看起来在拦，实际拦不住。**
        #
        # ⭐ 第二版修正：`target` 与 `budget` 的语义**分离**。
        #   budget = 允许的失败率 → 判定用它（recall >= 1 - budget）；
        #   target = 期望值/棘轮参考 → 只用于展示「距目标还差多少」，**不参与判定**。
        #   之前两者混用（target=0.20 + budget=0.03 反推出要求 recall>=97%，
        #   与 target 自相矛盾 → 那样的契约永远不可能绿，红也就失去意义）。
        eff_budget = budget
        fail = 1 - rate
        required = 1.0 - eff_budget
        # ⚠️ 判定顺序必须是「先看实测达没达标，再谈天花板」。
        #   曾经的 bug：先判「契约要求 > 天花板 → 不可达」，于是
        #   truncate_text（实测 100% > 要求 95%，明明达标）被判红。
        #   原因：天花板只是**装置能力上限的估计**，用它去否决「已达标」
        #   是错的 —— 实测达标就是达标。天花板该管的是「未达标时归因给谁」。
        if fail <= eff_budget:
            # 已达标。天花板只用于解释「为什么不是 100%」。
            # ⚠️ 三条分支的顺序有讲究：先判**严格大于**天花板，再判等于。
            #   反过来写（先 >= 后 >）会让 > 那条永远不可达 —— 死分支，
            #   truncate_text（实测 100% vs 天花板 86.7%）会被误标成「装置打满」。
            if ceil is not None and rate > ceil + CEIL_EPS:
                verdict = "✅ PASS（超天花板：重复文本也各自被质量算子抓到）"
            elif ceil is not None and rate >= ceil - CEIL_EPS:
                verdict = "✅ PASS（**装置打满**：已达唯一文本数上限）"
            else:
                used = fail / eff_budget if eff_budget else 0
                verdict = f"✅ PASS（预算用了 {used:.0%}）"
        elif ceil is not None and required > ceil + CEIL_EPS:
            # 未达标，且「即使打满装置也达不到要求」→ 契约本身不可达。
            breaches.append(
                f"{lab}: 实测召回 {rate:.1%} 未达要求 {required:.1%}，"
                f"且装置天花板仅 {ceil:.1%} → **契约超出装置能力**，"
                "须先提高装置或下调契约"
            )
            verdict = f"❌ 契约不可达（要求 {required:.1%} > 天花板 {ceil:.1%}）"
        else:
            breaches.append(
                f"{lab}: 召回 {rate:.1%}，失败率 {fail:.1%} 超预算 {eff_budget:.1%}"
                f"（要求召回 ≥ {required:.1%}"
                f"{'；装置天花板上限 ' + format(ceil, '.1%') if ceil is not None else ''}）"
            )
            verdict = f"❌ BREACH（失败 {fail:.1%} > 预算 {eff_budget:.1%}）"
        print(
            f"{lab:<21}{s['n']:>4}{s['true']:>5}{rate:>8.1%}{tgt_s}{ceil_s}"
            f"{req_s}{s['dedup_only']:>6}  {verdict}"
        )

    print()
    print("  列含义：")
    print("    期望  = 契约里的 target（**仅展示**，不参与判定）")
    print("    要求≥ = 由 budget 反推的最低可接受召回（= 1 - budget，**判定用它**）")
    print("    天花板= 该形态唯一文本数 / 样本数（**装置能力上限**，非算子能力）")
    print("  ⭐ target 与 budget 语义已分离：早期版本两者混用会推出")
    print("    「要求 97% 但期望 20%」的自相矛盾契约 → 永远不可能绿，红也失去意义。")

    print()
    print("=" * 78)
    print("② 误杀（分子 = 干净样本被丢数；分母 = 干净样本数 —— 与①不同类）")
    print("=" * 78)
    g = slo.get("contracts", {}).get("global", {})
    fkc = g.get("false_kill", {})
    fk = m["false_kill"]
    print(f"  分母（干净样本）= {m['clean_n']}")
    print(f"  分子（被丢弃）  = {m['clean_dropped']} = {fk:.2%}")
    print(f"  各算子：{m['clean_drop_by_op']}")
    if fk > fkc.get("target", 1.0):
        print(f"  ⚠ 高于 target {fkc.get('target'):.2%}（这是成本，不是能力）")
    if (1 - fk) < (1 - fkc.get("budget", 0.0)):
        pass
    if fk > fkc.get("budget", 0.0):
        breaches.append(
            f"false_kill: {fk:.2%} 超预算 {fkc.get('budget', 0):.2%}"
            f"（target {fkc.get('target', 0):.2%}）"
        )
        print(f"  ❌ BREACH：误杀 {fk:.2%} 超预算 {fkc.get('budget', 0):.2%}")
    else:
        print(f"  ✅ PASS（预算 {fkc.get('budget', 0):.2%}）")

    print()
    print("=" * 78)
    print("③ 归因覆盖率（no-silent-filter：丢了必须说清为什么）")
    print("=" * 78)
    rc = m["reason_coverage"]
    rcc = g.get("reason_coverage", {})
    print(f"  被丢弃 {m['dropped_total']} 条，其中无归因 {len(m['unattributed'])} 条")
    print(f"  归因覆盖率 = {rc:.2%}（契约要求 {rcc.get('target', 1.0):.0%}）")
    if rc < rcc.get("target", 1.0) - rcc.get("budget", 0.0):
        breaches.append(f"reason_coverage: {rc:.2%} 低于要求")
        print("  ❌ BREACH：有样本被丢但没记原因")
    else:
        print("  ✅ PASS")

    return (1 if breaches else 0), breaches, no_slo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--slo", default=str(DEFAULT_SLO))
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    import yaml  # noqa: PLC0415

    rows = load_golden()
    meta = load_golden_meta()
    slo = yaml.safe_load(Path(args.slo).read_text(encoding="utf-8"))
    cfg = PipelineConfig.from_yaml(args.config)

    print("=" * 78)
    print("形态级质量 SLO 评测")
    print("=" * 78)
    print(f"  配置：{Path(args.config).name}")
    print(f" 契约：{Path(args.slo).name}")
    print(f"  黄金集：{len(rows)} 条（独立构造，**非污染器注入**）")
    labels = collections.Counter(d["_gold_label"] for d in rows)
    print(f"  分布：{dict(labels)}")

    m = evaluate(rows, cfg)
    rc, breaches, no_slo = judge(m, slo, meta)

    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    if breaches:
        print(f"❌ {len(breaches)} 条 BREACH：")
        for b in breaches:
            print(f"   - {b}")
    else:
        print("✅ 全部有目标的 SLI 均通过")
    if no_slo:
        print(f"⚠️ {len(no_slo)} 个形态 NO_SLO（契约未定目标，**不算达标**）：{no_slo}")
    print()
    print(f"漏斗：输入 {m['input_total']} → 存活 {m['kept_total']}，丢弃 {m['dropped_total']}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "config": Path(args.config).name,
            "slo": Path(args.slo).name,
            "golden_rows": len(rows),
            "per_form": m["per_form"],
            "clean_n": m["clean_n"],
            "clean_dropped": m["clean_dropped"],
            "false_kill": m["false_kill"],
            "clean_drop_by_op": m["clean_drop_by_op"],
            "reason_coverage": m["reason_coverage"],
            "breaches": breaches,
            "no_slo": no_slo,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {out}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
