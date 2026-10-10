"""训练配方：**从数据集 manifest 现算配置**，然后按配置训。

为什么要独立成模块（而不是塞进脚本）：
1. 「配置从哪来」是**唯一真相源**纪律的落点。配方必须由 manifest 推出
   （vocab/block_size/tokenizer），不能脚本里手写一份——否则数据集换了
   tokenizer，配方还按老的来，训出来的东西是错的，而且**不报错**。
2. 配方要能被**两个入口共用**：`scripts/train_from_dataset.py`（生产）
   与对照实验脚本（G1）。共用一份实现 = 判据只有一个定义处。
3. 判据纪律：每个数字都来自本次实测或 manifest 实读，**不许占位**。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass(frozen=True)
class TrainConfig:
    """训练配方。字段全部来自 manifest 实读或显式入参。"""

    name: str
    recipe_id: str  # 配置指纹——改了配置就该换id，否则历史训练结果会混淆
    vocab_size: int
    block_size: int
    n_layer: int
    n_head: int
    n_embd: int
    dropout: float = 0.1
    tie_weights: bool = True

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def make_recipe_id(cfg_dict: dict[str, Any]) -> str:
    """配方指纹 = 配置内容的 sha256 前12 位。

    ⚠️ 为什么要它：`training_runs` 是**多版本历史**，没有指纹就无法回答
    「这条训练结果对应的是哪套配置」——改了 lr 却沿用旧 recipe_id，
    历史记录会说谎。
    """
    import hashlib

    blob = json.dumps(cfg_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def train_config_from_manifest(
    manifest: dict[str, Any],
    *,
    n_layer: int = 4,
    n_head: int = 4,
    n_embd: int = 256,
    block_size: int | None = None,
) -> TrainConfig:
    """从 manifest 推出配方。

    **vocab_size 从 manifest 的 `tokenizer_vocab_size` 来**，不硬编码 ——
    硬编码 21128（uer）而数据集用 Qwen（151936）→ 模型的 embedding 层
    词表不对，训出来的 loss 毫无意义，且**不会报任何错**。
    """
    vocab = int(manifest.get("tokenizer_vocab_size") or 0)
    if vocab <= 0:
        raise ValueError(
            f"manifest 缺 tokenizer_vocab_size（数据集 {manifest.get('name')}）——"
            "**不许硬编码词表**：那会让训练静默地用错 embedding"
        )
    bs = block_size or int(manifest.get("pack_block_size") or 0)
    if bs <= 0:
        raise ValueError(
            f"manifest 缺 pack_block_size（数据集 {manifest.get('name')}）——"
            "本模块只支持 **定长 block** 口径（变长样本需先 packing）"
        )
    body = {
        "vocab_size": vocab,
        "block_size": bs,
        "n_layer": n_layer,
        "n_head": n_head,
        "n_embd": n_embd,
        "tokenizer": manifest.get("tokenizer", "?"),
    }
    return TrainConfig(
        name=f"tiny-causal-lm-L{n_layer}-H{n_head}-E{n_embd}",
        recipe_id=make_recipe_id(body),
        n_layer=n_layer,
        n_head=n_head,
        n_embd=n_embd,
        vocab_size=vocab,
        block_size=bs,
    )


class CausalSelfAttention(nn.Module):
    def __init__(self, cfg: TrainConfig):
        super().__init__()
        if cfg.n_embd % cfg.n_head:
            raise ValueError(f"n_embd({cfg.n_embd}) 必须被 n_head({cfg.n_head}) 整除")
        self.n_head = cfg.n_head
        self.head_dim = cfg.n_embd // cfg.n_head
        self.qkv = nn.Linear(cfg.n_embd, 3 * cfg.n_embd, bias=False)
        self.proj = nn.Linear(cfg.n_embd, cfg.n_embd, bias=False)
        self.dropout = nn.Dropout(cfg.dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, C = x.shape
        q, k, v = self.qkv(x).split(C, dim=2)
        # (B,T,C) -> (B,n_head,T,head_dim)
        q = q.view(B, T, self.n_head, self.head_dim).transpose(1, 2)
        k = k.view(B, T, self.n_head, self.head_dim).transpose(1, 2)
        v = v.view(B, T, self.n_head, self.head_dim).transpose(1, 2)
        y = F.scaled_dot_product_attention(q, k, v, is_causal=True,
                                           dropout_p=self.dropout.p
                                           if self.training else 0.0)
        y = y.transpose(1, 2).contiguous().view(B, T, C)
        return self.proj(y)


class Block(nn.Module):
    def __init__(self, cfg: TrainConfig):
        super().__init__()
        self.ln1 = nn.LayerNorm(cfg.n_embd)
        self.attn = CausalSelfAttention(cfg)
        self.ln2 = nn.LayerNorm(cfg.n_embd)
        self.mlp = nn.Sequential(
            nn.Linear(cfg.n_embd, 4 * cfg.n_embd, bias=False),
            nn.GELU(),
            nn.Linear(4 * cfg.n_embd, cfg.n_embd, bias=False),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.ln1(x))
        return x + self.mlp(self.ln2(x))


class TinyCausalLM(nn.Module):
    """小型causal LM。规模刻意小——目的是**验证数据集能被训练**，
    不是刷benchmark。选它是因为：本机能GPU 跑、几分钟一轮、
    且换数据集时行为稳定（不会因为模型太小而学不出信号）。

    ⚠️ 一处容易写错的地方：logits 过lm_head 前**不要再 softmax**。
    交叉熵内部做log_softmax，重复softmax 会让loss 恒不下降但**不报错**
    （典型静默失效）。这里只返回 logits。
    """

    def __init__(self, cfg: TrainConfig):
        super().__init__()
        self.cfg = cfg
        self.tok_emb = nn.Embedding(cfg.vocab_size, cfg.n_embd)
        self.pos_emb = nn.Embedding(cfg.block_size, cfg.n_embd)
        self.drop = nn.Dropout(cfg.dropout)
        self.blocks = nn.ModuleList(Block(cfg) for _ in range(cfg.n_layer))
        self.ln_f = nn.LayerNorm(cfg.n_embd)
        self.lm_head = nn.Linear(cfg.n_embd, cfg.vocab_size, bias=False)
        if cfg.tie_weights:
            self.lm_head.weight = self.tok_emb.weight
        self.apply(self._init)

    @staticmethod
    def _init(m: nn.Module) -> None:
        if isinstance(m, nn.Linear):
            nn.init.normal_(m.weight, mean=0.0, std=0.02)
            if m.bias is not None:
                nn.init.zeros_(m.bias)
        elif isinstance(m, nn.Embedding):
            nn.init.normal_(m.weight, mean=0.0, std=0.02)

    @staticmethod
    def count_params(cfg_or_model) -> int:
        m = (cfg_or_model if isinstance(cfg_or_model, nn.Module)
             else TinyCausalLM(cfg_or_model))
        return sum(p.numel() for p in m.parameters() if p.requires_grad)

    def forward(self, idx: torch.Tensor) -> torch.Tensor:
        B, T = idx.shape
        pos = torch.arange(T, device=idx.device)
        x = self.tok_emb(idx) + self.pos_emb(pos)[None, :, :]
        x = self.drop(x)
        for b in self.blocks:
            x = b(x)
        return self.lm_head(self.ln_f(x))


@dataclass
class RecipeResult:
    """配方结果。**每个数字都来自实测**，无占位。"""

    val_loss: list[float] = field(default_factory=list)
    train_loss: list[float] = field(default_factory=list)
    tokens_seen: int = 0
    wall_seconds: float = 0.0
    trained_at: str = ""
    precision: str = "fp32"
    peak_gpu_gb: float = 0.0

    @property
    def tokens_per_second(self) -> float:
        return self.tokens_seen / self.wall_seconds if self.wall_seconds else 0.0

    @property
    def delta_val_loss(self) -> float:
        if len(self.val_loss) < 2:
            return 0.0
        return self.val_loss[-1] - self.val_loss[0]


# ── 容量公式（这一行决定了 batch_size 上限，不是模型深度）─────────────
# logits 显存 ≈ **B × T × V × dtype_bytes**（+梯度同量）
# ⚠️ 中文的坑：V 通常是 15 万级（Qwen 151665），而英文 5 万级。
#   B=8, T=512, V=151665, fp32 → logits 2.48 GB，梯度同量 → 峰值 10.3 GB
#   （RTX 4060 是 8 GB → **直接 OOM**，而且报错在 optimizer.step()，
#离「词表太大」这个真正原因隔了十万八千里）。
#   bf16 同配置 4.0 GB / 1.17 s每步，fp32 10.3 GB / 4.65 s每步。
# **调 batch 前先算这个公式**，别靠 OOM 试错。
def logits_bytes(batch: int, block_size: int, vocab: int, bytes_per: int = 4) -> int:
    """单个 logits 张量的字节数（不含梯度；含梯度约 2 倍）。"""
    return batch * block_size * vocab * bytes_per


def run_recipe(
    cfg: TrainConfig,
    train_dl,
    val_dl,
    *,
    steps: int,
    lr: float = 3e-4,
    seed: int = 0,
    eval_every: int = 0,
    device: str = "auto",
    precision: str = "fp32",
) -> RecipeResult:
    """按配方训练。`train_dl`/`val_dl` 产出 {"input_ids","labels"}。

    ⚠️ seed 纪律：DataLoader 的 shuffle 与模型 init必须**同一个 seed**。
    两者分开 seed 会导致「同seed 两次训练结果不同」，
    而这在对照实验里会被误读成「清洗有效」。
    """
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    torch.manual_seed(seed)
    dev = torch.device(device)

    # ⚠️ 精度只影响**计算**，loss 永远在 fp32 算（autocast 内部会升回来）。
    #    在 bf16 下直接算交叉熵 → 数值不稳，loss 会在某个值卡住不降，
    #    而**不报任何错**（典型静默失效）。
    amp = precision == "bf16"
    if amp and dev.type != "cuda":
        raise SystemExit("bf16 需要 CUDA；本机无 GPU 请用 --precision fp32")
    model = TinyCausalLM(cfg).to(dev)
    if amp:
        model = model.to(torch.bfloat16)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, betas=(0.9, 0.95),
                            weight_decay=0.1)
    res = RecipeResult(trained_at=datetime.now(timezone.utc).isoformat())
    res.val_loss.append(_eval(model, val_dl, dev))
    t0 = time.time()

    model.train()
    it = iter(train_dl)
    for step in range(1, steps + 1):
        try:
            batch = next(it)
        except StopIteration:  # 一个 epoch 用尽 → 重开（不丢数据）
            it = iter(train_dl)
            batch = next(it)
        batch = {k: v.to(dev) for k, v in batch.items()}
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=amp):
            logits = model(batch["input_ids"])
        # loss 强制 fp32：见上面 amp 处的说明
        loss = F.cross_entropy(
            logits[:, :-1, :].float().reshape(-1, logits.size(-1)),
            batch["labels"][:, 1:].reshape(-1),
            ignore_index=-100,
        )
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        res.train_loss.append(float(loss.detach()))
        ntok = int((batch["labels"][:, 1:] != -100).sum())
        res.tokens_seen += ntok
        if eval_every and step % eval_every == 0:
            res.val_loss.append(_eval(model, val_dl, dev))
            print(f"  step {step:4d} | train {float(loss):.4f} "
                  f"| val {res.val_loss[-1]:.4f}", flush=True)
    res.val_loss.append(_eval(model, val_dl, dev))
    res.wall_seconds = time.time() - t0
    res.precision = precision
    if dev.type == "cuda":
        res.peak_gpu_gb = round(torch.cuda.max_memory_allocated() / 1e9, 3)
    return res


def _eval(model: nn.Module, loader, dev: torch.device) -> float:
    model.eval()
    tot = 0.0
    n = 0
    with torch.no_grad():
        for batch in loader:
            ids = batch["input_ids"].to(dev)
            labels = batch["labels"].to(dev)
            amp_eval = next(model.parameters()).dtype == torch.bfloat16
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=amp_eval):
                logits = model(ids)
            tgt = labels[:, 1:]
            loss = F.cross_entropy(
                logits[:, :-1, :].float().reshape(-1, logits.size(-1)),
                tgt.reshape(-1),
                ignore_index=-100,
                reduction="sum",
            )
            ntok = int((tgt.reshape(-1) != -100).sum())
            tot += float(loss)
            n += ntok
    model.train()
    return tot / n if n else float("nan")
