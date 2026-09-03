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
    """训练/推理同协议（chat template）+ completion 必须完整落在窗口内。

    首训的隐藏大坑：新闻正文 ~1500 token >> 窗口 384，右截断后 completion
    完全在窗外 → 模型学的是「续写文章」而非「输出判分」（loss 1.9 的真相）。
    修正：prompt 按 completion 长度预留预算截断，JSON 判分永不被截掉。
    """
    order = list(range(len(rows)))
    rng.shuffle(order)
    templated = [
        tok.apply_chat_template(
            [{"role": "user", "content": r["prompt"]}],
            tokenize=False,
            add_generation_prompt=True,
        )
        for r in rows
    ]
    completions = [r["completion"] for r in rows]

    def encode(texts, max_len):
        return tok(texts, add_special_tokens=False, truncation=True, max_length=max_len)[
            "input_ids"
        ]

    for start in range(0, len(order) - batch + 1, batch):
        idx = order[start : start + batch]
        p_ids, f_ids = [], []
        for i in idx:
            c_ids = encode([completions[i]], 48)[0]
            budget = seq_len - len(c_ids) - 4  # completion 完整保留，prompt 吃剩余预算
            p = encode([templated[i]], budget)[0]
            p_ids.append(p)
            f_ids.append((p + c_ids)[:seq_len])
        max_len = max(len(f) for f in f_ids)
        input_ids, attn, labels = [], [], []
        for p, f in zip(p_ids, f_ids):
            pad = max_len - len(f)
            input_ids.append(f + [tok.pad_token_id] * pad)
            attn.append([1] * len(f) + [0] * pad)
            lab = [-100] * len(p) + f[len(p) :] + [-100] * pad
            labels.append(lab)

        def to_tensor(x):
            return torch.tensor(x, dtype=torch.long).to("cuda")

        yield to_tensor(input_ids), to_tensor(attn), to_tensor(labels)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="data/raw/news_corpus.jsonl")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--out", default="models/judge_lora_v1")
    parser.add_argument("--n-clean", type=int, default=1600)
    parser.add_argument("--n-dirty", type=int, default=1600)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--seq-len", type=int, default=640)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--benchmark",
        default="benchmarks/judge_news_v1",
        help="结构性隔离：其 clean 项的源文档不进训练集",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    corpus = load_corpus(Path(args.corpus))
    exclude: set[str] = set()
    bitems = Path(args.benchmark) / "items.jsonl"
    if bitems.exists():
        for ln in bitems.read_text(encoding="utf-8").split("\n"):
            if ln.strip():
                sid = json.loads(ln).get("source_id")
                if sid:
                    exclude.add(sid)
    logging.info("域语料 %s 篇（排除 benchmark 源 %s 篇）", len(corpus), len(exclude))
    rows = build_sft_rows(
        corpus,
        n_clean=args.n_clean,
        n_dirty=args.n_dirty,
        seed=TRAIN_SEED,
        exclude_source_ids=exclude,
    )
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
