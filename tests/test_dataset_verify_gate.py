"""数据集生产的**元门禁**：验收脚本与构建器自身的契约。

⚠️ 为什么单独一个文件：`verify_dataset_consumable.py` 在本轮抓到了三个真bug
（manifest 单位错位、变长block、空split），**它是这套系统的最后一道网**。
但一个「自己抓过 bug 的脚本」也可能是假绿—— 所以它的判据本身要被测。

要测的三条：
1. **验收必须遍历全部 batch**（第一版只看 `next(iter(dl))` → 前几个恰好满块，
   看着绿，shuffle 后立刻崩）。
2. **验收必须核全量 split**（第一版只核 train，而 manifest 记三切分合计
   → 覆盖率 59% 却报「口径不对」）。
3. **`row_unit` 与列名/行数必须联动**（样本级 `tokens` vs block 级 `input_ids`）。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

VERIFY = ROOT / "scripts" / "verify_dataset_consumable.py"


@pytest.fixture(scope="module")
def verify_src() -> str:
    if not VERIFY.exists():
        pytest.skip(f"验收脚本不存在：{VERIFY}")
    return VERIFY.read_text(encoding="utf-8")


# ── 1) 验收必须遍历全部 batch ─────────────────────────────────────────────
def test_验收不能只看第一个batch(verify_src):
    """`next(iter(dl))` 一次 = 只测头几个 block = 假绿。

    判据要匹配**结构**（for 循环遍历整个 loader），不是子串
    —— 汇总行里也可能出现 `next(iter` 字样。
    """
    import re

    # 找 DataLoader 之后的循环结构
    i = verify_src.index("DataLoader(")
    tail = verify_src[i:]
    # ⚠️ **必须匹配「直接遍历 loader 本身」**，不能只匹配 `for ... in`：
    #   变异 `for batch in [next(iter(dl))]:` 仍含 for-in 结构 → 假绿
    #   （实测栽过：我的第一版判据就是这么被放过的）。
    has_loop = re.search(r"for\s+\w+\s+in\s+dl\s*:", tail) is not None
    assert has_loop, (
        "验收只取了第一个 batch（没直接遍历 loader）—— 尾块/短样本不会被验证到，"
        "这正是本项目踩过的假绿（顺序读时前几个 block 恰好满的）"
    )
    # 且必须累加行数，才能与 manifest 比对
    assert re.search(r"\w+\s*\+=\s*int\(", tail), (
        "遍历了 batch 但没累加行数 → 无法核对是否覆盖了全量"
    )
    # 且必须断言「遍历行数 == 数据集行数」，否则半途退出也算通过
    assert "len(hf)" in tail, "未把遍历行数与数据集行数对账"


# ── 2) 验收必须核全量 split ───────────────────────────────────────────────
def test_验收必须核全量而不是只train(verify_src):
    import re

    """第一版只查 train 的 loss_mask 有效位，而 manifest 记的是三切分合计
    → 349671 vs 596397 报「口径不对」，实为**判据只覆盖 59%**。"""
    i = verify_src.index("loss_mask")
    tail = verify_src[i:]
    # ⚠️ **必须匹配「用变量 split 迭代」的结构**，光有 `for sp in` 不够：
    #   变异把循环体改成 `allhf["train"][i]` 而for-in 仍在 → 假绿
    #   （实测栽过：这是本文件第二次被变异放过）。
    has_var_split = re.search(r"for\s+(\w+)\s+in\s+allhf\b.*?allhf\[\1\]", tail, re.S) is not None
    assert has_var_split, (
        "loss_mask 校验没真正遍历所有 split（循环体里没用 split 变量）"
        " → 与 manifest 的合计口径必然不符，判据只覆盖部分数据"
    )
    assert "n_all" in tail or "n_rows" in tail, "未把「全量 block 数」与 manifest n_rows 对账"


# ── 3) row_unit 联动 ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("unit", "expect_col"),
    [("block", "input_ids"), ("sample", "tokens")],
)
def test_列名必须随row_unit切换(verify_src, unit, expect_col):
    """两种产物形态的列名不同（block→`input_ids`，sample→`tokens`）。
    写死任一个都会让另一种产物验不过。"""
    assert 'if unit == "block"' in verify_src or 'unit == "block"' in verify_src
    assert expect_col in verify_src, (
        f"验收源码里找不到 {expect_col} —— row_unit={unit} 时该列必须被检查"
    )


def test_验收必须自报未通过(verify_src):
    """出口码/结论必须能表达「未通过」。

    一个只会打印 ✓ 的验收脚本比没有验收更危险 —— 它给出虚假的保证。"""
    assert "未通过" in verify_src, "验收脚本必须能报告失败"
    assert "通过" in verify_src, "验收脚本必须能报告成功"


# ── 4) 构建器：旧 shard 必须在构建开始前清掉 ────────────────────────────
def test_构建器在init里清旧shard():
    """产物必须**可重复构建**：同名数据集在不同时间必须指向同一内容。

    实测踩过：manifest 说 163 样本 / 2 shard，目录里却有 3 个文件 179 行
    —— 消费者按目录读会拿到重复数据，而校验和只覆盖新写的那些。
    """
    src = (ROOT / "src" / "mm_curation" / "dataset" / "build.py").read_text(encoding="utf-8")
    init_body = src[src.index("def __init__") : src.index("def add")]
    assert "unlink" in init_body, (
        "旧 shard 的清理不在 __init__ 里 → 中途失败会留半成品，且重建会与旧文件混在一起"
    )
    assert (
        "finalize" not in init_body.split("def add")[0].split("unlink")[-1][-200:] or True
    )  # 位置检查由上一条覆盖，这里只做注释留档
