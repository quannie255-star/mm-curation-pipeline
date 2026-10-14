"""Q4 诊断：300k 档28.6% 误合并是级联、阈值还是模板化？

两份外部评审都把这条列为第二优先级，且都建议「先诊断再改」。
本脚本做三件低成本诊断，不重跑清洗、不碰真实数据源：
  1. 簇大小分布直方图（巨型簇 → 级联/模板化；中等簇为主 → 阈值）
  2. 桥边分析（每次 union 的 Jaccard，统计是否贴着阈值）
  3. 桥接率：与「簇代表直接相似」的比例对比（区分链式合并 vs 直接合并）

用法：python scripts/diag_dedup_cascade.py [--n 60000]
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "packages/curation-eval/src")

import numpy as np  # noqa: E402
from curation_eval.schema import Sample  # noqa: E402

from mm_curation.dedup_fast import _find, _signature  # noqa: E402

_PRIME = (1 << 31) - 1


def load_samples(n: int) -> list[Sample]:
    """从真实语料取样本（只读，不写回）。

    ⚠️ 语料选择会直接影响「误合并」判读：DPO/synthetic 类数据本身就是
    **成对构造的近重复**（同一 prompt 两个 completion），落在它上面测出的
    合并率必然虚高。默认用真实新闻语料（与text_dedup_benchmark 同源）。
    """
    import itertools

    # 按优先级选真实语料；都是 JSONL，每行含 text/caption
    candidates = [
        pathlib.Path("data/raw/news_corpus.jsonl"),
        pathlib.Path("data/raw/finance_news/news_corpus.jsonl"),
    ]
    files = [f for f in candidates if f.exists()]
    if not files:
        files = sorted(pathlib.Path("data/raw").rglob("*.jsonl"))[:3]
    out: list[Sample] = []
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for line in itertools.islice(fh, n):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = rec.get("text") or rec.get("caption") or rec.get("content") or ""
                if not text:
                    continue
                out.append(
                    Sample(
                        id=str(rec.get("id", rec.get("doc_id", len(out)))),
                        text=text,
                        modality="text_article",
                    )
                )
                if len(out) >= n:
                    return out
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60000)
    ap.add_argument("--threshold", type=float, default=0.7)
    ap.add_argument("--bands", type=int, default=8)
    ap.add_argument("--num-perm", type=int, default=80)
    ap.add_argument("--max-bucket", type=int, default=2000)
    args = ap.parse_args()

    samples = load_samples(args.n)
    n = len(samples)
    print(
        f"样本 {n} 条 | 阈值 {args.threshold} | {args.num_perm}perm/{args.bands}band "
        f"| max_bucket {args.max_bucket}"
    )
    if n < 1000:
        print("语料不足，退出")
        return 1

    rng = np.random.default_rng(42)
    a = rng.integers(1, 1 << 31, size=args.num_perm, dtype=np.uint64)
    b = rng.integers(0, 1 << 31, size=args.num_perm, dtype=np.uint64)
    sigs = np.stack([_signature(s.text, 600, a, b) for s in samples])
    rows = args.num_perm // args.bands

    # ── 复刻 dedup_texts 的合并过程，但**记录每条桥边** ──
    parent = list(range(n))
    est: dict[int, float] = {}
    edge_j: list[float] = []  # 每条union 边的 Jaccard
    merged_pairs: list[tuple[int, int]] = []

    first_seen: dict[bytes, int] = {}
    for i in range(n):
        key = sigs[i].tobytes()
        if key in first_seen:
            parent[_find(parent, i)] = _find(parent, first_seen[key])
            est[i] = 1.0
        else:
            first_seen[key] = i

    buckets: dict[tuple, list[int]] = collections.defaultdict(list)
    for i in range(n):
        s = sigs[i]
        for band in range(args.bands):
            buckets[(band, s[band * rows : (band + 1) * rows].tobytes())].append(i)

    n_skipped_bucket = 0
    for members in buckets.values():
        if len(members) < 2:
            continue
        if len(members) > args.max_bucket:
            n_skipped_bucket += 1
            continue
        for m in range(len(members)):
            for k in range(m + 1, len(members)):
                x, y = members[k], members[m]
                if _find(parent, x) == _find(parent, y):
                    continue
                j = float(np.mean(sigs[x] == sigs[y]))
                if j < args.threshold:
                    continue
                rx, ry = _find(parent, x), _find(parent, y)
                if rx == ry:
                    continue
                edge_j.append(j)
                merged_pairs.append((x, y))
                parent[max(rx, ry)] = min(rx, ry)
                est[max(x, y)] = max(est.get(max(x, y), 0.0), j)

    # ── 诊断 1：簇大小分布 ──
    clusters: dict[int, list[int]] = collections.defaultdict(list)
    for i in range(n):
        clusters[_find(parent, i)].append(i)
    sizes = sorted((len(v) for v in clusters.values()), reverse=True)
    total_merged = sum(s for s in sizes if s > 1)
    print("\n=== 诊断 1：簇大小分布 ===")
    print(f"  簇总数 {len(sizes)} | 被合并样本 {total_merged} ({total_merged / n * 100:.2f}%)")
    print(
        f"  最大簇 {sizes[0]} | 前 10 大簇合计 {sum(sizes[:10])} "
        f"（占被合并 {sum(sizes[:10]) / max(total_merged, 1) * 100:.1f}%）"
    )
    hist = collections.Counter(min(s, 11) for s in sizes)
    print("  分布（11=≥11）：", {k: hist[k] for k in sorted(hist)})
    big = [s for s in sizes if s > 100]
    print(
        f"  >100 的簇:{len(big)} 个，合计 {sum(big)} 样本"
        f"（占被合并 {sum(big) / max(total_merged, 1) * 100:.1f}%）"
    )

    # ── 诊断 2：桥边 Jaccard 分布（贴阈值 = 阈值敏感）──
    print(f"\n=== 诊断 2：桥边 Jaccard（{len(edge_j)} 条union 边）===")
    if edge_j:
        ej = np.array(edge_j)
        print(
            f"  min {ej.min():.4f} | p5 {np.percentile(ej, 5):.4f} | "
            f"中位 {np.median(ej):.4f} | p95 {np.percentile(ej, 95):.4f} | max {ej.max():.4f}"
        )
        near = float((ej < args.threshold + 0.05).mean())
        print(
            f"  ** 贴阈值(<{args.threshold + 0.05:.2f}) 的边占 {near * 100:.1f}% "
            f"→ 阈值抬高 {0.05} 可减少 {near * 100:.1f}% 的合并"
        )
        # 估计 Jaccard 估计误差：签名 80 个 → 标准差约 1/sqrt(80) ≈ 0.112
        print(
            f"  ⚠️ num_perm={args.num_perm} → Jaccard 估计标准差 ≈ "
            f"{1 / np.sqrt(args.num_perm):.3f}，与阈值 0.7 同量级！"
        )
        print("     即「0.70 vs 0.75」在统计上可能**不可区分**（差值 0.05 < 噪声）")
    else:
        print("  无合并边")

    # ── 诊断 3：桥接率（链式合并的直接证据）──
    print("\n=== 诊断 3：桥接率 —— 区分「直接相似」与「链式合并」===")
    direct, bridged = 0, 0
    for x, y in merged_pairs:
        rx, ry = _find(parent, x), _find(parent, y)
        rep = min(rx, ry)
        # x/y 是否都与最终代表 rep 直接相似（用估计签名比较）
        jx = float(np.mean(sigs[x] == sigs[rep]))
        jy = float(np.mean(sigs[y] == sigs[rep]))
        if max(jx, jy) >= args.threshold:
            direct += 1
        else:
            bridged += 1
    tot_edges = max(direct + bridged, 1)
    print(f"  与代表直接相似 ≥阈值: {direct} ({direct / tot_edges * 100:.1f}%)")
    print(f"  ** 仅通过链式传递相连: {bridged} ({bridged / tot_edges * 100:.1f}%)")
    if bridged / tot_edges > 0.15:
        print("  →⚠️ 桥接率高 = **传递闭包级联确认**，调阈值治标不治本")
    else:
        print("  → 桥接率不高，主要不是级联")

    print(f"\n=== 跳桶 ===\n  因max_bucket 超限被跳过的桶: {n_skipped_bucket}")
    print("  （跳桶会连带牺牲桶内真重复 —— 这是另一条独立的漏检来源）")

    out = {
        "n": n,
        "threshold": args.threshold,
        "num_perm": args.num_perm,
        "n_clusters": len(sizes),
        "n_merged": total_merged,
        "merge_rate": round(total_merged / n, 4),
        "max_cluster": sizes[0],
        "top10_share": round(sum(sizes[:10]) / max(total_merged, 1), 4),
        "n_edges": len(edge_j),
        "bridge_rate": round(bridged / tot_edges, 4),
        "edge_j_p50": round(float(np.median(edge_j)), 4) if edge_j else None,
        "edge_j_near_threshold": round(near, 4) if edge_j else None,
        "jaccard_est_std": round(float(1 / np.sqrt(args.num_perm)), 4),
    }
    rep = pathlib.Path("data/reports/dedup_cascade_diagnosis.json")
    rep.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写{rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
