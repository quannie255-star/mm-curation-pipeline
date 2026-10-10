"""`verify_claims.py` 自身的元测试 —— 门禁崩了等于门禁不存在。

⚠️ 2026-10-09 实测栽过：新增一条 claim 指到 `leakage_check.minhash_leaks`
（**list**），门禁在打印表格时炸
`TypeError: unsupported format string passed to list.__format__`。
后果比红更坏：**输出停在倒数第 5 行，前面所有 PASS 行原样刷屏**，
看着像「跑通了」，实际后面的 claim 根本没被校验 —— 这是一种假绿。

所以要锁两件事：
① `check_claim` 对非标量返回 `pointer-broken`（不依赖格式化兜底）；
② `_fmt_cell` 对任意Python 值都不抛异常。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_GATE = ROOT / "scripts" / "verify_claims.py"


def _load():
    spec = importlib.util.spec_from_file_location("vc_meta", _GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def vc():
    return _load()


def test_门禁模块能导入且暴露所需接口(vc):
    for name in ("check_claim", "check_facades", "check_coverage", "_fmt_cell"):
        assert hasattr(vc, name), f"verify_claims.py 缺 {name}"


@pytest.mark.parametrize(
    "value",
    [
        None, True, False, 0, -1, 3.5, "abc", "",
        [], [1, 2, 3], {}, {"a": 1}, set(), (1, 2),
    ],
)
def test_fmt_cell对任意值都不抛(vc, value):
    """回归：list 曾让门禁直接崩在f-string 上。"""
    out = vc._fmt_cell(value)
    assert isinstance(out, str)
    assert out != "", "None/空容器也要给出可见占位，不能是空串"


def test_fmt_cell把容器标出类型与长度(vc):
    """容器必须能看出「里面有几个」—— 否则排查时看不出漏检了多少项。"""
    assert "len=3" in vc._fmt_cell([1, 2, 3])
    assert "3" in vc._fmt_cell({"a": 1, "b": 2, "c": 3})
    assert "dict" in vc._fmt_cell({"a": 1})
    assert vc._fmt_cell(None) == "—"


def test_非标量claim判指针断而非通过(vc, tmp_path):
    """核心回归：list 型pointer 不能算 PASS。"""
    doc = tmp_path / "m.json"
    doc.write_text(json.dumps({"leak": [{"a": 1}, {"a": 2}, {"a": 3}], "n": 3}),
                   encoding="utf-8")
    r = vc.check_claim({"id": "x", "file": str(doc), "pointer": "leak",
                        "expected": 3, "tol": 0.0, "comparator": "exact"})
    assert r["status"] == "pointer-broken", (
        f"list 型指针被判成 {r['status']} —— 会让「锁 3 对泄漏」变成一句空话")
    # 标量兄弟字段仍然可用
    r2 = vc.check_claim({"id": "y", "file": str(doc), "pointer": "n",
                         "expected": 3, "tol": 0.0, "comparator": "exact"})
    assert r2["status"] == "pass"


def test_标量漂移仍判drift(vc, tmp_path):
    """新逻辑不能顺手把真漂移也放过。"""
    doc = tmp_path / "r.json"
    doc.write_text(json.dumps({"v": 1.0}), encoding="utf-8")
    r = vc.check_claim({"id": "z", "file": str(doc), "pointer": "v",
                        "expected": 2.0, "tol": 1e-6, "comparator": "approx"})
    assert r["status"] == "drift"
    ok = vc.check_claim({"id": "z", "file": str(doc), "pointer": "v",
                         "expected": 1.0, "tol": 1e-6, "comparator": "approx"})
    assert ok["status"] == "pass"


def test_全量注册表的claim都能被渲染不崩(tmp_path):
    """真跑一次门禁并检查 rc + 输出尾部（崩了就没有尾部）。

    ⚠️ 必须带 `--reports-missing skip`（2026-10-10 修）：`data/reports/` 是**生成物、
    不入库**，干净检出（CI）里 28 条 claim 必然缺报告。不带这个开关时门禁默认
    `fail` → 干净检出里 rc=1 → **本机全绿、CI 红**（本轮实测栽过）。
    带 skip 后两种环境都是 rc=0，而 facade / derived / coverage 三层照常被校验。
    """
    reg = ROOT / "docs" / "claims.json"
    assert reg.exists(), "docs/claims.json 不存在"
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(_GATE), "--reports-missing", "skip"],
        capture_output=True, text=True, cwd=str(ROOT), encoding="utf-8",
        errors="replace", timeout=300,
    )
    out = r.stdout + r.stderr
    assert "Traceback" not in out, (
        "门禁抛异常了 —— 崩溃时前面刷屏的 PASS 行会让人误以为跑通\n"
        + out[-1500:])
    assert r.returncode == 0, f"门禁 rc={r.returncode}\n{out[-1500:]}"
    # 必须有完整的三行汇总，缺任何一行都说明中途退出
    for kw in ("条 claim", "条门面", "覆盖率棘轮"):
        assert kw in out, f"汇总缺「{kw}」→ 门禁中途退出了\n{out[-800:]}"
