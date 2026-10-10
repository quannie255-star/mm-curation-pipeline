"""体检脚本：面试前跑一次，回答「这个项目在**别人机器上**能不能跑」。

## 为什么要有它
`make repro` 的自我描述是「一键复现证明链」。实测在本机上，
裸 `python` 解析到 3.13 —— 没有 numpy、没有 mm_curation，一跑就
`ModuleNotFoundError`。这种报错的坏处是：**它出现在深层脚本里，
现场看半天不知道是「解释器选错了」**。

所以把「解释器对不对 / 依赖齐不齐 / 门禁绿不绿」三件事收进一个命令，
缺什么就打印**可直接粘贴执行的修复命令**，而不是抛堆栈。

## 用法
    python -X utf8 scripts/doctor.py
    make doctor          # 等价，且解释器由Makefile 探测

退出码：0 = 一切就绪；1 = 有阻塞项（清单见输出）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: 依赖 → 缺了会怎样。「为什么需要」比「装它」更重要：
#: 面试官问「为什么要 torch」时，能答出用途比能背出包名值钱。
CORE_DEPS = [
    ("yaml", "读 YAML 配置（算子参数全靠它）"),
    ("numpy", "算子数值计算（P/R、阈值扫描）"),
    ("pandas", "报告聚合与 CSV 台账"),
    ("torch", "域专属判官 LoRA 微调 / CLIP 实验"),
    ("transformers", "encoder 与判官底座"),
]

#: 可选依赖：缺了不影响主链路，但要能说清「为什么缺也能跑」。
OPTIONAL_DEPS = [
    ("ray", "Ray 运行时（local 之外的第二条执行路径；缺了包侧少 5 条测试）"),
    ("duckdb", "湖分区 SQL 契约闸门（缺了数仓那条门禁跑不了）"),
    ("dbt", "数仓建模构建（make airflow-build 需要）"),
    ("fastapi", "平台 HTTP 层（服务鉴权/配额门禁需要）"),
    ("streamlit", "深潜用的可视化入口"),
]


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=20)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def main() -> int:
    py = sys.executable
    print("=" * 68)
    print("体检：面试前跑一次，确认「在别人机器上也跑得起来」")
    print("=" * 68)
    print(f"\n解释器      : {py}")
    print(f"版本        : {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        print("⚠️低于 3.11。项目按 3.11 开发，3.10 及以下可能有语法/typing 差异。")

    blockers: list[str] = []

    print("\n[1] 核心依赖")
    for mod, why in CORE_DEPS:
        ok = _has(mod)
        print(f"  {'✅' if ok else '❌'} {mod:14s}{why}")
        if not ok:
            blockers.append(mod)

    print("\n[2] 可选依赖（缺了不影响主链路）")
    for mod, why in OPTIONAL_DEPS:
        ok = _has(mod)
        print(f"  {'✅' if ok else 'ℹ️ '} {mod:14s}{why}")

    print("\n[3] 项目自身可导入")
    try:
        import mm_curation  # noqa: F401

        print("  ✅ mm_curation 可导入")
    except ImportError:
        print("  ❌ mm_curation 不可导入 → 装本地包：")
        print("     python -X utf8 -m pip install -e .")
        blockers.append("mm_curation")

    pkg = REPO / "packages" / "curation-eval"
    if pkg.exists():
        try:
            sys.path.insert(0, str(pkg / "src"))
            import curation_eval  # noqa: F401

            print("  ✅ curation_eval 可导入")
        except ImportError:
            print("  ℹ️  curation_eval 不可导入 → 装协议包：")
            print("     python -X utf8 -m pip install -e packages/curation-eval")

    print("\n[4] 工作区是否干净（脏工作区会让人怀疑数字被手改过）")
    st = _git("status", "--porcelain")
    changed = [ln for ln in st.splitlines() if ln.strip()]
    if not changed:
        print("  ✅ 干净")
    else:
        mods = [ln for ln in changed if ln[:2].strip() in {"M", "A", "D", "R"}]
        untracked = [ln for ln in changed if ln.startswith("??")]
        print(f"  ℹ️  {len(changed)} 处未提交（已跟踪改动 {len(mods)} / 未跟踪 {len(untracked)}）")
        if len(mods) > 20:
            print(
                "     ⚠️ 改动较多。面试前建议提交或 stash，"
                "让对方看到的是「一个干净 commit 对应一组数字」。"
            )

    print("\n[5] 对外数字门禁（文档里的数字 == 实跑结果？）")
    vc = subprocess.run(
        [py, "-X", "utf8", "scripts/verify_claims.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    tail = (vc.stdout or vc.stderr).strip().splitlines()
    for line in tail[-4:]:
        print(f"  {line}")
    if vc.returncode != 0:
        blockers.append("verify_claims 漂移")

    print("\n" + "=" * 68)
    if blockers:
        print(f"❌ 有 {len(blockers)} 项阻塞：{', '.join(blockers)}")
        print("\n修复顺序建议：")
        print("  1) 装依赖    python -X utf8 -m pip install -r requirements.txt")
        print("  2) 装本地包  python -X utf8 -m pip install -e .")
        print("  3) 装协议包  python -X utf8 -m pip install -e packages/curation-eval")
        print("  4) 数字漂移  看上面 verify_claims 逐条点名的文档，改文档 + 改 facade")
        return 1

    print("✅ 就绪：解释器对、依赖齐、对外数字无漂移。")
    print("   下一步可跑：make repro（一键复现证明链）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
