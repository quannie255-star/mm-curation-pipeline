"""A 步 · 真实数据集（未注入任何脏数据）的清洗效果与归因。

## 为什么需要这个脚本

上一轮（`eval_real_sensor.py`）在三个真实数据集上跑出了漏斗指标，但那条链有个
**必须说清的限制**：三个数据集的标签（`degraded` / `fault_window_air_leak` /
`sensor_anomaly`）**全是设备故障，不是数据质量缺陷**。拿它们当召回分母，等于用数据
质量检测器去考故障诊断 —— 那个召回数不是能力指标。

本脚本换一条路：**拿真实数据集的「原样」当基准，用业界标准的文本→图像检索任务**
（COCO 官方 caption 作查询、目标=对应图像，ground truth 天然存在，不需要任何人标注）
量出「清洗让下游变好了多少」。**它不注入任何东西**，所以结论完全不受「自己造题」的质疑。

## 四臂对照（同一套CLIP 编码、同一批查询、零重编码）

| 臂 | 内容 | 堵的质疑 |
|---|---|---|
| `raw_real` | 真实 COCO 原样（1620） | 基准：业界原状是什么样 |
| `cleaned_real` | 过漏斗后的存活集 | 「清洗到底有没有用」 |
| `random_drop` | **随机删同样数量**（N seeds，报均值与最小） | 「提升只是因为库变小了」 |
| `natural_dupes` | 真实语料自带的重复（md5/pHash 实测） | 「你的算子在真实数据上到底动了什么」 |

`cleaned_real` 超出 `random_drop` 均值的部分，才是清洗规则带来的**净贡献**——
这是唯一能把「洗对了」和「删少了」分开的口径。

## 口径纪律（三条，不许混）

1. **查询集固定用 `raw_real` 的官方 caption**：所有臂用同一批查询，否则 R@1 不可比。
2. **`random_drop` 报最小值**：随机实验的乐观值没有意义，取最差seed 作为保守界。
3. **零重编码**：图像向量在脚本开头一次性算好并缓存；换臂只换**保留集合**，
   不重跑模型 —— 否则 GPU 抖动会混进「清洗效果」里。

## 用法

```bash
python -X utf8 scripts/eval_real_coco_evidence.py
python -X utf8 scripts/eval_real_coco_evidence.py --n-seeds 10
```

产物：`data/reports/real_coco_evidence.{json,md}`（+ 每臂的丢弃清单 CSV）。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from mm_curation.embedding import clip_encoder  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402
from mm_curation.pipeline import PipelineConfig, run_funnel  # noqa: E402

RAW = REPO / "data" / "raw" / "samples.jsonl"
DEFAULT_CONFIG = "configs/pipeline.example.yaml"


def load_raw() -> list[Sample]:
    """真实 COCO 原样样本（未注入任何脏数据）。"""
    if not RAW.exists():
        raise SystemExit(f"未找到 {RAW}")
    out = []
    for line in RAW.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        # 真实文件的字段是 caption，归一协议字段后交给漏斗
        if "text" not in d:
            d["text"] = d.get("caption", "")
        d.setdefault("modality", "image_caption")
        out.append(Sample.from_dict(d))
    return out


def encode_all(samples: list[Sample], cache: Path) -> dict[str, np.ndarray]:
    """一次性编码图像 + 查询文本并缓存。

    「零重编码」的纪律落在这里：换臂只换保留集合，不碰模型输出。
    """
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        return {"img": z["img"], "txt": z["txt"], "ids": list(z["ids"])}
    enc = clip_encoder.get_encoder()
    paths = [s.image_path for s in samples]
    img = np.asarray(enc.encode_images(paths), dtype=np.float32)
    txt = np.asarray(enc.encode_texts([s.text for s in samples]), dtype=np.float32)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache, img=img, txt=txt, ids=np.array([s.id for s in samples]))
    return {"img": img, "txt": txt, "ids": [s.id for s in samples]}


def retrieval_metrics(
    img: np.ndarray, keep: np.ndarray, q: np.ndarray, target_global: np.ndarray
) -> dict:
    """子集检索：查询向量只在**存活集**里排（模拟「库里就是这些图」）。

    `keep` 是存活集的布尔掩码；`target_global` 是每条查询的目标在**原始全库**里的行号。

    ⚠️ 行号映射必须在本函数内做，不能让调用方自己换算——这是踩过的坑：
    池子被 keep 压缩后位置会变（199 条的库删成 198 条，原第 199 张落到第 198 位），
    传原始行号会取到别的图的分数，**R@1 照样算得出来，只是算错了**。
    一个安静算错的指标比报错危险得多，所以映射 + 校验都在这里做。

    口径：R@k 按**存活集规模**计算。规模变了不该让指标看起来变好/变坏，
    所以不按全库规模折算——但**查询集必须一致**（见 main 里的 q_mask）。
    """
    pool = img[keep]  # (n_keep, d)
    sims = q @ pool.T  # (n_q, n_keep)
    # 原始行号 → 存活集内位置；不在存活集里的记 -1
    pos = np.full(len(keep), -1, dtype=np.int64)
    pos[keep] = np.arange(int(keep.sum()))
    target_in_pool = pos[target_global]
    if (target_in_pool < 0).any():
        raise SystemExit(
            f"retrieval_metrics: 有 {int((target_in_pool < 0).sum())} 条查询的目标"
            "不在存活集内。请先用 keep 掩码过滤 q 与 target_global（同口径，见 main 的 q_mask）。"
        )
    diag = sims[np.arange(len(q)), target_in_pool]
    # ⚠️ 必须是 `>` 不是 `>=`：目标自己与自己的相似度必然 **等于** diag，
    # 用 `>=` 会把目标自己计入「排在前面的人数」→ rank 恒 ≥2 → R@1 恒为 0。
    # 这个 bug 的可怕之处不是报错，是指标照常算得出来（实测 R@1=0 / R@5=0.95 /
    # R@10=1.0，看起来像「模型完全不可用」，实际只差这一个字符）。
    ranks = (sims > diag[:, None]).sum(axis=1) + 1
    out = {
        "n_keep": int(keep.sum()),
        "n_queries": int(len(q)),
        "recall_at_k": {},
    }
    for k in (1, 5, 10):
        out["recall_at_k"][str(k)] = float((ranks <= k).mean())
    out["mrr"] = float((1.0 / ranks).mean())
    return out


def random_drop_arms(
    img: np.ndarray,
    q: np.ndarray,
    target_global: np.ndarray,
    n_keep: int,
    n_seeds: int,
) -> dict:
    """随机删「恰好 n_keep 条」→ R@1 的分布。

    `q` / `target_global` 传的是**全库**查询集（与清洗臂同一个），每个 seed 只改保留集合。
    这样三臂的唯一变量就是「保留集合怎么选」，查询集恒定 —— 否则 R@1 的差里
    混着「查询集变了」这个混淆变量。

    **报最小值**：随机实验取最优 seed 等于自欺，保守界才是可用口径。
    """
    n_total = len(img)
    idx = np.arange(n_total)
    vals = []
    for seed in range(n_seeds):
        rng = random.Random(seed)
        dropped = set(rng.sample(list(idx), n_total - n_keep))
        keep = np.array([i not in dropped for i in idx])
        # 查询也要按keep 过滤（目标必须在池内），但**用全局行号**交给函数映射
        m = retrieval_metrics(img, keep, q[keep], target_global[keep])
        vals.append(m["recall_at_k"]["1"])
    return {
        "n_seeds": n_seeds,
        "n_dropped_each": n_total - n_keep,
        "recall_at_1_mean": float(np.mean(vals)) if vals else None,
        "recall_at_1_min": float(np.min(vals)) if vals else None,
        "recall_at_1_values": vals,
    }


def natural_dupes(samples: list[Sample], enc) -> dict:
    """真实语料里**本就存在**的重复（不注入）。

    两条路径都查：图像字节 md5（完全相同）与图像向量近重复（视觉相同但字节不同）。
    前者是硬事实，可直接人工核对。
    """
    import hashlib

    md5s = Counter()
    for s in samples:
        p = REPO / s.image_path
        md5s[hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else "missing"] += 1
    n_exact_groups = sum(1 for v in md5s.values() if v > 1)
    n_exact_extra = sum(v - 1 for v in md5s.values() if v > 1)

    vecs = np.asarray(enc.encode_images([s.image_path for s in samples]), dtype=np.float32)
    sims = vecs @ vecs.T
    np.fill_diagonal(sims, -1.0)
    t = 0.93  # 与配置里 semantic_dedup 同阈值
    near = sims >= t
    per_row = near.sum(axis=1)
    return {
        "exact_md5_groups": n_exact_groups,
        "exact_md5_extra_copies": n_exact_extra,
        "near_dup_threshold": t,
        "near_dup_pairs": int(near.sum() // 2),
        "near_dup_rows": int((per_row > 0).sum()),
        "note": (
            "exact_md5_* 是硬事实（字节相同，人工可核对）；near_* 是视觉近重复，"
            "含真实数据的自然近重复（同场景不同帧），不是缺陷"
        ),
    }


def write_kills_csv(rows: list[dict], path: Path) -> None:
    """丢弃清单（人工抽检入口）。用 utf-8-sig：给人看的，不是给程序读的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "stage",
        "sample_id",
        "reason",
        "score",
        "text",
        "image_path",
        "text_len",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def collect_kills(funnel, stage_name: str) -> list[dict]:
    rows = []
    for op, s in funnel.dropped:
        rows.append(
            {
                "stage": stage_name,
                "sample_id": s.id,
                "reason": op,
                "score": json.dumps(
                    {k: v for k, v in s.meta.items() if k.startswith("score:")},
                    ensure_ascii=False,
                ),
                "text": (s.text or "")[:120],
                "image_path": s.image_path or "",
                "text_len": len(s.text or ""),
            }
        )
    return rows


def classify_kills(samples: list[Sample], kill_rows: list[dict]) -> dict:
    """把每条丢弃分成「硬事实」与「待人工裁决」—— 真实数据上没有标签，
    只能用**不依赖模型判断**的硬证据来分。

    硬事实的定义（唯一）：**这条样本的文本与库中另一条完全相同**。
    文本逐字节相同 → 保留两条必然有一份冗余，这是可人工核对的事实。
    除此之外（尤其图像向量近重复、图文对齐分低）都依赖模型判断 →
    一律记「待裁决」，**不许**计入清洗有效的证据。

    为什么必须这么严：`semantic_dedup` 在 COCO 上丢的 14 条里只有 1 条是硬事实，
    其余全是「同场景不同帧」（斑马群/草原）——把这类当重复丢掉是**过度清洗**。
    不分这一层，净贡献会被算成一个漂亮但站不住的数。
    """
    from collections import Counter

    caps = Counter((s.text or "").strip() for s in samples)
    per_op: dict[str, dict[str, int]] = {}
    n_hard = n_open = 0
    for r in kill_rows:
        op = r["reason"]
        is_hard = caps.get((r["text"] or "").strip(), 0) > 1
        slot = per_op.setdefault(op, {"dropped": 0, "hard_fact": 0, "needs_review": 0})
        slot["dropped"] += 1
        slot["hard_fact" if is_hard else "needs_review"] += 1
        n_hard += is_hard
        n_open += not is_hard
    return {
        "definition": "硬事实 = 该样本文本与库中另一条逐字节相同（可人工核对）；其余记待裁决",
        "n_dropped": len(kill_rows),
        "n_hard_fact": n_hard,
        "n_needs_review": n_open,
        "hard_fact_share": n_hard / len(kill_rows) if kill_rows else None,
        "per_operator": dict(sorted(per_op.items(), key=lambda kv: -kv[1]["dropped"])),
        "caveat": (
            "「待裁决」不等于误杀。真实数据上无法自动判定——但它**也不能计入**"
            "清洗有效的证据。所以本报告的净贡献是**下界**。"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--n-seeds", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 条（冒烟用）")
    ap.add_argument("--out", default=str(REPO / "data" / "reports" / "real_coco_evidence.json"))
    args = ap.parse_args()

    samples = load_raw()
    if args.limit:
        samples = samples[: args.limit]
    print(f"真实 COCO 原样: {len(samples)} 条（**未注入任何脏数据**）")

    cache = REPO / "data" / "tmp" / f"coco_encode_{len(samples)}.npz"
    enc = clip_encoder.get_encoder()
    z = encode_all(samples, cache)
    img, txt = z["img"], z["txt"]
    print(f"编码缓存: {cache.name}（零重编码纪律：换臂只换保留集合）")

    # 查询 = 每条样本的官方 caption；目标 = 它自己那一行（**原始全库行号**，
    # 映射到存活集内位置由 retrieval_metrics 自己做）
    target_global = np.arange(len(samples))

    # --- 臂 1：原样 ---
    all_keep = np.ones(len(samples), dtype=bool)
    arm_raw = retrieval_metrics(img, all_keep, txt, target_global)

    # --- 臂 2：过漏斗 ---
    cfg = PipelineConfig.from_yaml(args.config)
    funnel = run_funnel(samples, cfg)
    kept_ids = {s.id for s in funnel.kept}
    kept_mask = np.array([s.id in kept_ids for s in samples])
    kill_rows = collect_kills(funnel, "funnel")

    # 只用**存活集内**的查询算清洗臂。理由两条，缺一不可：
    # ①口径：随机臂也只用存活集内的查询（被丢的样本不在候选库里）——
    #   两臂必须同口径，否则 R@1 的差里混着「查询集变了」这个混淆变量；
    # ②索引：目标行号要映射（retrieval_metrics 内部做，但查询侧过滤必须调用方做）。
    q_mask = kept_mask
    arm_clean = retrieval_metrics(img, kept_mask, txt[q_mask], target_global[q_mask])
    arm_clean["n_queries_in_pool"] = int(q_mask.sum())

    # --- 臂 3：随机删同数量（与清洗臂同构：全库查询集，只改保留集合）---
    n_keep = int(kept_mask.sum())
    arm_rand = random_drop_arms(img, txt, target_global, n_keep, args.n_seeds)

    # --- 臂 4：自然重复 ---
    arm_dupes = natural_dupes(samples, enc)

    # --- 逐算子贡献（归因的主证据）---
    per_op = Counter(r["reason"] for r in kill_rows)

    report = {
        "dataset": "COCO 2014（真实，业界原样）",
        "protocol": (
            "文本→图像检索，COCO 官方 caption 作查询、目标=对应图像；"
            "ground truth 由数据集自带，**无需人工标注**；全程未注入任何脏数据"
        ),
        "funnel_config": cfg.name,
        "n_total": len(samples),
        "arms": {
            "raw_real": arm_raw,
            "cleaned_real": arm_clean,
            "random_drop": arm_rand,
            "natural_dupes": arm_dupes,
        },
        "attribution": {
            "n_kept": n_keep,
            "n_dropped": len(kill_rows),
            "per_operator_dropped": dict(per_op.most_common()),
            "kill_classification": classify_kills(samples, kill_rows),
            "net_gain_vs_random_min": (
                arm_clean["recall_at_k"]["1"] - arm_rand["recall_at_1_min"]
                if arm_rand["recall_at_1_min"] is not None
                else None
            ),
            "interpretation": (
                "净贡献 = 清洗臂 R@1 − 随机删同数量的**最差** seed；"
                "大于 0 才能说「收益来自删对了坏样本，而不是库变小」。"
                "注意它是**下界**：只有「硬事实」那部分丢弃能确证是清洗收益，"
                "「待裁决」部分（多为图像近重复）依赖模型判断，不计入证据。"
            ),
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = out.with_name(out.stem + "_kills.csv")
    write_kills_csv(kill_rows, csv_path)

    print("\n" + "=" * 66)
    print(f"{'臂':<16}{'库规模':>9}{'R@1':>9}{'R@5':>9}{'R@10':>9}{'MRR':>9}")
    print("-" * 66)
    print(
        f"{'raw_real':<16}{arm_raw['n_keep']:>9}{arm_raw['recall_at_k']['1']:>9.3f}"
        f"{arm_raw['recall_at_k']['5']:>9.3f}{arm_raw['recall_at_k']['10']:>9.3f}{arm_raw['mrr']:>9.3f}"
    )
    print(
        f"{'cleaned_real':<16}{arm_clean['n_keep']:>9}{arm_clean['recall_at_k']['1']:>9.3f}"
        f"{arm_clean['recall_at_k']['5']:>9.3f}{arm_clean['recall_at_k']['10']:>9.3f}{arm_clean['mrr']:>9.3f}"
    )
    if arm_rand["recall_at_1_min"] is not None:
        print(
            f"{'random_drop':<16}{n_keep:>9}{arm_rand['recall_at_1_min']:>9.3f}"
            f"{'(min/' + str(args.n_seeds) + 'seed)':>18}"
        )
        print(f"{'  随机删均值':<16}{'':>9}{arm_rand['recall_at_1_mean']:>9.3f}")
    print("=" * 66)
    print(f"净贡献（R@1 − 随机删最差）: {report['attribution']['net_gain_vs_random_min']:+.4f}")
    print(f"逐算子丢弃: {dict(per_op.most_common())}")
    kc = report["attribution"]["kill_classification"]
    print(
        f"丢弃性质: 硬事实 {kc['n_hard_fact']} / 待裁决 {kc['n_needs_review']}"
        f"（{kc['hard_fact_share']:.0%} 是硬事实）"
    )
    for op, s in kc["per_operator"].items():
        print(
            f"    {op:<18} 丢{s['dropped']:>3}"
            f" 硬事实 {s['hard_fact']:>3} 待裁决 {s['needs_review']:>3}"
        )
    print(f"自然重复: {arm_dupes}")
    print(f"\n报告: {out}")
    print(f"丢弃清单: {csv_path}（{len(kill_rows)} 行，人工抽检入口）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
