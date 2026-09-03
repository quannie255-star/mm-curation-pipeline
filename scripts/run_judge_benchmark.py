"""ζ4：在冻结 benchmark 上结算判官的可信度（κ/P/R/解析率）。

同一 runner 评两种判官，出「钱表」：
- 通用模型（--adapter 缺省）：复现 δ 的阴性基线（κ≈0）
- 微调判官（--adapter models/judge_lora_v1）：本平台的核心主张

结果追加 runs/experiments.jsonl（实验 ledger），报告落 data/reports/。
"""

from __future__ import annotations

import argparse
import json
import logging
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
from curation_eval import cohen_kappa, pr_from_drops  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from mm_curation.operators.llm_judge import _JUDGE_PROMPT, _parse_score  # noqa: E402

LEDGER = Path("runs/experiments.jsonl")


def score_texts(
    texts: list[str], model, tok, device: str, *, batch: int = 8, max_new_tokens: int = 48
) -> list[float | None]:
    """批量打分：套 chat template（笔记 #54：不套 = 指令遵循崩坏）。"""
    scores: list[float | None] = []
    tok.padding_side = "left"  # decoder-only 批量生成必须左 padding（右移会劣化输出）
    model.eval()
    with torch.no_grad():
        for start in range(0, len(texts), batch):
            chunk = texts[start : start + batch]
            prompts = [
                tok.apply_chat_template(
                    [{"role": "user", "content": _JUDGE_PROMPT + t[:2000]}],
                    tokenize=False,
                    add_generation_prompt=True,
                )
                for t in chunk
            ]
            enc = tok(
                prompts, return_tensors="pt", truncation=True, max_length=768, padding=True
            ).to(device)
            out = model.generate(
                **enc, max_new_tokens=max_new_tokens, do_sample=False, pad_token_id=tok.pad_token_id
            )
            for b in range(len(chunk)):
                content = tok.decode(
                    out[b][enc["input_ids"].shape[1] :], skip_special_tokens=True
                ).strip()
                raw = _parse_score(content)
                scores.append(raw / 10.0 if raw is not None else None)
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", default="benchmarks/judge_news_v1")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--adapter", default=None, help="LoRA 适配器目录；缺省=通用模型")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--tag", default="generic")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    bdir = Path(args.benchmark)
    items = [
        json.loads(ln)
        for ln in (bdir / "items.jsonl").read_text(encoding="utf-8").split("\n")
        if ln.strip()
    ]
    manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    logging.info(
        "benchmark %s：%s 条（manifest version %s）",
        args.benchmark,
        len(items),
        manifest["version"],
    )

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=torch.float16 if device == "cuda" else torch.float32
    ).to(device)
    if args.adapter:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, args.adapter)
        logging.info("已加载 adapter %s", args.adapter)
    model.eval()

    t0 = time.perf_counter()
    scores = score_texts([it["text"] for it in items], model, tok, device, batch=args.batch)
    elapsed = time.perf_counter() - t0

    judged = {it["id"]: sc for it, sc in zip(items, scores) if sc is not None}
    n_unparsed = len(items) - len(judged)
    y_judge = {i: (1 if sc < args.threshold else 0) for i, sc in judged.items()}
    dirty_ids = {it["id"] for it in items if it["label"] == "dirty"}
    kappa = cohen_kappa(list(y_judge.values()), [1 if i in dirty_ids else 0 for i in y_judge])
    drops = {i for i, d in y_judge.items() if d == 1}
    pr = pr_from_drops(
        sorted(drops),
        [
            type(
                "R", (), {"id": it["id"], "labels": {"dirty": 1} if it["label"] == "dirty" else {}}
            )()
            for it in items
        ],
    )

    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run": "judge_benchmark_eval",
        "stage": "eval",
        "benchmark": args.benchmark,
        "adapter": args.adapter or "(generic)",
        "threshold": args.threshold,
        "kappa": kappa,
        "precision": pr["precision"],
        "recall": pr["recall"],
        "n_judged": len(judged),
        "n_unparsed": n_unparsed,
        "seconds": round(elapsed, 1),
        "score_p50": sorted(judged.values())[len(judged) // 2] if judged else None,
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    logging.info(
        "κ=%s P=%s R=%s（评判 %s/未解析 %s，%.0fs）→ ledger",
        kappa,
        pr["precision"],
        pr["recall"],
        len(judged),
        n_unparsed,
        elapsed,
    )


if __name__ == "__main__":
    main()
