"""文本模态训练对比：干净 vs 脏语料微调 GPT-2 zh（V2 β T6，镜像 P4 实验）。

协议：同一基座（uer/gpt2-chinese-cluecorpussmall），等步数分别在
干净语料与注入脏语料（按 dirty_rate 混入四种损伤：编码乱码/段落复读/
重复字符/截断）上继续训练，在**不相交的干净 held-out 测试集**上测困惑度
——脏语料训练的模型 ppl 显著更高 = 文本模态的"清洗提升训练效果"直接证据。

注：首跑用 dirty_rate=0.10 + 旧损伤集，ppl 差仅 0.4%（噪声级）——
旧 mojibake 对纯中文是 gbk 字节被 utf-8 丢弃后的良性截断，truncate 也
不破坏语言分布，真正有害的损伤占比太低。故损伤集重造且默认剂量提到 1.0。

用法：python scripts/finetune_gpt2.py [--steps 1500] [--dirty-rate 1.0]
产物：data/reports/finetune_text_eval.{json,md}
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from mm_curation.gpt2_weights import ensure_local_gpt2  # noqa: E402

CORPUS = Path("data/raw/text_corpus.jsonl")
REPORT = Path("data/reports/finetune_text_eval")


def _mangle(text: str, rng: random.Random) -> str:
    """四种文本损伤之一，全部与清洗算子同靶且对 LM 真有害：
    编码乱码（char_repetition 无关，但属 L1 规则靶）/ 段落复读
    （line_repetition 靶）/ 重复字符（char_repetition 靶）/ 截断
    （doc_length 靶——它不破坏语言分布，伤的是训练容量）。"""
    kind = rng.choice(["mojibake", "repeat", "char_repeat", "truncate"])
    if kind == "mojibake":
        # 现实中最常见的乱码：UTF-8 字节流被按 GBK 误解码（每两个字节
        # 强行拼成一个汉字），仍是合法汉字但语义全毁——旧实现反着来，
        # gbk 字节按 utf-8 解码时被 errors="ignore" 丢光，退化成良性截断
        return text.encode("utf-8").decode("gbk", errors="replace")
    if kind == "repeat":
        segs = text.split("\n")
        pos = rng.randrange(len(segs))
        return "\n".join(segs[:pos] + [segs[pos]] * 3 + segs[pos:])
    if kind == "char_repeat":
        ch = text[rng.randrange(len(text))]
        pos = rng.randrange(len(text))
        return text[:pos] + ch * rng.randint(30, 80) + text[pos:]
    return text[: max(20, len(text) // 10)]


def _load_sets(n_train: int, n_test: int, dirty_rate: float, seed: int):
    # 同 T5：按字面 \n 切分，防正文中的 U+2028/U+2029 被 splitlines() 误切。
    rows = [json.loads(ln) for ln in CORPUS.read_text(encoding="utf-8").split("\n") if ln.strip()]
    eligible = [r["text"] for r in rows if len(r["text"]) >= 100]
    rng = random.Random(seed)
    rng.shuffle(eligible)
    test = eligible[:n_test]
    train = eligible[n_test : n_test + n_train]
    dirty = [_mangle(t, rng) if rng.random() < dirty_rate else t for t in train]
    return train, dirty, test


def _batches(texts, tok, batch, seq_len):
    for start in range(0, len(texts) - batch + 1, batch):
        enc = tok(
            texts[start : start + batch],
            return_tensors="pt",
            truncation=True,
            max_length=seq_len,
            padding=True,
        )
        labels = enc["input_ids"].masked_fill(enc["attention_mask"] == 0, -100)
        yield enc["input_ids"], enc["attention_mask"], labels


@torch.no_grad()
def evaluate(model, tok, texts, batch, seq_len, device):
    model.eval()
    total_loss, total_tokens = 0.0, 0
    for input_ids, attn, labels in _batches(texts, tok, batch, seq_len):
        out = model(
            input_ids=input_ids.to(device), attention_mask=attn.to(device), labels=labels.to(device)
        )
        n = (labels != -100).sum().item()
        total_loss += out.loss.item() * n
        total_tokens += n
    return float(torch.exp(torch.tensor(total_loss / max(total_tokens, 1))))


def train(model, tok, texts, steps, batch, seq_len, lr, device, seed):
    rng = random.Random(seed)
    opt = torch.optim.AdamW(model.parameters(), lr=lr)
    model.train()
    step = 0
    while step < steps:
        chunk = rng.sample(texts, min(batch, len(texts)))
        enc = tok(chunk, return_tensors="pt", truncation=True, max_length=seq_len, padding=True)
        labels = enc["input_ids"].masked_fill(enc["attention_mask"] == 0, -100)
        out = model(
            input_ids=enc["input_ids"].to(device),
            attention_mask=enc["attention_mask"].to(device),
            labels=labels.to(device),
        )
        opt.zero_grad()
        out.loss.backward()
        opt.step()
        step += 1
        if step % 250 == 0:
            logging.info("  step %s/%s loss=%.4f", step, steps, out.loss.item())
    model.eval()
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=1500)
    parser.add_argument("--n-train", type=int, default=20000)
    parser.add_argument("--n-test", type=int, default=2000)
    parser.add_argument("--dirty-rate", type=float, default=1.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not CORPUS.exists():
        logging.error("语料缺失: %s（先 python scripts/download_text_corpus.py）", CORPUS)
        sys.exit(1)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    train_clean, train_dirty, test = _load_sets(args.n_train, args.n_test, args.dirty_rate, seed=42)
    logging.info(
        "train_clean=%s train_dirty=%s held-out=%s (device=%s)",
        len(train_clean),
        len(train_dirty),
        len(test),
        device,
    )

    model_dir = str(ensure_local_gpt2())
    tok = AutoTokenizer.from_pretrained(model_dir)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token or tok.mask_token
    base = AutoModelForCausalLM.from_pretrained(model_dir).to(device)

    results = {"base": evaluate(base, tok, test, 32, 256, device)}
    logging.info("base ppl=%.2f", results["base"])

    for name, texts in [("clean_ft", train_clean), ("dirty_ft", train_dirty)]:
        model = copy.deepcopy(base)
        # hash(str) 按进程加盐——固定种子保证实验可复现
        train(
            model,
            tok,
            texts,
            args.steps,
            8,
            256,
            5e-5,
            device,
            seed={"clean_ft": 1, "dirty_ft": 2}[name],
        )
        results[name] = evaluate(model, tok, test, 32, 256, device)
        logging.info("%s ppl=%.2f", name, results[name])
        del model
        torch.cuda.empty_cache()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.with_suffix(".json").write_text(
        json.dumps(
            {
                "steps": args.steps,
                "n_train": args.n_train,
                "dirty_rate": args.dirty_rate,
                "ppl": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md = [
        "# 文本模态训练对比：干净 vs 脏语料（GPT-2 zh，等步数）",
        "",
        f"- 微调 {args.steps} 步（lr 5e-5，batch 8，seq 256），"
        f"脏语料损伤率 {args.dirty_rate:.0%}，held-out {args.n_test} 篇干净维基",
        "",
        "| 模型 | held-out 困惑度 |",
        "|---|---|",
    ]
    for name in ("base", "clean_ft", "dirty_ft"):
        md.append(f"| {name} | {results[name]:.2f} |")
    gap = (results["dirty_ft"] - results["clean_ft"]) / results["clean_ft"]
    verdict = "✅ 方向正确且超过 5% 验收线" if gap > 0.05 else "❌ 未达验收线（要求差值为正且 >5%）"
    md += [
        "",
        f"- dirty_ft 相对 clean_ft 困惑度 **{gap:+.1%}** — {verdict}",
        "- 损伤集：编码乱码（UTF-8→GBK 误解码）/ 段落复读 / 重复字符 / 截断，"
        "分别对应漏斗的 L1 规则与重复率算子",
    ]
    REPORT.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    logging.info("报告: %s.{json,md}", REPORT)


if __name__ == "__main__":
    main()
