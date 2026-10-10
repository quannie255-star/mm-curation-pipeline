#!/usr/bin/env python
"""清洗 vs 未清洗的**可比**对照：同一 held-out + 多seed 噪声地板。

⚠️ 为什么需要这个脚本（而不是直接比两份训练报告）：
    第一版对照直接比 `train_recipe_news_zh_v2` 与 `_news_zh_v1_raw`
    的 val loss，得到 6.4477 vs 6.4469（差 0.0008）—— **这个比较不成立**，
    两个硬伤：
      ① **held-out 不同**：两臂各自的 val 集来自不同样本集合的切分
         （281 vs 274 block）。模型 A 在自己的 held-out 上评、B 在自己的
         held-out 上评 → 唯一变量不是「清洗与否」，是两个尺子。
         记忆里的判据纪律：held-out 必须是**两臂共同的尺子**。
      ② **没有噪声地板**：0.0008 差多少才算「真的不同」？不知道。
         不同 seed 会给出天然抖动，没有地板就无法判定。
    所以这里重做：两臂在**同一个 held-out**（清洗组的 test split）上评，
    每臂跑多个 seed，**用 seed 间抖动实测噪声地板**。

    这与 G1 (`eval_training_utility.py`) 的区别：G1 回答「清洗是否影响
    训练效用」，本脚本回答「**生产数据集**发布值不值得」—— 判据不同，
    刻意不合并。

用法:
    python scripts/compare_cleaned_vs_raw.py --seeds 3 --steps 300
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import torch  # noqa: E402
from torch.utils.data import DataLoader  # noqa: E402

from mm_curation.dataset import (  # noqa: E402
    run_recipe,
    train_config_from_manifest,
)

sys.path.insert(0, str(ROOT / "scripts"))
from train_from_dataset import BlockDataset, load_split  # noqa: E402

LOG = logging.getLogger("compare")


def paired_sign_test(diffs: list[float]) -> dict:
    """配对设计的显著性判定（纯函数，无 I/O，可被测试与变异验证）。

    `diffs[i]` = 第 i 个 seed 上 **未清洗臂 − 清洗臂** 的 held-out loss。
    差为**正** → 未清洗更差 → 清洗更好。

    ⚠️⚠️这版修掉了一个**会把阴性报成阳性**的真缺陷：
    原判据写 `elif k == 0: verdict = "...→ **清洗更好**"`，其中
    `k = min(n_pos, n_neg)`。**全负时n_pos=0，k 也是 0** ——
    于是「清洗更差」被硬编码成「清洗更好」。判据必须**由符号方向
    决定结论**，不能由「有没有相反符号」决定结论。

    第一版更大的问题：用「同臂跨 seed 的 max−min」当噪声地板，
    实测 Δ/地板 = 1.009，判据刚好越过门槛就报阳性（假阳性）。
    配对能消掉绝大部分 seed 效应，用独立样本的比较等于主动扔信息。
    """
    n = len(diffs)
    n_pos = sum(1 for d in diffs if d > 0)
    n_neg = sum(1 for d in diffs if d < 0)
    n_zero = sum(1 for d in diffs if d == 0)
    k = min(n_pos, n_neg)
    p_two = (min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
             if n > 0 else 1.0)
    sd = statistics.stdev(diffs) if n > 1 else 0.0
    t_stat = (statistics.fmean(diffs) / (sd / n ** 0.5)) if sd else float("nan")
    # 达p<0.05（双尾）所需的最少同号对。
    # 全同号时 p = 2 × 0.5^n，要p < 0.05 需 n ≥ 6（n=5 → 0.0625 仍不显著）。
    # ⚠️ 这里的循环**曾经差一格**：条件写成 `2 * 0.5 ** (need + 1) > 0.05`
    # 会返回 5，而 5 个同号实际 p=0.0625 并不显著 —— 日志会骗读者
    # 「至少需 5 个」而第5 个并不够。由 tests/test_compare_criterion.py 抓住。
    need = 1
    while 2 * 0.5 ** need >= 0.05:
        need += 1

    # 结论**只由多数方向 + p 值决定**，不看 k 是否为 0
    if n < 2:
        verdict = "seed 不足，无法判定"
    elif n_zero == n:
        verdict = "配对差全为 0 → **测不出差异**（两臂逐位相同）"
    elif p_two >= 0.05:
        verdict = (f"配对差 {n_pos}/{n} 同号但 p={p_two:.3f} ≥ 0.05 → "
                   "**测不出差异**（方向一致但样本量不足，不能报优劣）")
    else:
        direction = "清洗更好" if n_pos > n_neg else "清洗更差"
        verdict = f"{max(n_pos, n_neg)}/{n} 配对差同号且 p={p_two:.3f}<0.05 → **{direction}**"
    return {
        "n": n, "n_pos": n_pos, "n_neg": n_neg, "n_zero": n_zero, "k": k,
        "p_two_sided": round(p_two, 6), "sd": round(sd, 6),
        "t_stat": round(t_stat, 4) if t_stat == t_stat else None,
        "n_needed_for_p05": need, "verdict": verdict,
    }


def train_one(man: dict, held: BlockDataset, steps: int, seed: int,
              n_layer: int, n_embd: int, batch: int) -> float:
    """在给定 held-out 上训一臂，返回 val loss（nats/token）。"""
    tr_rows = load_split(man, "train")
    train_ds = BlockDataset(tr_rows, "loss_mask" in tr_rows[0])
    g = torch.Generator().manual_seed(seed)
    dl = DataLoader(train_ds, batch_size=batch, shuffle=True, generator=g)
    cfg = train_config_from_manifest(man, n_layer=n_layer, n_head=4,
                                      n_embd=n_embd,
                                      block_size=len(held[0]["input_ids"]))
    torch.manual_seed(seed)
    res = run_recipe(cfg, dl, DataLoader(held, batch_size=batch),
                     steps=steps, seed=seed, precision="bf16")
    return res.val_loss[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="清洗 vs 未清洗的可比对照")
    ap.add_argument("--cleaned", default="news_zh_v2")
    ap.add_argument("--raw", default="news_zh_v1_raw")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--n-layer", type=int, default=4)
    ap.add_argument("--n-embd", type=int, default=256)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mans = {}
    for n in (args.cleaned, args.raw):
        p = ROOT / "datasets" / n / "manifest.json"
        if not p.exists():
            raise SystemExit(f"❌ 缺manifest：{p}")
        mans[n] = json.loads(p.read_text(encoding="utf-8"))

    # ⭐ 共同 held-out = **清洗组的 test split**，两臂都只在它上面评。
    # 放在臂外（两臂都不训练它）—— 一旦某臂参与构造它，那条臂自带优势。
    held_rows = load_split(mans[args.cleaned], "test")
    held = BlockDataset(held_rows, "loss_mask" in held_rows[0])
    LOG.info("共同 held-out：%d block（清洗组 test split，两臂都不训练它）",
             len(held))

    results: dict[str, list[float]] = {}
    for arm in (args.cleaned, args.raw):
        vals = []
        for seed in range(args.seeds):
            v = train_one(mans[arm], held, args.steps, seed,
                          args.n_layer, args.n_embd, args.batch)
            LOG.info("  %-16s seed=%d val_loss=%.4f", arm, seed, v)
            vals.append(v)
        results[arm] = vals
        LOG.info("%-16s 均值 %.4f", arm, statistics.fmean(vals))

    a, b = statistics.fmean(results[args.cleaned]), statistics.fmean(results[args.raw])
    delta = b - a
    LOG.info("")
    LOG.info("=== 判据（配对符号检验）===")
    LOG.info("  清洗 %.4f | 未清洗 %.4f", a, b)
    LOG.info("  Δ(未清洗 − 清洗) = %+.4f", delta)

    # ⚠️⚠️ **不用「max−min 地板」，改用配对符号检验**。
    # 第一版用「同臂跨 seed 的 max−min」当噪声地板，实测 Δ/地板 = **1.009**，
    # 判据刚好越过门槛就报「清洗更好」—— 这跟噪声没区别，属**假阳性**。
    # 两条硬伤：
    #   ① max−min 受单个极值支配，n=3 时极不稳定；
    #   ② 忽略了两臂**共用同一批 seed** 这个配对设计 —— 配对能消掉
    #     绝大部分 seed 效应，用独立样本的比较方式等于主动扔掉信息。
    # 配对设计下唯一可看的量是「每对seed 的差」，问题变成：
    #     这n 个差是否**同号**？
    diffs = [raw - cl for cl, raw in zip(results[args.cleaned], results[args.raw])]
    st = paired_sign_test(diffs)
    n, n_pos, n_neg, k = st["n"], st["n_pos"], st["n_neg"], st["k"]
    p_two, sd, t_stat = st["p_two_sided"], st["sd"], st["t_stat"]
    LOG.info("  配对差 %s", [f"{d:+.4f}" for d in diffs])
    LOG.info("  同号性 %d 正 / %d 负（n=%d）| 双尾符号检验 p = %.3f", n_pos, n_neg,
             n, p_two)
    LOG.info("  配对标准差 %.4f | t = %.2f（df=%d）", sd, t_stat, n - 1)
    LOG.info("  要达 p<0.05（双尾）至少需 %d 个同号配对对，本次 %d 个",
             st["n_needed_for_p05"], k)
    verdict = st["verdict"]
    LOG.info("  判定：%s", verdict)

    payload = {
        "protocol": {
            "held_out": f"{args.cleaned}/test（两臂共同，两臂都不训练它）",
            "n_held_out_blocks": len(held),
            "seeds": args.seeds,
            "steps": args.steps,
            "batch": args.batch,
            "model": {"n_layer": args.n_layer, "n_head": 4, "n_embd": args.n_embd},
            "precision": "bf16",
            "design": "**配对**（两臂共用同一批 seed）→ 符号检验",
        },
        "arms": {k: {"per_seed": v, "mean": statistics.fmean(v)}
                 for k, v in results.items()},
        "delta_raw_minus_cleaned": round(delta, 6),
        "paired_diffs": [round(d, 6) for d in diffs],
        "sign_test": st,
        "verdict": verdict,
        "caveat": (
            "⚠️ 判据只覆盖**本批语料 + 本配方**。清洗丢弃量仅 1.6%"
            "（1960→1929，text_minhash 去重 25 条转载），"
            "效应量本就小；这个实验回答的是「这批数据清洗有没有效」，"
            "不是「清洗一般有没有效」。"
        ),
    }
    out = ROOT / "data" / "reports" / "cleaned_vs_raw.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    LOG.info("报告 → %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
