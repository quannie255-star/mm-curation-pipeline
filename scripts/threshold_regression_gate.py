"""阈值回归门（V2 ε2）：threshold_scan 曲线对冻结基线，偏移超警戒即红。

与 data_ci_* 门禁的分工：那些锁「单点质量数字」（召回/误杀达标），本门禁
锁「整条阈值敏感性曲线」——上游换库版本/换实现（imagehash、datasketch、
numpy 算法）导致算子行为静默漂移时，单测仍绿、单点门禁可能仍过，但曲线
形状（拐点位置、recall-误杀权衡）会先变。逐点比对冻结基线
（configs/threshold_baseline.json），任何一点偏移超容差即 exit 1。

门禁算子（CI 轻依赖内可跑，见 data-ci.yml；不拉 torch）：
- minhash_lsh（datasketch，文本近重复主算子）
- phash_near（imagehash，图像近重复主算子）
blur 需 opencv、clip_alignment/semantic_dedup 需编码器，不入本门禁。
扫描经 threshold_scan.scan_operator 复用其 THRESHOLD_SPECS（区间/生产默认
单一定义源，两处永不漂移）。

合成带标注语料（seed 固定；文本配方=文本门禁笔记 #49，图像配方=图像门禁
笔记 #57——8x8 随机块是 phash 可区分的前提，裁剪 1~5% 是 #57 标定结论）：
- 文本 base 250 + near 80（删 1 词 + 邻位交换，labels.dirty=near_duplicate_text）
- 图像 base 300 + near 100（1~5% 裁剪 + JPEG 重编码，labels.dirty=near_duplicate_image）

容差：主靶 recall 偏移 ≤ 0.05、干净误杀率偏移 ≤ 0.02（逐点对称）。
基线里记录 imagehash/datasketch/PIL 版本——基线 diff 应人工审查：
环境升级导致的曲线变化要么重标定门限、要么 --update-baseline 显式接受。

用法：python -X utf8 scripts/threshold_regression_gate.py [--scale 1.0]
      python -X utf8 scripts/threshold_regression_gate.py --update-baseline
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "packages" / "curation-eval" / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

from curation_eval import Sample  # noqa: E402

from threshold_scan import scan_operator  # noqa: E402

GATE_OPS = ("minhash_lsh", "phash_near")
BASELINE_PATH = _ROOT / "configs" / "threshold_baseline.json"

N_TXT_BASE, N_TXT_NEAR = 250, 80
N_IMG_BASE, N_IMG_NEAR = 300, 100

TOL_RECALL = 0.05
TOL_KILL = 0.02

# 与 data_ci_benchmark.py 同源词表（大词表随机组句，两两 4-gram Jaccard≈0）
VOCAB = (
    "铁路 湖泊 森林 邮票 陶瓷 电路 港口 梯田 乐队 蜡染 冰川 帆船 蜂巢 拱桥 "
    "竹编 灯塔 麦浪 溶洞 算盘 风筝 盐场 藤蔓 木雕 砂岩 灯谜 鸽哨 苔藓 潮汐 "
    "鼓楼 皮影 砚台 篝火 峡谷 缂丝 雪线 泉眼 戏台 舷窗 铜鼓 沙洲 染坊 碑林 "
    "渔火 花窗 石阶 茶马 帐幕 铁塔 芦苇 砖窑 渡口 星图 织机 崖壁 驿站 堰坝"
).split()
WORDS_PER_DOC = 40


def build_text_corpus(n_base: int, n_near: int) -> list[Sample]:
    """文本 base（干净）+ near 注入（删 1 词 + 邻位交换）。"""
    rng = random.Random(42)
    base_words = [[rng.choice(VOCAB) for _ in range(WORDS_PER_DOC)] for _ in range(n_base)]
    samples = [Sample(id=f"tbase{i:05d}", text="".join(ws)) for i, ws in enumerate(base_words)]
    for k in range(n_near):
        ws = list(base_words[rng.randrange(n_base)])
        del ws[rng.randrange(len(ws))]
        pos = rng.randrange(len(ws) - 1)
        ws[pos], ws[pos + 1] = ws[pos + 1], ws[pos]
        samples.append(
            Sample(id=f"tnear{k:05d}", text="".join(ws), labels={"dirty": "near_duplicate_text"})
        )
    return samples


def _base_images(n: int, tmp: Path, rng: random.Random) -> None:
    """8x8 随机灰度块 -> 64x64 PNG（无损，字节稳定；配方见笔记 #57）。"""
    from PIL import Image

    for i in range(n):
        grid = [[rng.randint(0, 255) for _ in range(8)] for _ in range(8)]
        img = Image.new("L", (64, 64))
        img.putdata([grid[y // 8][x // 8] for y in range(64) for x in range(64)])
        img.save(tmp / f"ibase{i:05d}.png")


def _near_image(src: Path, dest: Path, rng: random.Random) -> None:
    """1~5% 轻度裁剪 + JPEG 重编码（损伤强度=图像门禁 #57 三轮标定结论）。"""
    from PIL import Image

    img = Image.open(src)
    w, h = img.size
    fx, fy = rng.uniform(0.95, 0.99), rng.uniform(0.95, 0.99)
    x0, y0 = int(w * (1 - fx) * rng.random()), int(h * (1 - fy) * rng.random())
    img = img.crop((x0, y0, x0 + int(w * fx), y0 + int(h * fy)))
    img.save(dest, "JPEG", quality=rng.randint(35, 50))


def build_image_corpus(n_base: int, n_near: int, tmp: Path) -> list[Sample]:
    """图像 base（干净）+ near 注入（裁剪重编码）。"""
    rng = random.Random(43)
    _base_images(n_base, tmp, rng)
    samples = [
        Sample(id=f"ibase{i:05d}", image_path=str(tmp / f"ibase{i:05d}.png")) for i in range(n_base)
    ]
    for k in range(n_near):
        bi = rng.randrange(n_base)
        dest = tmp / f"inear{k:05d}.jpg"
        _near_image(tmp / f"ibase{bi:05d}.png", dest, rng)
        samples.append(
            Sample(
                id=f"inear{k:05d}", image_path=str(dest), labels={"dirty": "near_duplicate_image"}
            )
        )
    return samples


def scan_curves(scale: float, tmp: Path) -> dict[str, list[dict]]:
    """门禁算子逐阈值点扫描，返回 {op: [{threshold, primary_recall, clean_kill_rate}]}。"""
    curves: dict[str, list[dict]] = {}
    corpora: dict[str, list[Sample]] = {
        "minhash_lsh": build_text_corpus(round(N_TXT_BASE * scale), round(N_TXT_NEAR * scale)),
        "phash_near": build_image_corpus(round(N_IMG_BASE * scale), round(N_IMG_NEAR * scale), tmp),
    }
    for op in GATE_OPS:
        points, _targets = scan_operator(op, corpora[op])
        curves[op] = [
            {
                "threshold": p.threshold,
                "primary_recall": p.primary_recall,
                "clean_kill_rate": p.clean_kill_rate,
            }
            for p in points
        ]
    return curves


def _env_meta() -> dict:
    import imagehash
    from PIL import Image

    meta = {"python": sys.version.split()[0], "pillow": Image.__version__}
    try:
        import datasketch

        meta["datasketch"] = datasketch.__version__
    except AttributeError:  # 老版本无 __version__
        meta["datasketch"] = "unknown"
    try:
        meta["imagehash"] = imagehash.__version__
    except AttributeError:
        meta["imagehash"] = "unknown"
    return meta


def update_baseline(scale: float) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="thr_gate_"))
    try:
        curves = scan_curves(scale, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    payload = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scale": scale,
            "corpus": {
                "text": [N_TXT_BASE, N_TXT_NEAR],
                "image": [N_IMG_BASE, N_IMG_NEAR],
            },
            "tolerances": {"recall": TOL_RECALL, "clean_kill_rate": TOL_KILL},
            "env": _env_meta(),
        },
        "curves": curves,
    }
    BASELINE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"基线已写入 {BASELINE_PATH}")
    print("请人工审查基线 diff（env 版本变化说明漂移来源）后再提交")


def compare(baseline: dict, curves: dict) -> list[str]:
    """逐点比对，返回超警戒描述列表（空 = 通过）。"""
    problems: list[str] = []
    base_curves = baseline.get("curves", {})
    for op in GATE_OPS:
        if op not in base_curves:
            problems.append(f"{op}: 基线缺失该算子——重新 --update-baseline")
            continue
        base_pts = {p["threshold"]: p for p in base_curves[op]}
        for pt in curves[op]:
            t = pt["threshold"]
            if t not in base_pts:
                problems.append(
                    f"{op}: 阈值 {t} 不在基线中——THRESHOLD_SPECS 变了，重新 --update-baseline"
                )
                continue
            bp = base_pts[t]
            for key, tol in (("primary_recall", TOL_RECALL), ("clean_kill_rate", TOL_KILL)):
                new, old = pt[key], bp[key]
                if new is None or old is None:
                    if new != old:
                        problems.append(f"{op}@{t}: {key} {old} -> {new}（None 翻转）")
                    continue
                if abs(new - old) > tol:
                    problems.append(
                        f"{op}@{t}: {key} {old:.4f} -> {new:.4f}"
                        f"（偏移 {abs(new - old):.4f} > {tol}）"
                    )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--baseline", default=str(BASELINE_PATH))
    parser.add_argument("--update-baseline", action="store_true", help="重新生成冻结基线")
    args = parser.parse_args()

    if args.update_baseline:
        update_baseline(args.scale)
        return

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"门禁红：基线不存在 {baseline_path}（先 --update-baseline）")
        sys.exit(1)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    tmp = Path(tempfile.mkdtemp(prefix="thr_gate_"))
    try:
        t0 = time.perf_counter()
        curves = scan_curves(args.scale, tmp)
        elapsed = time.perf_counter() - t0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    problems = compare(baseline, curves)
    if problems:
        print(f"门禁红：阈值曲线偏移 {len(problems)} 处（耗时 {elapsed:.1f}s）")
        for p in problems:
            print(f"  - {p}")
        print("排查：算子实现 diff / 依赖版本 diff（基线 meta.env）/ 语料生成器变化")
        sys.exit(1)
    print(f"门禁绿：{len(GATE_OPS)} 算子曲线逐点比对通过（耗时 {elapsed:.1f}s）")


if __name__ == "__main__":
    main()
