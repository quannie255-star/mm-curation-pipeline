"""γ3 验收 + κ 规模拐点：本地 vs Ray 双运行时漏斗基准。

同一份 config（configs/text_funnel.yaml，剔除 perplexity——GPU 算子的 Ray
分发属后续），分别在 LocalSequentialExecutor 与 RayDistributedExecutor 上跑：
- 耗时对比（含 ray.init 启动与每级 materialize 开销）
- 等价性（γ3 口径）：kept 集按 id 相等 + 每级 StageStat 数字相等 + 逐 id 分数相等
- 行序不承诺（ray 不保序）；去重簇代表选择依赖输入序，若集合出现差异会在
  报告中如实呈现

κ 扩展（2026-09-15）：--corpus 指定独立语料（扩量语料勿混入 β 基线文件）、
--tile 语料平铺膨胀（副本 id 加 -r{k} 后缀 + 相邻句交换扰动，测吞吐不测质量）、
--out 报告名（默认保持 γ3 路径兼容）。规模梯次每档一次调用，报告按档落盘。

用法：python -X utf8 scripts/ray_funnel_benchmark.py [--n 100000] [--tile 1]
      [--corpus data/raw/text_corpus_1m.jsonl] [--out scale_crossover/n100k]
产物：data/reports/<out>.{json,md}
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402
from curation_eval import LocalSequentialExecutor, Sample  # noqa: E402

from mm_curation.pipeline.config import PipelineConfig  # noqa: E402

CONFIG = Path("configs/text_funnel.yaml")
REPORT = Path("data/reports/ray_funnel_benchmark")
GPU_OPS = {"perplexity"}  # 本期不进 Ray（GPU worker 调度属后续）

_SENT_SPLIT = re.compile(r"(?<=[。！？；])")


def _mutate(text: str) -> str:
    """平铺副本扰动：交换前两个句子，避免全量精确重复使去重行为退化。"""
    parts = _SENT_SPLIT.split(text, maxsplit=2)
    if len(parts) >= 2:
        parts[0], parts[1] = parts[1], parts[0]
    return "".join(parts)


def iter_corpus_lines(corpus: Path | None):
    """流式读语料行（1M 档 read_text 全量会撑内存），缺省回落 config。"""
    if corpus is not None:
        with open(corpus, encoding="utf-8") as f:
            yield from f
        return
    rows = Path(
        yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["dataset"]["raw_jsonl"]
    )
    with open(rows, encoding="utf-8") as f:
        yield from f


def load_samples(n: int, tile: int, corpus: Path | None) -> list[Sample]:
    out: list[Sample] = []
    i = 0
    for ln in iter_corpus_lines(corpus):
        if len(out) >= n * tile:
            break
        if not ln.strip():
            continue
        text = json.loads(ln)["text"]
        for k in range(tile):
            if len(out) >= n * tile:
                break
            doc_id = f"doc{i:06d}" if k == 0 else f"doc{i:06d}-r{k}"
            out.append(Sample(id=doc_id, text=text if k == 0 else _mutate(text)))
        i += 1
    return out


def cpu_config() -> PipelineConfig:
    cfg = PipelineConfig.from_yaml(CONFIG)
    cfg.operators = [s for s in cfg.operators if s.op not in GPU_OPS]
    return cfg


def stage_diff(a: list, b: list) -> list[str]:
    keys = ("op", "n_in", "n_out", "dropped", "skipped")
    return [
        f"{x.op}: {tuple(getattr(x, k) for k in keys)} != {tuple(getattr(y, k) for k in keys)}"
        for x, y in zip(a, b)
        if any(getattr(x, k) != getattr(y, k) for k in keys)
    ]


def run_local(ops, samples):
    t = time.perf_counter()
    res = LocalSequentialExecutor().run(ops, samples)
    return res, time.perf_counter() - t, 0.0


def run_ray(ops, samples):
    from curation_eval import RayDistributedExecutor

    t = time.perf_counter()
    ray_exe = RayDistributedExecutor(num_cpus=8, object_store_memory=2_000_000_000)
    t_init = time.perf_counter() - t
    t = time.perf_counter()
    res = ray_exe.run(ops, samples)
    return res, time.perf_counter() - t, t_init


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100000)
    parser.add_argument("--tile", type=int, default=1, help="语料平铺倍数（κ 规模实验）")
    parser.add_argument("--corpus", default=None, help="语料 jsonl 路径（缺省读 config）")
    parser.add_argument("--out", default=None, help="报告名（默认 ray_funnel_benchmark，γ3 兼容）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    corpus = Path(args.corpus) if args.corpus else None
    report = Path("data/reports") / (args.out or REPORT.name)
    samples = load_samples(args.n, args.tile, corpus)
    cfg = cpu_config()
    ops = [spec.build() for spec in cfg.operators]
    logging.info("语料 %s 篇（tile=%s，corpus=%s），%s 级 CPU 算子（%s）",
                 len(samples), args.tile, corpus or "config", len(ops), [o.name for o in ops])

    local, t_local, _ = run_local(ops, samples)
    logging.info("local %.1fs，kept %s", t_local, len(local.kept))

    ray_res, t_ray, t_init = run_ray(ops, samples)
    logging.info("ray %.1fs（init %.1fs），kept %s", t_ray, t_init, len(ray_res.kept))

    local_ids = {s.id for s in local.kept}
    ray_ids = {s.id for s in ray_res.kept}
    stats_diff = stage_diff(local.stats, ray_res.stats)
    local_scores = {
        s.id: {k: v for k, v in s.meta.items() if k.startswith("score:")} for s in samples
    }
    score_bad = 0
    for s in ray_res.kept:
        if any(local_scores[s.id].get(k) != v for k, v in s.meta.items() if k.startswith("score:")):
            score_bad += 1

    verdict = {
        "kept_ids_equal": local_ids == ray_ids,
        "kept_n_local": len(local_ids),
        "kept_n_ray": len(ray_ids),
        "stats_equal": not stats_diff,
        "stats_diff": stats_diff,
        "score_mismatch": score_bad,
        "seconds_local": round(t_local, 2),
        "seconds_ray": round(t_ray, 2),
        "seconds_ray_init": round(t_init, 2),
        "n": len(samples),
        "tile": args.tile,
        "corpus": str(corpus) if corpus else "config",
        "ops": [o.name for o in ops],
    }
    logging.info("等价性: %s", {k: v for k, v in verdict.items() if k != "stats_diff"})

    report.parent.mkdir(parents=True, exist_ok=True)
    report.with_suffix(".json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ok = verdict["kept_ids_equal"] and verdict["stats_equal"] and score_bad == 0
    md = [
        "# 双运行时漏斗基准（local vs Ray）",
        "",
        f"- 语料 {len(samples):,} 篇（tile={args.tile}，corpus={corpus or 'config'}），"
        f"CPU 算子 {len(ops)} 级（{' → '.join(o.name for o in ops)}）；perplexity（GPU）不进 Ray",
        "",
        "| 运行时 | 耗时 | kept |",
        "|---|---|---|",
        f"| local 串行 | {t_local:.1f}s | {len(local_ids):,} |",
        f"| ray（8 CPU，含 init {t_init:.1f}s） | {t_ray:.1f}s | {len(ray_ids):,} |",
        "",
        f"- 等价性（γ3 口径）：kept 集 id 相等 = `{verdict['kept_ids_equal']}`；"
        f"每级 StageStat 相等 = `{verdict['stats_equal']}`；"
        f"逐 id 分数不一致 = {score_bad} 条",
        f"- 结论：**{'✅ 双运行时等价' if ok else '❌ 等价性未通过（见上）'}**；"
        "行序不承诺（ray 不保序），集合口径等价",
        "- 单机小规模下 Ray 不追求快于本地（调度/序列化开销换横向扩展能力），"
        "价值在多机水平扩展与算子图复用",
    ]
    report.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    logging.info("报告: %s.{json,md}", report)


if __name__ == "__main__":
    main()
