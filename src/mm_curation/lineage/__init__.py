"""血缘与数据契约：让「改一个算子会影响谁」和「schema 变了谁负责」可被机器回答。

两个子模块：

- `graph`：从判决书反推血缘图（实体 → 作业 → 裁决），命名对齐 OpenLineage。
- `contract`：数据集对外承诺的 schema 与断言，可进 CI 阻断。

两者合起来回答数据治理岗位最常问的两个问题：
**这个数从哪来（血缘）**、**它的格式谁负责（契约）**。
"""

from __future__ import annotations

from .contract import (
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    Contract,
    check_contract,
    load_contract,
    load_contracts,
)
from .graph import (
    EDGE_GENERATED_BY,
    EDGE_USED,
    NODE_ENTITY,
    NODE_JOB,
    NODE_VERDICT,
    LineageGraph,
    build_lineage,
    lineage_from_file,
)

__all__ = [
    "EDGE_GENERATED_BY",
    "EDGE_USED",
    "NODE_ENTITY",
    "NODE_JOB",
    "NODE_VERDICT",
    "LineageGraph",
    "build_lineage",
    "lineage_from_file",
    "Contract",
    "check_contract",
    "load_contract",
    "load_contracts",
    "STATUS_PASS",
    "STATUS_FAIL",
    "STATUS_ERROR",
]
