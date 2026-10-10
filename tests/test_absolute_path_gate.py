"""绝对路径门禁的「装置可用性」判据。

## 为什么这组测试存在

`scripts/no_absolute_paths.py` 曾在**目录不存在时直接 `[RED]` + 返回 2**。
理由大概是「目录都不在了，肯定哪里不对」。但 `warehouses/` 当时
**未入库** —— CI 的干净检出必然没有这个目录 → **门禁永久红**。

这是最坏的一种门禁失效形态：**报的是「问题」，实际是「装置不可用」**。
与 skill 里 `ENV_ERROR_CODES` 那条纪律同源 ——
`rc=2`（用法错/装置错）绝不能当成「判据发现了问题」。

修法是把三态分开，而且**每一态都要能自证**：

| 目录状态 | 期望 rc | 期望输出 | 含义 |
|---|---|---|---|
| 不存在 | 0 | `[WARN] 未执行` | 装置不可用，不挡CI |
| 存在但无可扫源文件 | 0 | `[WARN] 未执行` | 同上（空目录不是「干净」） |
| 有源文件且干净 | 0 | `[OK] … N 个源文件` | 真扫过了 |
| 有源文件且含硬编码 | 1 | `[RED] … 发现 N 处` | 真问题 |

⚠️ **「空目录 = 通过」是新的假绿**：只判 `not hits` 会让
「一个文件都没扫」和「扫过了都没问题」混为一谈。
所以判据用**实际扫过的文件数**，不是目录是否存在。

⚠️ 同时验证一条纪律：`scan()` 与「数文件」必须用**同一个过滤条件**。
本项目栽过「注释里写了『别复制过滤规则』，然后当场复制了」。
复制后的分叉方向恰恰是「把没扫的算成扫过的」= 永远朝假绿偏。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "no_absolute_paths.py"


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("abs_gate", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(gate, root: pathlib.Path) -> tuple[int, str, str]:
    """把 WAREHOUSES 指到临时目录跑一次 main，返回 (rc, stdout, stderr)。"""
    orig = gate.WAREHOUSES
    gate.WAREHOUSES = root
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gate.main()
    finally:
        gate.WAREHOUSES = orig
    return rc, out.getvalue(), err.getvalue()


def test_目录不存在必须报未执行而不是红色(gate, tmp_path):
    """CI 干净检出里 warehouses/ 可能不存在 → 不能永久红。"""
    rc, out, err = _run(gate, tmp_path / "not_there")
    assert rc == 0, f"装置不可用不该挡 CI（rc={rc}）：{err}"
    assert "[WARN]" in err, err
    assert "未执行" in err, err
    # 关键：不能自称「通过」—— 那会把「没扫」写成「扫过了」
    assert "[OK]" not in out, out
    assert "[RED]" not in err, err


def test_空目录必须报未执行而不是通过(gate, tmp_path):
    """只有生成物目录 = 一个源文件都没扫到≠ 干净。"""
    root = tmp_path / "wh"
    (root / "target").mkdir(parents=True)          # 生成物目录，应被过滤
    (root / "target" / "compiled.sql").write_text(
        "select * from read_parquet('C:/Users/x/a.parquet')", encoding="utf-8"
    )
    rc, out, err = _run(gate, root)
    assert rc == 0, f"空目录不该挡 CI（rc={rc}）：{err}"
    assert "[WARN]" in err and "未执行" in err, err
    assert "[OK]" not in out, "空目录不能自称 [OK] —— 那是把「没扫」写成「通过」"


def test_真实目录必须报出扫过的文件数(gate):
    """真跑一遍，且必须带上文件数（证明确实扫过，不是空转）。"""
    root = REPO_ROOT / "warehouses"
    if not root.exists():
        pytest.skip("warehouses/ 不存在（本仓库未入库）")
    rc, out, err = _run(gate, root)
    assert rc == 0, err
    assert "[OK]" in out, out
    assert "个源文件" in out, f"必须报出扫过的文件数：{out}"
    n = int(out.split("个源文件")[0].split("下 ")[-1].strip())
    assert n > 0, out


def test_硬编码路径必须红且给出位置(gate, tmp_path):
    """正向：真问题必须拦住，且要能定位到文件与行号。"""
    root = tmp_path / "wh"
    root.mkdir()
    (root / "m.sql").write_text(
        "select 1\nselect * from read_parquet('C:/Users/someone/d.parquet')\n",
        encoding="utf-8",
    )
    rc, out, err = _run(gate, root)
    assert rc == 1, f"真问题必须红（rc={rc}）：{out}{err}"
    # RED 正文走stdout（只有「装置不可用」的 WARN 走 stderr，
    # 这样 CI 日志里「结论」和「环境提醒」能一眼分开）
    assert "[RED]" in out, out + err
    assert "m.sql:2" in out, f"必须能定位到文件与行号：{out}"


def test_过滤条件只有一处定义(gate):
    """`scan()` 与「数文件」必须共用 `_scannable_files()`。

    ⚠️ 本项目栽过：注释写「别复制过滤规则」，然后当场复制了一份。
    后果不是报错，是**分叉永远朝假绿方向偏**（多算或少扫都会
    让「实际没扫」被当成「扫过了」）。
    """
    import inspect

    src = inspect.getsource(gate.scan)
    assert "_scannable_files(root)" in src, "scan() 必须走统一过滤入口"
    # scan 里不应再出现裸的 rglob / SCAN_SUFFIXES / _is_generated 过滤
    assert "root.rglob" not in src, "scan() 里不该再自己 rglob（过滤条件会分叉）"
    assert "_is_generated(" not in src, "scan() 里不该再自己判生成物"


def test_扫描集合与计数一致(gate):
    """数量必须由同一个集合算出，不能是另一套规则数出来的。"""
    root = REPO_ROOT / "warehouses"
    if not root.exists():
        pytest.skip("warehouses/ 不存在")
    files = gate._scannable_files(root)
    assert gate._count_scannable(root) == len(files)
    # 逐个确认 scan 真的能看到它们（用只含注释的空内容不会命中，
    # 所以这里只验证「文件存在且可读」，命中逻辑由上面那条测试负责）
    for f in files:
        assert f.is_file(), f
