"""守住黄金集的**独立性**：它不能 import 污染器。

这是整个 SLO 面板可信度的地基：如果 build_golden_set.py 改成从
`curation_eval.contamination` 取形态、用污染器造样本，那么
「算子能抓到污染器造的形态」就是**自证** —— 闭环回来，
面板上的召回数字毫无意义（这正是本项目 v1 报出召回 100% 的机制）。

这类退化极隐蔽：数字照样漂亮，只是**不再有意义**。
所以用源码级断言把它钉死，且**不依赖运行时的 import 重定向**。
"""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_golden_set.py"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_golden_set_does_not_import_contaminators() -> None:
    """黄金集构造器**禁止** import 污染器模块。"""
    mods = _imported_modules(SCRIPT)
    forbidden = {m for m in mods if "contamination" in m or "contaminator" in m}
    assert not forbidden, (
        f"黄金集构造器不得依赖污染器：发现 {sorted(forbidden)}。"
        "用污染器造评估集会形成自证闭环（召回数字不再有意义）。"
    )


def test_golden_set_imports_are_explicitly_allowed() -> None:
    """白名单：只允许少量无害依赖，出现新依赖时必须有人想清楚。"""
    allowed_prefixes = (
        "mm_curation",
        "sys",
        "json",
        "random",
        "pathlib",
        "collections",
        "__future__",
    )
    mods = _imported_modules(SCRIPT)
    unexpected = sorted(m for m in mods if not m.startswith(allowed_prefixes))
    assert not unexpected, (
        f"黄金集构造器新增了未预期的依赖 {unexpected}；新增前请确认它不会把污染器实现带进来。"
    )


def test_golden_set_criteria_are_human_checkable() -> None:
    """每个形态必须配一条**人可核对**的判据，否则标注者无从判断。"""
    src = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # PROBE_SPECS = {...}
    specs = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "PROBE_SPECS":
                    specs = node.value
    assert specs is not None, "找不到 PROBE_SPECS 定义"
    assert isinstance(specs, ast.Dict), "PROBE_SPECS 必须是字面量字典"
    assert len(specs.keys) >= 6, f"形态过少：{len(specs.keys)}"
    # 每个值是 (构造器, 判据字符串) 二元组
    for v in specs.values:
        assert isinstance(v, ast.Tuple) and len(v.elts) == 2, "形态定义应为 (构造器, 判据)"
        crit = v.elts[1]
        assert isinstance(crit, ast.Constant) and isinstance(crit.value, str), (
            "判据必须是字符串常量，供人标时核对"
        )
        assert len(crit.value) >= 6, "判据太短，人无法据此判断"


SLO = Path(__file__).resolve().parents[1] / "configs" / "detection_slo.yaml"
MUT = Path(__file__).resolve().parents[1] / "scripts" / "mutation_test_detection_slo.py"


def test_slo_contract_keeps_its_comments_and_sli_section() -> None:
    """真契约**必须**保留手写注释与 sli 段。

    为什么会需要这条测试：本轮变异测试第一版把真契约路径直接传给
    `write()`，`yaml.safe_dump` 的输出整份覆盖了 configs/detection_slo.yaml
    → 注释、`sli` 段、`version` 全丢，只剩扁平化后的数据。
    数据仍能解析，所以**没有任何报错** —— 契约被悄悄削掉了上下文与出处说明。
    → 断言：文件必须含 `sli:` 段、`#` 注释、以及 '不 import 污染器' 这类
      决定口径的说明。
    """
    raw = SLO.read_text(encoding="utf-8")
    for needle in ("sli:", "contracts:", "version:", "不 import", "预算", "棘轮"):
        assert needle in raw, (
            f"契约缺少 `{needle}` —— 疑似被 yaml.safe_dump 覆盖过"
            "（变异测试必须写 configs/_mutations/，不得就地改写真契约）"
        )
    assert raw.count("#") >= 10, "契约的注释被削掉了（口径与出处说明会丢失）"


def test_mutation_test_never_writes_the_real_contract() -> None:
    """变异测试**不得**把真契约路径当作写入目标。

    用源码级断言守住：write() 只接受「内容」，路径由模块统一生成到
    configs/_mutations/。若日后有人给 write() 加回路径参数，这条会红。
    """
    src = MUT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "write":
            n_args = len(node.args.args)
            assert n_args == 1, (
                f"write() 应只接受 1 个参数（内容），实得 {n_args} 个 —— "
                "接收目标路径就有可能误写真契约（第一版就是这么坏的）"
            )
    # 断言 _mutations 目录常量存在
    assert "_mutations" in src, "变异配置必须落在 configs/_mutations/ 隔离目录"
    # 且代码里不得出现 write(SLO 之类把真契约当写入目标的形态
    assert "write(SLO" not in src, "发现把真契约传给 write() 的调用"
