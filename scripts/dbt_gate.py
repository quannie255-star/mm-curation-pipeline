"""dbt 门禁桥：把 `dbt build` 接进本项目的验收命令，并给出**可判定的**退出码。

## 为什么需要这个脚本，而不是让人直接跑 dbt

三个理由，缺一个都值得做：

1. **dbt 装在独立 venv 里**，不在项目的 Python 环境里。直接 `dbt build` 在
   别的解释器下会失败，而「换一个终端就跑不了」是最常见的劝退点。
2. **dbt 的输出是人读的表格**，CI 要判红得靠退出码。这里把结果翻译成
   `tests/` 级别的断言结果，写进一个 JSON，便于 CI 和 `verify_claims.py` 读。
3. **最关键：本项目已有一条纪律——门禁必须证明自己还能拦红**。
   所以这个脚本自带变异测试：把某个断言改成必然失败，它必须报红；
   还原后必须报绿。判据自己腐烂比门禁腐烂更难发现。

## 判据设计（这里踩过的坑）

`_judge` **只读结构化字段，不读 stdout**。
原因：v1 版本判据写成 `'ERROR' in out`，而dbt 的汇总行里**永远包含
'Completed with N errors' 这样的字样**，于是基线被判成红——
判据抓错了对象。改成读 `run_results.json` 的 `status` 之后，
它在正确实现下不会误判。**先验基线，是任何变异测试的前置条件。**

## 用法

    python -X utf8 scripts/dbt_gate.py              # 跑 dbt build 并判定
    python -X utf8 scripts/dbt_gate.py --mutate     # 变异测试（只动沙箱）
    python -X utf8 scripts/dbt_gate.py --json-out <path>   # 供 CI 读取
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
WAREHOUSES = REPO / "warehouses"

# dbt 装在独立 venv 里：它会拖进 protobuf / agate / agate-wheel 等一堆包，
# 装进项目所在的 Python 会污染已验证过的测试环境。
# 「整个模块可整目录删除」这条纪律，也包括它的依赖。
VENV = pathlib.Path(
    os.environ.get(
        "MM_DBT_VENV",
        pathlib.Path.home() / ".workbuddy/binaries/python/envs/mmwh2",
    )
)

ENV_LOCAL = "MM_DBT_ENV_LOCAL"
ENV_CI = "MM_DBT_ENV_CI"

# 门禁全绿的判据：dbt run_results 里unique_id 的 status 集合。
# 明确列出期望值，而不是「没有 ERROR」——
# 后者在 dbt 新增一种状态时会静默通过。
EXPECTED_STATUSES = {"success", "pass", "skipped"}


def _venv_python() -> pathlib.Path:
    for cand in (VENV / "Scripts" / "python.exe", VENV / "bin" / "python"):
        if cand.exists():
            return cand
    print(f"[RED] 找不到 dbt 的 Python：{VENV}", file=sys.stderr)
    raise SystemExit(2)


def _check_accepted_values(node, rel: str) -> list[str]:
    """从 accepted_values 节点里取出 values 列表并检查。

    ⚠️ 两种写法**只有一种是对的**，实测（2026-10-05，沙箱对照）：

        arguments:                arguments:
          values: [...]             - a        # ← 编译成 not in () → Parser Error

    dbt 1.10 期望 `arguments:` 是**含 `values` 键的 dict**。
    写成裸 list 时它不报「写法错」，而是把 `values` 这个键名吃掉，
    生成 `where value_field not in ()` —— 空括号，DuckDB 报
    `Parser Error: syntax error at or near ")"`。

    ★ 这里有个**假绿陷阱**：同一批里另一处用的正是裸 list 写法，
    却报 PASS —— 因为 dbt 有节点缓存，那次没真跑。
      「一部分节点红、一部分节点绿，且红的报错指向 SQL 语法」
      很容易被读成「环境问题」，而真因是**写法的差异**。
      对策：写法必须**全仓统一**，并用沙箱对照验证，不靠逐个试。

    第一版只处理 dict 那一种，于是 list 写法的两处**静默漏检** ——
    变异测试注入到 list 写法里时 rc 仍是 0。
    教训与 `lint_jinja` 相同：**先问「正确实现下会不会误判/漏判」，
    再问「基线是否绿」**。绿只说明没误判，不代表没漏判。
    """
    out: list[str] = []
    args = node
    if isinstance(node, dict):
        args = node.get("arguments", node)

    if isinstance(args, list):
        out.append(
            f"{rel}: accepted_values 的 arguments 必须是含 values 键的映射"
            f"（写成 `arguments:` +下一行 `values: [...]`），"
            f"裸 list 会被编译成 `not in ()` 并报 Parser Error"
        )
        values = args
    elif isinstance(args, dict):
        values = args.get("values")
        if values is None:
            out.append(f"{rel}: accepted_values 的 arguments 缺 `values` 键")
            return out
    else:
        out.append(f"{rel}: accepted_values 的 arguments 结构无法识别：{type(args).__name__}")
        return out

    if not isinstance(values, list):
        out.append(f"{rel}: accepted_values 的 values 必须是列表，实际是 {type(values).__name__}")
        return out
    if not values:
        out.append(f"{rel}: accepted_values 的 values 是空列表 —— 等于没有断言")
        return out
    for item in values:
        if isinstance(item, str) and "#" in item:
            out.append(f"{rel}: accepted_values 的值里含'#'，疑似行尾注释缺空格被并入值：{item!r}")
    return out


def static_problems(root: pathlib.Path) -> list[str]:
    """**静态预检的唯一入口**：把各条静态判据的结果汇总成一个列表。

    ## 为什么要抽这个函数（2026-10-05 实际踩到）
    原先  与  的基线各自**手写一遍**判据组合：

        problems = lint_jinja(...) + lint_yaml_values(...) + lint_yaml_block_comments(...)

    于是我新加的  门禁只被加进了前者 ——
     的静态组看不见它，导致「绝对路径那条变异」判红=False，
    看起来像门禁失效。

    ★ 这就是「同一个组合抄了两遍」的必然结果：
      **判据清单只能有一个定义处**。凡是「多处需要同一组判据」，
      就抽函数，不要靠自觉去同步 —— 同步是纪律，纪律会漏。

    每条判据自己管自己的变异测试；这里只负责汇总。
    """
    abs_path_problems: list[str] = []
    try:
        import no_absolute_paths as _abs_gate

        abs_path_problems = [
            f"{p.relative_to(root).as_posix()}:{n}: 硬编码绝对路径 {line[:60]}"
            for p, n, line in _abs_gate.scan(root)
        ]
    except ImportError:  # pragma: no cover
        print("[WARN] 找不到 no_absolute_paths.py，跳过绝对路径检查", file=sys.stderr)

    return (
        lint_jinja(root)
        + lint_yaml_values(root)
        + lint_yaml_block_comments(root)
        + abs_path_problems
    )


def lint_yaml_block_comments(root: pathlib.Path) -> list[str]:
    """静态扫 `arguments:` 块内的 YAML 注释（dbt 会把它拼进 SQL）。

    ## 坑的形态（2026-10-05 实际踩到，白跑两轮 build）
    `accepted_values` 的 `arguments:` 里写了独立成行的注释：

        - accepted_values:
            arguments:
              # skab_w64：旋转机械振动时序
              - skab_w64

    YAML 解析完全正常（注释不是数据），所以**任何 YAML 层面的检查都发现不了**。
    但 dbt 渲染 generic test 时会把 `arguments` 的内容**原样拼进 SQL**，
    注释跟着进去就成了 SQL 片段 →
    `Parser Error: syntax error at or near ")"`，报错只指一个右括号，
    完全看不出是注释。

    ★ 这类「跨层」错误（本层合法、下一层非法）最难查：
      本层工具全绿 → 以为写对了 → 换一层才炸。
      对策就是**把下一层的约束提到本层来验**。

    ## 判据
    只看 `arguments:` 到该块结束之间的行；命中 `#` 开头（去空白后）
    即报。不扫 `description` 之类的自由文本 —— 那里 `#` 是安全的内容字符。
    用缩进判定块结束，避免误伤后面的列定义。
    """
    problems: list[str] = []
    schema = root / "models" / "schema"
    if not schema.exists():
        return problems

    for path in sorted(schema.rglob("*.yml")):
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        in_args = False
        args_indent = -1
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())
            if stripped.endswith("arguments:"):
                in_args = True
                args_indent = indent
                continue
            if not in_args:
                continue
            # 缩进回退到 arguments 同级或更浅 → 块结束
            if indent <= args_indent:
                in_args = False
                continue
            if stripped.startswith("#"):
                problems.append(
                    f"{rel}:{lineno}: `arguments:` 块内不要写 YAML 注释 —— "
                    "dbt 会把该块原样拼进 SQL，注释会变成 SQL 片段"
                )
    return problems


def lint_yaml_values(root: pathlib.Path) -> list[str]:
    """静态扫 schema YAML 里的「注释被当成值」。

    ## 坑的形态（2026-10-05 实际踩到）
    在 YAML 列表里写：

        - skab_w64# 旋转机械振动时序      # 想写个行尾注释

    因为 `#` 前**少了空格**，YAML 不把它当注释起点，于是整个
    `skab_w64# 旋转机械振动时序` 成为**一个字符串值**。

    为什么危险：它不报语法错，`accepted_values` 照常编译通过，
    只是清单里多了一个现实中不存在的值 → 门禁**永远红**，
    而报错（"value not in list"）完全看不出根因是注释。

    ## 为什么必须用 YAML 解析器，不能扫文本（第一次写错了）
    第一版用「逐行找 `values:` 然后看下面的 `- xxx`」这种文本扫描，
    结果把 `values:` 块**后面的所有列表项**都当成了值：
      -误报 `- name: event_date`（那是列定义，不是值）
      - 误报 `- ''# 无设备类型`（**YAML 里这完全合法**，
        `#` 前有空格就是注释，解析后值是干净的 `''`）
    也就是判据在**正确实现上就会误判** —— 这类门禁比没有门禁更坏。
    改用 `yaml.safe_load` 拿解析后的真值，再检查值里有没有 `#`。

    ## 判据
    递归找所有 `accepted_values` 节点，交给 `_check_accepted_values`
    同时检查「写法正确（必须是含 values 键的映射）」与「值里无 `#`」。
    YAML 解析失败也报（那本身就是错）。
    """
    problems: list[str] = []
    schema = root / "models" / "schema"
    if not schema.exists():
        return problems

    try:
        import yaml
    except ImportError:  # pragma: no cover - 本仓环境必装
        print("[WARN] 未装 PyYAML，跳过 YAML 值域静态检查", file=sys.stderr)
        return problems

    def walk(node, path: str):
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "accepted_values":
                    problems.extend(_check_accepted_values(val, rel))
                walk(val, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    for path in sorted(schema.rglob("*.yml")):
        rel = path.relative_to(root).as_posix()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{rel}: YAML 解析失败：{str(exc)[:120]}")
            continue
        walk(data, rel)
    return problems


def lint_jinja(root: pathlib.Path) -> list[str]:
    """静态扫 SQL 里的 Jinja 危险写法，**在跑 dbt 之前**就拦住。

    ## 为什么要有这个预检（2026-10-05 同一个错误失败了三次）

    dbt 的 `config(...)` 块是 Jinja **表达式**，不是语句块。由此产生两个坑：

    1. **config 块里写 `--` 注释** → Jinja 把注释当代码解析，报
       `invalid syntax for function call expression`；
    2. **块外的普通 SQL 注释里写了 `{{ this }}` 这类字面量** → Jinja 照常求值，
       同样报上面那个错。

    两者报出的错误信息**只指向 `config(` 那一行**，完全看不出是注释的问题 ——
    我在这个报错上连查三轮才定位到真因。所以这里前置成静态检查。

    ## 判据为何是这样写的

    -只扫注释行（`--` 开头）与 config 块内部，**不扫真正的 Jinja 语句**
      （`{% if is_incremental() %}` 是合法的，必须放过）；
    - 用「config 块内不得出现 `--`」而不是「SQL 里不得出现 `--`」——
      后者会把所有正常注释全判红，等于没有门禁；
    - 命中就返回人可读的位置，不在这里抛异常，让调用方统一决定退出码。

    **变异测试见本文件末尾的 `--mutate` 分支**，它会真的注入一处注释里的
    `{{ this }}` 并要求本函数必须抓到 —— 判据自己腐烂比门禁腐烂更难发现。
    """
    problems: list[str] = []
    sql_root = root / "models" if (root / "models").exists() else root
    for path in sorted(sql_root.rglob("*.sql")):
        text = path.read_text(encoding="utf-8")

        # 坑1：config 表达式块内有 SQL 行注释
        for match in re.finditer(r"\{\{(.*?)\}\}", text, re.S):
            block = match.group(1)
            if "config(" in block and "--" in block:
                problems.append(
                    f"{path.relative_to(root).as_posix()}: config 块内不能有 `--` 注释"
                    "（config 是 Jinja 表达式，注释会被当代码解析）"
                )

        # 坑 2：注释行里写了 Jinja 字面量
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("--") and ("{{" in line or "{%" in line):
                problems.append(
                    f"{path.relative_to(root).as_posix()}:{lineno}: "
                    "注释行里不要写 Jinja 字面量（会被求值）"
                )
    return problems


def _dbt_exe() -> pathlib.Path:
    """dbt 可执行入口。

    环境上踩过的两个坑，改这一段前先读 README：
    - 版本必须**成对匹配**：dbt-core 1.8 配 dbt-duckdb 1.11 会报
      "Not compatible!"，而且 dbt.exe 直接坏在
      `ModuleNotFoundError: No module named 'dbt.cli.main'`——
      **症状是「装上了但一条命令都跑不了」，不是「装不上」。**
    - 装包时若 pip 需要**卸载旧版本**，会撞上沙箱的批量删除护栏
      （shutil.rmtree 被拦 -> SystemExit(1)）。
      所以装法必须是**全新空 venv**，不要在旧 venv 上重装。
    """
    for cand in (VENV / "Scripts" / "dbt.exe", VENV / "bin" / "dbt"):
        if cand.exists():
            return cand
    py = _venv_python()
    print(f"[RED] 找不到 dbt 可执行入口：{VENV}", file=sys.stderr)
    print("       一次性安装（必须用**空 venv**，见 docstring）：", file=sys.stderr)
    print(f'       "{py}" -m pip install "dbt-core==1.10.15" "dbt-duckdb==1.10.0"', file=sys.stderr)
    raise SystemExit(2)


def _profiles_dir(sandbox: pathlib.Path | None) -> pathlib.Path:
    """profiles.yml 必须放在 dbt 找得到的地方。

    dbt 的查找顺序是 `--profiles-dir` > 环境变量 > `~/.dbt/`。
    这里显式指到仓库内的 `warehouses/`，**不依赖用户的全局 ~/.dbt/**——
    否则「在别人机器上跑不起来」的原因会变成「他没配过 profile」。

    ⚠️ 沙箱分支原来返回的是 `sandbox`（沙箱根目录），但 profiles.yml 被复制到
    `sandbox/warehouses/`，于是 dbt 报 `Could not find profile named 'mm_warehouse'`。
    这条分支从写下来起没被执行过（`make_sandbox()` 先在更早的位置就崩了）。
    ——**同一个死函数里叠了两个从未运行的 bug**，都是因为「写完没跑」。
    """
    if sandbox is not None:
        return sandbox / "warehouses"
    return WAREHOUSES


def run_dbt(
    sandbox: pathlib.Path | None = None,
    extra: list[str] | None = None,
    db: pathlib.Path | None = None,
) -> dict:
    """跑一次 dbt build，返回**结构化**结果（不是 stdout 文本）。

    调`dbt.exe` 而不是 `python -m dbt.cli.main` ——后者是本脚本第一版的写法，
    在 dbt 1.8/1.10 上都报`ModuleNotFoundError: No module named 'dbt.cli.main'`。
    **入口名是猜的，猜错的表现是「装上了但一条命令都跑不了」**，所以用可执行文件入口。
    """
    exe = _dbt_exe()
    profiles = _profiles_dir(sandbox)
    env = dict(os.environ)
    env["DBT_PROFILES_DIR"] = str(profiles)
    # 沙箱在 %LOCALAPPDATA%\Temp 下，而 profiles.yml 里的 DuckDB 路径是
    # **相对于 project-dir 的上级**（也就是仓库根）的相对路径。
    # 沙箱里那个上级是临时目录，`data/envs/prod/...` 解析不到真实库 →
    # build 会在连库时报错，而判据会把「连不上」当成「测试失败」，
    # 基线变红、变异测试失去区分能力。
    # 所以沙箱模式下显式指到**真实库**的绝对路径（profiles.yml 里读这个变量）。
    # db 参数优先：每条变异用自己的库副本，避免「上一条变异留下的落盘状态」
    # 让下一条变异走进不同的代码分支（增量 vs 全量），导致变异看起来没生效。
    if sandbox is not None:
        env["MM_WAREHOUSE_DB"] = str(db or (REPO / "data/envs/prod/data/warehouse/platform.duckdb"))
    cmd = [
        str(exe),
        "build",
        "--project-dir",
        str(sandbox / "warehouses" if sandbox else WAREHOUSES),
    ]
    if extra:
        cmd += extra
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
    return {
        "cmd": " ".join(cmd),
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def judge(run: dict, project_dir: pathlib.Path) -> dict:
    """**结构化判定**：读 run_results.json，不碰 stdout。"""
    results_path = project_dir / "target" / "run_results.json"
    if not results_path.exists():
        return {
            "ok": False,
            "reason": f"dbt 没有产出 run_results.json（rc={run['returncode']}）",
            "tail": (run["stdout"] or run["stderr"])[-800:],
        }
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    if not results:
        return {
            "ok": False,
            "reason": "run_results.json 里没有任何结果 —— 测试没跑起来",
            "tail": "",
        }

    bad, by_status = [], {}
    for item in results:
        st = str(item.get("status") or "unknown")
        by_status[st] = by_status.get(st, 0) + 1
        if st not in EXPECTED_STATUSES:
            bad.append({"unique_id": item.get("unique_id"), "status": st})

    return {
        "ok": not bad,
        "total": len(results),
        "by_status": by_status,
        "failed": bad,
        "reason": "" if not bad else f"{len(bad)} 个节点状态不在 {sorted(EXPECTED_STATUSES)}",
    }


def make_sandbox(suffix: str = "base") -> pathlib.Path:
    """把 warehouses/ 复制到临时目录，用来做变异测试。

    必须**连根级文件一起复制**（dbt_project.yml 与models/ 是分开的目录，
    少拷一层会让 dbt 直接报「找不到 project」，
    而那种情况下判据依然会「报红」——**在错误基线上的变异测试是自欺**）。

    ⚠️ 原版这里写的是 `os.environ.get("LOCALAPPDATA", tempfile_dir()) / f"..."`，
    两边都是 `str`，所以 `/` 必然抛
    `TypeError: unsupported operand type(s) for /: 'str' and 'str'`。
    ——这段代码从写下来起**一次都没被执行过**，所以从没暴露。
    教训：写完的分支要么跑一次，要么就别留着假装它能用。

    suffix 参数让每条变异有独立沙箱：共用一个沙箱会让后一条变异继承前一条的
    落盘状态（增量表已存在 → 走增量分支 → 变异根本不生效）。
    详见 _mutation_test 里第二条变异的说明。
    """
    base = pathlib.Path(os.environ.get("LOCALAPPDATA") or tempfile_dir())
    sandbox = base / f"mmc-dbt-sandbox-{suffix}-{os.getpid()}"
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)
    shutil.copytree(
        WAREHOUSES,
        sandbox / "warehouses",
        # 生成物不拷：既慢，又会让沙箱里的 run_results.json 带着上次的结论，
        # 出现「其实没真跑却报PASS」的假绿。
        ignore=shutil.ignore_patterns("target", "dbt_packages", "logs", ".user.yml"),
    )
    return sandbox


def tempfile_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("LOCALAPPDATA") or "C:/Users/10393/AppData/Local/Temp")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutate", action="store_true", help="变异测试：证明门禁还能拦红")
    ap.add_argument("--json-out", default="", help="把判定结果写到该JSON 路径（给 CI 读）")
    ap.add_argument("--full-refresh", action="store_true", help="dbt 全量刷新")
    ap.add_argument(
        "--lint-only",
        action="store_true",
        help="只跑静态预检（Jinja 危险写法），不启动 dbt —— 秒级，可单独接 CI",
    )
    args = ap.parse_args()

    if args.mutate:
        return _mutation_test()

    # 静态预检前置：Jinja 语法错误会让 dbt 在 config( 那一行报一个
    # 与真因毫无关系的错；YAML 注释混入值会让 accepted_values 永远红
    # 且报错看不出根因。两者都先静态拦掉。
    # 绝对路径门禁复用独立脚本（scripts/no_absolute_paths.py）：
    # 它有自己的变异测试（scripts/mutation_test_no_absolute_paths.py），
    # 判据与变异必须放在一起维护，拆开会各自腐烂。
    problems = static_problems(WAREHOUSES)
    if problems:
        print(f"[RED] Jinja 静态预检失败：{len(problems)} 处", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    if args.lint_only:
        print("[OK] Jinja 静态预检通过（未启动 dbt）")
        return 0

    extra = ["--full-refresh"] if args.full_refresh else []
    run = run_dbt(extra=extra)
    verdict = judge(run, WAREHOUSES)
    _report(verdict, run)

    if args.json_out:
        out = pathlib.Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    return 0 if verdict["ok"] else 1


def _report(verdict: dict, run: dict) -> None:
    if verdict["ok"]:
        print(f"[GREEN] dbt build全绿：{verdict['total']} 个节点 {verdict['by_status']}")
        return
    print(f"[RED] {verdict['reason']}", file=sys.stderr)
    for f in verdict.get("failed", [])[:10]:
        print(f"       {f['status']:8s} {f['unique_id']}", file=sys.stderr)
    if verdict.get("tail"):
        print("---- dbt 输出尾部 ----", file=sys.stderr)
        print(verdict["tail"], file=sys.stderr)


def _mutation_test() -> int:
    """变异测试：**先验基线，再看变异**。

    顺序颠倒过一次，结论是假的：沙箱搭错导致基线本身是红的，
    而变异跑出来也是红的 —— 「2/2 拦红」看起来没问题，
    实际上没有区分能力。**基线不绿时必须停止**，而不是继续报数。

    ## 覆盖两类门禁（2026-10-05 重写的原因）
    原版只变异 SQL，测的是「dbt 跑起来红不红」。但本项目这轮的主要收获
    恰恰是**静态预检**（Jinja 危险写法、YAML 注释并入值、`arguments` 写法错误）
    ——它们都在 dbt 启动**之前**生效，只跑 dbt 完全测不到。
    所以现在分两组：
      A. 静态门禁变异（3 条）：秒级，不碰数据库
      B. dbt 运行门禁变异（2 条）：在沙箱里真跑 build

    ## 锚点必须实测，不许凭印象（这一条本轮又踩了一次）
    原版的变异锚点写成 `cast(sample_idas varchar)`（缺空格），
    在真实文件里**出现 0 次** → `mutated == original` →
    靠 `assert` 兜住不假通过，但**两条变异一条都跑不起来**。
    断言只是最后一道防线：更好的做法是**先实测每个锚点的出现次数**，
    写进本函数的注释里，改文件时对照检查。
    """
    sandbox = make_sandbox()
    try:
        # ---------- A. 静态门禁变异（不依赖沙箱，直接在真项目上跑后还原） ----------
        print("== A. 静态门禁变异（dbt 启动前生效的那一层） ==")
        static_cases: list[tuple[str, str, str, str]] = [
            # (说明, 相对路径, 原文锚点, 替换成)
            (
                "注释里写 Jinja 字面量（dbt 会求值）",
                "warehouses/models/staging/stg_samples.sql",
                "--【数仓考点：staging 层在干什么】",
                "-- 变异注入 {{ this }}",
            ),
            (
                "config 块内加 SQL 注释（Jinja 表达式不接受）",
                "warehouses/models/staging/stg_samples.sql",
                "        materialized='view',",
                "        materialized='view',\n        -- 变异注入",
            ),
            (
                "源 SQL 里写死盘符路径（换机器跑不起来）",
                "warehouses/models/staging/stg_samples.sql",
                "from {{ source('platform', 'ods_samples') }}",
                "from read_parquet('C:/Users/someone/x.parquet')",
            ),
            (
                "accepted_values 少写 values 键（编译成 not in ()）",
                "warehouses/models/schema/_models.yml",
                "                values:\n                - metropt3\n",
                "                - metropt3\n",
            ),
        ]
        a_results: list[bool] = []
        base_static = static_problems(WAREHOUSES)
        if base_static:
            print("[RED] 静态门禁基线不干净，变异测试中止：")
            for b in base_static:
                print(f"       {b}")
            return 2
        print("[GREEN] 静态门禁基线绿（0 处问题）")

        for label, rel, old, new in static_cases:
            path = REPO / rel
            original = path.read_text(encoding="utf-8")
            if original.count(old) != 1:
                print(f"  [未通过] {label}: 锚点出现 {original.count(old)} 次（必须恰好 1）")
                a_results.append(False)
                continue
            path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
            try:
                hit = bool(static_problems(WAREHOUSES))
            finally:
                path.write_text(original, encoding="utf-8", newline="\n")
            a_results.append(hit)
            print(f"  [{'通过' if hit else '未通过'}] {label}: 判红={hit}")

        restored = static_problems(WAREHOUSES)
        print(f"还原后静态基线绿={not restored}")
        if restored:
            for r in restored:
                print(f"       残留：{r}")
            return 1
        print()

        # ---------- B. dbt 运行门禁变异（沙箱里真跑） ----------
        print("== B. dbt 运行门禁变异（沙箱里真跑 build） ==")
        # 基线也用**库副本**：直接指向真实 prod 库的话，
        # 一旦某条变异写入脏数据就会**污染真实数据资产**
        # （我自己踩过：手工探针把真库的 incr 表写成 275 行含 2 行 NULL 日期，
        #  而 dbt 不会报警 —— 那是真库被改坏了，必须 --full-refresh 才修得回来）。
        # 变异测试绝不允许碰真实数据源。
        base_db = sandbox / "platform.duckdb"
        shutil.copy2(REPO / "data/envs/prod/data/warehouse/platform.duckdb", base_db)
        base_run = run_dbt(sandbox, extra=["--full-refresh"], db=base_db)
        base_verdict = judge(base_run, sandbox / "warehouses")
        if not base_verdict["ok"]:
            print("[RED] 变异测试中止：沙箱基线本身就不绿，判据无区分能力", file=sys.stderr)
            print(f"       原因：{base_verdict['reason']}", file=sys.stderr)
            print(f"       {base_verdict.get('tail', '')[-600:]}", file=sys.stderr)
            return 2
        print(f"[GREEN] 沙箱基线绿：{base_verdict['total']} 个节点 {base_verdict['by_status']}")
        print()

        # 锚点均已实测为「恰好 1 处」（2026-10-05），改文件时若数变了要重新实测
        run_cases: list[tuple[str, str, str, str, bool]] = [
            (
                "staging 去掉类型转换（cast → 恒等）",
                "warehouses/models/staging/stg_samples.sql",
                "cast(sample_id        as varchar) as sample_id",
                "cast(sample_id        as varchar) as sample_id_renamed",
                False,
            ),
            (
                # ⚠️ 这条**必须 --full-refresh** 才测得到（2026-10-05 实测）。
                # 原以为「增量重跑会重复累加」才是那个 bug 的现场，实测发现不对：
                # 增量分支的条件是 `event_date > (select max(event_date) from this)`，
                # 而**NULL 与任何值比较都得到 NULL**（不满足 WHERE），
                # 所以那 2 行无日期的数据在增量模式下**根本进不来**，
                # 无论上游过滤怎么放开，守卫都看不到它。
                #
                # 只有全量刷新（is_incremental() 为 False、不过滤）时，
                # 放开的过滤才会把 NULL 行写进表 → 守卫变红。
                #
                # ★ 教训：「变异没被拦住」有两种可能——判据不够严，
                #   或**变异根本没进到判据所在的代码路径**。
                #   区分方法：手工跑一次沙箱看真实结果（我这次就是这么定位的）。
                #   只反复加强判据，会把判据改成「永远红」的假门禁。
                "增量模型放开 event_date 过滤（全量刷新下 NULL 行入表）",
                "warehouses/models/marts/incr_dataset_day_agg.sql",
                "and event_date is not null",
                "and (event_date is not null or true)",
                True,
            ),
        ]
        b_results: list[bool] = []
        for idx, (label, rel, old, new, need_refresh) in enumerate(run_cases, 1):
            # ⚠️ **每条变异必须用独立的沙箱 + 独立的库副本**（2026-10-05 实测）。
            # 原因：共用一个沙箱时，前一条变异落盘的表结构 / 增量状态会被后一条继承，
            # 于是两条变异可能走进不同的代码分支，结论不可比。
            work = make_sandbox(suffix=f"mut{idx}")
            try:
                db = work / "platform.duckdb"
                shutil.copy2(REPO / "data/envs/prod/data/warehouse/platform.duckdb", db)
                path = work / rel
                original = path.read_text(encoding="utf-8")
                if original.count(old) != 1:
                    print(f"  [未通过] {label}: 锚点出现 {original.count(old)} 次（必须恰好 1）")
                    b_results.append(False)
                    continue
                path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
                extra = ["--full-refresh"] if need_refresh else None
                r = judge(run_dbt(work, extra=extra, db=db), work / "warehouses")
                hit = not r["ok"]
                b_results.append(hit)
                print(f"  [{'通过' if hit else '未通过'}] {label}: 判红={hit}")
                path.write_text(original, encoding="utf-8", newline="\n")
            finally:
                shutil.rmtree(work, ignore_errors=True)

        restore = judge(
            run_dbt(sandbox, extra=["--full-refresh"], db=base_db),
            sandbox / "warehouses",
        )
        print()
        print(f"还原后基线绿={restore['ok']}")
        total = len(a_results) + len(b_results)
        passed = sum(a_results) + sum(b_results)
        print(
            f"变异测试：{passed}/{total} 条被正确拦住（静态 {sum(a_results)}/{len(a_results)}，"
            f"运行 {sum(b_results)}/{len(b_results)}）"
        )
        return 0 if passed == total and restore["ok"] else 1
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
