"""ζ3：Qwen2.5-0.5B LoRA 微调「专属数据判官」（本机 8GB 可跑）。

用法：python -X utf8 scripts/finetune_judge_lora.py --corpus data/raw/news_corpus.jsonl
产物：models/judge_lora_v1/{adapter, tokenizer} + runs/experiments.jsonl（实验 ledger）

产品语义：微调后的判官与 LlmJudgeOp/serve_judge.py 是同一协议位——
同一个 rubric prompt、同一份 JSON 输出契约，漏斗配置零改动即插即用。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 权重优先走本地缓存（模型已在 serve_judge 首跑时下载）；镜像兜底
import os  # noqa: E402

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import torch  # noqa: E402
from peft import LoraConfig, get_peft_model  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from mm_curation.tuning.judge_data import TRAIN_SEED, build_sft_rows, write_sft_jsonl  # noqa: E402

LEDGER = Path("runs/experiments.jsonl")


def load_corpus(path: Path, min_chars: int = 200) -> list:
    from curation_eval import Sample

    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]
    return [Sample(id=r["id"], text=r["text"]) for r in rows if len(r["text"]) >= min_chars]


def batches(rows: list[dict], tok, batch: int, seq_len: int, rng: random.Random):
    order = list(range(len(rows)))
    rng.shuffle(order)
    for start in range(0, len(order) - batch + 1, batch):
        chunk = [rows[i] for i in order[start : start + batch]]
        prompts = [r["prompt"] for r in chunk]
        full = [r["prompt"] + r["completion"] for r in chunk]
        enc_p = tok(prompts, return_tensors="pt", truncation=True, max_length=seq_len)
        enc_f = tok(
            full, return_tensors="pt", truncation=True, max_length=seq_len + 48, padding=True
        )
        labels = enc_f["input_ids"].clone()
        # 只对 completion 段计 loss：prompt 部分置 -100（长度差按 token 级对齐）
        for b in range(len(chunk)):
            plen = min((enc_p["attention_mask"][b] == 1).sum().item(), seq_len)
            labels[b, :plen] = -100
        labels[enc_f["attention_mask"] == 0] = -100
        yield enc_f["input_ids"], enc_f["attention_mask"], labels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="data/raw/news_corpus.jsonl")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--out", default="models/judge_lora_v1")
    parser.add_argument("--n-clean", type=int, default=1600)
    parser.add_argument("--n-dirty", type=int, default=1600)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--seq-len", type=int, default=384)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    corpus = load_corpus(Path(args.corpus))
    logging.info("域语料 %s 篇（≥200 字）", len(corpus))
    rows = build_sft_rows(corpus, n_clean=args.n_clean, n_dirty=args.n_dirty, seed=TRAIN_SEED)
    sft_path = Path("data/interim/judge_sft.jsonl")
    write_sft_jsonl(rows, sft_path)
    n_dirty = sum(1 for r in rows if r["label"] == "dirty")
    logging.info("SFT 行 %s（dirty %s）→ %s", len(rows), n_dirty, sft_path)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=torch.float16 if device == "cuda" else torch.float32
    ).to(device)
    model.config.use_cache = False
    lora = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_r * 2,
        lora_dropout=0.05,
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()
    model.train()

    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr)
    total_loss, n_steps, t0 = 0.0, 0, time.perf_counter()
    for epoch in range(args.epochs):
        for input_ids, attn, labels in batches(rows, tok, args.batch, args.seq_len, rng):
            out = model(
                input_ids=input_ids.to(device),
                attention_mask=attn.to(device),
                labels=labels.to(device),
            )
            opt.zero_grad()
            out.loss.backward()
            opt.step()
            total_loss += out.loss.item()
            n_steps += 1
            if n_steps % 50 == 0:
                logging.info(
                    "epoch %s step %s loss=%.4f (%.1fs)",
                    epoch,
                    n_steps,
                    total_loss / n_steps,
                    time.perf_counter() - t0,
                )
    final_loss = total_loss / max(n_steps, 1)
    logging.info("训练完成：%.1fs，final loss %.4f", time.perf_counter() - t0, final_loss)

    out_dir = Path(args.out)
    model.save_pretrained(out_dir)
    tok.save_pretrained(out_dir)

    ledger_row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run": "judge_lora_v1",
        "stage": "train",
        "model": args.model,
        "adapter": str(out_dir),
        "corpus": args.corpus,
        "n_sft": len(rows),
        "n_dirty": n_dirty,
        "train_seed": TRAIN_SEED,
        "config": {
            "epochs": args.epochs,
            "batch": args.batch,
            "seq_len": args.seq_len,
            "lr": args.lr,
            "lora_r": args.lora_r,
        },
        "final_loss": round(final_loss, 4),
        "seconds": round(time.perf_counter() - t0, 1),
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ledger_row, ensure_ascii=False) + "\n")
    logging.info("adapter 已存 %s，ledger 已记 %s", out_dir, LEDGER)


if __name__ == "__main__":
    main()
