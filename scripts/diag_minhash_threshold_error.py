"""Q4 深度诊断（二）：MinHash 估计误差在**阈值区**到底多大。

第一轮诊断（diag_dedup_cascade.py）发现真实新闻语料里几乎没有天然近重复对
（最高真实 Jaccard 仅 0.18），阈值区的误差**测不到** —— 那是语料性质，不是算法性质。
本脚本用**受控注入**补上：在**原文层面**构造扰动，真值与估计都走同一条路径。

⚠️⚠️ **装置教训（本脚本第一版就踩了）**：
   第一版把「保留的 shingle 集合」当真值算Jaccard（得 0.73），但 `_signature`
   吃的是把 shingle 用 latin-1 重建的**文本**，重新 4-gram 后集合完全变了
   （真值 0.0021）→ 误判率 62.6%，且**加 num_perm 也降不下来**（62.6%→62.6%）。
   **判据的输入不是被测对象的输入 = 装置坏了**，不是被测对象差。
   判据：若某项改进**完全不改变结果**，先怀疑装置，别急着改被测对象。

现在：真值 = `shingles(原文)` 与 `shingles(扰动文本)` 的精确 Jaccard，
     估计 = `_signature(原文)` 与 `_signature(扰动文本)` 的比较。**两侧同一函数族。**
"""

from __future__ import annotations

import json
import pathlib
import random
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "packages/curation-eval/src")

import numpy as np  # noqa: E402

from mm_curation.dedup_fast import _signature  # noqa: E402

_CORPORA = [
    pathlib.Path("data/raw/news_corpus.jsonl"),
    pathlib.Path("data/raw/finance_news/news_corpus.jsonl"),
]
PREFIX = 600


def load_texts() -> list[str]:
    out: list[str] = []
    for f in _CORPORA:
        if not f.exists():
            continue
        for line in f.open(encoding="utf-8"):
            if not line.strip():
                continue
            t = json.loads(line).get("text") or ""
            if t:
                out.append(t)
    return out


def shingles(text: str) -> set[bytes]:
    """与 dedup_fast._signature **完全一致**的 shingle 口径（含 prefix 与补齐）。"""
    data = text.encode("utf-8")[:PREFIX].ljust(4, b"\x00")
    return {data[i : i + 4] for i in range(len(data) - 3)}


def exact_jaccard(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    u = len(sa | sb)
    return len(sa & sb) / u if u else 0.0


def perturb(text: str, keep: float, rng: random.Random) -> str:
    """**原文层面**的扰动：按字符切窗口，保留一部分、替换一部分。

    必须在字符层面做，且截断到PREFIX —— 这样 `shingles()` 算出的真值
    与 `_signature()` 估计的对象一致（否则就是上一版的装置错误）。
    """
    head = text[:PREFIX]
    chars = list(head)
    n = len(chars)
    mask = [rng.random() < keep for _ in range(n)]
    # 保证至少有 20 个字符留下，否则 Jaccard 恒为 0
    idx = [i for i, m in enumerate(mask) if m]
    if len(idx) < 20:
        for i in rng.sample(range(n), min(20, n)):
            mask[i] = True
    out = []
    for i, c in enumerate(chars):
        if not mask[i]:
            continue
        # 少量字符替换成随机中文/字母（模拟真实近重复的错字）
        if rng.random() < 0.03:
            out.append(rng.choice("的了在是和有国这中大小"))
        else:
            out.append(c)
    return "".join(out)


def main() -> int:
    rng = random.Random(7)
    texts = load_texts()
    avg = int(sum(map(len, texts)) / max(len(texts), 1))
    print(f"真实语料 {len(texts)} 篇（平均 {avg} 字符，shingle 口径 prefix={PREFIX}）")

    variants: list[tuple[int, str]] = []
    truths: list[float] = []
    for _ in range(2000):
        i = rng.randrange(len(texts))
        v = perturb(texts[i], rng.uniform(0.55, 0.95), rng)
        if not v:
            continue
        variants.append((i, v))
        truths.append(exact_jaccard(texts[i], v))
    truth = np.array(truths)
    print(f"构造 {len(truth)} 对 | 真实 Jaccard min {truth.min():.4f} "
          f"中位 {np.median(truth):.4f} max {truth.max():.4f}")

    band = (truth >= 0.55) & (truth <= 0.95)
    print(f"** 阈值 0.70 所在区(0.55~0.95)：{int(band.sum())} 对 ← 决策相关区")
    if int(band.sum()) < 20:
        print("⚠️ 阈值区样本不足，结果不可用")
        return 1
    # 装置自检：真值分布必须跨过阈值，否则测不到误判
    print(f"   装置自检：真值 <0.70 占 {int((truth < 0.70).sum())}，≥0.70 占 "
          f"{int((truth >= 0.70).sum())}（两类都要有才测得出误判）\n")

    print("=== MinHash 估计误差（真实算法路径，只看阈值区）===")
    print(f"{'num_perm':>8} {'偏差中位':>9} {'p95':>8} {'阈值0.70误判':>13} {'阈值0.75误判':>13}")
    rows = []
    for num_perm in (80, 160, 320):
        rng_np = np.random.default_rng(42)
        a = rng_np.integers(1, 1 << 31, size=num_perm, dtype=np.uint64)
        b = rng_np.integers(0, 1 << 31, size=num_perm, dtype=np.uint64)
        sig_a = {i: _signature(texts[i], PREFIX, a, b) for i in range(len(texts))}
        errs: list[float] = []
        flip70 = flip75 = 0
        sel = 0
        for idx, tv in enumerate(truths):
            if not (0.55 <= tv <= 0.95):
                continue
            src, v = variants[idx]
            sel += 1
            est = float(np.mean(sig_a[src] == _signature(v, PREFIX, a, b)))
            errs.append(abs(est - tv))
            if (est >= 0.70) != (tv >= 0.70):
                flip70 += 1
            if (est >= 0.75) != (tv >= 0.75):
                flip75 += 1
        if not sel:
            continue
        errs_a = np.array(errs)
        row = {
            "num_perm": num_perm,
            "n_band": int(sel),
            "err_median": round(float(np.median(errs_a)), 4),
            "err_p95": round(float(np.percentile(errs_a, 95)), 4),
            "flip_rate_070": round(flip70 / sel, 4),
            "flip_rate_075": round(flip75 / sel, 4),
        }
        rows.append(row)
        print(f"{num_perm:>8} {row['err_median']:>9.4f} {row['err_p95']:>8.4f} "
              f"{row['flip_rate_070'] * 100:>12.1f}% {row['flip_rate_075'] * 100:>12.1f}%")

    print()
    if len(rows) >= 2:
        lo, hi = rows[0], rows[-1]
        if lo["flip_rate_070"] > 0:
            gain = (lo["flip_rate_070"] - hi["flip_rate_070"]) / lo["flip_rate_070"]
            print(f"** num_perm {lo['num_perm']}→{hi['num_perm']}：阈值 0.70 误判率 "
                  f"{lo['flip_rate_070'] * 100:.1f}% → {hi['flip_rate_070'] * 100:.1f}%"
                  f"（降 {gain * 100:.0f}%）")
        else:
            print("** p80 误判率已为 0——估计误差不是问题")

    rep = pathlib.Path("data/reports/minhash_threshold_band_error.json")
    rep.write_text(
        json.dumps(
            {
                "n_corpus": len(texts),
                "n_pairs": len(truths),
                "n_band_055_095": int(band.sum()),
                "truth_below_070": int((truth < 0.70).sum()),
                "truth_ge_070": int((truth >= 0.70).sum()),
                "rows": rows,
                "note": "受控注入（原文层面扰动）；真值与估计同走 shingles/_signature "
                "函数族。上一版因真值算在shingle 集合、估计算在重建文本上（不同对象）"
                "得出 62.6% 假误判率，已修正。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已写 {rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
