#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""标定图像检测算子（wm_nsfw_cnn）在**干净真实数据**上的误杀率。

背景：本脚本的数值直接决定「能不能把 wm_nsfw_cnn 接入默认配置」。此前该算子
存在但不在 configs/pipeline.example.yaml 里，于是 watermark 类污染召回为 0；
把它接进来能救回召回，但**必须同时报出它在干净数据上误杀多少**——否则就是
「用误杀换召回」，不能对外说成能力提升。

为什么必须大样本（这是本脚本存在的理由）：在 30 条的小样本上实测误杀
3/30 = 10%，看起来比文档声称的 1.0% 高一个量级；在 600 条上实测是
7/600 = 1.17%，与文档同量级。**小样本比率会给出错误结论**——两者差 8 倍。

四项判据，每项都对应一种「看起来在拦、其实没拦」的失败模式：
  ① 干净集必须**真的干净**：只读原始样本，一个污染器都不施加。
     反例：复用注入臂的「阴性对照」当干净集 —— 那是 v1 报出召回 100% 的原因。
  ② 分子分母口径分离：误杀率 = 被该算子丢弃的干净样本 / **全部**干净样本。
     绝不只统计「被丢弃的那些」，那是恒真判据。
  ③ 报score 分位数，不只报「超阈比例」。分布若是双峰（大量贴近 1.0 + 少量
     贴近 0），说明阈值落在两峰之间的低密度区，判据才站得住。
  ④ **反向还原**：阈值单调调高，被丢样本必须随之变少 —— 证明「丢弃」确由
     该算子判定，而不是别的机制顺带丢的。没有这一步，①~③ 全可能是假绿。

⚠️ 算子API 的三个坑（都踩过，别再猜）：
   - `WmNsfwCnnOp` 是 **BatchOperator**（批量推理），**没有 score() 方法**。
   - 分数写在 `meta["score:wm_nsfw_cnn"]`，**带 `score:` 前缀**。
   - `available_operator_metas()` 返回 **dict**（不是 list）；
     `get_operator_meta(name)` 取元数据，`get_operator` **不存在**。

用法：
    python -X utf8 scripts/calibrate_image_detector_fp.py
    python -X utf8 scripts/calibrate_image_detector_fp.py --n 1200
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "packages/curation-eval/src")

from curation_eval import get_operator_meta  # noqa: E402

import mm_curation.operators  # noqa: E402,F401  导入即触发算子注册
from mm_curation.pipeline import OperatorSpec, PipelineConfig, run_funnel  # noqa: E402

DEFAULT_OP = "wm_nsfw_cnn"
#: 阈值扫描点，用于判据 ④「反向还原」。必须递减覆盖到低阈值，
#: 只测一个点无法证明丢弃确由该算子判定。
THRESHOLDS = (0.30, 0.20, 0.10, 0.05, 0.02)


def to_samples(rows: list[dict]) -> list:
    from mm_curation.operators.base import Sample

    return [Sample.from_dict(d) for d in rows]


def load_clean(n: int, path: str = "data/raw/samples.jsonl") -> list[dict]:
    """只取原始样本，**不施加任何污染器** —— 判据①：这一臂必须真的干净。"""
    raw = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").split("\n")
        if line.strip()
    ][:n]
    out: list[dict] = []
    for d in raw:
        d.setdefault("text", d.get("caption", ""))
        d.setdefault("modality", "image_caption")
        out.append(d)
    return out


def with_op(cfg: PipelineConfig, op: str, params: dict, tag: str) -> PipelineConfig:
    """在**内存里**给配置追加算子 —— 本脚本不写任何 config 文件。"""
    existing = {o.op for o in cfg.operators}
    if op in existing:
        return dataclasses.replace(cfg, name=f"{cfg.name}_{tag}")
    spec = OperatorSpec(op=op, params=params)
    return dataclasses.replace(cfg, name=f"{cfg.name}_{tag}", operators=[*cfg.operators, spec])


def dropped_by_op(baseline: list[dict], cfg: PipelineConfig) -> dict[str, int]:
    """返回 {算子名: 丢弃条数}。分母统一由调用方给出，不在这里藏。"""
    result = run_funnel(to_samples(baseline), cfg)
    counts: dict[str, int] = {}
    for op_name, _s in result.dropped:
        counts[op_name] = counts.get(op_name, 0) + 1
    return counts


def raw_scores(rows: list[dict], op_name: str) -> list[float]:
    """一次批量推理拿全部分数（逐条调 run_batch 会重复推理 N 遍）。

    判据③的输入。分数从 `meta["score:<算子名>"]` 读—— 带 `score:` 前缀。
    """
    from mm_curation.operators.base import Sample

    # 走 config 的 build() 路径拿实例，避免各处import 算子模块
    op_obj = OperatorSpec(op=op_name, params={}).build()

    samples = [Sample.from_dict(d) for d in rows]
    op_obj.run_batch(samples)
    key = f"score:{op_name}"
    out: list[float] = []
    for s in samples:
        v = s.meta.get(key)
        if v is not None:
            out.append(float(v))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--n", type=int, default=600, help="干净样本条数（小样本会给出错误误杀率，默认 600）"
    )
    ap.add_argument("--op", default=DEFAULT_OP)
    ap.add_argument("--config", default="configs/pipeline.example.yaml")
    args = ap.parse_args()

    rows = load_clean(args.n)
    cfg = PipelineConfig.from_yaml(args.config)
    meta = get_operator_meta(args.op)

    print("=" * 72)
    print(f"标定 {args.op} 在干净真实数据上的误杀率")
    print("=" * 72)
    print(f"干净集 = 前 {len(rows)} 条原始样本（未过任何污染器）")
    print(
        f"算子元数据：cost={meta.cost_class.name} "
        f"modalities={sorted(meta.modalities)} "
        f"required={sorted(meta.required_fields)}"
    )
    if "image_caption" not in meta.modalities:
        print(
            "★ 该算子模态不含 image_caption，接进本数据集不会评——"
            "run_funnel 会对全不相交直接抛 ValueError"
        )
        return 1

    print()
    print("① 基线：当前配置的干净集丢弃（不含该算子）")
    base_counts = dropped_by_op(rows, cfg)
    base_any = sum(base_counts.values())
    print(f"   分母 = {len(rows)}")
    print(f"   分子 = {base_any} = {base_any / len(rows):.2%}")
    print(f"   各算子：{base_counts}")

    print()
    print("② 接入该算子后的干净集丢弃")
    cfg_op = with_op(cfg, args.op, {}, "with_op")
    op_counts = dropped_by_op(rows, cfg_op)
    op_any = sum(op_counts.values())
    op_fp = op_counts.get(args.op, 0)
    print(f"   分母 = {len(rows)}，被丢弃 {op_any} = {op_any / len(rows):.2%}")
    print(f"   各算子：{op_counts}")
    print(f"   ★ {args.op} 单独误杀 = {op_fp}/{len(rows)} = {op_fp / len(rows):.2%}")

    print()
    print("③ score 分位数（判据③：分布要落在阈值两侧的低密度区）")
    scores = raw_scores(rows, args.op)
    if not scores:
        print("   ★ 取不到 score，无法验证 → 判据不成立，直接判失败")
        return 1
    scores.sort()

    def q(p: float) -> float:
        return scores[min(int(len(scores) * p), len(scores) - 1)]

    print(f"   有效 score {len(scores)}/{len(rows)}（缺失 {len(rows) - len(scores)}）")
    print(
        f"   min={q(0):.4f} p25={q(0.25):.4f} p50={q(0.5):.4f} "
        f"p75={q(0.75):.4f} p95={q(0.95):.4f} p99={q(0.99):.4f}"
    )
    thr = THRESHOLDS[0]
    n_by_score = sum(1 for v in scores if v < thr)
    print(f"   判脏线 score < {thr}")
    print(
        f"   按分数应误杀 {n_by_score}/{len(scores)} = "
        f"{n_by_score / len(scores):.2%}"
        f"   漏斗实测 {op_fp}/{len(rows)} = {op_fp / len(rows):.2%}"
    )
    if abs(n_by_score - op_fp) > max(2, 0.02 * len(scores)):
        print("   ★ 分数口径与漏斗口径不一致 → 有样本在到达该算子前已被别的算子丢掉。")
        print("     这不是「它更准」，而是「它没看到那些样本」—— 报数时必须说清楚。")

    print()
    print("④ 反向还原（判据④：阈值越高，误杀必须越少）")
    print(f"   {'阈值':>6}{'应误杀':>9}{'误杀率':>10}")
    print("   " + "-" * 27)
    prev = None
    mono = True
    for t in THRESHOLDS:
        n = sum(1 for v in scores if v < t)
        print(f"   {t:>6}{n:>9}{n / len(scores):>10.2%}")
        if prev is not None and n > prev:
            mono = False
        prev = n
    if not mono:
        print("   ★★ 非单调 → 有别的机制在丢弃，本脚本结论全部作废，必须继续查。")
        return 1
    print("   ✅ 单调 → 丢弃确由该算子判定")

    print()
    print("=" * 72)
    print("结论")
    print("=" * 72)
    print(f"   接入前干净集丢弃率= {base_any / len(rows):.2%}")
    print(f"   接入后干净集丢弃率  = {op_any / len(rows):.2%}")
    print(
        f"   净增误杀            = {op_fp / len(rows):.2%}"
        f"（{base_counts.get(args.op, 0)} → {op_fp} 条）"
    )
    print()
    print("   对外表述纪律：补算子换召回**必须同时报净增误杀**，")
    print("   单独说「召回提升 x%」而隐去误杀变化 = 用误杀换召回，不算能力提升。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
