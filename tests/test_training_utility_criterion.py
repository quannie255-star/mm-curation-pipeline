"""G1 训练效用：**判据的量纲**必须被锁住（判据腐烂回归测试）。

背景（2026-10-08 实测踩到的一个**恒假判据**）：

    noise_floor = results.get("noise_probe")   # ← 噪声臂的**绝对 ppl**

而 `delta` 是**差值**（ref − cleaned，量级 ±0.2）。
拿绝对量当差值的门槛 → `|Δ| < 7.82` **永远成立** → 任何实验都判「测不出差异」。

    真实地板 = |7.8200 − 7.8637| = 0.0437
    Δ= −0.195 是它的 **4.46 倍** → 正确判定是「cleaned 显著更差」

形态值得记：**日志里算对了（`abs(probe - ref)`），只有判据取错了量**。
所以「我看日志时觉得没问题」完全不能代替判据本身的量纲检查。

这类缺陷不报错、报告照常生成、`verdict` 字段还是一句像模像样的中文，
只是**永远给不出结论** —— 属于「恒真判据」里最隐蔽的一种：
「测不出差异」这句话本身是诚实的，它只是**恒真**。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location(
        "eval_training_utility", REPO_ROOT / "scripts" / "eval_training_utility.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def payload() -> dict:
    p = REPO_ROOT / "data" / "reports" / "training_utility.json"
    if not p.exists():
        pytest.skip("训练效用报告尚未生成（需跑 scripts/eval_training_utility.py）")
    return json.loads(p.read_text(encoding="utf-8"))


# ── 判据本体 ────────────────────────────────────────────────────────────────


def test_噪声地板必须是差值而非绝对值(mod, payload):
    """核心回归：噪声地板的定义 = |probe − ref|，不是 probe。"""
    p = payload["ppl"]
    ref = payload["utility"]["reference_arm"]
    floor = payload["utility"]["noise_floor"]

    expected = abs(p["noise_probe"] - p[ref])
    assert floor == pytest.approx(expected, abs=1e-9), (
        f"noise_floor={floor} 但|probe−ref|={expected:.6f}。"
        "若两者差一个数量级，说明存的是噪声臂的**绝对 ppl**——"
        "那会让判据恒真（任何 |Δ| 都小于一个 ppl 量级）。"
    )


def test_判据量纲与_delta同量级(mod, payload):
    """Δ 与 noise_floor 必须是**同一个量纲**（都是差值）。

    这是上一条的加强版：一个 ppl 值再小（比如 0.004）也仍是绝对量，
    而 Δ 是 0.19 —— 差 46 倍，判据照样恒真。
    """
    delta = abs(payload["utility"]["delta_ppl"])
    floor = payload["utility"]["noise_floor"]

    # 真实噪声地板下，Δ 与 floor 应当可比（倍数为个位数到几十倍量级）
    ratio = delta / floor if floor else float("inf")
    assert 0.05 < ratio < 100, (
        f"Δ/floor = {ratio:.3g} 超出合理区间。"
        "太小→ 判据过严（真差异被判成噪声）；太大 → 量纲错配（拿绝对量当门槛）。"
    )


# ── 判据的方向性（防止期望写反）────────────────────────────────────────────


def test_脚本必须自己算出差值地板而不是绝对值(mod):
    """**直接验脚本**，而不是只读报告 JSON。

    ⚠️ 这是本文件自己的一个盲区（实测踩到）：前两条测试读的是
    `payload["utility"]["noise_floor"]` —— 那是**上一次写文件时的产物**。
    于是把脚本里的判据改回恒假版（`noise_floor = probe`），
    报告 JSON 没变 → **两条测试照样全绿**，变异被放过。

    判据的代码路径必须被直接调用，否则它退化了我不会知道。
    """
    ppl = {
        "base": 15.72,
        "unclean_matched": 7.8637,
        "cleaned": 8.0587,
        "noise_probe": 7.8200,
    }
    assert hasattr(mod, "compute_noise_floor"), (
        "脚本尚未提出 compute_noise_floor() —— 噪声地板还是内联表达式，"
        "无法被单独测试（正是本轮那个恒假判据藏身的地方）"
    )
    floor = mod.compute_noise_floor(ppl, "unclean_matched")
    assert floor == pytest.approx(abs(7.8200 - 7.8637), abs=1e-9), (
        f"脚本算出的噪声地板 = {floor}，应为 |probe−ref| = 0.0437。"
        "若等于 7.82（绝对 ppl），判据恒真。"
    )


@pytest.mark.parametrize(
    ("probe", "cleaned", "ref", "expect"),
    [
        # 地板 = |7.01− 7.00| = 0.01；Δ=+0.50 远超 → 显著更好
        (7.01, 6.50, 7.00, "显著更好"),
        # 地板 0.01；Δ=−0.50 远超 → 显著更差（**这正是本项目的真实结论**）
        (7.01, 7.50, 7.00, "显著更差"),
        # 地板 = |7.010− 7.000| = 0.010；Δ=+0.005 < 地板 → 真·测不出
        (7.010, 6.995, 7.000, "测不出"),
    ],
)
def test_判定方向必须与符号一致(mod, probe, cleaned, ref, expect):
    """喂构造输入，验判据三元。

    ⚠️ 这里刻意包含一条「Δ 与地板同量级且略大」的用例 ——
    上一版恒假判据对**所有**输入都返回「测不出」，只有构造输入才暴露。
    """
    floor = abs(probe - ref)
    delta = ref - cleaned
    verdict = mod.classify_verdict(delta, floor) if hasattr(mod, "classify_verdict") else None
    if verdict is None:
        pytest.skip("脚本尚未提出 classify_verdict()，判据仍是内联三元")
    assert expect in verdict, (
        f"Δ={delta:+.4f} floor={floor:.4f} 期望含 {expect!r}，实得 {verdict!r}"
    )


def test_取绝对值当门槛必须被识别为恒假(mod):
    """**反向验证**：把「绝对值当门槛」这种实现放回去，判据必须给出不同结论。

    没有这条，上面三条构造用例就只是「测三个点」，拦不住量纲退化。
    """
    if not hasattr(mod, "classify_verdict"):
        pytest.skip("脚本尚未提出 classify_verdict()")
    delta = -0.195
    good = mod.classify_verdict(delta, abs(7.8200 - 7.8637))
    # 如果实现回退成拿绝对 ppl 当门槛：
    bad = mod.classify_verdict(delta, 7.8200)
    assert good != bad, "传入绝对 ppl 作为地板时结论居然一样 → 判据又恒真了"
    assert "测不出" in bad, "传入绝对 ppl（量级 7.8）时 Δ=−0.195 应被判为测不出"
    assert "更差" in good, "传入真实地板（0.0437）时 Δ=−0.195 应判为显著更差"
