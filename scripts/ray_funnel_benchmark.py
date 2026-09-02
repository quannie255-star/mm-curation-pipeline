"""γ3 验收：本地 vs Ray 双运行时漏斗基准（10 万档）。

同一份 config（configs/text_funnel.yaml，剔除 perplexity——GPU 算子的 Ray
分发属后续），分别在 LocalSequentialExecutor 与 RayDistributedExecutor 上跑：
- 耗时对比（含 ray.init 启动与每级 materialize 开销）
- 等价性（γ3 口径）：kept 集按 id 相等 + 每级 StageStat 数字相等 + 逐 id 分数相等
- 行序不承诺（ray 不保序）；去重簇代表选择依赖输入序，若集合出现差异会在
  报告中如实呈现

用法：python -X utf8 scripts/ray_funnel_benchmark.py [--n 100000]
产物：data/reports/ray_funnel_benchmark.{json,md}
"""

from __future__ import annotations

import argparse
import json
import logging
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


def load_samples(n: int) -> list[Sample]:
    rows = CONFIG and Path(
        yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["dataset"]["raw_jsonl"]
    )
    lines = rows.read_text(encoding="utf-8").split("\n")
    out, i = [], 0
    for ln in lines:
        if len(out) >= n:
            break
        if not ln.strip():
            continue
        out.append(Sample(id=f"doc{i:06d}", text=json.loads(ln)["text"]))
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    samples = load_samples(args.n)
    cfg = cpu_config()
    ops = [spec.build() for spec in cfg.operators]
    logging.info("语料 %s 篇，%s 级 CPU 算子（%s）", len(samples), len(ops), [o.name for o in ops])

    t = time.perf_counter()
    local = LocalSequentialExecutor().run(ops, samples)
    t_local = time.perf_counter() - t
    logging.info("local %.1fs，kept %s", t_local, len(local.kept))

    from curation_eval import RayDistributedExecutor

    t = time.perf_counter()
    ray_exe = RayDistributedExecutor(num_cpus=8, object_store_memory=2_000_000_000)
    t_init = time.perf_counter() - t
    t = time.perf_counter()
    ray_res = ray_exe.run(ops, samples)
    t_ray = time.perf_counter() - t
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
    }
    logging.info("等价性: %s", verdict)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.with_suffix(".json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ok = verdict["kept_ids_equal"] and verdict["stats_equal"] and score_bad == 0
    md = [
        "# γ3 验收：本地 vs Ray 双运行时漏斗基准",
        "",
        f"- 语料 {len(samples):,} 篇（维基 zh），CPU 算子 {len(ops)} 级"
        f"（{' → '.join(o.name for o in ops)}）；perplexity（GPU）本期不进 Ray",
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
    REPORT.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    logging.info("报告: %s.{json,md}", REPORT)


if __name__ == "__main__":
    main()
