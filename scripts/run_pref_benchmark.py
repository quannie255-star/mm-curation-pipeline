"""η-a 评测：冻结偏好 benchmark 上出钱表（命中率 / 分歧率 / 通用基线）。

用法：python -X utf8 scripts/run_pref_benchmark.py \
        --adapters PA=models/judge_pref_PA,PB=models/judge_pref_PB
两个 adapter 都会在全部题目上作答；命中率按各自 persona 的题目结算，
分歧率在两两同文档题对上按「选中的变体（S/F）」归一化比较。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

LEDGER = Path("runs/experiments.jsonl")
_CHOICE_RE = re.compile(r'"choice"\s*:\s*"(甲|乙)"')


def parse_choice(content: str) -> str | None:
    """只抓字母不解析整块 JSON——生成常在闭合前被 max_new_tokens 截断
    （DPO 判官输出多行美化 JSON，一次 eval 54/60 因此误判为 None）。"""
    m = _CHOICE_RE.search(content)
    return m.group(1) if m else None


def answer_all(
    prompts: list[str],
    model,
    tok,
    device: str,
    *,
    batch: int = 1,
    max_new_tokens: int = 96,
    max_length: int = 704,
) -> list[str | None]:
    """逐条生成（无 padding）：批量左 padding 路径曾被实测出与单样本不一致的
    结果（DPO adapter 对 pad 敏感，见 η-a 笔记），判分以单样本为准。"""
    outs: list[str | None] = []
    with torch.no_grad():
        for p in prompts:
            templated = tok.apply_chat_template(
                [{"role": "user", "content": p}], tokenize=False, add_generation_prompt=True
            )
            enc = tok(templated, return_tensors="pt", truncation=True, max_length=max_length).to(
                device
            )
            gen = model.generate(
                **enc, max_new_tokens=max_new_tokens, do_sample=False, pad_token_id=tok.pad_token_id
            )
            content = tok.decode(
                gen[0][enc["input_ids"].shape[1] :], skip_special_tokens=True
            ).strip()
            outs.append(parse_choice(content))
    return outs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", default="benchmarks/pref_news_v1")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--adapters", default="PA=models/judge_pref_PA,PB=models/judge_pref_PB")
    parser.add_argument("--generic", action="store_true", help="只跑通用基线")
    parser.add_argument("--max-length", type=int, default=704)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    items = [
        json.loads(ln)
        for ln in (Path(args.benchmark) / "items.jsonl").read_text(encoding="utf-8").split("\n")
        if ln.strip()
    ]
    logging.info("benchmark %s：%s 题", args.benchmark, len(items))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    prompts = [it["prompt"] for it in items]

    adapter_map = {}
    if not args.generic:
        for pair in args.adapters.split(","):
            k, v = pair.split("=")
            adapter_map[k.strip()] = v.strip()

    def eval_with(adapter: str | None, tag: str) -> dict[str, dict]:
        model = AutoModelForCausalLM.from_pretrained(
            args.model, torch_dtype=torch.float16 if device == "cuda" else torch.float32
        ).to(device)
        if adapter:
            from peft import PeftModel

            model = PeftModel.from_pretrained(model, adapter)
            logging.info("已加载 adapter %s", adapter)
        model.eval()
        choices = answer_all(prompts, model, tok, device,
                             max_length=args.max_length)
        result: dict[str, dict] = {}
        for it, ch in zip(items, choices):
            result[f"{it['persona']}/{it['kind']}/{it['id']}"] = {
                "choice": ch,
                "gold": it["gold"],
                "variant": it["variant_map"].get(ch) if ch else None,
                "gold_variant": it["gold_variant"],
                "source_id": it["source_id"],
            }
        Path("data/reports").mkdir(exist_ok=True)
        Path(f"data/reports/pref_answers_{tag}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        del model
        torch.cuda.empty_cache()
        return result

    answers: dict[str, dict] = {}
    for persona, adapter in adapter_map.items():
        answers[persona] = eval_with(adapter, f"tuned-{persona}")
    if args.generic or not adapter_map:
        answers["generic"] = eval_with(None, "generic")

    # ---- 命中率（各自 persona 的 main 题）----
    report: dict = {"n_items": len(items), "judges": {}}
    for name, res in answers.items():
        hits: dict[str, list[int]] = {}
        for key, r in res.items():
            persona, kind = key.split("/")[0], key.split("/")[1]
            hits.setdefault(f"{persona}/{kind}", []).append(int(r["choice"] == r["gold"]))
        summary = {k: round(sum(v) / len(v), 4) for k, v in hits.items()}
        report["judges"][name] = summary
        logging.info("%s 命中率: %s", name, summary)

    # ---- 分歧率（PA vs PB 判官，main 题按文档配对，归一化到变体）----
    if "PA" in answers and "PB" in answers:
        pa_main = {
            r["source_id"]: r
            for k, r in answers["PA"].items()
            if k.split("/")[1] == "main" and r["choice"]
        }
        pb_main = {
            r["source_id"]: r
            for k, r in answers["PB"].items()
            if k.split("/")[1] == "main" and r["choice"]
        }
        shared = set(pa_main) & set(pb_main)
        dis = sum(1 for sid in shared if pa_main[sid]["variant"] != pb_main[sid]["variant"])
        report["disagreement_rate"] = round(dis / len(shared), 4) if shared else None
        report["n_disagree_pairs"] = len(shared)
        logging.info("分歧率: %s（%s 对文档）", report["disagreement_rate"], len(shared))

    ts = datetime.now(timezone.utc).isoformat()
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": ts,
                    "run": "pref_benchmark_eval",
                    "stage": "eval",
                    "benchmark": args.benchmark,
                    "report": report,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    report["ts"] = ts
    Path("data/reports").mkdir(exist_ok=True)
    Path("data/reports/pref_alignment.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logging.info("报告: data/reports/pref_alignment.json + ledger")


if __name__ == "__main__":
    main()
