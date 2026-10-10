"""构建一个可直接用于训练/分析的数据集制品。

    python -X utf8 scripts/build_dataset.py \
        --input data/processed/finance_news_funnel/cleaned.jsonl \
        --name finance_news_v1 \
        --split-key symbol \
        --tokenizer Qwen/Qwen2.5-0.5B-Instruct

产物：
    datasets/<name>/manifest.json      唯一真相源
    datasets/<name>/shards/*.parquet   zstd 列式，可 datasets/DataLoader 直接读
    data/reports/dataset_build.json    构建报告（清洗产物侧）

设计纪律：
- manifest 里每个数字都由本次构建实测写入，**没有占位**
- round-trip 不通过直接 raise（语料被静默改写 = 生产系统不可接受）
- 切分前查泄漏；同一 symbol 的样本必须同 split（否则泄漏实体而非样本）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("build_dataset")


def main() -> int:
    ap = argparse.ArgumentParser(description="构建训练/分析可直接消费的数据集制品")
    ap.add_argument("--input", required=True, help="清洗产物 cleaned.jsonl")
    ap.add_argument("--name", required=True, help="数据集名（datasets/<name>/）")
    ap.add_argument("--tokenizer", default="Qwen/Qwen2.5-0.5B-Instruct")
    ap.add_argument("--out-root", default=str(ROOT / "datasets"))
    ap.add_argument("--val-ratio", type=float, default=0.1)
    ap.add_argument("--test-ratio", type=float, default=0.1)
    ap.add_argument(
        "--split-key",
        default=None,
        help="分层切分键（如 finance的 symbol）—— 同一键的样本必须同 split",
    )
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument(
        "--pack-block-size",
        type=int,
        default=0,
        help=">0 时产出 block 级 shard（定长，可直接 collate 训练）；"
        "0 = 保持样本级（保留 id/text 便于分析）",
    )
    ap.add_argument("--shard-rows", type=int, default=2000)
    ap.add_argument("--funnel-config", default="", help="漏斗配置文件（写进 manifest 谱系）")
    ap.add_argument("--license", default="unknown")
    ap.add_argument("--notes", default="")
    ap.add_argument("--limit", type=int, default=0, help=">0 时只取前 N 条（调试用）")
    args = ap.parse_args()

    src = pathlib.Path(args.input)
    if not src.exists():
        logger.error("输入不存在: %s", src)
        return 1

    from transformers import AutoTokenizer

    from mm_curation.dataset import DatasetBuilder, iter_jsonl

    logger.info("加载 tokenizer: %s", args.tokenizer)
    tok = AutoTokenizer.from_pretrained(args.tokenizer)

    # 漏斗算子清单：从 config 现读，不手抄
    ops: list[str] = []
    if args.funnel_config:
        cfg = pathlib.Path(args.funnel_config)
        if cfg.exists():
            raw = yaml_safe_load(cfg.read_text(encoding="utf-8"))
            ops = [str(o["op"]) for o in raw.get("operators", []) if isinstance(o, dict)]
            logger.info("漏斗算子（从 config 现读）: %s", ops)

    out_dir = pathlib.Path(args.out_root) / args.name
    builder = DatasetBuilder(
        args.name,
        tok,
        out_dir=out_dir,
        tokenizer_name=args.tokenizer,
        source_files=[str(src)],
        funnel_config=args.funnel_config,
        funnel_ops=ops,
        license_=args.license,
        notes=args.notes,
        shard_rows=args.shard_rows,
        val_ratio=args.val_ratio,
        test_ratio=args.test_ratio,
        split_key=args.split_key,
        max_tokens_per_sample=args.max_tokens or None,
        pack_block_size=args.pack_block_size or None,
    )

    t0 = time.time()
    n_in = 0
    for rec in iter_jsonl(src):
        rec.setdefault("source", src.name)
        builder.add(rec)
        n_in += 1
        if n_in % 500 == 0:
            logger.info(
                "  读入 %d 条，已 tokenize %d 条 (%.1fs)", n_in, builder.n_samples, time.time() - t0
            )
        if args.limit and n_in >= args.limit:
            break

    builder.finalize()
    man = builder.manifest
    assert man is not None

    dt = time.time() - t0
    logger.info("")
    logger.info("=== 构建完成 (%.1fs) ===", dt)
    logger.info("输出目录   %s", out_dir)
    logger.info("样本       %d（读入 %d，空/无效 %d）", man.n_samples, n_in, n_in - man.n_samples)
    logger.info("token      %d  (%.3f 字符/token)", man.n_tokens, man.compression_chars_per_token)
    logger.info("切分       %s", man.splits)
    if man.pack_block_size:
        logger.info("packing   block_size=%d → %s", man.pack_block_size, man.n_blocks)
    logger.info("shard      %d 个（含校验和）", man.n_shards)
    logger.info("截断       %d 条 @ max_tokens=%s", man.n_truncated, man.max_tokens_per_sample)
    lk = man.leakage_check
    logger.info(
        "泄漏检查md5 %d / minhash %d → %s",
        len(lk.get("md5_leaks", [])),
        len(lk.get("minhash_leaks", [])),
        "干净" if lk.get("clean") else "**有泄漏**",
    )
    logger.info("round-trip 抽检 %d 条，全部逐字节还原", builder._rt_checked)
    logger.info("")
    logger.info("manifest 里 training_runs 为空 —— 必须真训一次再回填，**不许占位**")

    rep = ROOT / "data" / "reports" / "dataset_build.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(
        json.dumps(
            {
                "manifest": man.to_dict(),
                "elapsed_s": round(dt, 2),
                "n_read": n_in,
                "throughput_docs_per_s": round(n_in / max(dt, 1e-9), 1),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("构建报告   %s", rep)
    return 0


def yaml_safe_load(text: str):
    import yaml

    return yaml.safe_load(text) or {}


if __name__ == "__main__":
    raise SystemExit(main())
