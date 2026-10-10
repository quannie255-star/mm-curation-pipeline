"""数据混比（mixing）实验：测「多源按什么比例混，下游训练效用最好」。

为什么需要它
------------
`docs/GAP_ANALYSIS.md` 的 G4项。单源实验只能回答「清洗有没有用」，
回答不了「源怎么配」—— 而后者是 DataComp-LM 等SOTA 数据管线的核心决策面，
且那个工作流给出一个反直觉结论：**混入高质量源不一定有帮助，可能反噬**。
没实测过就照搬别人的比例 = 无根据。

⚠️⚠️ 三条口径纪律（违反其一结论即无效，脚本内已用assert 兜住）
----------------------------------------------------------------------
1. **长度必须对齐，否则混的不是源而是长度。**
   实测三源长度中位数 **158 / 1423 / 160**（chinanews 是 wikipedia 的 9 倍）。
   直接按条数混 → 混比实验同时变了「来源」与「单条长度」两个变量，
   结论无法归因。所以每条按**目标长度窗口**采样，三源共用同一窗口。

2. **等量、等步数、等 held-out**。唯一变量 = 源比例。
   每臂总条数固定为 `--n-total`（默认 2400），按比例分配到各源。

3. **必须报噪声地板**。不测「同数据同比例不同 seed」的波动，
   就无法判断 Δ 是真效应还是训练随机性（同`eval_training_utility.py` 的纪律）。

臂设计
------
| 臂 | 组成 |
|---|---|
| `mono_wiki` | 100% wikipedia（单源基线） |
| `mono_news` | 100% chinanews |
| `mix_50_25_25` | 50% wiki / 25% news / 25% finance |
| `mix_80_10_10` | 80% wiki / 10% news / 10% finance |
| `mix_33_33_33` | 三源均分 |
| `noise_<臂>` | 同一比例、不同 seed（噪声地板） |

**不设「哪个比例最优」的目标值** —— 那是结论，不是前提。
门禁只判「混比相对最优单源是否更好」这个方向，
以及「Δ 是否超过噪声地板」这个显著性。

用法
----
    python -X utf8 scripts/eval_source_mixing.py --arms all
    python -X utf8 scripts/eval_source_mixing.py --arms base-only# 只测采样与配比，不训练
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import pathlib
import random
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "packages/curation-eval/src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 丢弃率离群判据：某臂丢弃率超过「各臂丢弃率中位 × 本值」即视为有混淆变量。
# ⚠️ 显式提成常量（而不是内联魔法数）—— 变异测试要能把它打到。
# 1.8 的来历：本批中位 11% × 1.8 = 19.8%，把 22.5% 的 finance 正确排除、
# 而 11% 的四个臂全部保留。改成 2.0 就会把 finance 放回「可比」——
# 这就是它必须被变异测试盯着的原因。
DROP_TOL = 1.8

# 显著性阈值：|Δppl| 至少要达到噪声地板的这个倍数才算显著。
# ⚠️ 显式提成常量 —— 变异测试要能把它打到。
# 5 倍的来历：噪声地板是「同臂不同 seed」的差异，5 倍意味着
# 「换随机种子不可能解释这个差」。偏保守，宁可少报显著。
SIG_RATIO = 5.0

log = logging.getLogger("mixing")

#: 三个来源。`name` 用于报告；`path` 指语料；`len_window` 留空=用全局窗口。
SOURCES: dict[str, dict[str, Any]] = {
    "wiki": {"path": ROOT / "data/raw/text_corpus.jsonl"},
    "news": {"path": ROOT / "data/raw/news_corpus.jsonl"},
    "finance": {"path": ROOT / "data/raw/finance_news/news_corpus.jsonl"},
}

#: 各臂的源配比（权重和为1即可，脚本归一化）。
ARMS: dict[str, dict[str, float]] = {
    "mono_wiki": {"wiki": 1.0},
    "mono_news": {"news": 1.0},
    "mono_finance": {"finance": 1.0},
    "mix_50_25_25": {"wiki": 0.50, "news": 0.25, "finance": 0.25},
    "mix_80_10_10": {"wiki": 0.80, "news": 0.10, "finance": 0.10},
    "mix_33_33_33": {"wiki": 1 / 3, "news": 1 / 3, "finance": 1 / 3},
}

#: 全局长度窗口（字符数）。所有源共用 —— 这是纪律 1 的落地。
LEN_LO = 60
#: ⚠️ 上界**不能拍脑袋**：实测各源长度中位 158/ 1423 / 160，
#: `[60,400]` 会把 chinanews 砍到只剩 **252** 条 → `mono_news` 臂
#: 因「凑不够 60%」而不可比。这不是 bug，是**该报的约束**：
#: 本项目**没有**足量的中文长文本源，这是 G4 的真实数据边界。
LEN_HI = 1200


def load_source(name: str) -> list[dict]:
    path = SOURCES[name]["path"]
    if not path.exists():
        raise SystemExit(f"❌ 语料不存在: {path}")
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("text"):
                d["_source"] = name
                rows.append(d)
    log.info("源 %-8s 读入 %d 条（%s）", name, len(rows), path.name)
    return rows


def build_pool(rng: random.Random, n_per_source_cap: int) -> dict[str, list[dict]]:
    """读三源并**按同一长度窗口**过滤 —— 纪律 1。"""
    pool: dict[str, list[dict]] = {}
    for name in SOURCES:
        rows = load_source(name)
        kept = [r for r in rows if LEN_LO <= len(r["text"]) <= LEN_HI]
        log.info(
            "  %-8s 长度窗口 [%d,%d] 保留 %d/%d = %.1f%%",
            name, LEN_LO, LEN_HI, len(kept), len(rows), 100 * len(kept) / max(len(rows), 1),
        )
        if not kept:
            raise SystemExit(
                f"❌ 源 {name} 在长度窗口 [{LEN_LO},{LEN_HI}] 内**一条都没有** —— "
                "换窗口，别拿空集跑（会得到恒真结论）"
            )
        rng.shuffle(kept)
        pool[name] = kept[:n_per_source_cap]
    return pool


def global_feasible(
    pool: dict[str, list[dict]],
    ratios: dict[str, float],
    n_total: int,
) -> int:
    """在**所有臂**里取共同的可行上限（纪律 2 的落地）。

    ⚠️ 必须**全局统一**，不能逐臂各算各的：
    逐臂缩放会让「最紧的臂」压成它自己的上限、而「宽松的臂」保持满量
    → 各臂总量不同 → 混比实验同时变了「源比例」与「数据量」，
    结论**无法归因**（与 G1 那个不等量坑是同一类错误）。

    实测：`finance` 源在长度窗口内只有 **214** 条，而 `mix_50_25_25`
    要给它 25% → 全局上限约 850，于是所有臂统一按 850 跑。
    """
    total_w = sum(ratios.values())
    feasible = n_total
    for name, w in ratios.items():
        avail = len(pool.get(name, []))
        need = w / total_w
        if need > 0 and avail / need < feasible:
            feasible = int(avail / need)
    if feasible < n_total:
        log.warning(
            "⚠️ 数据最紧的源把**所有臂**统一压到 %d/%d 条（%.0f%%）—— 各臂等量，结论可归因",
            feasible, n_total, 100 * feasible / n_total,
        )
    if feasible < n_total * 0.3:
        raise SystemExit(
            f"❌ 全局上限只有 {feasible}/{n_total} 条（{feasible/n_total:.0%}），实验无意义。\n"
            "   可选处置：减小 --n-total、放宽 LEN_HI、或补数据源。\n"
            "   ⚠️ 本项目**没有**足量的第二/第三中文源，这是真实数据边界，不是配置错误。"
        )
    return feasible


def assemble_arm(
    pool: dict[str, list[dict]],
    ratios: dict[str, float],
    n_total: int,
    seed: int,
) -> list[dict]:
    """按配比组装一臂的样本池（纪律 2 的落地：各臂共用同一可行上限）。"""
    total_w = sum(ratios.values())
    if total_w <= 0:
        raise SystemExit("❌ 配比权重和必须为正")

    feasible = global_feasible(pool, ratios, n_total)
    out: list[dict] = []
    for name, w in ratios.items():
        want = int(round(feasible * w / total_w))
        sub = list(pool.get(name, [])[:want])
        rng = random.Random(seed + hash(name) % 10000)
        rng.shuffle(sub)
        out.extend(sub)
    rng = random.Random(seed)
    rng.shuffle(out)
    return out


def arm_stats(rows: list[dict], ratios: dict[str, float]) -> dict:
    """如实记录实际配比 —— **请求配比 ≠ 实际配比**（数据不足时会偏）。"""
    from collections import Counter  # noqa: PLC0415

    cnt = Counter(r["_source"] for r in rows)
    lens = sorted(len(r["text"]) for r in rows)
    return {
        "requested_ratios": {k: round(v, 4) for k, v in ratios.items()},
        "actual_counts": dict(cnt),
        "actual_ratios": {k: round(cnt.get(k, 0) / max(len(rows), 1), 4) for k in cnt},
        "n_total": len(rows),
        "len_p10": lens[len(lens) // 10],
        "len_p50": lens[len(lens) // 2],
        "len_p90": lens[9 * len(lens) // 10],
    }


def run_funnel(pool: list[dict], config_path: pathlib.Path) -> tuple[list[dict], dict]:
    """过**与生产同一条**漏斗，不另写近似逻辑。"""
    from mm_curation.operators.base import Sample  # noqa: PLC0415
    from mm_curation.pipeline import PipelineConfig  # noqa: PLC0415
    from mm_curation.pipeline import run_funnel as _run  # noqa: PLC0415

    if not config_path.exists():
        raise SystemExit(f"❌ 配置不存在: {config_path}")
    cfg = PipelineConfig.from_yaml(config_path)

    rows_for_sample = [
        {"id": str(r["id"]), "text": r["text"], "modality": "text_article"}
        for r in pool
    ]
    samples = [Sample.from_dict(d) for d in rows_for_sample]
    result = _run(samples, cfg)

    kept_ids = {s.id for s in result.kept}
    id2row = {str(r["id"]): r for r in pool}
    # ⚠️ 必须按 **id** 反查，不按 text —— 两篇正文相同会被误判成「都存活」
    # （本项目已在 `eval_training_utility.py` 上踩过同样的坑）。
    kept = [id2row[i] for i in kept_ids if i in id2row]
    return kept, {
        "config": str(config_path.relative_to(ROOT)),
        "n_in": len(samples),
        "n_kept": len(result.kept),
        "drop_rate": 1 - len(result.kept) / max(len(samples), 1),
    }


def write_report(out_path: pathlib.Path, payload: dict) -> None:
    """写出报告。`default=str` 保证**任何**意外类型都不会让训练白跑。

    ⚠️ 这不是防御性编程洁癖，是实测踩出来的：
    `payload["protocol"]["base_model"] = ensure_local_gpt2()`（WindowsPath）
    → 训练全部跑完 → `json.dumps` 在**最后一行**抛 TypeError →
    7 臂、约 1 小时的 GPU 训练结果**一个数字都没留下**。
    这种失败点离原因极远，所以写出器必须自带兜底。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log.info("已写 %s", out_path)


def comparable_arms(payload: dict, tol: float = 1.6) -> tuple[list[str], list[str]]:
    """把臂分成「可归因」与「有混淆变量」两组。

    ## 为什么必须自动分，而不是手工指定

    上一版报告是我手写的，把四个臂标成「✅ 可比」。但那个判断的依据是
    **当时那批数据的长度中位恰好接近**。换一批数据（比如窗口放宽到 1200、
    或者某个源的长度分布变了），「可比」就悄悄不成立了，
    而手写的报告会**继续声称可比** —— 一份不会腐烂的报告就是假绿。

    所以判据必须是**现算的**：长度中位落在同一倍数带内 + 丢弃率不离群。
    两个条件都从 payload 现取，阈值显式写成参数（便于变异测试）。

    ⚠️ 判据方向：**宁可少判可比，不可多判**。把有混淆的臂误算进「干净子集」
    会让归因失效；反过来只是少报一个结论方向。

    ## ⚠️ 为什么用**众数聚类**而不是「中位数 ± 带子」

    第一版用「长度中位 ± tol 倍」。本项目栽在这上面两次：
    1. 绝对带宽（丢弃率 ±0.15）在中位被离群值拉高时**带子跟着变宽**，
       于是「离群值自己把判据放松了」。
    2. **中位数在「多数同时离群」时完全失效** —— 实测把 4 个臂的长度
       全设成 9999，剩下的中位就变成 9999，于是 9999 们**互相可比**，
       而真正的正常臂反倒成了离群。

    所以改成：先按长度把臂**分成簇**（相对最小值聚类），
    取**最大的簇**作为「主流」，只有主流内的臂才判可比。
    「多数同时离群」时最大簇仍然是离群群，但**簇内互比**，
    而正常臂会单独成簇 → 被排除。这在两种失效模式下都成立。
    """
    arms = payload.get("arms", {})
    ppl = payload.get("ppl", {})
    funnel = payload.get("funnel", {})
    rows = []
    for name, a in arms.items():
        if name not in ppl:
            continue
        rows.append(
            {
                "name": name,
                "ppl": float(ppl[name]),
                "len_p50": int(a.get("len_p50", 0)),
                "drop": float(funnel.get(name, {}).get("drop_rate", 0.0)),
                "ratios": a.get("actual_ratios", {}),
            }
        )
    rows = [r for r in rows if r["len_p50"] > 0]
    if not rows:
        return [], []

    # ── 长度分簇：按「与最小值的比值」聚类（比值比差值稳定，不受量级影响）──
    base_len = min(r["len_p50"] for r in rows)
    ratios = sorted((r["len_p50"] / base_len, r["name"]) for r in rows)
    clusters: list[list[tuple[float, str]]] = [[ratios[0]]]
    for ratio, name in ratios[1:]:
        if ratio <= clusters[-1][0][0] * tol:
            clusters[-1].append((ratio, name))
        else:
            clusters.append([(ratio, name)])
    # 最大簇 = 主流（并列时取更小的那簇：宁可认为「主流是最短的那些」，
    # 因为「长度异常」在本实验里始终是混淆变量的方向）
    clusters.sort(key=lambda c: (len(c), -c[0][0]), reverse=True)
    main_cluster = clusters[0]
    main_names = {name for _, name in main_cluster}
    # 丢弃率也用**簇内相对判据**，且基准取**簇内最小值**而不是最大值。
    # ⚠️ 取最大值时判据恒真：离群臂自己就是 max，
    # 于是「离群 ≤ 离群 × 倍数」永远成立 —— 离群值把自己判成了可比。
    # （与长度判据那个「中位数被拉高」是同一个病，只是换了形式复发。）
    main_drops = [r["drop"] for r in rows if r["name"] in main_names]
    drop_base = min(main_drops) if main_drops else 0.0

    good, bad = [], []
    for r in rows:
        drop_ok = (
            r["drop"] <= drop_base * DROP_TOL if drop_base > 0 else r["drop"] <= 0.0
        )
        (good if (r["name"] in main_names and drop_ok) else bad).append(r["name"])
    return good, bad


def render_markdown(payload: dict) -> str:
    """由 payload 现算出人读版 md（含干净子集判定）。"""
    proto = payload.get("protocol", {})
    ppl = payload.get("ppl", {})
    concl = payload.get("conclusion", {})
    arms = payload.get("arms", {})
    funnel = payload.get("funnel", {})
    good, bad = comparable_arms(payload)
    fz = float(concl.get("noise_floor", 0.0))

    L: list[str] = []
    L.append("# 数据混比实验（G4）\n")
    L.append(
        f"**每臂** {proto.get('n_total_per_arm')} 条 · **步数** {proto.get('steps')}"
        f" · **seed** {proto.get('seed')} · **held-out** {proto.get('n_test')}"
        f" · **长度窗口** {proto.get('len_window')}\n"
    )

    # ── 总判定 ──
    L.append("## 脚本的总判定\n")
    L.append(f"> {concl.get('verdict')}\n")
    L.append(
        f"它比较的是**最优单源 vs 最优混比**"
        f"（{concl.get('best_single')} vs {concl.get('best_mix')}，"
        f"Δ={concl.get('delta_best_single_minus_best_mix'):+.4f}，噪声地板 {fz:.5f}）。\n"
    )

    # ── 全部结果 ──
    L.append("## 全部臂\n")
    L.append("| 臂 | ppl | 长度中位 | 丢弃率 | 归因 |")
    L.append("|---|---|---|---|---|")
    for name in ppl:
        if name == "base":
            continue
        a = arms.get(name, {})
        fr = float(funnel.get(name, {}).get("drop_rate", 0.0))
        tag = "✅ 可比" if name in good else "⚠️ 有混淆"
        L.append(
            f"| {name} | {ppl[name]:.4f} | {a.get('len_p50', '—')} | "
            f"{fr * 100:.1f}% | {tag} |"
        )
    if "base" in ppl:
        L.append(f"| base（未训练，参照） | {ppl['base']:.4f} | — | — | — |")
    L.append("")

    # ── 干净子集内的显著性 ──
    L.append("## ✅ 干净子集内的逐段显著性\n")
    if len(good) < 2:
        L.append("可比臂不足 2 个，本轮**无法在子集内做显著性比较**。\n")
    else:
        # ⚠️ 两个坑（第一版都踩了）：
        # ① 顺序不能用 `good` 的 dict 顺序 —— 那是**入库顺序**不是**梯度顺序**，
        #    逐段比较会跨族（mono_wiki → mix_50_25_25 跳过了 80% 那档）。
        #    必须按 wiki 占比**排序**，比较才落在同一条梯度上。
        # ② 判定必须看**带符号的Δ**。第一版用 `abs(d)` 定"变好/变差"，
        #    于是 Δ=−1.13（ppl 下降= 变好）被标成"显著变差"。
        #    `abs` 只用来看「是否超过噪声地板」，方向要另判。
        ordered = sorted(
            good,
            key=lambda n: -float(arms.get(n, {}).get("actual_ratios", {}).get("wiki", 0.0)),
        )
        L.append("按 wiki 占比从高到低排列（唯一变量 = wiki 比例）：\n")
        L.append("| 区间 | Δppl | 倍噪声 | 判定 |")
        L.append("|---|---|---|---|")
        for x, y in zip(ordered, ordered[1:]):
            d = ppl[y] - ppl[x]
            ratio = abs(d) / fz if fz else float("inf")
            if ratio < SIG_RATIO:
                verdict = "测不出"
            elif d > 0:
                verdict = "**显著变差**（ppl 升高）"
            else:
                verdict = "显著变好（ppl 降低）"
            L.append(f"| {x} → {y} | {d:+.4f} | {ratio:.2f}× | {verdict} |")
        L.append("")
        L.append(
            f"> 判据：|Δppl| ≥ **{SIG_RATIO:g} 倍**噪声地板才算显著；方向由 Δ 的**符号**决定\n"
            f"> （ppl 升高 = 训练效果变差）。{SIG_RATIO:g} 偏保守 —— 宁可少报显著。"
        )

    # ── 混淆变量 ──
    if bad:
        L.append("## ⚠️ 有混淆变量的臂（**不能**单独用来论证「该源质量差」）\n")
        for name in bad:
            a = arms.get(name, {})
            fr = float(funnel.get(name, {}).get("drop_rate", 0.0))
            L.append(
                f"- `{name}`：长度中位 {a.get('len_p50', '—')}、丢弃率 {fr * 100:.1f}%，"
                f"ppl {ppl[name]:.4f} —— 这个数里混着非源质量的因素。"
            )
        L.append("")

    L.append("## 口径纪律（代码强制）\n")
    L.append(f"- **长度窗口对齐**：{proto.get('discipline', '')}")
    L.append(f"- **等量必须全局统一算**：`min(global_feasible(所有臂))` = "
             f"{proto.get('feasible_per_arm')}；逐臂算会引入量变量。")
    L.append("")
    L.append("## 复跑\n")
    L.append("```bash\npython -X utf8 scripts/eval_source_mixing.py "
             "--n-total 200 --n-test 150 --steps 500 --noise-probe\n```")
    return "\n".join(L) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arms", choices=("all", "base-only"), default="all")
    parser.add_argument("--n-total", type=int, default=2400, help="每臂总条数（三源按配比分）")
    parser.add_argument("--n-test", type=int, default=400)
    parser.add_argument("--steps", type=int, default=500)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--seq-len", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--noise-probe",
        action="store_true",
        help="给最优臂再跑一个不同 seed 的同配比臂，实测噪声地板",
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "configs/text_funnel.yaml"),
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "data/reports/source_mixing.json"),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    rng = random.Random(args.seed)
    pool = build_pool(rng, n_per_source_cap=max(args.n_total, 1000))

    # ⚠️ `base-only` 也要组装**所有臂**：预检的意义正是发现
    # 「某一臂因数据不足而悄悄凑出不同配比」。只跑第一臂等于没预检。
    arms = list(ARMS.items())
    built: dict[str, dict] = {}
    for name, ratios in arms:
        rows = assemble_arm(pool, ratios, args.n_total, args.seed)
        stats = arm_stats(rows, ratios)
        built[name] = {"rows": rows, "stats": stats}
        log.info(
            "臂 %-14s n=%d 实际配比=%s 长度中位=%d",
            name, stats["n_total"], stats["actual_ratios"], stats["len_p50"],
        )

    # 各臂总量必须一致 —— 混比实验里量不一致 = 结论无效。
    # ⚠️ 上限必须取**所有臂里最紧的那个**：只按第一个臂算的话，
    #   mono_wiki（不含 finance）不会触发约束，等到 mix_* 才崩 ——
    #   那是「用单个样本推断总体」的经典错误。
    feasible_all = min(
        global_feasible(pool, ratios, args.n_total) for _name, ratios in arms
    )
    totals = {k: v["stats"]["n_total"] for k, v in built.items()}
    spread = max(totals.values()) - min(totals.values())
    if spread > max(2, int(0.02 * min(totals.values()))):
        raise SystemExit(
            f"❌ 各臂总量不一致（{totals}）—— 混进了数据量变量，结论无效。\n"
            "   这是逐臂缩放的经典错误：必须先算全局可行上限（global_feasible）。"
        )

    payload: dict[str, Any] = {
        "protocol": {
            "n_total_per_arm": args.n_total,
            "feasible_per_arm": feasible_all,
            "n_test": args.n_test,
            "len_window": [LEN_LO, LEN_HI],
            "seed": args.seed,
            "steps": args.steps,
            "base_model": "sshleifer/tiny-gpt2" if args.arms != "all" else None,
            "discipline": (
                "**长度窗口对齐**：三源长度中位 158/1423/160，"
                "不按同一窗口采样则「源比例」与「单条长度」两个变量混在一起，结论无法归因。"
                "**等量等步数**：唯一变量 = 源比例。"
            ),
        },
        "arms": {k: v["stats"] for k, v in built.items()},
        "funnel": {},
        "ppl": {},
        "conclusion": {},
    }

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.arms == "base-only":
        for name in built:
            kept, fstats = run_funnel(built[name]["rows"], pathlib.Path(args.config))
            payload["funnel"][name] = fstats
            payload["ppl"][name] = None
        payload["conclusion"] = {
            "mode": "base-only",
            "note": "只验证采样与漏斗，未训练。",
        }
        write_report(out_path, payload)
        return 0

    # ---- 训练 ----
    import importlib.util  # noqa: PLC0415

    import torch  # noqa: PLC0415
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: PLC0415

    # `scripts` 不是包（没有 __init__.py），必须按**路径**加载。
    # ⚠️ 用同一份 train/evaluate 实现 —— 另写一套近似逻辑会让两个实验
    # 的协议悄悄分叉，「两个结论一致」就失去意义（A3 决策的前提）。
    _tu_path = ROOT / "scripts/eval_training_utility.py"
    _spec = importlib.util.spec_from_file_location("_tu", _tu_path)
    if _spec is None or _spec.loader is None:
        raise SystemExit(f"❌ 加载 {_tu_path} 失败")
    _tu = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tu)
    evaluate, train = _tu.evaluate, _tu.train

    # ⚠️ 必须用**本地**权重：`sshleifer/tiny-gpt2` 需要联网，
    # 而 `eval_training_utility.py` 用的是 `ensure_local_gpt2()`。
    # 两个实验必须**同一基座**，否则「两臂一致才算证据」不成立。
    from mm_curation.gpt2_weights import ensure_local_gpt2  # noqa: PLC0415

    model_dir = ensure_local_gpt2()
    # ⚠️ 必须存 **str**：`ensure_local_gpt2()` 返回 WindowsPath，
    # 直接塞进 payload 会让训练跑完后 `json.dumps` 抛
    # 「Object of type WindowsPath is not JSON serializable」——
    # **7 臂、约 1 小时的 GPU 训练全部白跑**，且只在最后一行炸，看不出原因。
    payload["protocol"]["base_model"] = str(model_dir)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(model_dir)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    base = AutoModelForCausalLM.from_pretrained(model_dir).to(device)
    log.info("基座就绪（device=%s）", device)

    # held-out：从 wiki 抽，**不参与任何臂**
    test_rows = pool["wiki"][: args.n_test]
    held = [r["text"] for r in test_rows]
    log.info("held-out %d 条（来自 wiki，不参与任何臂的构造）", len(held))

    ppl: dict[str, float] = {
        "base": evaluate(base, tok, held, 32, args.seq_len, device)
    }
    log.info("base ppl=%.4f", ppl["base"])

    for name, spec in built.items():
        kept, fstats = run_funnel(spec["rows"], pathlib.Path(args.config))
        payload["funnel"][name] = fstats
        texts = [r["text"] for r in kept]
        model = copy.deepcopy(base)
        train(model, tok, texts, args.steps, args.batch, args.seq_len, args.lr, device,
              seed=args.seed + 100)
        ppl[name] = evaluate(model, tok, held, 32, args.seq_len, device)
        log.info("%-14s ppl=%.4f（过漏斗后 %d/%d 条）", name, ppl[name], len(kept), len(texts))
        del model
        torch.cuda.empty_cache()

    if args.noise_probe:
        best = min((k for k in ppl if k != "base"), key=lambda k: ppl[k])
        rows = assemble_arm(pool, ARMS[best], args.n_total, args.seed + 7777)
        kept, _ = run_funnel(rows, pathlib.Path(args.config))
        model = copy.deepcopy(base)
        train(model, tok, [r["text"] for r in kept], args.steps, args.batch, args.seq_len,
              args.lr, device, seed=args.seed + 8888)
        ppl[f"noise_{best}"] = evaluate(model, tok, held, 32, args.seq_len, device)
        log.info("噪声地板 (%s, 不同 seed) ppl=%.4f", best, ppl[f"noise_{best}"])
        del model
        torch.cuda.empty_cache()
        payload["protocol"]["noise_probe_arm"] = best

    payload["ppl"] = ppl

    # ---- 结论：只判方向与显著性，不预设「哪个比例最优」 ----
    # ⚠️ **先把 ppl 落盘，再算结论**。
    # 上一次7 臂训练跑完，却在最后一行 `json.dumps` 抛 TypeError
    # （WindowsPath 不可序列化）→ 一个数字都没留下。
    # 结论段有计算（min/差值/verdict 字符串），它崩不该带走 ppl。
    payload["conclusion"] = {"status": "未计算（ppl 已保底落盘）"}
    write_report(out_path, payload)

    singles = {k: v for k, v in ppl.items() if k.startswith("mono_")}
    mixes = {k: v for k, v in ppl.items() if k.startswith("mix_")}
    best_single = min(singles, key=lambda k: singles[k]) if singles else None
    best_mix = min(mixes, key=lambda k: mixes[k]) if mixes else None
    noise_keys = [k for k in ppl if k.startswith("noise_")]
    noise = None
    if noise_keys:
        nk = noise_keys[0]
        noise = abs(ppl[nk] - ppl[nk.removeprefix("noise_")])

    concl: dict[str, Any] = {
        "best_single": best_single,
        "best_single_ppl": ppl.get(best_single) if best_single else None,
        "best_mix": best_mix,
        "best_mix_ppl": ppl.get(best_mix) if best_mix else None,
        "noise_floor": noise,
    }
    if best_single and best_mix:
        delta = ppl[best_single] - ppl[best_mix]
        concl["delta_best_single_minus_best_mix"] = delta
        if noise is None:
            concl["verdict"] = "INCONCLUSIVE（无噪声地板，无法判断 Δ 是否为真效应）"
        elif delta > noise:
            concl["verdict"] = "混合优于单源，且 Δ 超过噪声地板"
        elif delta < -noise:
            concl["verdict"] = "单源优于混合 —— 与『混高质量源总是有帮助』相反"
        else:
            concl["verdict"] = "混合与单源在噪声内无差别"
    payload["conclusion"] = concl

    write_report(out_path, payload)
    # 人读版 md：与 JSON **同一次运行**产出，不手工维护。
    # ⚠️ 上一次跑批 7 臂 GPU（约 50 分钟）后 `_write()` 崩在最后一行，
    # JSON 也没留下 → 必须「先落 JSON、再算结论、最后写 md」，
    # 且md 失败**不许带走 JSON**（已经先写好了）。
    md_path = out_path.with_suffix(".md")
    try:
        md_path.write_text(render_markdown(payload), encoding="utf-8")
        log.info("已写 %s", md_path)
    except Exception:  # noqa: BLE001
        # md 是人读版，写不出来不该让整个跑批以失败告终——
        # JSON 已经保底落盘，数字没丢。必须显式告知，别静默。
        log.exception("⚠️ md 报告写出失败（JSON 已落盘，数字没丢）：%s", md_path)
    print("\n=== 混比结论 ===")
    print(json.dumps(concl, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
