"""Chinese-CLIP 干净/脏集微调对比（Phase2 P4）：代理验证 → 训练级直接验证。

问题：D3 证明清洗提升「检索质量」（代理指标）；训练数据质量的金标准是
「模型训练效果」。本实验：同一基座（Chinese-CLIP ViT-B/16）分别在
干净集与脏集上做等步数对比微调，再看检索指标——若脏数据学的模型更差，
则「脏数据伤模型」有了直接证据（而非只有代理证据）。

评测协议与 D3 对齐：干净索引图像 + 同一查询集（held_out/self 分列），
名次判定用保守并列口径（score >= target 的个数）。

用法：python scripts/finetune_clip.py [--steps 100]
产物：models/finetune/{clean_ft,dirty_ft}.pt + data/reports/finetune_eval.{json,md}
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import torch  # noqa: E402
from PIL import Image  # noqa: E402

from mm_curation.eval.metrics import mrr, recall_at_k  # noqa: E402
from mm_curation.eval.retrieval import build_queries  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402

CLEAN = Path("data/processed/cn_flickr_curation_v2/cleaned.jsonl")
DIRTY = Path("data/interim/contaminated/samples.jsonl")
MODEL_DIR = Path(__file__).resolve().parents[1] / "models/chinese-clip-vit-base-patch16"
OUT = Path("models/finetune")
REPORT = Path("data/reports/finetune_eval.json")


def _load_base():
    from transformers import ChineseCLIPModel, ChineseCLIPProcessor

    model = ChineseCLIPModel.from_pretrained(MODEL_DIR)
    processor = ChineseCLIPProcessor.from_pretrained(MODEL_DIR)
    return model, processor


def _features(model, processor, device, images, texts=None):
    """图像走 get_image_features；文本绕开 transformers 4.57 的 pooler
    回归 bug（同 clip_encoder.py 的处理：CLS -> text_projection）。"""
    with torch.no_grad():
        if images is not None:
            inputs = processor(images=images, return_tensors="pt").to(device)
            emb = model.get_image_features(**inputs)
        else:
            inputs = processor(
                text=texts, return_tensors="pt", padding=True, truncation=True, max_length=64
            ).to(device)
            out = model.text_model(
                input_ids=inputs["input_ids"],
                attention_mask=inputs.get("attention_mask"),
                token_type_ids=inputs.get("token_type_ids"),
            )
            emb = model.text_projection(out.last_hidden_state[:, 0])
    return torch.nn.functional.normalize(emb, dim=-1)


def finetune(model, processor, device, pairs, steps, lr=5e-6, batch=32, seed=42):
    import random

    rng = random.Random(seed)
    opt = torch.optim.AdamW(model.parameters(), lr=lr)
    model.train()
    for step in range(steps):
        batch_pairs = rng.sample(pairs, batch)
        images = [Image.open(p).convert("RGB").resize((224, 224)) for p, _ in batch_pairs]
        texts = [t for _, t in batch_pairs]
        img_inputs = processor(images=images, return_tensors="pt").to(device)
        txt_inputs = processor(
            text=texts, return_tensors="pt", padding=True, truncation=True, max_length=64
        ).to(device)
        img_emb = torch.nn.functional.normalize(model.get_image_features(**img_inputs), dim=-1)
        txt_out = model.text_model(
            input_ids=txt_inputs["input_ids"],
            attention_mask=txt_inputs.get("attention_mask"),
            token_type_ids=txt_inputs.get("token_type_ids"),
        )
        txt_emb = torch.nn.functional.normalize(
            model.text_projection(txt_out.last_hidden_state[:, 0]), dim=-1
        )
        logits = model.logit_scale.exp().clamp(max=100) * img_emb @ txt_emb.t()
        labels = torch.arange(len(batch_pairs), device=device)
        loss = 0.5 * (
            torch.nn.functional.cross_entropy(logits, labels)
            + torch.nn.functional.cross_entropy(logits.t(), labels)
        )
        opt.zero_grad()
        loss.backward()
        opt.step()
    model.eval()
    return model


@torch.no_grad()
def retrieval_eval(model, processor, device, index_paths, queries, k_list=(1, 5, 10)):
    """全量图像向量 + 查询向量，纯 numpy 余弦排名（119x1585 规模无需 FAISS）。"""

    imgs = [Image.open(p).convert("RGB") for p in index_paths]
    img_emb = (
        torch.cat(
            [
                _features(model, processor, device, images=imgs[i : i + 64])
                for i in range(0, len(imgs), 64)
            ]
        )
        .cpu()
        .numpy()
    )
    txt_emb = (
        torch.cat(
            [
                _features(
                    model,
                    processor,
                    device,
                    images=None,
                    texts=[q.text for q in queries[i : i + 64]],
                )
                for i in range(0, len(queries), 64)
            ]
        )
        .cpu()
        .numpy()
    )
    sims = txt_emb @ img_emb.T  # (n_q, n_index)
    rankings = []
    for qi, q in enumerate(queries):
        col = qi  # 查询按 clean 顺序构造，target = 同下标图像（held_out/self 均如此）
        target_score = sims[col, col]
        rankings.append(int((sims[col] >= target_score).sum()))  # 保守并列口径
    by_origin = {}
    for q, r in zip(queries, rankings):
        by_origin.setdefault(q.origin, []).append(r)

    def block(rs):
        return {
            "n": len(rs),
            "recall_at_k": {k: recall_at_k(rs, k) for k in k_list},
            "mrr": mrr(rs),
        }

    return {
        "n_queries": len(queries),
        "recall_at_k": {k: recall_at_k(rankings, k) for k in k_list},
        "mrr": mrr(rankings),
        "per_origin": {o: block(rs) for o, rs in sorted(by_origin.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=100)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not CLEAN.exists() or not DIRTY.exists():
        logging.error("缺少 cleaned/contaminated 数据（先 make data && make funnel）")
        sys.exit(1)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    clean_rows = [json.loads(line) for line in CLEAN.read_text(encoding="utf-8").splitlines()]
    dirty_rows = [json.loads(line) for line in DIRTY.read_text(encoding="utf-8").splitlines()]
    clean_pairs = [(r["image_path"], r.get("text") or r.get("caption", "")) for r in clean_rows]
    dirty_pairs = [(r["image_path"], r.get("text") or r.get("caption", "")) for r in dirty_rows]
    queries = build_queries([Sample.from_dict(r) for r in clean_rows])
    index_paths = [r["image_path"] for r in clean_rows]
    logging.info(
        "干净对 %s / 脏对 %s / 查询 %s（%s steps 微调）",
        len(clean_pairs),
        len(dirty_pairs),
        len(queries),
        args.steps,
    )

    base, processor = _load_base()
    results = {}
    results["base"] = retrieval_eval(
        copy.deepcopy(base).to(device), processor, device, index_paths, queries
    )
    logging.info(
        "base: R@1=%.3f MRR=%.3f", results["base"]["recall_at_k"][1], results["base"]["mrr"]
    )

    OUT.mkdir(parents=True, exist_ok=True)
    for name, pairs in [("clean_ft", clean_pairs), ("dirty_ft", dirty_pairs)]:
        model = copy.deepcopy(base).to(device)
        finetune(model, processor, device, pairs, args.steps)
        torch.save(model.state_dict(), OUT / f"{name}.pt")
        results[name] = retrieval_eval(model, processor, device, index_paths, queries)
        logging.info(
            "%s: R@1=%.3f MRR=%.3f", name, results[name]["recall_at_k"][1], results[name]["mrr"]
        )
        del model
        torch.cuda.empty_cache()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps({"steps": args.steps, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md = [
        "# 微调对比：干净集 vs 脏集（Chinese-CLIP ViT-B/16，等步数）",
        "",
        f"- 微调 {args.steps} 步（lr 5e-6，batch 32），评测协议同 D3",
        "",
        "| 模型 | R@1 | R@5 | R@10 | MRR |",
        "|---|---|---|---|---|",
    ]
    for name in ("base", "clean_ft", "dirty_ft"):
        r = results[name]
        md.append(
            f"| {name} | {r['recall_at_k'][1]:.3f} | {r['recall_at_k'][5]:.3f} |"
            f" {r['recall_at_k'][10]:.3f} | {r['mrr']:.3f} |"
        )
    Path(str(REPORT).replace(".json", ".md")).write_text("\n".join(md) + "\n", encoding="utf-8")
    logging.info("报告: %s", REPORT)


if __name__ == "__main__":
    main()
