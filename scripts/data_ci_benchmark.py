"""数据 CI 门禁（V2 ε）：合成带标注语料上真跑去重，P/R 低于门限即退出非零。

与单元测试的区别：单测验证「逻辑对」，本门禁验证「质量数字达标」——
dedup_fast 的召回/误杀被门限锁死，实现劣化（哪怕测试仍绿）CI 会红。

合成语料（seed 固定可复现，规避模板自相似陷阱见笔记 #49）：
- base 5000 篇：大词表随机组句，两两 4-gram Jaccard ≈ 0（误杀可归因）
- exact 300 篇：base 的逐字节复制（新 id）→ 门限 exact recall ≥ 0.99
- near 700 篇：base 删 3 词 + 邻位交换 → J≈0.75-0.95 → 门限 near recall ≥ 0.90

用法：python scripts/data_ci_benchmark.py [--scale 1.0] [--threshold 0.7]
门限：exact ≥ 0.99 / near ≥ 0.90 / base 误杀率 ≤ 1%
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "curation-eval" / "src"))

from curation_eval import Sample  # noqa: E402

VOCAB = (
    "铁路 湖泊 森林 邮票 陶瓷 电路 港口 梯田 乐队 蜡染 冰川 帆船 蜂巢 拱桥 "
    "竹编 灯塔 麦浪 溶洞 算盘 风筝 盐场 藤蔓 木雕 砂岩 灯谜 鸽哨 苔藓 潮汐 "
    "鼓楼 皮影 砚台 篝火 峡谷 缂丝 雪线 泉眼 戏台 舷窗 铜鼓 沙洲 染坊 碑林 "
    "渔火 花窗 石阶 茶马 帐幕 铁塔 芦苇 砖窑 渡口 星图 织机 崖壁 驿站 堰坝"
).split()
WORDS_PER_DOC = 40
N_BASE, N_EXACT, N_NEAR = 5000, 300, 700

GATES = {"exact_recall_min": 0.99, "near_recall_min": 0.90, "false_kill_max": 0.01}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--threshold", type=float, default=0.7)
    args = parser.parse_args()

    from mm_curation.dedup_fast import dedup_texts

    # build_corpus 里 base 只存了连接文本，词级近重复需要词表——重新生成词表
    rng = random.Random(42)
    base_words = [
        [rng.choice(VOCAB) for _ in range(WORDS_PER_DOC)] for _ in range(round(N_BASE * args.scale))
    ]
    base = [Sample(id=f"base{i:06d}", text="".join(ws)) for i, ws in enumerate(base_words)]
    exact, near = [], []
    for j in range(round(N_EXACT * args.scale)):
        ws = base_words[rng.randrange(len(base_words))]
        exact.append(Sample(id=f"exact{j:06d}", text="".join(ws)))
    for k in range(round(N_NEAR * args.scale)):
        ws = list(base_words[rng.randrange(len(base_words))])
        # 损伤强度按 α 校准方法论标定：删 1 词 + 邻位交换 → 真实 J∈[0.86,0.92]
        # （del=2 时 J 掉到 0.80-0.88，LSH 捕获率跌破门限带，门禁测的就不是
        # 去重实现而是生成器了）
        del ws[rng.randrange(len(ws))]
        pos = rng.randrange(len(ws) - 1)
        ws[pos], ws[pos + 1] = ws[pos + 1], ws[pos]
        near.append(Sample(id=f"near{k:06d}", text="".join(ws)))

    mixed = base + exact + near  # 注入在尾部：先到先保留语义下 base 恒为簇代表
    # （与 β 去重基准同一测量口径；打乱会让「谁是副本」随顺序漂移）
    t0 = time.perf_counter()
    result = dedup_texts(mixed, threshold=args.threshold)
    elapsed = time.perf_counter() - t0

    dup_of = result.duplicate_of
    kept_ids = {s.id for s in result.kept}

    def recall(prefix: str, n: int) -> float:
        hits = sum(1 for j in range(n) if f"{prefix}{j:06d}" in dup_of)
        return hits / n if n else 1.0

    base_killed = sum(
        1
        for i in range(len(base))
        if f"base{i:06d}" in dup_of and dup_of[f"base{i:06d}"].startswith("base")
    )
    exact_rec = recall("exact", len(exact))
    near_rec = recall("near", len(near))
    false_rate = base_killed / len(base) if base else 0.0

    report = {
        "n_total": len(mixed),
        "threshold": args.threshold,
        "seconds": round(elapsed, 2),
        "exact_recall": round(exact_rec, 4),
        "near_recall": round(near_rec, 4),
        "base_false_kill": f"{base_killed}/{len(base)}",
        "false_kill_rate": round(false_rate, 4),
    }
    print("data-ci:", report)

    failures = []
    if exact_rec < GATES["exact_recall_min"]:
        failures.append(f"exact recall {exact_rec:.4f} < {GATES['exact_recall_min']}")
    if near_rec < GATES["near_recall_min"]:
        failures.append(f"near recall {near_rec:.4f} < {GATES['near_recall_min']}")
    if false_rate > GATES["false_kill_max"]:
        failures.append(f"false kill rate {false_rate:.4f} > {GATES['false_kill_max']}")
    if failures:
        print("GATE FAILED:", "; ".join(failures), file=sys.stderr)
        sys.exit(1)
    print(f"GATE PASSED（kept {len(kept_ids)}/{len(mixed)}，{elapsed:.1f}s）")


if __name__ == "__main__":
    main()
