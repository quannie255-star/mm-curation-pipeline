"""解析 coverage_classic.json，按「函数是否真被执行」盘点清洗用到/没用到的代码。

覆盖源：coverage run --source=src/mm_curation 对 scripts/run_classic_cleaning.py 的运行。
口径：
  - 文件级百分比会骗人——import 一个模块时其顶层 import/常量/类定义已执行，
    于是「从没被调用」的模块也显示低覆盖。故本脚本用**函数体级**判定：
    一个函数「被执行」= 其行区间内有至少一行落在 executed_lines（不在 missing）。
  - 「导入过但零函数执行」的模块 = 本次清洗未走到（归档候选，附模态 caveat）。
  - 「被执行模块内零命中的函数」= 模块内的死路径。

用法（清洗跑完 coverage 后）：
    coverage json -o coverage_classic.json
    python -X utf8 scripts/summarize_coverage.py
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mm_curation"
# 默认维持首次经典数据集清洗的口径（向后兼容）；广覆盖运行通过 argv 覆盖。
COV_JSON = ROOT / "coverage_classic.json"
OUT = ROOT / "data" / "processed" / "classic_text" / "coverage_report.md"


def _funcs_with_exec(src_text: str, executed: set[int], missing: set[int]):
    """返回 [(qualname, exec_body_lines, total_body_lines, executed_bool)]。

    关键点：函数定义行（def）在 import 时即执行，不能据此判「被调用」。
    只有**函数体**行（def 行之后）出现在 executed_lines 才算真正执行过。
    """
    tree = ast.parse(src_text)
    out = []

    def walk(prefix, node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(child, "end_lineno", child.lineno)
                body = set(range(child.lineno + 1, end + 1))  # 不含 def 行
                body_exec = body & executed
                body_real = body - missing
                hit = bool(body_exec)
                out.append((f"{prefix}{child.name}", len(body_exec), len(body_real), hit))
                walk(f"{prefix}{child.name}.", child)
            elif isinstance(child, ast.ClassDef):
                walk(f"{prefix}{child.name}.", child)

    walk("", tree)
    return out


def _reason(rel: str) -> str:
    if "image" in rel or "clip" in rel:
        return "图像模态算子（本次文本清洗不触发）"
    if "fhir" in rel:
        return "FHIR/医疗模态（本次不触发）"
    if "industrial" in rel:
        return "工业传感模态（本次不触发）"
    if "agent" in rel:
        return "Agent 编排层（本次未启用）"
    if "serving" in rel or "studio" in rel or "platform" in rel:
        return "服务/前端/平台（本次未启用）"
    if "synthesis" in rel or "contamination" in rel:
        return "数据合成/污染器（本次未启用）"
    if "extract" in rel:
        return "抽取层（本次未启用）"
    if "verdict" in rel:
        return "判决书台账（本次未挂 verdict_ledger）"
    if "tuning" in rel:
        return "调参/标注工具（本次未启用）"
    if "monitoring" in rel or "drift" in rel:
        return "监控/漂移（本次未启用）"
    if "warehouse" in rel or "lineage" in rel or "dataset" in rel:
        return "数仓/血缘/数据集构建（本次未启用）"
    if "embedding" in rel:
        return "嵌入编码器（本次未启用）"
    if "detector" in rel:
        return "检测器模型（本次未启用）"
    if "benchmarks" in rel:
        return "基准构建（本次未启用）"
    if "cli" in rel:
        return "CLI 入口（本次未直接调用）"
    if rel.endswith("__init__.py"):
        return "包初始化（仅 import 副作用）"
    return "其他功能（本次未启用）"


def main() -> int:
    # 用法: python -X utf8 scripts/summarize_coverage.py [cov_json] [out_md]
    #   - cov_json: coverage json 路径（默认 coverage_classic.json）
    #   - out_md:   报告输出路径（默认 data/processed/classic_text/coverage_report.md）
    cov_json = Path(sys.argv[1]) if len(sys.argv) > 1 else COV_JSON
    out_md = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    if not cov_json.exists():
        print(f"缺少 {cov_json}，请先 coverage run + coverage json", file=sys.stderr)
        return 1
    cov = json.loads(cov_json.read_text(encoding="utf-8"))
    files = cov.get("files", {})

    used_mod, idle_mod, dead_in_used = [], [], []
    for fpath, info in files.items():
        fp = Path(fpath)
        if not fp.is_absolute():
            fp = ROOT / fpath
        try:
            rel = fp.relative_to(SRC)
        except ValueError:
            continue
        summ = info.get("summary", {})
        n_stmt = summ.get("num_statements", 0)
        if n_stmt == 0:
            continue
        executed = set(info.get("executed_lines", []))
        missing = set(info.get("missing_lines", []))
        try:
            funcs = _funcs_with_exec(fp.read_text(encoding="utf-8"), executed, missing)
        except Exception:
            funcs = []
        if not funcs:
            # 无函数定义的模块（如纯 __init__）按导入副作用处理
            idle_mod.append((str(rel), 0, 0, _reason(str(rel))))
            continue
        n_exec = sum(1 for _, _, _, hit in funcs if hit)
        if n_exec == 0:
            idle_mod.append((str(rel), 0, len(funcs), _reason(str(rel))))
        else:
            used_mod.append((str(rel), n_exec, len(funcs)))
            for name, el, tl, hit in funcs:
                if not hit:
                    dead_in_used.append((str(rel), name))

    used_mod.sort(key=lambda r: (-r[1], r[0]))
    idle_mod.sort(key=lambda r: r[0])

    L = ["# 清洗代码覆盖盘点（函数级口径）", ""]
    L.append(f"覆盖源：`src/mm_curation/`（项目自有清洗代码），由 `coverage run` 包裹 "
             f"广覆盖驱动得到；coverage json = `{cov_json.name}`。")
    L.append("")
    L.append("> 关键口径：文件级百分比会因「import 即执行顶层代码」而虚高，故本盘点以"
             "**函数体是否真正被执行**为准。")
    L.append("> 「导入过但零函数执行」≠ 死代码——多数为**其他模态/功能**（图像、FHIR、"
             "工业传感、Agent、服务、平台、合成、抽取、数仓等）或**本次未启用的算子**")
    L.append(">（部分需 GPU/权重的算子在本机 CPU 环境未跑到）。")
    L.append("> 它们不应直接判为可删，仅代表「本次广覆盖运行没走到」。")
    L.append("")
    L.append(f"## 一、本次清洗真正执行到函数的模块（{len(used_mod)} 个）")
    L.append("")
    L.append("| 模块 | 执行函数数 / 总函数数 |")
    L.append("|---|---|")
    for rel, ne, nt in used_mod:
        L.append(f"| `{rel}` | {ne}/{nt} |")
    L.append("")
    L.append(f"## 二、导入过但本次零函数执行的模块（{len(idle_mod)} 个，归档候选·需结合功能判断）")
    L.append("")
    L.append("| 模块 | 函数数 | 可能原因（勿直接删） |")
    L.append("|---|---|---|")
    for rel, _, nf, reason in idle_mod:
        L.append(f"| `{rel}` | {nf} | {reason} |")
    L.append("")
    L.append(f"## 三、被执行模块内、本次零命中的函数（模块内死路径，{len(dead_in_used)} 个）")
    L.append("")
    if dead_in_used:
        for rel, name in dead_in_used:
            L.append(f"- `{rel}` :: `{name}`")
    else:
        L.append("- 无（被执行模块内的函数均至少命中一次）")
    L.append("")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(L), encoding="utf-8")
    print(f"写出 {out_md}")
    print(f"\n本次执行到函数的模块：{len(used_mod)}")
    for rel, ne, nt in used_mod:
        print(f"  {ne:>2}/{nt:<2}  {rel}")
    print(f"\n导入过但零函数执行的模块：{len(idle_mod)}")
    for rel, _, nf, _ in idle_mod:
        print(f"  ({nf} fn)  {rel}")
    print(f"\n模块内死路径函数：{len(dead_in_used)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
