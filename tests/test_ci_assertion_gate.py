"""`scripts/mutation_test_ci_assertions.py` 的测试。

## 为什么这些测试存在

那个变异脚本在开发过程中连踩**四个装置坑**，每一个都表现为
「判据看起来全部有效，实际一条都没执行」：

| # | 坑 | 表象 | 被谁抓住 |
|---|---|---|---|
| 1 | Windows 路径里的反斜杠被 bash 吃掉 | rc=127 全红 → 「4/4 通过」 | 装置自检 |
| 2 | `cwd` 传 MSYS 路径给 `CreateProcess` | `NotADirectoryError` | 装置自检 |
| 3 | PATH 上的 `bash` 是 **WSL2**，看不到 Windows Temp | `cd` 失败 → 仍然全红 | 装置自检 |
| 4 | 锚点没含闭合引号，抽取的 bash 语法不完整 | 前面检查全跑完，最后 EOF 报错 | 语法断言 |

坑 1-3 的共同点：**失败形态与「判据正确地拦截」完全一样**（都是红）。
不主动区分就永远发现不了，而报告会写「全部通过」。

坑 4 更隐蔽：所有检查都已打印通过，只在最后一句 echo 报 EOF ——
看上去像「差一点就通了」，很容易被当成环境噪声忽略。

所以这里把「装置本身是否可信」和「判据是否有效」**分别**测一遍。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "mutation_test_ci_assertions.py"


def _load():
    spec = importlib.util.spec_from_file_location("mca", SCRIPT)
    assert spec and spec.loader, f"无法加载 {SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    if sys.platform == "win32":
        pytest.importorskip("subprocess")
    return _load()


# ── 判据侧：4 组输入必须给出 4 个不同的答案 ────────────────────────
# 只测「跑得通」是不够的 —— 恒真的判据也跑得通。
def test_真实输入必须绿(mod):
    rc, out = mod._run(mod.bash_for_tests(), mod.REAL)
    assert rc == 0, f"正确输入不该变红：{out[-400:]}"


def test_汇总行缺失必须红(mod):
    """facade 层被整体删掉 —— 最典型的「门禁绿了但没在扫」。"""
    txt = "\n".join(ln for ln in mod.REAL.splitlines() if "条门面" not in ln) + "\n"
    rc, out = mod._run(mod.bash_for_tests(), txt)
    assert rc != 0
    assert "缺少某一层" in out, out[-300:]


def test_部分未扫必须红(mod):
    """只校验 85/87 —— 条数看起来齐全，实际漏了两条。"""
    txt = mod.REAL.replace("87 条门面：87 PASS", "87 条门面：85 PASS")
    rc, out = mod._run(mod.bash_for_tests(), txt)
    assert rc != 0
    assert "85/87" in out, out[-300:]


def test_两处真相不一致必须红(mod):
    """注册表声明 90、汇总说 87 —— 有人在两边各改了一个数。"""
    txt = mod.REAL.replace("门面条数 87（下限 87）", "门面条数 90（下限 90）")
    rc, out = mod._run(mod.bash_for_tests(), txt)
    assert rc != 0
    assert "两处真相不一致" in out, out[-300:]


# ── 双向对照：现行版拦得住 + 旧版拦不住 ──────────────────────────
def test_旧写法漏检而现行版拦住(mod):
    """这是本轮那个缺陷的直接证据。

    旧写法 `报绿 && n < 80` 的第二个条件在上游 exit 之后**恒假**，
    所以喂「只扫到 85/87」它会绿 —— 即漏检。
    现行写法必须红。两者缺一，结论都不成立。
    """
    bad = mod.REAL.replace("87 条门面：87 PASS", "87 条门面：85 PASS")
    cur = mod._run(mod.bash_for_tests(), bad)
    assert cur[0] != 0, "现行版竟然没拦住"

    old_bash = mod.bash_for_tests().replace(
        'if [ "$fac_pass" != "$fac_total" ]; then',
        'if echo "$fac" | grep -q "0 漂移" && [ "$n" -lt 80 ]; then',
    )
    assert old_bash != mod.bash_for_tests(), "变异锚点失配 —— 测的不是那个缺陷"
    old = mod._run(old_bash, bad)
    assert old[0] == 0, "旧写法居然拦住了，说明缺陷描述与代码不符"


# ── 装置侧：保证「红」不是环境噪音 ────────────────────────────────
def test_取到的bash必须真的能看到工作目录(mod):
    """装置自检。

    本轮三个坑（路径转义 / cwd 形式 / WSL bash）的**共同表象**都是
    「所有用例都红」。若选到的 bash 根本看不到工作目录，
    上面那些「必须红」的测试会**因为错误的原因**通过。
    所以这条必须独立成立：先证明装置活着，再谈判据结论。
    """
    rc, out = mod._run("cat gate-claims.txt" + "\n", mod.REAL)
    assert rc == 0, f"取到的 bash 读不到工作目录文件：{out[-300:]}"
    assert "条门面" in out, out[-300:]


def test_装置故障码必须被当成装置故障而非判据结论(mod):
    """rc=126/127/2 是「命令没跑起来」，不是「判据说不行」。

    如果变异脚本把它们当红，报告就会把
    「bash 没启动」写成「判据成功拦截了问题」。
    """
    assert mod.ENV_ERROR_CODES == (126, 127, 2)
    # 造一个真的找不到命令的场景，确认它落进这个集合
    rc, out = mod._run("this_command_does_not_exist_zzz" + "\n", mod.REAL)
    assert rc == 127, f"期望 127，实际 {rc}：{out[-200:]}"
    assert rc in mod.ENV_ERROR_CODES


def test_posix分支不得依赖windows路径(mod):
    """跨平台：`C:/Program Files/Git/...` 在 ubuntu 上不存在。

    只留Windows 分支 → CI 上直接抛 RuntimeError 中止，
    这是「本地绿 / CI 因环境红」的典型。
        """
    import os

    real = os.name
    try:
        os.name = "posix"
        got = mod._find_bash()
    finally:
        os.name = real
    assert got, "POSIX 分支必须给出一个可执行文件"
    assert pathlib.Path(got).exists(), got


def test_断言从工作流现取而非本地硬编码(mod):
    """判据只允许有一个定义处。

    抄一份到测试文件里的后果：工作流改了没人记得回来改测试，
    于是测试一直在验一个**已经不跑的**判据 —— 变异悄悄失效。
    """
    text = (REPO_ROOT / ".github/workflows/gate-ci.yml").read_text(encoding="utf-8")
    got = mod._extract_assert()
    assert got.strip(), "抽取结果为空"
    # 现取的内容必须逐行出现在工作流里
    for line in got.splitlines()[:5]:
        assert line in text, f"抽取出的行不在工作流里：{line!r}"


def test_抽取的bash语法必须完整(mod):
    """坑 4 的回归：锚点不含闭合引号 → 语法不完整但报错在最后一行。

    用 `bash -n` 做纯语法检查：不执行、只解析。
        """
    import subprocess

    body = mod.bash_for_tests()
    proc = subprocess.run(
        [mod._find_bash(), "-n", "-s"],
        input=body + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"抽取的 bash 语法不完整：{proc.stderr[-300:]}"
