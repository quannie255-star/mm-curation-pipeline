"""图像漏斗 × Ray 等价性测试（补强验收，镜像 γ3 口径 + dedup 标记口径）。

用真实去重算子（md5_exact / phash_near）+ 合成图像跑 local/ray 双跑。
等价 = γ3 三口径（kept 集按 id / StageStat / 逐 id 分数）+ 第四口径：
dedup 标记（duplicate_of 映射）逐 id 相等——簇代表选择依赖输入序，
框架层修复（run_batch_mixed_modality 按 id 规范化排序）的图像模态验收。
CI 无 ray 时 importorskip 自动跳过。
"""

from __future__ import annotations

import shutil

import pytest

ray = pytest.importorskip("ray")

from curation_eval import LocalSequentialExecutor, RayDistributedExecutor, Sample  # noqa: E402

from mm_curation.operators.dedup import Md5ExactDedup, PHashNearDedup  # noqa: E402


def _make_images(tmp_path) -> list[Sample]:
    """10 张 8x8 随机块图案 + 3 张字节级复制品 + 3 张轻噪声近重复。

    构图要点（前两次失败换来的）：phash 是 32x32 resize + DCT 低频 8x8
    中位数阈值——无低频结构的图会互相塌缩（随机噪声图如此），单频光栅
    的 hash 又只差几个 bit（能量集中一个 bin）。8x8 随机块放大到 64x64
    的低频结构 = 一个随机 64bit hash，且**测试内自校验**：换种子直到
    10 张基图两两 phash 距离 >= 16（> 阈值 12 且留出近重复扰动的余量）。
    簇代表 = 字典序最小 id（框架确定性约定，γ3 文档化取舍）：
    exact*/near* 排在 u* 前，故代表是复制品而非原始图——测试按此断言。
    """
    import random

    import imagehash
    from PIL import Image

    samples: list[Sample] = []
    paths = tmp_path / "images"
    paths.mkdir(exist_ok=True)  # 双跑各装载一次，确定性种子保证两次生成字节一致

    base: list[Image.Image] = []
    for seed in range(100):
        rng = random.Random(seed)
        grids = [[[rng.random() for _ in range(8)] for _ in range(8)] for _ in range(10)]
        base = []
        for g in grids:
            img = Image.new("L", (64, 64))
            img.putdata([int(255 * g[y // 8][x // 8]) for y in range(64) for x in range(64)])
            base.append(img)
        hashes = [imagehash.phash(im) for im in base]
        pairs = (h1 - h2 for i, h1 in enumerate(hashes) for h2 in hashes[i + 1 :])
        if min(pairs, default=64) >= 16:
            break
    else:
        pytest.fail("100 个种子内构造不出两两 phash 距离 >= 16 的基图")

    for i, img in enumerate(base):
        p = paths / f"u{i}.png"
        img.save(p)
        samples.append(Sample(id=f"u{i}", text=f"图{i}", image_path=str(p)))
    for j in range(3):  # 精确重复：字节复制
        src = paths / f"u{j}.png"
        dst = paths / f"exact{j}.png"
        shutil.copyfile(src, dst)
        samples.append(Sample(id=f"exact{j}", text=f"复制{j}", image_path=str(dst)))
    for j in range(3):  # 近重复：同图改 2 个像素（phash 距离必然 <= 12）
        src = Image.open(paths / f"u{j + 3}.png")
        px = list(src.getdata())
        px[j * 100] = (px[j * 100] + 30) % 256
        px[j * 100 + 1] = (px[j * 100 + 1] + 30) % 256
        src.putdata(px)
        p = paths / f"near{j}.png"
        src.save(p)
        samples.append(Sample(id=f"near{j}", text=f"近重{j}", image_path=str(p)))
    return samples


def _dedup_marks(result) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in list(result.kept) + [x for _, x in result.dropped]:
        m = {k: v for k, v in s.meta.items() if k.startswith("dedup:")}
        if m:
            out[s.id] = m
    return out


@pytest.fixture(scope="module")
def ray_exe():
    exe = RayDistributedExecutor(num_cpus=2, object_store_memory=800_000_000)
    yield exe
    ray.shutdown()


def test_image_dedup_ray_equivalent(ray_exe, tmp_path):
    """md5+phash 双运行时：γ3 三口径 + dedup 标记口径全等。"""
    ops = [Md5ExactDedup(), PHashNearDedup(threshold=12)]
    samples = _make_images(tmp_path)

    local = LocalSequentialExecutor().run(ops, samples)
    ray_res = ray_exe.run(ops, _make_images(tmp_path))

    assert {s.id for s in local.kept} == {s.id for s in ray_res.kept}
    assert [(st.op, st.n_in, st.n_out, st.dropped) for st in local.stats] == [
        (st.op, st.n_in, st.n_out, st.dropped) for st in ray_res.stats
    ]
    local_marks = _dedup_marks(local)
    assert local_marks == _dedup_marks(ray_res)
    # 污染注入生效自检：3 exact（md5）+ 3 near（phash）都被抓出，
    # 簇代表是字典序最小 id（exact*/near* < u*），非原始图——框架约定如此
    assert len(local_marks) == 6
    assert {v["duplicate_of"] for m in local_marks.values() for v in m.values()} == {
        "exact0",
        "exact1",
        "exact2",
        "near0",
        "near1",
        "near2",
    }
