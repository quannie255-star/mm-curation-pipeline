"""补强验收：图像漏斗 local vs Ray 双运行时等价性基准（全量 2106）。

γ3 只验证了文本漏斗（configs/text_funnel.yaml）；本脚本对图像漏斗
（configs/pipeline.example.yaml，剔除 clip_alignment / semantic_dedup 两个
GPU 算子）做同款双跑。等价性口径在 γ3 三口径之上新增第四条：
dedup 标记（duplicate_of 映射）逐 id 相等——簇代表选择正是本次靶子
（γ3 教训：批量算子「先到先保留」依赖输入序；框架层修复见
run_batch_mixed_modality 的 id 规范化排序，本基准即该修复的实测验收）。

用法：python -X utf8 scripts/ray_image_funnel_benchmark.py
产物：data/reports/ray_image_funnel_benchmark.{json,md}
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from curation_eval import LocalSequentialExecutor, Sample  # noqa: E402

from mm_curation.pipeline.config import PipelineConfig  # noqa: E402
from ray_funnel_benchmark import stage_diff  # noqa: E402

CONFIG = Path("configs/pipeline.example.yaml")
RAW_JSONL = Path("data/interim/contaminated/samples.jsonl")
REPORT = Path("data/reports/ray_image_funnel_benchmark")
GPU_OPS = {"clip_alignment", "semantic_dedup"}  # 本期不进 Ray（同 γ3 对 perplexity 的处理）


def load_samples() -> list[Sample]:
    """Sample.from_dict 装载：v1 caption 键永久兼容（schema.py），无需特判。"""
    return [
        Sample.from_dict(json.loads(ln))
        for ln in RAW_JSONL.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def cpu_config() -> PipelineConfig:
    cfg = PipelineConfig.from_yaml(CONFIG)
    cfg.operators = [s for s in cfg.operators if s.op not in GPU_OPS]
    return cfg


def dedup_marks(result) -> dict[str, dict]:
    """全量 dedup 标记（kept 簇代表无标记，标记在被丢弃的重复样本上）。"""
    out: dict[str, dict] = {}
    for s in result.kept:
        m = {k: v for k, v in s.meta.items() if k.startswith("dedup:")}
        if m:
            out[s.id] = m
    for _, s in result.dropped:
        m = {k: v for k, v in s.meta.items() if k.startswith("dedup:")}
        if m:
            out[s.id] = m
    return out


def score_marks(samples: list[Sample]) -> dict[str, dict]:
    return {s.id: {k: v for k, v in s.meta.items() if k.startswith("score:")} for s in samples}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    samples = load_samples()
    cfg = cpu_config()
    ops = [spec.build() for spec in cfg.operators]
    names = [o.name for o in ops]
    logging.info("图像语料 %s 条，%s 级 CPU 算子（%s）", len(samples), len(ops), names)

    t = time.perf_counter()
    local = LocalSequentialExecutor().run(ops, samples)
    t_local = time.perf_counter() - t
    logging.info("local %.1fs，kept %s", t_local, len(local.kept))

    from curation_eval import RayDistributedExecutor

    t = time.perf_counter()
    # 2GB object store 在本机内存紧张时会 init 失败（可用内存为负）——降到 1GB
    ray_exe = RayDistributedExecutor(num_cpus=8, object_store_memory=1_000_000_000)
    t_init = time.perf_counter() - t
    t = time.perf_counter()
    ray_res = ray_exe.run(ops, samples)
    t_ray = time.perf_counter() - t
    logging.info("ray %.1fs（init %.1fs），kept %s", t_ray, t_init, len(ray_res.kept))

    # 口径 4：全量 dedup 标记（含被丢样本）——簇代表换了谁必须看得见
    local_dd, ray_dd = dedup_marks(local), dedup_marks(ray_res)
    dd_bad = sorted(
        f"{sid}: {local_dd.get(sid)} != {ray_dd.get(sid)}"
        for sid in local_dd.keys() | ray_dd.keys()
        if local_dd.get(sid) != ray_dd.get(sid)
    )

    # 口径 3：逐 id 分数（与 γ3 同：以全量输入为参照系比对 ray kept）
    local_scores = score_marks(samples)
    score_bad = sum(
        1
        for s in ray_res.kept
        if any(local_scores[s.id].get(k) != v for k, v in s.meta.items() if k.startswith("score:"))
    )

    verdict = {
        "kept_ids_equal": {s.id for s in local.kept} == {s.id for s in ray_res.kept},
        "kept_n_local": len(local.kept),
        "kept_n_ray": len(ray_res.kept),
        "stats_equal": not stage_diff(local.stats, ray_res.stats),
        "stats_diff": stage_diff(local.stats, ray_res.stats),
        "score_mismatch": score_bad,
        "dedup_marks_equal": not dd_bad,
        "dedup_marks_n": len(local_dd),
        "dedup_marks_diff": dd_bad[:20],
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
    ok = (
        verdict["kept_ids_equal"]
        and verdict["stats_equal"]
        and score_bad == 0
        and verdict["dedup_marks_equal"]
    )
    md = [
        "# 补强验收：图像漏斗 local vs Ray 双运行时等价性",
        "",
        f"- 语料 {len(samples):,} 条（contaminated 全量，含注入污染），"
        f"CPU 算子 {len(ops)} 级（{' → '.join(o.name for o in ops)}）；"
        "clip_alignment / semantic_dedup（GPU）本期不进 Ray",
        "",
        "| 运行时 | 耗时 | kept |",
        "|---|---|---|",
        f"| local 串行 | {t_local:.1f}s | {verdict['kept_n_local']:,} |",
        f"| ray（8 CPU，含 init {t_init:.1f}s） | {t_ray:.1f}s | {verdict['kept_n_ray']:,} |",
        "",
        f"- 等价性（γ3 三口径 + dedup 标记）：kept 集 id 相等 = `{verdict['kept_ids_equal']}`；"
        f"每级 StageStat 相等 = `{verdict['stats_equal']}`；逐 id 分数不一致 = {score_bad} 条；"
        f"dedup 标记（{len(local_dd)} 条）相等 = `{verdict['dedup_marks_equal']}`",
        f"- 结论：**{'✅ 双运行时等价（簇代表确定）' if ok else '❌ 等价性未通过（见上）'}**；"
        "行序不承诺（ray 不保序），集合口径等价",
        "- dedup 标记口径是本次靶子：批量算子「先到先保留」依赖输入序，"
        "框架层修复（run_batch_mixed_modality 执行前按 id 规范化排序）在图像模态的实测验收",
    ]
    REPORT.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    logging.info("报告: %s.{json,md}", REPORT)


if __name__ == "__main__":
    main()
