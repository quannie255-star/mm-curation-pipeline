"""广覆盖清洗驱动（测试项目「普适性」——跨模态/跨特征小批量跑通）。

目标（对齐用户「覆盖的全一点，量不用很大，关键测普适性」）：
在**一个** `coverage run --source=src/mm_curation` 会话里，把各模态/特征的
清洗代码路径都跑到，从而：
  ① 输出每个模态的清洗效果（入/出量、逐算子丢弃数、耗时）；
  ② 配合 coverage json + summarize_coverage.py 盘点「这次到底用到了哪些代码」，
     暴露产品化覆盖缺口（哪些模态/算子在本机 CPU 环境跑不到、为什么）。

覆盖的模态/特征（量都很小，重在「走通」而非「大规模」）：
  - text_article      : 经典 AG News 子集（reuse classic_text 漏斗）
  - text_article(zh) : 中文新闻子集（news_zh_funnel，含 chinese_ratio/doc_length/perplexity）
  - image_caption    : COCO-CN 图文污染子集（pipeline.v3_full_coverage，
                       全图文算子含 CLIP/检测/语义去重）
  - image_contam     : 对干净图文样本跑 ContaminationPlan 图像污染生成
                       （覆盖 contamination 图像分支）
  - fhir_resource    : 合成 FHIR R4 + 合规脏注入（funnel_fhir）
  - industrial_sensor: 合成工业传感 + 故障注入（funnel_industrial）
  - normalize(V6 α)  : TextNormalizeTransformer 改写通道（text_normalize + transformer）

每段独立 try/except，单段失败不影响整体覆盖会话；结果写入
data/processed/broad_coverage/run_log.json。

用法：
    coverage run --source=src/mm_curation scripts/run_broad_coverage.py
    coverage json -o coverage_broad.json
    python -X utf8 scripts/archive/summarize_coverage.py coverage_broad.json
    python -X utf8 scripts/archive/summarize_coverage.py coverage_broad.json <out.md>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages" / "curation-eval" / "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("broad_coverage")

OUT_DIR = ROOT / "data" / "processed" / "broad_coverage"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _load_jsonl(path: Path, limit: int | None):
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                break
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _agg_drops(result) -> dict[str, int]:
    """从 FunnelResult.dropped（(算子名, 样本) 列表）聚合逐算子丢弃数。

    注意：StageStat 的字段是 `dropped`（int）而非 `n_dropped`，且批量算子
    的丢弃只落在 result.dropped，stats 聚合不可靠——直接用权威列表。
    """
    drops: dict[str, int] = {}
    for op_name, _sample in getattr(result, "dropped", []) or []:
        drops[op_name] = drops.get(op_name, 0) + 1
    return drops


def _section_text_classic(limit: int, run_log: list) -> None:
    from mm_curation.operators.base import Sample
    from mm_curation.pipeline import PipelineConfig, run_funnel

    cfg_path = ROOT / "configs" / "archive" / "classic_text.yaml"
    config = PipelineConfig.from_yaml(str(cfg_path))
    config.output_dir = OUT_DIR / "text_classic"
    rows = _load_jsonl(ROOT / "data/raw/classic/ag_news_train.jsonl", limit)
    samples = [Sample.from_dict(r) for r in rows]
    t0 = time.time()
    result = run_funnel(samples, config)
    run_log.append({
        "section": "text_classic",
        "config": str(cfg_path),
        "status": "ok",
        "n_in": len(samples),
        "n_kept": len(result.kept),
        "per_op_drops": _agg_drops(result),
        "time_s": round(time.time() - t0, 2),
    })
    log.info("text_classic: %d -> %d kept", len(samples), len(result.kept))


def _section_text_chinese(limit: int, run_log: list) -> None:
    from mm_curation.normalize.transformer import TextNormalizeTransformer
    from mm_curation.operators.base import Sample
    from mm_curation.pipeline import PipelineConfig, run_funnel

    cfg_path = ROOT / "configs" / "news_zh_funnel.yaml"
    config = PipelineConfig.from_yaml(str(cfg_path))
    config.output_dir = OUT_DIR / "text_chinese"
    rows = _load_jsonl(ROOT / "data/interim/news_full_cleaned.jsonl", limit)
    samples = [
        Sample.from_dict({
            "id": r.get("id") or f"zh{i}",
            "text": r.get("text") or "",
            "modality": "text_article",
            "meta": r.get("meta", {}),
        })
        for i, r in enumerate(rows)
        if (r.get("text") or "").strip()
    ]
    # V6 α 归一化改写通道（覆盖 transformer.py + text_normalize.py）
    norm = TextNormalizeTransformer()
    t0 = time.time()
    norm_results = [norm.transform(s) for s in samples]
    n_changed = sum(1 for r in norm_results if getattr(r, "changed", False))
    n_emptied = sum(1 for r in norm_results if r.sample is None)
    # 归一化后再次收集样本（改写后的）跑漏斗
    normed = [r.sample for r in norm_results if r.sample is not None]
    result = run_funnel(normed, config)
    run_log.append({
        "section": "text_chinese",
        "config": str(cfg_path),
        "status": "ok",
        "n_in_raw": len(samples),
        "n_after_normalize": len(normed),
        "normalize_changed": n_changed,
        "normalize_emptied": n_emptied,
        "n_kept": len(result.kept),
        "per_op_drops": _agg_drops(result),
        "time_s": round(time.time() - t0, 2),
    })
    log.info(
        "text_chinese: %d 原始 -> 归一化改写 %d / 清空 %d -> 漏斗 %d kept",
        len(samples), n_changed, n_emptied, len(result.kept),
    )


def _build_image_samples(rows: list[dict], limit: int, root: Path) -> list:
    from mm_curation.operators.base import Sample

    out = []
    for r in rows[:limit]:
        ip = r.get("image_path")
        if not ip:
            continue
        ip = str(root / ip.replace("\\", "/"))
        text = r.get("caption") or r.get("text") or ""
        labels = r.get("labels") or {}
        out.append(Sample.from_dict({
            "id": r.get("id") or f"img{len(out)}",
            "text": text,
            "image_path": ip,
            "modality": "image_caption",
            "meta": r.get("meta", {}),
            "labels": labels,
        }))
    return out


def _section_image_funnel(limit: int, run_log: list) -> None:
    from mm_curation.pipeline import PipelineConfig, run_funnel

    cfg_path = ROOT / "configs" / "pipeline.v3_full_coverage.yaml"
    config = PipelineConfig.from_yaml(str(cfg_path))
    config.output_dir = OUT_DIR / "image_funnel"
    rows = _load_jsonl(ROOT / "data/interim/contaminated/samples.jsonl", None)
    samples = _build_image_samples(rows, limit, ROOT)
    t0 = time.time()
    result = run_funnel(samples, config)
    run_log.append({
        "section": "image_funnel",
        "config": str(cfg_path),
        "status": "ok",
        "n_in": len(samples),
        "n_kept": len(result.kept),
        "per_op_drops": _agg_drops(result),
        "time_s": round(time.time() - t0, 2),
    })
    log.info("image_funnel: %d -> %d kept", len(samples), len(result.kept))


def _section_image_contamination(limit: int, run_log: list) -> None:
    from curation_eval import ContaminationPlan

    from mm_curation.operators.base import Sample

    rows = _load_jsonl(ROOT / "data/interim/contaminated/samples.jsonl", None)
    # 取干净样本（无 labels）作为基底，剥离污染标记
    clean = [r for r in rows if not r.get("labels")][:limit]
    corpus = [
        Sample.from_dict({
            "id": r.get("id") or f"c{idx}",
            "text": r.get("caption") or r.get("text") or "",
            "image_path": str(ROOT / r["image_path"].replace("\\", "/")),
            "modality": "image_caption",
            "meta": r.get("meta", {}),
            "labels": {},
        })
        for idx, r in enumerate(clean)
    ]
    kinds = {
        "exact_duplicate": 0.15,
        "blur": 0.15,
        "low_resolution": 0.15,
        "mismatched_pair": 0.20,
        "watermark": 0.10,
        "mojibake": 0.15,
        "truncate_text": 0.10,
    }
    tmp = OUT_DIR / "image_contam_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    mixed, manifest = ContaminationPlan(
        inject_rate=0.6, seed=7, kinds=kinds
    ).run(corpus, tmp)
    # 顺带把污染后的图文样本喂进图文漏斗，测真实召回（与 fhir/industrial 对齐）
    from mm_curation.operators.base import Sample
    from mm_curation.pipeline import PipelineConfig, run_funnel

    fcfg = PipelineConfig.from_yaml(str(ROOT / "configs" / "pipeline.v3_full_coverage.yaml"))
    fcfg.output_dir = OUT_DIR / "image_contam_funnel"
    mixed_mm = [Sample.from_dict(s.to_dict()) for s in mixed]
    res = run_funnel(mixed_mm, fcfg)
    drops = _agg_drops(res)
    caught = {s.id for _, s in res.dropped if s.labels}
    n_dirty = sum(1 for s in mixed_mm if s.labels)
    recall = round(len(caught) / n_dirty, 4) if n_dirty else 0.0
    run_log.append({
        "section": "image_contamination",
        "status": "ok",
        "n_clean": len(corpus),
        "n_mixed": len(mixed),
        "n_injected": manifest.get("n_injected"),
        "counts": manifest.get("counts"),
        "funnel_n_in": len(mixed_mm),
        "funnel_n_kept": len(res.kept),
        "funnel_per_op_drops": drops,
        "n_dirty": n_dirty,
        "recall": recall,
        "time_s": round(time.time() - t0, 2),
    })
    log.info(
        "image_contamination: %d 干净 -> 注入 %d；漏斗召回 %.1f%%（%d/%d）",
        len(corpus), manifest.get("n_injected"), recall * 100, len(caught), n_dirty,
    )


def _section_fhir(scale: float, run_log: list) -> None:
    from curation_eval import ContaminationPlan

    from mm_curation.data.fhir_synth import generate_corpus
    from mm_curation.operators.base import Sample
    from mm_curation.pipeline import PipelineConfig, run_funnel

    KINDS = {
        "fhir_phi_leak": 1.0, "fhir_code_invalid": 1.0, "fhir_time_inverted": 1.0,
        "fhir_ref_broken": 1.0, "fhir_unit_off": 1.0,
    }
    cfg_path = ROOT / "configs" / "funnel_fhir.yaml"
    config = PipelineConfig.from_yaml(str(cfg_path))
    config.output_dir = OUT_DIR / "fhir"
    corpus = generate_corpus(seed=42, scale=scale)
    plan = ContaminationPlan(inject_rate=0.3, seed=42, kinds=dict(KINDS))
    mixed, manifest = plan.run(corpus, OUT_DIR / "fhir_tmp")
    samples = [Sample.from_dict(s.to_dict()) for s in mixed]
    t0 = time.time()
    result = run_funnel(samples, config)
    run_log.append({
        "section": "fhir",
        "config": str(cfg_path),
        "status": "ok",
        "n_in": len(samples),
        "n_kept": len(result.kept),
        "n_injected": manifest.get("n_injected"),
        "per_op_drops": _agg_drops(result),
        "time_s": round(time.time() - t0, 2),
    })
    log.info(
        "fhir: %d -> %d kept (注入 %d)", len(samples), len(result.kept), manifest.get("n_injected")
    )


def _section_industrial(scale: float, run_log: list) -> None:
    from curation_eval import ContaminationPlan

    from mm_curation.data.sensor_synth import generate_corpus
    from mm_curation.operators.base import Sample
    from mm_curation.pipeline import PipelineConfig, run_funnel

    KINDS = {
        "sensor_cal_offset": 1.0, "sensor_flatline": 1.0, "sensor_out_of_range": 1.0,
        "sensor_unit_swap": 1.0, "sensor_unplanned_silence": 1.0,
    }
    cfg_path = ROOT / "configs" / "funnel_industrial.yaml"
    config = PipelineConfig.from_yaml(str(cfg_path))
    config.output_dir = OUT_DIR / "industrial"
    corpus = generate_corpus(seed=42, scale=scale)
    plan = ContaminationPlan(inject_rate=0.3, seed=42, kinds=dict(KINDS))
    mixed, manifest = plan.run(corpus, OUT_DIR / "industrial_tmp")
    samples = [Sample.from_dict(s.to_dict()) for s in mixed]
    t0 = time.time()
    result = run_funnel(samples, config)
    run_log.append({
        "section": "industrial",
        "config": str(cfg_path),
        "status": "ok",
        "n_in": len(samples),
        "n_kept": len(result.kept),
        "n_injected": manifest.get("n_injected"),
        "per_op_drops": _agg_drops(result),
        "time_s": round(time.time() - t0, 2),
    })
    log.info(
        "industrial: %d -> %d kept (注入 %d)",
        len(samples),
        len(result.kept),
        manifest.get("n_injected"),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text-classic-n", type=int, default=400)
    ap.add_argument("--text-chinese-n", type=int, default=150)
    ap.add_argument("--image-n", type=int, default=80)
    ap.add_argument("--image-contam-n", type=int, default=40)
    ap.add_argument("--fhir-scale", type=float, default=0.1)
    ap.add_argument("--industrial-scale", type=float, default=0.1)
    args = ap.parse_args()

    run_log: list[dict] = []
    sections = [
        ("text_classic", lambda: _section_text_classic(args.text_classic_n, run_log)),
        ("text_chinese", lambda: _section_text_chinese(args.text_chinese_n, run_log)),
        ("image_funnel", lambda: _section_image_funnel(args.image_n, run_log)),
        ("image_contamination", lambda: _section_image_contamination(args.image_contam_n, run_log)),
        ("fhir", lambda: _section_fhir(args.fhir_scale, run_log)),
        ("industrial", lambda: _section_industrial(args.industrial_scale, run_log)),
    ]
    for name, fn in sections:
        try:
            log.info("=== 运行段: %s ===", name)
            fn()
        except Exception as e:  # noqa: BLE001 - 单段失败不阻断整体覆盖
            run_log.append({"section": name, "status": "error", "error": repr(e)})
            log.exception("段 %s 失败（继续）: %s", name, e)

    out = OUT_DIR / "run_log.json"
    out.write_text(json.dumps(run_log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {out}")
    print("\n=== 广覆盖运行摘要 ===")
    for r in run_log:
        if r.get("status") == "ok":
            extra = ""
            if "n_kept" in r:
                extra = f"  {r.get('n_in')}->{r['n_kept']} kept"
            elif "n_mixed" in r:
                extra = f"  {r.get('n_clean')}->{r['n_mixed']} (注入 {r.get('n_injected')})"
            print(f"  [OK] {r['section']:<20}{extra}  {r['time_s']}s")
        else:
            print(f"  [ERR] {r['section']:<20} {r.get('error')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
