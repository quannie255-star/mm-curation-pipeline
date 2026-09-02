"""去重三件套测试：先到先保留约定 + 与污染器的闭环互测。

闭环测试是项目灵魂：污染器造靶子（带 ground truth），
清洗算子必须把靶子全部抓住（召回）且不误杀干净样本（精确）。
"""

from __future__ import annotations

import random

from PIL import Image, ImageDraw

from mm_curation.contamination import ContaminationPlan
from mm_curation.operators import build_operator
from mm_curation.operators.base import Sample


def _textured_image(seed: int, size=(320, 240)) -> Image.Image:
    """结构化图像（渐变 + 几何形状）：pHash 有稳定低频结构可依赖，
    纯噪声图的高频随机性会让感知哈希失去意义。
    渐变方向随种子变化，保证不同种子产出的图彼此差异足够大。"""
    rng = random.Random(seed)
    w, h = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    base = (seed * 37 % 255, seed * 61 % 255, seed * 89 % 255)
    orientation = seed % 3
    for i in range(max(w, h)):
        c = tuple(int(v * i / max(w, h)) for v in base)
        if orientation == 0:
            draw.line([(i, 0), (i, h)], fill=c)
        elif orientation == 1:
            draw.line([(0, i), (w, i)], fill=c)
        else:
            draw.line([(i, 0), (0, i)], fill=c)
            draw.line([(w - i, h), (w, h - i)], fill=c)
    for _ in range(4):
        x0, y0 = rng.randint(0, w - 60), rng.randint(0, h - 60)
        color = tuple(rng.randint(0, 255) for _ in range(3))
        box = (x0, y0, x0 + rng.randint(30, 80), y0 + rng.randint(30, 80))
        if rng.random() < 0.5:
            draw.ellipse(box, fill=color)
        else:
            draw.rectangle(box, fill=color)
    return img


# caption 必须彼此差异明显：近似重复的夹具会被 minhash_lsh 正确地聚掉
_CAPTIONS = [
    "一只金毛犬在夕阳下的海滩上奔跑",
    "城市夜景中的霓虹灯广告牌特写",
    "厨房料理台上摆满了新鲜的蔬菜水果",
    "雪山脚下一栋木屋冒出袅袅炊烟",
    "图书馆里学生正在安静地阅读书籍",
    "摩托车手戴着头盔在赛道上疾驰",
]


def _make_samples(tmp_path, n=4, prefix="COCO_train2014_%012d") -> list[Sample]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    samples = []
    for i in range(n):
        p = tmp_path / f"img{i}.jpg"
        _textured_image(i).save(p, "JPEG", quality=92)
        samples.append(
            Sample(
                id=prefix % i,
                image_path=str(p),
                text=_CAPTIONS[i % len(_CAPTIONS)],
                meta={"tags": ["照片"]},
            )
        )
    return samples


def _dedup(op_name, samples, **params):
    return build_operator({"op": op_name, "params": params}).run_batch(samples)


# ---------------- md5_exact ----------------


def test_md5_exact_drops_byte_copy_keep_first(tmp_path):
    samples = _make_samples(tmp_path)
    dup = Sample(id="dup", image_path=samples[0].image_path, text=samples[0].text)
    kept = _dedup("md5_exact", [samples[0], dup, samples[1]])
    assert [s.id for s in kept] == [samples[0].id, samples[1].id]  # 先到先保留
    assert dup.meta["dedup:md5_exact"]["duplicate_of"] == samples[0].id


def test_md5_exact_keeps_different_bytes(tmp_path):
    samples = _make_samples(tmp_path, n=2)
    assert len(_dedup("md5_exact", samples)) == 2


# ---------------- phash_near ----------------


def test_phash_near_drops_reencode(tmp_path):
    samples = _make_samples(tmp_path, n=1)
    reencoded = tmp_path / "reencoded.jpg"
    Image.open(samples[0].image_path).save(reencoded, "JPEG", quality=35)
    dup = Sample(id="dup", image_path=str(reencoded), text=samples[0].text)
    kept = _dedup("phash_near", [samples[0], dup])
    assert [s.id for s in kept] == [samples[0].id]


def test_phash_near_keeps_distinct_images(tmp_path):
    samples = _make_samples(tmp_path, n=3)
    kept = _dedup("phash_near", samples)
    assert len(kept) == 3  # 不同内容的图不应被误杀


# ---------------- minhash_lsh ----------------


def test_minhash_lsh_catches_perturbed_caption(tmp_path):
    from mm_curation.contamination.base import ContaminationContext
    from mm_curation.contamination.impl import NearDuplicateText

    src = _make_samples(tmp_path, n=1)[0]
    original = src.text
    dup = Sample(id="dup", image_path=src.image_path, text=original)
    ctx = ContaminationContext([src], tmp_path / "dirty", random.Random(7))
    NearDuplicateText().apply(dup, 0, ctx)  # 注入与真实污染同源的扰动
    other = Sample(id="other", image_path=src.image_path, text="完全不相关的另一段描述文字")
    kept = _dedup("minhash_lsh", [src, dup, other])
    assert [s.id for s in kept] == [src.id, other.id]


# ---------------- 闭环：污染器造靶子 -> 去重算子全抓 ----------------


def _closed_loop(tmp_path, kind, dedup_op, **params):
    samples = _make_samples(tmp_path / "clean", n=6)
    mixed, _ = ContaminationPlan(inject_rate=0.5, seed=11, kinds={kind: 1.0}).run(
        samples, tmp_path / "dirty"
    )
    kept = _dedup(dedup_op, mixed, **params)
    kept_ids = {s.id for s in kept}
    orig_ids = {s.id for s in samples}
    injected_ids = {s.id for s in mixed} - orig_ids
    missed = injected_ids & kept_ids
    falsely_killed = orig_ids - kept_ids
    assert not missed and not falsely_killed, (
        f"{kind} 闭环失败：漏抓 {missed}，误杀 {falsely_killed}"
    )


def test_closed_loop_exact_duplicate(tmp_path):
    _closed_loop(tmp_path, "exact_duplicate", "md5_exact")


def test_closed_loop_near_duplicate_image(tmp_path):
    _closed_loop(tmp_path, "near_duplicate_image", "phash_near")


def test_closed_loop_near_duplicate_text(tmp_path):
    _closed_loop(tmp_path, "near_duplicate_text", "minhash_lsh")
