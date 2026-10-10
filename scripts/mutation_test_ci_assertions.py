r"""CI 断言本身的变异测试：证明 `gate-ci.yml` 里的 bash 断言还会红。

## 为什么这个文件存在（不是形式主义）

`gate-ci.yml` 的两条断言是**唯一**能拦住
「门禁条数看起来齐全、实际一层没扫」的东西。而断言一旦恒真，
CI 会一直绿 —— 这正是本项目反复栽的形态（记忆里的「恒真判据是最大的假绿来源」）。

**本轮真抓到的缺陷**（不是假设，是这个文件被写出来的原因）：

```bash
if echo "$fac" | grep -q '门禁：0\|0 漂移' && [ "$n" -lt 80 ]; then
```

这段的第二个条件**恒假** —— 上一段已经在 `n < 80` 时 `exit 1`，
所以能走到这里时 `n >= 80` 必然成立。结果是：

* 它看起来是「facade 报绿但条数不足」的第二道保险；
* 实际上**永远不会响**；
* 而注释里还写着它就是干这个的。

**一段永远不会响的保险比没有保险更坏** —— 它让人以为覆盖面被守着。
修法是比较**两个独立的量**（注册表声明多少 vs 实际扫多少），
见 `gate-ci.yml` 断言 2b。

## 为什么要在本机跑 bash（而不是只靠 yaml 语法检查）

断言的逻辑在 bash 里，`actionlint` 只能验语法、验不了语义。
而本轮踩的坑**语法完全正确**。所以这里直接调真 bash 跑四组输入：

| 组 | 输入 | 期望 | 验的是什么 |
|---|---|---|---|
| 真实 | 模拟干净检出的真输出 | 绿 | 正确实现不误红 |
| 变体 A | 删掉 facade 汇总行 | 红 | 「层被删」能被抓 |
| 变体 B | facade PASS 少于总数 | 红 | 「部分没扫」能被抓 |
| 变体 C | facade 总数 ≠ 注册表下限 | 红 | 两处真相不一致能被抓 |

**只跑真实输入是不够的** —— 一个恒真的断言同样会在真实输入上绿。
必须有「错误输入必须变红」这一半，否则测的是「脚本没崩」而不是「判据有效」。

## 实现纪律（沿用本项目的判据纪律）

* 断言逻辑**只定义一次**：把 gate-ci.yml 里那段 bash 抽成 `ASSERT_BASH`，
  测它时用的是**同一段字符**，不许另抄一份（抄两份 = 测的不是在跑的那个）。
* 每条错误输入都必须**真的让 rc != 0**；rc=0 的「红」等于没红。
* 全部在临时目录里造输入，**不碰仓库任何文件**。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# bash 的「装置故障码」：命令找不到 / 用法错。
# ⚠️ 这些**不是**判据结论，是「断言根本没执行」。
# 混进红项会造出「全红 = 全对」的漂亮假绿（本轮踩过）。
ENV_ERROR_CODES = (126, 127, 2)

# 被测对象**不定义在这里** —— 它唯一的定义处是 `gate-ci.yml`。
# 本项目栽过的坑：判据抄两份 = 测的不是在跑的那个，而且工作流改了不会有人
# 记得回来改测试，于是变异悄悄失效。所以下面 `_extract_assert()` 现取，
# 取不到就 assert 失败（不许「跳过」——跳过等于测了个空）。


def _extract_assert() -> str:
    """从 gate-ci.yml 里抠出断言 2 的 bash 段。

    抠不出来就**失败**，不许「跳过」——记忆里明写：
    「变异/审计脚本禁止硬编码锚点；现取；找不到就 assert 失败，不要跳过」。
    跳过 = 变异悄悄失效 = 测了个空。
    """
    wf = REPO_ROOT / ".github" / "workflows" / "gate-ci.yml"
    assert wf.exists(), f"工作流不存在：{wf}"
    text = wf.read_text(encoding="utf-8")
    anchor = "fac=$(grep -E '^[0-9]+ 条门面：'"
    assert anchor in text, "工作流里找不到断言 2 的锚点 —— 变异测试中止"
    # 从 anchor 抠到「声明一致」那句 echo（断言 2 的末尾）。
    # ⚠️ 锚点必须包含**闭合引号**：踩过一次 —— 锚点只到
    # 「声明一致」这几个字，echo 语句的收尾 `"` 被切在下一行之外，
    # bash 报 `unexpected EOF while looking for matching '"'`。
    # 症状极隐蔽：前面所有检查都已跑完并打印，看起来像"差一点点就通了"。
    end_anchor = '两层都在扫，且扫到的条数与声明一致"'
    assert end_anchor in text, "工作流里找不到断言 2 的结尾 —— 变异测试中止"
    i = text.index(anchor)
    j = text.index(end_anchor, i) + len(end_anchor)
    body = text[i:j]
    # 去掉 YAML 的行首缩进，bash 对此敏感
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return "\n".join(lines)


def bash_for_tests() -> str:
    """组装待验证的 bash：**从工作流现取**，不本地硬编码。

    `set -euo pipefail` 是 workflow step 自己的设置，不在被抽取的断言体里，
    这里补上 —— 否则测的是「关掉 -e 的另一次运行」，漏掉 -e 的失败路径。

    判据只有这一个定义处（工作流）。抄一份到本脚本会让
    「工作流改了但脚本没改」变成隐形失效。
    """
    body = _extract_assert()
    assert body.strip(), "从工作流抽出的断言为空"
    return "set -euo pipefail" + chr(10) + body


def _find_bash() -> str:
    """找到一个**能看到 Windows 工作目录**的 bash。

    ⚠️ 本轮最大的一个装置坑：**PATH 上的 `bash` 是 WSL2 的**
    （实测 `bash -c 'uname -a'` → `Linux moka ... microsoft-standard-WSL2`）。
    WSL 看不到 Windows 的 Temp 目录，于是 `cd` 必失败 →
    四组用例全红 → 报告上写「判据全部有效」，**实际一条都没执行**。

    Git Bash（`C:/Program Files/Git/bin/bash.exe`）实测能看到
    Windows 工作目录并正常读文件，所以显式找它。
    找不到就**报错中止**，绝不回退到 PATH 上的 `bash` ——
    回退等于把「装置不可用」伪装成「判据生效」。

    ⚠️ 第二个坑（跨平台）：上面这两个路径是 **Windows 专有**，
    在 ubuntu CI 上 `pathlib.Path("C:/Program Files/...").exists()` 是 False。
    若只留这一个分支，CI 上会直接抛 RuntimeError 中止 ——
    **又是「本地绿 / CI 因环境红」**（记忆里明写这条纪律）。
    所以必须分平台：Windows 走 Git Bash，POSIX（含 ubuntu CI）
    走 PATH 上的 `bash`。装置自检会独立验证选出来的那个
    **真的能看到工作目录**，所以猜错只会让自检红，不会变成假绿。
    """
    if os.name != "nt":
        posix = shutil.which("bash")
        if not posix:
            raise RuntimeError("找不到 bash，无法验证 gate-ci.yml 里的断言")
        return posix
    for cand in ("C:/Program Files/Git/bin/bash.exe", "C:/Program Files/Git/usr/bin/bash.exe"):
        if pathlib.Path(cand).exists():
            return cand
    raise RuntimeError(
        "找不到 Git Bash —— 无法验证 gate-ci.yml 里的 bash 断言。"
        "拒绝回退到 PATH 上的 `bash`（本机实测那是 WSL2，"
        "看不到 Windows 临时目录，会造出全红假绿）。"
    )


def _run(bash_body: str, gate_txt: str) -> tuple[int, str]:
    """在临时目录里跑一段 bash + 一份门禁输出，返回 (rc, 输出)。

    ⚠️ 两条平台纪律（本轮各踩一次，都是**装置坑**，不是判据结论）：

    1. **不把脚本路径当参数传**。`bash /c/Users/.../assert.sh` 在本机报
       `No such file or directory` —— Git Bash 的 `/c/` 挂载对
       Python 侧 `subprocess` 传的路径并不稳定可见。
       改成把脚本**从 stdin 喂给 `bash -s`**，并在 bash 内部 `cd`：
       `bash -s` + 正文里的 `cd '<native path>'`。路径用 Windows 原生
       形式（反斜杠会被 bash 吃，所以先转成正斜杠），MSYS 能认。
    2. **cwd 必须是原生 Windows 路径**：给 `CreateProcess` 传 `/c/...`
       会抛 `NotADirectoryError`。
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="gate_assert_"))
    (tmp / "gate-claims.txt").write_text(gate_txt, encoding="utf-8")
    script = bash_body + chr(10)
    proc = subprocess.run(
        [_find_bash(), "-s"],
        # ⚠️ cwd 必须是**原生 Windows 路径**：给 `CreateProcess` 传 `/c/...`
        # 会抛 `NotADirectoryError`。Git Bash 会把它映射成 `/tmp/...`。
        cwd=str(tmp),
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout + proc.stderr)


REAL = """某某claim验证汇总
门面条数 87（下限 87） pass
87 条门面：87 PASS, 0 漂移（registry-stale = 字面量对不上来源）
2 条派生计数：2 PASS, 0 异常；2 篇门面正文覆盖率棘轮：0 篇越界
"""


def main() -> int:
    bash = bash_for_tests()
    print(f"[GREEN] 已从 gate-ci.yml 现取断言（{len(bash.splitlines())} 行，判据只有这一个定义处）")
    # ── 装置自检：先证明 bash 真能在这个环境跑起来 ──
    # 不做这一步的话，「所有错误用例都红了」里可能有一部分是
    # **bash 根本没启动**（Windows 路径被bash 吃掉 → rc=127）造成的。
    # 装置没跑起来 = 测了个空，而报告上看起来像「判据全部有效」。
    # 本轮实测踩过：四组用例全 rc=127，看起来「4/4 通过」，
    # 实际上一条判据都没执行。
    rc0, out0 = _run("echo DEVICE_ALIVE" + chr(10) + bash, REAL)
    if rc0 != 0 or "DEVICE_ALIVE" not in out0:
        print("[RED] 装置故障：bash 无法在临时目录执行 —— 测试中止")
        print("       " + out0.strip()[:200])
        return 2
    print("[GREEN] 装置自检通过（bash 能跑，且能读工作流现取的断言）")
    results: list[bool] = []

    # 每一组带一条**必须出现在报错里**的关键词：
    # ⚠️ 只判 rc 会漏掉「换了个原因也红了」。本轮实测踩到——
    # 把「汇总行缺失」检查整个删掉后，变体 A 仍然 rc=1
    # （被后面那条「总数 ≠ 下限」顺手拦住），而脚本照样报 6/6 通过。
    # **rc 对≠ 同一个原因拦的** —— 这是「门禁条数≠ 覆盖面」的近亲：
    # 必须核对**是哪条判据**响的，否则删掉一条判据根本看不出来。
    cases: list[tuple[str, str, bool, str | None]] = [
        ("真实输出（模拟干净检出）", REAL, True, None),
        (
            "变体 A：facade 汇总行整条缺失",
            chr(10).join(ln for ln in REAL.splitlines() if "条门面" not in ln) + chr(10),
            False,
            "缺少某一层",
        ),
        (
            "变体 B：facade 只扫到 85/87",
            REAL.replace("87 条门面：87 PASS", "87 条门面：85 PASS"),
            False,
            "部分门面没被扫到",
        ),
        (
            "变体 C：facade 总数与注册表下限不一致",
            REAL.replace("门面条数 87（下限 87）", "门面条数 90（下限 90）"),
            False,
            "两处真相不一致",
        ),
    ]

    for label, gate_txt, expect_green, expect_kw in cases:
        rc, out = _run(bash, gate_txt)
        # ⚠️ 装置故障码绝不算「判据正确地拦住了」。
        # 126/127 = 命令找不到；2 = bash 用法错。
        # 这些说明断言根本没执行，与「判定为红」是两件完全不同的事 ——
        # 混起来会造出「全红 = 全对」的漂亮假绿。
        if rc in ENV_ERROR_CODES:
            print(f"  [未通过] {label}: rc={rc} 是装置故障码，非判据结论 —— 中止")
            print("        └ " + out.strip()[:160])
            return 2
        green = rc == 0
        ok = green == expect_green
        # 红了还不够，**必须是因为预期的那条判据**才红
        if ok and not green and expect_kw and expect_kw not in out:
            ok = False
        results.append(ok)
        mark = "通过" if ok else "未通过"
        color = "GREEN" if green else "RED"
        print(f"  [{mark}] {label}: rc={rc} ({color}，期望{'绿' if expect_green else '红'})")
        if not green:
            # 把报错第一行打出来 —— 判红是对的，但要看得懂为什么红
            first = next((x for x in out.splitlines() if "error" in x), out.strip()[:90])
            print(f"        └ {first[:100]}")

    # ── 变异 1：换回原始的恒假写法，喂坏输入，它必须**仍然绿** ──
    #
    # ⚠️ 这里期望的是 rc == 0，**不是** rc != 0。理由：
    # 变异把工作里那条有效检查（fac_pass != fac_total）换成旧版的
    # `报绿 && n < 80`（第二个条件在上游 exit 之后恒假）。
    # 喂进「只扫到 85/87」：
    #   - 旧版→ 绿（恒假条件不成立 → 不exit）= **漏检**，这正是要证明的
    #   - 新版 → 红（上面「变体 B」已验证）
    # 两者对照才构成完整证据。
    #
    # 我第一次把这里写成「期望非0」，结果拿到 rc=0 判自己失败 ——
    # 其实是**判据写反了**：把「漏检」当成了「拦截」。
    # 这就是记忆里那条「差集方向写反」最隐蔽的一种：不是恒真，
    # 而是**恒假的期望**会让正确的变异被当成失败，反复"加强判据"
    # 直到造出一个永远红的假门禁。
    old = bash.replace(
        'if [ "$fac_pass" != "$fac_total" ]; then',
        'if echo "$fac" | grep -q "0 漂移" && [ "$n" -lt 80 ]; then',
    )
    assert old != bash, "变异未生效：锚点没匹配上"
    rc_old, _ = _run(old, REAL.replace("87 条门面：87 PASS", "87 条门面：85 PASS"))
    ok_old = rc_old == 0
    results.append(ok_old)
    print(
        f"  [{'通过' if ok_old else '未通过'}] 变异：换回恒假写法后，"
        f"「只扫到 85/87」rc={rc_old}（期望 0 = 旧版漏检，新版已拦）"
    )

    # 双向对照：同一条坏输入，新版必须红。写在一起才算证据。
    rc_new, _ = _run(bash, REAL.replace("87 条门面：87 PASS", "87 条门面：85 PASS"))
    ok_new = rc_new != 0
    results.append(ok_new)
    print(
        f"  [{'通过' if ok_new else '未通过'}] 对照：现行写法下同一条输入 "
        f"rc={rc_new}（期望非 0 = 现行版拦得住）"
    )

    print()
    if all(results):
        print(f"[GREEN] CI 断言变异测试 {len(results)}/{len(results)} 通过")
        return 0
    print(f"[RED] CI 断言变异测试失败：{results.count(False)}/{len(results)} 未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
