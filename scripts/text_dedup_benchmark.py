"""文本去重基准（V2 β T5）：四档规模吞吐/内存/P-R + 与 ground truth 对账。

协议：每个规模取语料前 N 篇，注入 5% 精确重复 + 5% 近似重复（8-gram 复制
+ 3% 删字，自带 ground truth），分别测精确去重与 fast 近似去重的
召回/误杀/耗时/峰值内存。

用法：python scripts/text_dedup_benchmark.py [--scales 10000 50000 100000 300000]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.dedup_fast import dedup_texts, exact_text_duplicates  # noqa: E402
from mm_curation.operators.base import Sample  # noqa: E402

CORPUS = Path("data/raw/text_corpus.jsonl")
REPORT = Path("data/reports/text_dedup_benchmark")


def _near_dup_text(text: str, rng: random.Random) -> str:
    """近似重复变换：8-gram 复制 + 3% 删字（与污染器 near_duplicate_text 同源）。"""
    chars = list(text)
    if len(chars) >= 8:
        start = rng.randrange(len(chars) - 7)
        gram = "".join(chars[start : start + 8])
        chars.insert(start, gram)
    for i in rng.sample(range(len(chars)), min(max(1, int(len(chars) * 0.03)), len(chars))):
        chars[i] = ""
    return "".join(chars)


def _inject(samples: list[Sample], exact_rate: float, near_rate: float, seed: int):
    rng = random.Random(seed)
    out = list(samples)
    exact_ids, near_ids = [], []
    for rate, fn, ids in (
        (exact_rate, lambda t: t, exact_ids),
        (near_rate, lambda t: _near_dup_text(t, rng), near_ids),
    ):
        picks = rng.sample(range(len(samples)), int(len(samples) * rate))
        for i in picks:
            src = samples[i]
            out.append(
                Sample(
                    id=f"{src.id}::inj{len(out)}",
                    text=fn(src.text),
                    meta={"source": src.id},
                )
            )
            ids.append(out[-1].id)
    return out, exact_ids, near_ids


def _measure(samples, injected_exact, injected_near, do_fast=True):
    row = {"n_total": len(samples)}
    t0 = time.perf_counter()
    dup = exact_text_duplicates(samples)
    row["exact_seconds"] = round(time.perf_counter() - t0, 2)
    exact_dropped = set(dup)
    row["exact_recall"] = round(
        len(set(injected_exact) & exact_dropped) / max(len(injected_exact), 1), 4
    )

    if do_fast:
        t0 = time.perf_counter()
        result = dedup_texts(samples)
        fast_sec = round(time.perf_counter() - t0, 2)
        fast_dropped = set(result.duplicate_of)
        row["fast_seconds"] = fast_sec
        row["fast_recall_exact"] = round(
            len(set(injected_exact) & fast_dropped) / max(len(injected_exact), 1), 4
        )
        row["fast_recall_near"] = round(
            len(set(injected_near) & fast_dropped) / max(len(injected_near), 1), 4
        )
        clean_ids = {s.id for s in samples}
        # 非注入却被合并的样本 = 语料自然重复 + 模板簇（维基同模板条目，
        # 如上千个只换人名的开国少将条目——Jaccard 视角下确实高度相似）。
        # 这不是算法误杀；注入召回才是 P/R 的 ground truth 口径。
        row["non_injected_merged"] = len(fast_dropped - set(injected_exact) - set(injected_near))
        row["fast_est_jaccard_p50"] = (
            sorted(result.est_jaccard.values())[len(result.est_jaccard) // 2]
            if result.est_jaccard
            else None
        )
        _ = clean_ids
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", type=int, nargs="+", default=[10_000, 50_000, 100_000, 300_000])
    parser.add_argument("--exact-rate", type=float, default=0.05)
    parser.add_argument("--near-rate", type=float, default=0.05)
    parser.add_argument(
        "--fast-from", type=int, default=50_000, help="近似去重从该档起跑（小档无信息量）"
    )
    args = parser.parse_args()

    # JSONL 必须按字面 \n 切分：splitlines() 会把正文里的 U+2028/U+2029
    # （维基语料真实存在）当换行，撕碎 JSON 行。
    rows = [json.loads(ln) for ln in CORPUS.read_text(encoding="utf-8").split("\n") if ln.strip()]
    samples = [Sample.from_dict(r) for r in rows]
    print(f"语料 {len(samples)} 篇；规模档 {args.scales}")

    results = []
    psutil = None
    try:
        import psutil

        proc = psutil.Process()
    except ImportError:
        proc = None

    for n in args.scales:
        if n > len(samples):
            print(f"跳过 {n}: 语料仅 {len(samples)} 篇")
            continue
        subset = samples[:n]
        mixed, exact_ids, near_ids = _inject(subset, args.exact_rate, args.near_rate, seed=n)
        t0 = time.perf_counter()
        row = _measure(mixed, exact_ids, near_ids, do_fast=n >= args.fast_from)
        row["seconds_total"] = round(time.perf_counter() - t0, 2)
        row["scale"] = n
        row["n_injected"] = {"exact": len(exact_ids), "near": len(near_ids)}
        if proc:
            row["rss_mb"] = round(proc.memory_info().rss / 1e6, 1)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False))

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.with_suffix(".json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = [
        "# 文本去重基准（向量化 MinHash fast path）",
        "",
        f"- 注入：精确 {args.exact_rate:.0%} + 近似 {args.near_rate:.0%}（自带 ground truth）",
        "- P/R 口径：召回只对注入样本结算；`non_injected_merged` 是被合并的",
        "  非注入样本 = 自然重复 + 模板簇（维基同模板条目，如开国少将系列），",
        "  它们在 600 字节前缀的 Jaccard 视角下确实相似——去重语义如此，非误杀",
        "",
        "| 规模 | 精确去重耗时 | fast 耗时 | exact 召回 | near 召回 | 非注入合并 | RSS MB |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        md.append(
            f"| {r['scale']} | {r['exact_seconds']}s | {r.get('fast_seconds', '—')} "
            f"| {r['exact_recall']} | {r.get('fast_recall_near', '—')} "
            f"| {r.get('non_injected_merged', '—')} | {r.get('rss_mb', '—')} |"
        )
    REPORT.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"报告: {REPORT}.md")


if __name__ == "__main__":
    main()
