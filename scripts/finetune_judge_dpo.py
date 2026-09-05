"""η-a：DPO 训练偏好判官（每 persona 一个 adapter，trl DPOTrainer + peft LoRA）。

用法：python -X utf8 scripts/finetune_judge_dpo.py --persona PA --out models/judge_pref_PA
数据：data/interim/pref_dpo.jsonl（build_pref_data.py 产物；本脚本按 persona 过滤）

协议红线（#58/#59）：prompt 预套 chat template（与推理一致）；
max_prompt/max_completion 预算保证 completion 完整进窗口。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import torch  # noqa: E402
from datasets import Dataset  # noqa: E402
from peft import LoraConfig  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402
from trl import DPOConfig, DPOTrainer  # noqa: E402

DATA = Path("data/interim/pref_dpo.jsonl")
LEDGER = Path("runs/experiments.jsonl")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--persona", required=True, help="PA/PB（偏好）或 EXT（抽取）")
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--out", default=None)
    parser.add_argument("--epochs", type=float, default=2)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=8)
    parser.add_argument("--lr", type=float, default=5e-6)
    parser.add_argument("--max-prompt", type=int, default=560)
    parser.add_argument("--max-completion", type=int, default=48)
    parser.add_argument("--max-length", type=int, default=608)
    parser.add_argument("--beta", type=float, default=0.1)
    args = parser.parse_args()
    out = Path(args.out or f"models/judge_pref_{args.persona}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    rows = [
        json.loads(ln)
        for ln in Path(args.data).read_text(encoding="utf-8").split("\n")
        if ln.strip() and json.loads(ln)["persona"] == args.persona
    ]
    if not rows:
        raise SystemExit(f"数据中无 persona={args.persona} 的行：{args.data}")
    logging.info("persona %s：%s 条 DPO 三元组", args.persona, len(rows))

    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    # 训练/推理同协议：prompt 预套 chat template（推理侧 run_pref_benchmark 同款）
    for r in rows:
        r["prompt"] = tok.apply_chat_template(
            [{"role": "user", "content": r["prompt"]}],
            tokenize=False,
            add_generation_prompt=True,
        )
    ds = Dataset.from_list(
        [{"prompt": r["prompt"], "chosen": r["chosen"], "rejected": r["rejected"]} for r in rows]
    )

    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.bfloat16)
    peft_config = LoraConfig(
        r=16,
        lora_alpha=32,
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
    config = DPOConfig(
        output_dir=str(out),
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        beta=args.beta,
        max_prompt_length=args.max_prompt,
        max_completion_length=args.max_completion,
        max_length=args.max_length,
        bf16=True,
        precompute_ref_log_probs=True,
        logging_steps=20,
        save_strategy="no",
        report_to=[],
        seed=42,
    )
    trainer = DPOTrainer(
        model=model,
        ref_model=None,
        args=config,
        train_dataset=ds,
        processing_class=tok,
        peft_config=peft_config,
    )
    t0 = __import__("time").perf_counter()
    trainer.train()
    elapsed = __import__("time").perf_counter() - t0

    trainer.save_model(str(out))
    tok.save_pretrained(str(out))

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "run": "pref_dpo_v1",
                    "stage": "train",
                    "persona": args.persona,
                    "model": args.model,
                    "adapter": str(out),
                    "n_triples": len(rows),
                    "config": {
                        "epochs": args.epochs,
                        "batch": args.batch,
                        "grad_accum": args.grad_accum,
                        "lr": args.lr,
                        "beta": args.beta,
                    },
                    "final_loss": trainer.state.log_history[-1].get("train_loss"),
                    "seconds": round(elapsed, 1),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    logging.info("adapter 已存 %s（%.0fs），ledger 已记", out, elapsed)


if __name__ == "__main__":
    main()
