#!/usr/bin/env python
"""用build_dataset.py 产出的数据集**真训一次模型**，把结果回填 manifest。

这是「数据清洗系统」与「**训练数据生产系统**」的分界：
- 清洗系统的产出是「保留率 97.67% + 判决书」—— 报告；
- 生产系统的产出是「一份数据集，用recipe R 训出模型 M，在val 上 ppl=P」
  —— **数据集必须对「值不值得训」负责**，跑不掉。

⚠️ 为什么不用 G1 那个脚本（`eval_training_utility.py`）：
    它是**三臂对照实验**专用（等量/等步数/噪声地板），读的是 JSONL 语料，
    绕过了数据集抽象。这里要的是**另一条路径**：从 manifest 出发，
    走标准 HF 入口读 parquet，像一个普通用户那样消费自己的产物。
    两条路径刻意分开—— 否则「数据集不可用」这件事会被对照实验的
    复杂逻辑掩盖掉。

用法:
    python scripts/train_from_dataset.py --dataset news_zh_v2 --steps 300
    python scripts/train_from_dataset.py --dataset news_zh_v2 --writeback# 回填 manifest
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from torch.utils.data import DataLoader, Dataset  # noqa: E402

from mm_curation.dataset.train_recipe import (  # noqa: E402
    RecipeResult,
    TinyCausalLM,
    run_recipe,
    train_config_from_manifest,
)

LOG = logging.getLogger("train_from_dataset")


def load_split(manifest: dict, split: str) -> list[dict]:
    """**从磁盘读**，不读manifest 里的统计——manifest 是声明，磁盘才是事实。"""
    import datasets as hfds

    root = ROOT / "datasets" / manifest["name"] / "shards"
    files = sorted(str(p) for p in root.glob(f"{split}-*.parquet"))
    if not files:
        raise SystemExit(f"❌ split={split} 没有 shard（{root}）")
    #⚠️ 必须**显式给 split 名**：`load_dataset(..., data_files=[文件])`
    # 会把唯一 split 命名为 "train"，哪怕这些文件其实是 val 的
    #（实测踩过：`["val"]` → KeyError 'val'）。
    ds = hfds.load_dataset("parquet", data_files={split: files})[split]
    return [dict(r) for r in ds]


class BlockDataset(Dataset):
    """最小只读适配：把 block 行包成 torch Dataset。

    为什么需要它而不是直接 `Dataset.from_parquet`：
    HF 的 Dataset 本身就能取行，但它不做 label 变换。causal LM 需要
    `labels` 里 padding 位为 -100（PyTorch 交叉熵约定），这层必须自己写。
    写成class 而不是一个函数，是为了让 DataLoader 能多进程读。
    """

    def __init__(self, rows: list[dict], has_mask: bool):
        self.rows = rows
        self.has_mask = has_mask

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, i: int) -> dict[str, torch.Tensor]:
        r = self.rows[i]
        ids = torch.tensor(r["input_ids"], dtype=torch.long)
        if self.has_mask and "loss_mask" in r:
            m = torch.tensor(r["loss_mask"], dtype=torch.bool)
            labels = torch.where(m, ids, torch.full_like(ids, -100))
        else:
            labels = ids.clone()
        return {"input_ids": ids, "labels": labels}


def evaluate(model: torch.nn.Module, loader: DataLoader) -> float:
    """held-out 评估：返回 loss（nats/token）。

    ⚠️ 用 **loss** 而不是 ppl：block 级定长数据里padding 位已被 -100 排除，
    loss 已是「有效 token 上的平均」，可比；ppl 是它的指数，两者在
    数值上单调等价，但 loss 的量级便于和训练 loss 直接对比（同尺度）。
    """
    model.eval()
    tot = 0.0
    n = 0
    with torch.no_grad():
        for batch in loader:
            out = model(batch["input_ids"])
            logits = out[:, :-1, :]
            tgt = batch["labels"][:, 1:]
            # -100 位不计入 loss（PyTorch 约定）
            loss = F.cross_entropy(
                logits.reshape(-1, logits.size(-1)),
                tgt.reshape(-1),
                ignore_index=-100,
                reduction="sum",
            )
            ntok = int((tgt.reshape(-1) != -100).sum())
            if ntok:
                tot += float(loss)
                n += ntok
    model.train()
    return tot / n if n else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description="从数据集 manifest 真训一次")
    ap.add_argument("--dataset", required=True, help="datasets/ 下的目录名")
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--n-layer", type=int, default=4)
    ap.add_argument("--n-head", type=int, default=4)
    ap.add_argument("--n-embd", type=int, default=256)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--precision", choices=["fp32", "bf16"], default="fp32",
                    help="bf16 约快 4 倍、显存减半（需 CUDA）")
    ap.add_argument("--writeback", action="store_true",
                    help="把训练结果写回 manifest 的 training_runs")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    mpath = ROOT / "datasets" / args.dataset / "manifest.json"
    if not mpath.exists():
        raise SystemExit(f"❌ manifest 不存在：{mpath}\n   先跑 scripts/build_dataset.py")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))

    LOG.info("=== 数据集 %s（row_unit=%s）===", manifest["name"],
             manifest.get("row_unit", "sample"))
    LOG.info("声明规模 %d block | %d 有效 token | padding %s%%",
             sum(manifest.get("n_blocks", {}).values()) or manifest["n_samples"],
             manifest.get("n_real_tokens_total", manifest["n_tokens"]),
             manifest.get("padding_pct", 0.0))

    tr = load_split(manifest, "train")
    va = load_split(manifest, "val")
    LOG.info("从磁盘读回：train %d block / val %d block", len(tr), len(va))
    if not tr or not va:
        raise SystemExit("❌ train/val 为空 —— 配方没法跑，也不许当成'训练成功'")

    has_mask = "loss_mask" in tr[0]
    train_ds = BlockDataset(tr, has_mask)
    val_ds = BlockDataset(va, has_mask)
    LOG.info("loss_mask 存在：%s（决定 padding 是否计入 loss）", has_mask)

    g = torch.Generator().manual_seed(args.seed)
    train_dl = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True,
                          generator=g, drop_last=False)
    val_dl = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False)

    cfg = train_config_from_manifest(
        manifest, n_layer=args.n_layer, n_head=args.n_head, n_embd=args.n_embd,
        block_size=train_ds[0]["input_ids"].numel(),
    )
    LOG.info("模型 %s（%.1fM 参数）", cfg.name,
             TinyCausalLM.count_params(cfg) / 1e6)

    t0 = time.time()
    res: RecipeResult = run_recipe(cfg, train_dl, val_dl, steps=args.steps,
                                   lr=args.lr, seed=args.seed,
                                   precision=args.precision)
    dt = time.time() - t0
    LOG.info("训练完成 %.1fs（%d 步，%.2fs/步）", dt, args.steps, dt / max(args.steps, 1))
    LOG.info("  初始 val loss %.4f → 最终 %.4f（Δ %+.4f）",
             res.val_loss[0], res.val_loss[-1], res.delta_val_loss)
    LOG.info("  tokens 消费 %d（%.0f tok/s）| 峰值显存 %.2f GB | %s",
             res.tokens_seen, res.tokens_per_second, res.peak_gpu_gb,
             res.precision)

    payload = {
        "recipe_id": cfg.recipe_id,
        "recipe": {
            "n_layer": cfg.n_layer, "n_head": cfg.n_head, "n_embd": cfg.n_embd,
            "block_size": cfg.block_size, "steps": args.steps,
            "batch_size": args.batch_size, "lr": args.lr, "seed": args.seed,
        },
        "dataset_fingerprint": manifest.get("shard_checksums", {}),
        "n_blocks": {"train": len(tr), "val": len(va)},
        "val_loss_first": round(res.val_loss[0], 6),
        "val_loss_final": round(res.val_loss[-1], 6),
        "delta_val_loss": round(res.delta_val_loss, 6),
        "tokens_seen": res.tokens_seen,
        "wall_seconds": round(dt, 2),
        "tokens_per_second": round(res.tokens_per_second, 1),
        "peak_gpu_gb": res.peak_gpu_gb,
        "precision": res.precision,
        "trained_at": res.trained_at,
    }
    rep = ROOT / "data" / "reports" / f"train_recipe_{args.dataset}.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    LOG.info("训练报告 → %s", rep)

    if args.writeback:
        # 回填 manifest：让「这个数据集用哪个 recipe 训出什么」可查。
        #⚠️ 只 append，不覆盖历史 runs —— 生产数据集是多版本演进的。
        manifest.setdefault("training_runs", []).append(payload)
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        LOG.info("已回填 manifest.training_runs（累计 %d 条）",
                 len(manifest["training_runs"]))
    else:
        LOG.info("未回填 manifest（加 --writeback 才会写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
