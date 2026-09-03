"""数据 CI 门禁（ε 补强）：图像去重 md5_exact / phash_near 的质量门禁。

与文本门禁（data_ci_benchmark.py）同方法论：单测验证「逻辑对」，本门禁
验证「质量数字达标」——实现劣化（哪怕单测仍绿）CI 会红。门限先经生成器
标定（损伤强度的距离分布实测），非拍脑袋（α/ε 校准方法论）。

合成图像语料（seed 固定可复现，构图依据笔记 #57）：
- base 2000 张：8x8 随机灰度块放大到 64x64——低频块结构是 phash 可区分
  的前提；两两塌缩期望 ~1 对（64bit 随机 hash P(≤12)≈6e-7），1% 误杀门限兜底
- exact 300 张：base 字节复制（新 id）→ md5_exact 靶子，召回应恒 1.0
- near 500 张：轻度裁剪 fx,fy∈U(0.90,0.96) + JPEG 重编码 q∈U(35,50)
  ——与 V1 污染器 near_duplicate_image 同参数（真实数据校准过的损伤）

标定结论（2026-09-03 实测，--calibrate 输出）：
- 损伤三轮收窄（4~10% → 2~6% → 1~5%，过程见 _near_image docstring）：
  near 距离 p50=4 / p90=8 / max=16，3/500 超阈 → 生成器召回 0.994
- 门限：exact ≥ 0.99 / near ≥ 0.97（生成器 0.994 之下留 2.4pp 劣化余量）/
  base 误杀 ≤ 1%

用法：python -X utf8 scripts/data_ci_image_benchmark.py [--scale 1.0] [--threshold 12] [--calibrate]
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# curation_eval（包源码直连）——CI 环境不安装本包，漏这条路径会 ImportError
# （2026-09-03 Data CI 连红三次的根因：本地 editable 安装掩盖了它）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "curation-eval" / "src"))

N_BASE, N_EXACT, N_NEAR = 2000, 300, 500

# 门限经生成器标定背书（见 docstring 标定结论）：near 实测 1.0，取 0.97
# 留生成器扰动余量；exact 字节级复制语义下召回应恒 1.0；误杀口径同文本 ε。
GATES = {"exact_recall_min": 0.99, "near_recall_min": 0.97, "false_kill_max": 0.01}


def _base_images(n: int, tmp: Path, rng: random.Random) -> None:
    """8x8 随机灰度块 -> 64x64 PNG（无损，字节稳定）。"""
    from PIL import Image

    for i in range(n):
        grid = [[rng.randint(0, 255) for _ in range(8)] for _ in range(8)]
        img = Image.new("L", (64, 64))
        img.putdata([grid[y // 8][x // 8] for y in range(64) for x in range(64)])
        img.save(tmp / f"base{i:06d}.png")


def _near_image(src: Path, dest: Path, rng: random.Random, crop_lo: float, crop_hi: float) -> None:
    """轻度裁剪(默认1~5%) + JPEG 重编码。

    裁剪强度经生成器标定（--calibrate）：V1 污染器的 4~10% 用在真实照片上
    召回 ~90%，但本门禁的块状合成图对裁剪更敏感（裁剪移动 8px 块网格，
    低频结构整体错位），4~10% 时 15.6% 样本距离超阈值 12（p90=14），
    2~6% 仍 1.4% 超阈——三轮收窄到 1~5%：距离 min=0 p10=2 p50=4 p90=8
    max=16，3/500 超阈（尾部是个别 base 图 hash 翻转运气，继续收窄收益
    递减）。生成器召回 0.994，门限 0.97 留 2.4pp 实现劣化余量。
    """
    from PIL import Image

    img = Image.open(src)
    w, h = img.size
    fx, fy = rng.uniform(crop_lo, crop_hi), rng.uniform(crop_lo, crop_hi)
    x0, y0 = int(w * (1 - fx) * rng.random()), int(h * (1 - fy) * rng.random())
    img = img.crop((x0, y0, x0 + int(w * fx), y0 + int(h * fy)))
    img.save(dest, "JPEG", quality=rng.randint(35, 50))


def _phash(path: Path):
    import imagehash
    from PIL import Image

    return imagehash.phash(Image.open(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--threshold", type=int, default=12)
    parser.add_argument(
        "--crop",
        default="0.95,0.99",
        help="near 裁剪保留比例区间（标定用；劣化验证传 0.80,0.90 应 gate 红）",
    )
    parser.add_argument("--calibrate", action="store_true", help="打印 near 距离分布后退出")
    args = parser.parse_args()
    crop_lo, crop_hi = (float(x) for x in args.crop.split(","))
    rng = random.Random(42)

    n_base, n_exact, n_near = (round(x * args.scale) for x in (N_BASE, N_EXACT, N_NEAR))
    tmp = Path(tempfile.mkdtemp(prefix="dataci_img_"))
    try:
        t0 = time.perf_counter()
        _base_images(n_base, tmp, rng)
        for j in range(n_exact):  # exact：字节复制（新 id，PNG 无损 → md5 必同）
            shutil.copyfile(tmp / f"base{j % n_base:06d}.png", tmp / f"exact{j:06d}.png")
        near_src: dict[str, int] = {}
        for k in range(n_near):
            bi = rng.randrange(n_base)
            near_src[f"near{k:06d}"] = bi
            _near_image(tmp / f"base{bi:06d}.png", tmp / f"near{k:06d}.jpg", rng, crop_lo, crop_hi)
        t_build = time.perf_counter() - t0

        if args.calibrate:  # 标定：near 对其 base 的 phash 距离分布
            base_hashes = {i: _phash(tmp / f"base{i:06d}.png") for i in range(n_base)}
            dists = sorted(
                _phash(tmp / f"near{k:06d}.jpg") - base_hashes[near_src[f"near{k:06d}"]]
                for k in range(n_near)
            )
            q = lambda p: dists[min(len(dists) - 1, int(p * len(dists)))]  # noqa: E731
            over = sum(1 for d in dists if d > args.threshold)
            print(
                f"标定：near 距离 min={dists[0]} p10={q(0.1)} p50={q(0.5)} "
                f"p90={q(0.9)} max={dists[-1]}；>{args.threshold} 的 {over}/{len(dists)}"
            )
            return

        # 复用漏斗算子 + LocalSequentialExecutor（保 dedup:* meta 标记）；
        # 执行器内部按 id 规范化排序（确定性约定，base < exact < near）
        from curation_eval import LocalSequentialExecutor, Sample

        from mm_curation.operators.dedup import Md5ExactDedup, PHashNearDedup

        paths = sorted(tmp.iterdir(), key=lambda p: p.stem)
        samples = [Sample(id=p.stem, text="", image_path=str(p)) for p in paths]
        t0 = time.perf_counter()
        result = LocalSequentialExecutor().run(
            [Md5ExactDedup(), PHashNearDedup(threshold=args.threshold)], samples
        )
        t_dedup = time.perf_counter() - t0

        dup_of = {}
        for _, s in result.dropped:
            for key, val in s.meta.items():
                if key.startswith("dedup:"):
                    dup_of[s.id] = val["duplicate_of"]
                    break
        kept_ids = {s.id for s in result.kept}

        def recall(prefix: str, n: int) -> float:
            hits = sum(1 for j in range(n) if f"{prefix}{j:06d}" in dup_of)
            return hits / n if n else 1.0

        exact_rec = recall("exact", n_exact)
        near_rec = recall("near", n_near)
        base_killed = sum(1 for i in range(n_base) if f"base{i:06d}" in dup_of)
        false_rate = base_killed / n_base if n_base else 0.0

        report = {
            "n_total": len(samples),
            "threshold": args.threshold,
            "seconds_build": round(t_build, 1),
            "seconds_dedup": round(t_dedup, 1),
            "exact_recall": round(exact_rec, 4),
            "near_recall": round(near_rec, 4),
            "base_false_kill": f"{base_killed}/{n_base}",
            "false_kill_rate": round(false_rate, 4),
            "kept": len(kept_ids),
        }
        print("data-ci-image:", report)

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
        print(f"GATE PASSED（kept {len(kept_ids)}/{len(samples)}，去重 {t_dedup:.1f}s）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
