"""稳健统计小工具：中位数 / MAD 尺度 / 稳健限。

**为什么单独成模块**（不是洁癖，是被教训过）：
同一套「中位数 + margin × 1.4826×MAD」现在有 **两个** 消费者 ——
`sensor_multivariate` 的 T²/SPE 控制限，和 `sensor_range` 在 **没有手册量程** 时
所用的数据包络（R8，2026-09-22）。式子各抄一遍迟早漂移，而本项目已经因为
「同一件事有两个数字」踩过一次（`docs/ENGINEERING_NOTES.md` #70）。所以这里做
唯一真相源：算子与离线脚本（`scripts/build_envelopes.py`）都从这里取。
"""

from __future__ import annotations

MAD_TO_SIGMA = 1.4826
"""MAD → σ 的一致性因子：正态下 `σ ≈ 1.4826 × MAD`。

做成常量而不是内联字面量，是为了让「尺度口径」只有一个出处。
"""


def median(values: list[float]) -> float:
    """中位数（偶数个取两中位数均值）；空列表给 0.0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def mad_scale(values: list[float], center: float) -> float:
    """稳健尺度 `1.4826 × MAD`（与 `sensor_drift` 的 `mad` 档**同一口径**）。

    取 MAD 而非标准差是为了抗污染：30% 的样本被判离群也不动摇（崩溃点 50%）。
    """
    return MAD_TO_SIGMA * median([abs(v - center) for v in values])


def robust_limit(values: list[float], margin: float) -> float | None:
    """稳健上界：`中位数 + margin × 1.4826×MAD`。

    **刻意不用「参考集 99 分位」这个 MSPC 标准写法**——它在参考集小的时候会退化：
    `n=50` 时 99 分位就是**最大值**，再乘 margin 之后没有任何点能超过它，
    判据会静默变成空转。实测踩过：向真实窗注入 330 条已知缺陷，
    分位版本一条都没抓到（`docs/ENGINEERING_NOTES.md` #75）。
    与 `sensor_drift` 的 `mad` 档**同一口径**（中心中位数、尺度 MAD），
    对离群与污染都稳健，且不依赖任何分布假设。

    返回 `None` = 样本**零离散度**（定不出限 → 调用方一律记未评，不许猜）。
    """
    if not values:
        return None
    center = median(values)
    scale = mad_scale(values, center)
    if scale <= 0:
        return None
    return center + margin * scale
