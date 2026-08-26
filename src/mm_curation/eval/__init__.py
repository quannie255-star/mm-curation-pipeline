"""eval 模块：检索质量评测 + 算子级 P/R 评测（D4）。"""

from .metrics import mrr, recall_at_k
from .operator_pr import (
    OPERATOR_TARGETS,
    OperatorPR,
    evaluate_all,
    evaluate_operator,
    render_pr_markdown,
    run_operator,
)
from .retrieval import EvalResult, QuerySpec, build_queries, compare, evaluate_index

__all__ = [
    "EvalResult",
    "OPERATOR_TARGETS",
    "OperatorPR",
    "QuerySpec",
    "build_queries",
    "compare",
    "evaluate_all",
    "evaluate_index",
    "evaluate_operator",
    "mrr",
    "recall_at_k",
    "render_pr_markdown",
    "run_operator",
]
