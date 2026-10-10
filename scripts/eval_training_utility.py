"""G1：训练效用判据层——「清洗后的数据训出的模型更好吗」。

为什么需要这个脚本（这是与业界 SOTA 的**最致命差距**）
----------------------------------------------------------
DataComp-LM 的实证结论：**衡量数据质量的 SOTA 判据是「下游模型涨多少」，
不是「检出率 / 误杀率」**。而且它明确指出——人类标注与高 ROC-AUC 的
过滤器**都**无法可靠预测训练效用。

本项目此前所有质量证据都是 **P:R**（`eval_detection_slo.py` /
`operator_pr.json`），那是**检测器的指标**，不是**数据的指标**。
`finetune_gpt2.py` 虽然有训练级验证，但它的两臂是
「**人工注入损伤的脏语料** vs 干净语料」——
被清洗的**不是**那批脏数据，而是另一批凭空造的损伤。
所以它证明的是「脏数据伤模型」，**不是**「我的漏斗有用」。

本脚本把三臂接成一条线，唯一的变量是**过不过漏斗**：

    臂A  base      基座，不微调（下界参照）
    臂 B  unclean   在**未清洗**语料上等步数微调
    臂 C  cleaned在**过了漏斗**的语料上等步数微调
          ↑
          B vs C 的差 = **本漏斗的训练效用**，这才是对外该报的数字

⚠️ 四条口径纪律（违反其一结论即无效）
----------------------------------------
1. **等步数、等 lr、等 batch、等 held-out，且等数据量**。
   唯一的变量只能是「数据过没过漏斗」。
   ⚠️ **「等数据量」这条第一版漏掉了，直接导致结论无效**：
   首跑实测 unclean 6000 条 / cleaned 4711 条 —— 等步数下 cleaned臂
   每步能抽到的样本更少，于是**「数据量」与「数据质量」两个变量混在一起**，
   跑出「清洗反而更差」（Δ = -0.1101）。那**不是清洗有害**，
   是**成本不对等的对照**。修法：`--equal-size`（默认开）把 unclean
   随机抽到与 cleaned 同量再比。
   ⚠️ 反过来也要看清：**这样测出的是「同量下的替换效应」**，
   它**回答不了**「值不值得为了质量牺牲 21% 数据量」——那是成本决策，
   由 `--unequal-size` 那组数据（两臂全量）提供，需与本组一并解读。
2. **B 与 C 必须同源**：C 是 B 的**子集**（漏斗只丢弃，不新增）。
   若 C 混入了 B 之外的样本，就成了「换了数据」而非「清洗了数据」。
   脚本会断言 `C ⊆ B`。
3. **不做「零重编码」之类的省事优化**：两臂必须真的各跑一次训练。
   复用权重 = 两臂不是独立的 = 结论无效。
4. **单seed，必须报「差异 vs 噪声」**：本脚本不跑多种子方差，
   所以 Δ 小于典型 seed 间波动时，唯一诚实的说法是「测不出差异」。
   报告里的 `noise_floor` 就是给这个用的——不许把噪声当结论。

⚠️ 这个脚本**不是** DataComp 口径，别对外夸大
----------------------------------------------
DataComp 的判据是「固定 recipe 训练后跑 53 个下游任务」。
本脚本是「小模型 + 固定 held-out 困惑度」，属于**代理的训练效用**。
它比 P:R 强得多（真的训了模型），但仍不等于 SOTA。
诚实说法：「**在本地小规模上验证了漏斗的训练效用方向**」，
不许说「达到 SOTA 训练效用判据」。

用法
----
    python -X utf8 scripts/eval_training_utility.py
    python -X utf8 scripts/eval_training_utility.py --steps 800 --n-train 8000
    python -X utf8 scripts/eval_training_utility.py --config configs/text_funnel.yaml

产物：data/reports/training_utility.{json,md}
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CORPUS = ROOT / "data/raw/text_corpus.jsonl"
REPORT = ROOT / "data/reports/training_utility"
DEFAULT_CONFIG = ROOT / "configs/text_funnel.yaml"

log = logging.getLogger("training_utility")


# ────────────────────────────── 语料与漏斗 ──────────────────────────────


def load_corpus() -> list[dict]:
    if not CORPUS.exists():
        raise SystemExit(f"❌ 语料缺失: {CORPUS}\n   先跑 python scripts/download_text_corpus.py")
    return [json.loads(ln) for ln in CORPUS.read_text(encoding="utf-8").split("\n") if ln.strip()]


def split_rows(rows: list[dict], n_train: int, n_test: int, seed: int):
    """按**固定种子**切出 held-out 与训练集。

    held-out 必须在**任何臂之外**——它是三臂共同的尺子，
    一旦某臂参与构造它，那条臂就自带优势（记忆里的判据纪律：
    「两臂唯一变量是 X」必须可核对）。
    """
    eligible = [r for r in rows if len(r.get("text", "")) >= 100]
    rng = random.Random(seed)
    rng.shuffle(eligible)
    need = n_test + n_train
    if len(eligible) < need:
        raise SystemExit(
            f"❌ 语料不足：需要 {need} 条（train {n_train} + held-out {n_test}），"
            f"只有 {len(eligible)} 条 ≥100 字。**不许偷偷减小 n-train**——"
            "那会让结论不可比。改用 --n-train 更小的值。"
        )
    held = eligible[:n_test]
    train_pool = eligible[n_test:need]
    return train_pool, held


def run_funnel(train_pool: list[dict], config_path: Path) -> tuple[list[dict], dict]:
    """把训练集过一遍真实漏斗，返回 (存活样本, 漏斗统计)。

    ⚠️ 走的是**与生产同一条** `run_funnel`，不是另写一套近似逻辑。
    否则「训练效用」测的是一个跟线上不同的清洗器，结论无效。

    ⚠️ `Sample.from_dict` 只保留已知字段，所以必须把 `text` 塞进 dict，
    否则模型侧会拿到空文本 —— 那时两臂都训的是空串，结论是纯噪声。
    """
    from mm_curation.operators.base import Sample  # noqa: PLC0415
    from mm_curation.pipeline import PipelineConfig  # noqa: PLC0415
    from mm_curation.pipeline import run_funnel as _run  # noqa: PLC0415

    if not config_path.exists():
        raise SystemExit(f"❌ 配置不存在: {config_path}")
    cfg = PipelineConfig.from_yaml(config_path)

    rows_for_sample = [
        {"id": str(r["id"]), "text": r["text"], "modality": "text_article"} for r in train_pool
    ]
    samples = [Sample.from_dict(d) for d in rows_for_sample]
    result = _run(samples, cfg)

    kept_ids = {s.id for s in result.kept}
    id2text = {str(r["id"]): r["text"] for r in train_pool}
    # 按**id** 反查（不按 text：两篇相同正文会被误当成都存活）
    kept_rows = [{"id": i, "text": id2text[i]} for i in kept_ids if i in id2text]

    stats = {
        "config": str(config_path.relative_to(ROOT)),
        "n_in": len(samples),
        "n_kept": len(kept_ids),
        "drop_rate": 1 - len(kept_ids) / max(len(samples), 1),
        "per_stage": [
            {
                "op": st.op,
                "n_in": st.n_in,
                "n_out": st.n_out,
                "dropped": st.dropped,
                "skipped": st.skipped,
            }
            for st in result.stats
        ],
    }
    return kept_rows, stats


def assert_same_source(kept_rows: list[dict], train_pool: list[dict]) -> None:
    """口径纪律 2：C 必须是 B 的子集，否则「换了数据」而非「清洗了数据」。"""
    if not kept_rows:
        raise SystemExit(
            "❌ 漏斗把所有训练样本都丢了 —— 无法做训练效用对照。"
            "这通常意味着配置与语料模态不匹配（fail-fast 该在run_funnel 里报）。"
        )
    pool_ids = {str(r["id"]) for r in train_pool}
    if pool_ids:
        kept_ids = {str(r["id"]) for r in kept_rows}
        if not kept_ids.issubset(pool_ids):
            raise SystemExit(
                "❌ 清洗后的集合不是原训练集的子集 —— "
                "两臂不再是「同一批数据过没过漏斗」，结论无效。"
            )


# ────────────────────────────── 训练与评测 ──────────────────────────────


def _batches(texts, tok, batch, seq_len):

    for start in range(0, max(len(texts) - batch + 1, 1), batch):
        enc = tok(
            texts[start : start + batch],
            return_tensors="pt",
            truncation=True,
            max_length=seq_len,
            padding=True,
        )
        labels = enc["input_ids"].masked_fill(enc["attention_mask"] == 0, -100)
        yield enc["input_ids"], enc["attention_mask"], labels


def evaluate(model, tok, texts, batch, seq_len, device):
    import torch  # noqa: PLC0415

    model.eval()
    total_loss, total_tokens = 0.0, 0
    with torch.no_grad():
        for input_ids, attn, labels in _batches(texts, tok, batch, seq_len):
            out = model(
                input_ids=input_ids.to(device),
                attention_mask=attn.to(device),
                labels=labels.to(device),
            )
            n = (labels != -100).sum().item()
            total_loss += out.loss.item() * n
            total_tokens += n
    return float(torch.exp(torch.tensor(total_loss / max(total_tokens, 1))))


def train(model, tok, texts, steps, batch, seq_len, lr, device, seed):
    """等步数微调。**三臂的 steps/lr/batch/seq_len 必须完全一致**。"""
    import torch  # noqa: PLC0415

    rng = random.Random(seed)
    opt = torch.optim.AdamW(model.parameters(), lr=lr)
    model.train()
    step = 0
    while step < steps:
        chunk = rng.sample(texts, min(batch, len(texts)))
        enc = tok(chunk, return_tensors="pt", truncation=True, max_length=seq_len, padding=True)
        labels = enc["input_ids"].masked_fill(enc["attention_mask"] == 0, -100)
        opt.zero_grad()
        out = model(
            input_ids=enc["input_ids"].to(device),
            attention_mask=enc["attention_mask"].to(device),
            labels=labels.to(device),
        )
        out.loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        step += 1
    return model


# ────────────────────────────────── 主流程 ──────────────────────────────────


def ref_arm_name(arms: list[tuple[str, list[str]]]) -> str:
    """对照臂名（等量用unclean_matched，全量用 unclean）。"""
    for name, _ in arms:
        if name.startswith("unclean"):
            return name
    return arms[0][0]


def compute_noise_floor(ppl: dict, ref_arm: str) -> float | None:
    """噪声地板 =「同数据、不同 seed」两臂 ppl 的**差值**。

    ⚠️ 独立成函数是因为它藏过一次**恒假判据**：原实现直接取
    `results.get("noise_probe")`，即噪声臂的**绝对 ppl**（7.82）。
    而 `delta` 是**差值**（±0.2 量级）—— 拿绝对量当差值的门槛，
    `|Δ| < 7.82` 永远成立，于是「实测噪声地板」变成一句**恒真的免责条款**：
    报告照常生成、verdict 也是句像模像样的中文，只是永远给不出结论。

    隐蔽之处在于**日志里算的是对的**（`abs(probe - ref_arm)`），
    只有判据取错了量 —— 所以「看日志觉得没问题」不能代替这项检查。

    同族纪律：门槛不能来自被测对象的**读数**，只能来自**独立估计的差**。
    """
    probe = ppl.get("noise_probe")
    if probe is None or ref_arm not in ppl:
        return None
    return abs(probe - ppl[ref_arm])


def classify_verdict(delta: float, noise_floor: float | None) -> str:
    """判定 Δ 是否超出噪声地板。**独立成函数**是为了能被单独测/ 变异。

    ⚠️ 这是本脚本最容易出假结论的地方，且已真踩过一次：
    · Δ 为正**不等于**清洗有用 —— 它可能只是噪声，所以必须与地板比。
    · **地板必须是差值**（|probe − ref|），不能是噪声臂的**绝对 ppl**。
      拿绝对量当门槛 → `|Δ| < 7.82` 恒成立 → 任何实验都判「测不出差异」。
      那样这函数会变成一个**恒真的免责条款**：报告照常生成、
      verdict 也是一句像模像样的中文，只是永远给不出结论。
    """
    if noise_floor is None:
        return "未测噪声地板 —— 本次无法判定差异是否真实"
    if delta > noise_floor:
        return "cleaned 显著更好"
    if delta < -noise_floor:
        return "cleaned 显著更差（值得查漏斗是否过度清洗）"
    if delta > 0:
        return "cleaned 略好但小于噪声地板 —— **测不出差异**"
    return "差异小于噪声地板 —— **测不出差异**"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=800)
    parser.add_argument("--n-train", type=int, default=8000)
    parser.add_argument("--n-test", type=int, default=1000)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--seq-len", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--equal-size",
        dest="equal_size",
        action="store_true",
        default=True,
        help="对照臂抽到与 cleaned 同量（默认开，这是唯一可比的口径）",
    )
    parser.add_argument(
        "--unequal-size",
        dest="equal_size",
        action="store_false",
        help=(
            "对照臂用全量（不复现那个把数据量与质量混在一起的错误）。"
            "此口径回答的是「值不值得为质量牺牲 X% 数据量」，"
            "**不能**用来回答「清洗有没有用」。"
        ),
    )
    parser.add_argument(
        "--noise-probe",
        action="store_true",
        help="额外跑一臂「同数据不同 seed」实测噪声地板（不做它就判不出 Δ 是否为真效应）",
    )
    parser.add_argument(
        "--arms",
        choices=("all", "base-only"),
        default="all",
        help="base-only 只测基座与漏斗统计（不训练，用于快速检查漏斗是否把数据杀光了）",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import torch  # noqa: PLC0415
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: PLC0415

    from mm_curation.gpt2_weights import ensure_local_gpt2  # noqa: PLC0415

    rows = load_corpus()
    train_pool, held = split_rows(rows, args.n_train, args.n_test, args.seed)
    log.info(
        "训练池 %s / held-out %s（held-out 不参与任何臂的构造）",
        len(train_pool),
        len(held),
    )

    kept_rows, funnel = run_funnel(train_pool, Path(args.config))
    assert_same_source(kept_rows, train_pool)
    log.info(
        "漏斗 %s：%s → %s（丢弃 %.2f%%）",
        funnel["config"],
        funnel["n_in"],
        funnel["n_kept"],
        funnel["drop_rate"] * 100,
    )
    if funnel["n_kept"] == 0:
        raise SystemExit("❌ 漏斗丢弃率 100% —— 无法训练，先查配置")

    if args.arms == "base-only":
        payload = {
            "protocol": "base-only（未训练）",
            "funnel": funnel,
            "n_train_pool": len(train_pool),
            "n_train_kept": len(kept_rows),
            "n_held_out": len(held),
        }
        _write(payload, funnel, args)
        return 0

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_dir = str(ensure_local_gpt2())
    tok = AutoTokenizer.from_pretrained(model_dir)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token or tok.mask_token
    base = AutoModelForCausalLM.from_pretrained(model_dir).to(device)
    log.info("基座就绪（device=%s）", device)

    held_texts = [r["text"] for r in held]
    results: dict[str, float] = {"base": evaluate(base, tok, held_texts, 32, args.seq_len, device)}
    log.info("base ppl=%.4f", results["base"])

    # ── 构造臂列表：等量对照是主判据，全量臂是成本决策参考 ──
    cleaned_texts = [r["text"] for r in kept_rows]
    pool_texts = [r["text"] for r in train_pool]
    n_clean, n_pool = len(cleaned_texts), len(pool_texts)

    arms: list[tuple[str, list[str]]] = []
    if args.equal_size:
        # 主判据：unclean 随机抽到与 cleaned **同量**。
        # 这样两臂的数据量与步数都相同，唯一变量 = 「这批数据过没过漏斗」。
        # ⚠️ 抽样用**独立 seed**（`:unclean_sample`），
        # 避免与训练 seed 耦合导致两臂抽到同样的样本序。
        n = min(n_clean, n_pool)
        rng = random.Random(f"{args.seed}:unclean_sample")
        matched = rng.sample(pool_texts, n) if n < n_pool else list(pool_texts)
        log.info("等量对照：unclean 从 %s 抽到 %s 条（= cleaned 条数）", n_pool, n)
        arms = [
            ("unclean_matched", matched),
            ("cleaned", cleaned_texts),
        ]
    else:
        # 成本决策参考：两臂全量。这个口径回答的是
        # 「值得为质量牺牲 X% 数据吗」，**不能**用来回答「清洗有没有用」。
        arms = [
            ("unclean", pool_texts),
            ("cleaned", cleaned_texts),
        ]

    for name, texts in arms:
        model = copy.deepcopy(base)
        # ⚠️ 两臂**同 seed**：唯一的变量是数据，不是采样顺序。
        train(
            model,
            tok,
            texts,
            args.steps,
            args.batch,
            args.seq_len,
            args.lr,
            device,
            seed=args.seed + 100,
        )
        results[name] = evaluate(model, tok, held_texts, 32, args.seq_len, device)
        log.info("%s ppl=%.4f（n_train=%s）", name, results[name], len(texts))
        del model
        torch.cuda.empty_cache()

    # ── 噪声地板：同一批数据、换个seed 再训一次 ──
    # 不测这个，就无法判断 Δ 是真效应还是训练抖动。
    # 这是「指标出现极端组合时先怀疑判据」的落地：Δ>0 也可能只是噪声。
    if args.noise_probe:
        ref_texts = dict(arms)[ref_arm_name(arms)]
        model = copy.deepcopy(base)
        train(
            model,
            tok,
            ref_texts,
            args.steps,
            args.batch,
            args.seq_len,
            args.lr,
            device,
            seed=args.seed + 999,
        )
        results["noise_probe"] = evaluate(model, tok, held_texts, 32, args.seq_len, device)
        log.info(
            "noise_probe ppl=%.4f（同数据不同 seed，与 %s 相差 %.4f）",
            results["noise_probe"],
            ref_arm_name(arms),
            abs(results["noise_probe"] - results[ref_arm_name(arms)]),
        )
        del model
        torch.cuda.empty_cache()

    ref_arm = "unclean_matched" if args.equal_size else "unclean"
    delta = results[ref_arm] - results["cleaned"]
    # 经验噪声地板：**同数据、不同 seed** 的 ppl **差异量**，不是它的 ppl 值。
    # ⚠️ 这里踩过一个恒假判据：原实现是 results.get("noise_probe")，
    #    存进去的是噪声臂的**绝对 ppl（7.82）**，而 Δ 是**差值（±0.19）**——
    #    拿绝对量当差值的门槛 → |Δ| 永远 < 7.82 → 永远判「测不出差异」。
    #    日志里算的是对的 abs(noise_probe - ref_arm)，只有判据取错了量，
    #    于是「实测噪声地板」变成了一个恒真的免责条款。
    #    真实地板 = |7.8200 − 7.8637| = 0.0437，Δ=−0.195 是它的 **4.46 倍** →
    #    正确判定是「cleaned 显著更差」，而不是「测不出差异」。
    noise_floor = compute_noise_floor(results, ref_arm)
    payload = {
        "protocol": {
            "steps": args.steps,
            "n_train_pool": len(train_pool),
            "n_train_kept": len(kept_rows),
            "n_held_out": len(held),
            "equal_size": args.equal_size,
            "reference_arm": ref_arm,
            "lr": args.lr,
            "batch": args.batch,
            "seq_len": args.seq_len,
            "seed": args.seed,
            "base_model": model_dir,
            "device": device,
        },
        "funnel": funnel,
        "ppl": results,
        "utility": {
            "reference_arm": ref_arm,
            "delta_ppl": delta,
            "relative": delta / results[ref_arm] if results[ref_arm] else None,
            "direction": (
                "cleaned 更好" if delta > 0 else ("持平" if delta == 0 else "cleaned 更差")
            ),
            "noise_floor": noise_floor,
            "verdict": classify_verdict(delta, noise_floor),
        },
        "caveat": (
            "本脚本是**代理的训练效用**（小模型 + 固定 held-out ppl），"
            "**不是** DataComp-LM 口径（固定 recipe 训练 + 53 个下游任务）。"
            "对外表述为「在本地小规模上验证了漏斗的训练效用方向」，"
            "不得说成「达到 SOTA 训练效用判据」。\n"
            "另：单 seed 或双 seed，**没有**训练方差估计的完整实验。"
            "凡 Δ 小于 noise_floor，唯一诚实的说法是「测不出差异」。"
        ),
    }
    _write(payload, funnel, args)
    log.info(
        "训练效用：unclean %.4f → cleaned %.4f（Δ=%+.4f，%s）",
        results["unclean"],
        results["cleaned"],
        delta,
        payload["utility"]["direction"],
    )
    return 0


def _write(payload: dict, funnel: dict, args) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if "ppl" not in payload:
        md = [
            "# 训练效用判据（base-only 预检）",
            "",
            f"- 漏斗 `{funnel['config']}`：{funnel['n_in']} → {funnel['n_kept']}"
            f"（丢弃 {funnel['drop_rate']:.2%}）",
            "",
            "| 阶段 | 入 | 留 | 丢弃 | 跳过 |",
            "|---|---|---|---|---|",
        ]
        for st in funnel["per_stage"]:
            md.append(
                f"| {st['op']} | {st['n_in']} | {st['n_out']} | {st['dropped']} | {st['skipped']} |"
            )
        REPORT.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
        return

    u = payload["utility"]
    p = payload["ppl"]
    ref = u["reference_arm"]
    noise = u.get("noise_floor")
    md = [
        "# 训练效用判据：清洗 vs 不清洗（GPT-2 zh，等量等步数）",
        "",
        "唯一的变量是**训练数据过不过漏斗**。各臂共享同一基座、同一 held-out、",
        "同一组超参，且**训练样本数相等**（不等量会把「质量」与「数量」两个变量混在一起）。",
        "",
        f"- 漏斗 `{funnel['config']}`：{funnel['n_in']} → {funnel['n_kept']}"
        f"（丢弃 {funnel['drop_rate']:.2%}）",
        f"- 步数 {payload['protocol']['steps']}，lr {payload['protocol']['lr']}，"
        f"batch {payload['protocol']['batch']}，seq {payload['protocol']['seq_len']}",
        f"- 设备 {payload['protocol']['device']}",
        "",
        "| 臂 | 训练样本数 | held-out ppl |",
        "|---|---|---|",
        f"| base（不微调） | — | {p['base']:.4f} |",
    ]
    # 臂名可能带 `_matched` 后缀（等量对照）或没有（不等量参考），都按实际键渲染
    for arm in ("unclean_matched", "unclean"):
        if arm in p:
            label = f"{arm}（不过漏斗）"
            md.append(f"| {label} | {payload['protocol']['n_train_pool']} | {p[arm]:.4f} |")
            break
    md.append(f"| cleaned（过漏斗） | {payload['protocol']['n_train_kept']} | {p['cleaned']:.4f} |")
    if "noise_probe" in p:
        md.append(f"| noise_probe（同数据不同 seed） | — | {p['noise_probe']:.4f} |")
    # 标题**由verdict 现算**，不手写。
    # ⚠️ 本轮踩过：md 里的标题是上一轮手写的「测不出清洗的训练效用」，
    # 而 JSON 里verdict 已翻转成「显著更差」 —— 两处口径分叉，
    # 人读的那份与机读的那份说法相反。标题必须现算。
    ratio = abs(u["delta_ppl"]) / noise if noise else None
    headline = (
        "测不出清洗的训练效用（差异小于噪声）"
        if (noise is not None and ratio is not None and ratio < 1)
        else (
            "清洗在本批语料上**显著更差**（超出噪声地板）"
            if (ratio is not None and ratio >= 1 and u["delta_ppl"] < 0)
            else (
                "清洗在本批语料上**显著更好**（超出噪声地板）"
                if ratio is not None and ratio >= 1
                else "未测噪声地板 —— 无法判定"
            )
        )
    )
    md += [
        "",
        f"# {headline}",
        "",
        f"Delta = {ref} - cleaned = **{u['delta_ppl']:+.4f}**（{u['direction']}）",
    ]
    if noise is not None:
        md += [
            f"- 噪声地板（同数据不同 seed）：**{noise:.4f}**",
            f"- |Δ| / 噪声地板 = **{ratio:.2f} 倍**" if ratio else "- |Δ| / 噪声地板 = —",
            f"- 判定：**{u.get('verdict', '—')}**",
        ]
    md += [
        "",
        "> ⚠️ **口径边界**：这是**代理的训练效用**（小模型 + 固定 held-out ppl），",
        "> **不是** DataComp-LM 口径（固定 recipe + 53 个下游任务）。",
        "> 对外只能说「在本地小规模上验证了漏斗的训练效用方向」。",
        "",
        "> ⚠️ 不等量口径（全量 vs 清洗后）回答的是「值不值得为质量牺牲 X% 数据量」，",
        "> **不能**用来回答「清洗有没有用」—— 那是本项目踩过的第一个坑。",
    ]
    REPORT.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
