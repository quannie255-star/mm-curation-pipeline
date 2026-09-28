"""编排层测试：拓扑序、环检测、本地执行器（重试/续跑/单阶段）、Airflow 导出与校验。

**重点在 `validate_airflow_source` 的负例**：一个只报 0 问题的校验器等于没有校验。
初版就漏掉了 `__start__ -> __start__` 自环，所以这里对每个校验项都配一个"改坏它"的用例。
"""

from __future__ import annotations

import time

import pytest

from mm_curation.platform.dag import (
    DAG,
    Context,
    LocalExecutor,
    Task,
    to_airflow_source,
    validate_airflow_source,
)
from mm_curation.platform.runs import FAILED, SKIPPED, SUCCESS, TIMEOUT, RunLedger

# ---------------------------------------------------------------------------
# 纯定义层（不需要 DuckDB）
# ---------------------------------------------------------------------------


def _ctx(tmp_path, ledger=None, *, events=None) -> Context:
    return Context(
        run_id="r1",
        batch_date="2026-09-22",
        root=tmp_path,
        ledger=ledger or RunLedger(tmp_path / "l.duckdb"),
        con=None,
        lake=None,
        verbose=False,
        extra={"events": events if events is not None else []},
    )


def _mk(name: str):
    """阶段函数签名是 `fn(ctx) -> dict`；用工厂造具名记录器。

    顺手把签名钉死在这里：签名一旦膨胀成 `fn(ctx, name)`，
    所有实现都得跟着改，而"阶段只认一个上下文"是本层的基本约定。
    """

    def _fn(ctx: Context) -> dict:
        ctx.extra["events"].append(name)
        return {"n_out": 1, "fingerprint": f"fp-{name}"}

    return _fn


def test_dag_order_is_topological():
    dag = DAG("d")
    dag.tasks = [
        Task("c", _mk("c"), deps=("b",)),
        Task("a", _mk("a")),
        Task("b", _mk("b"), deps=("a",)),
    ]
    assert [t.name for t in dag.order()] == ["a", "b", "c"]


def test_dag_cycle_raises_instead_of_silently_using_declaration_order():
    dag = DAG("d")
    dag.tasks = [
        Task("a", _mk("a"), deps=("b",)),
        Task("b", _mk("b"), deps=("a",)),
    ]
    with pytest.raises(ValueError, match="存在环"):
        dag.order()


def test_dag_missing_dependency_raises():
    dag = DAG("d")
    dag.tasks = [Task("a", _mk("a"), deps=("nope",))]
    with pytest.raises(KeyError):
        dag.order()


def test_local_executor_runs_in_dependency_order(tmp_path):
    events: list[str] = []
    dag = DAG("d")
    dag.tasks = [
        Task("a", _mk("a")),
        Task("b", _mk("b"), deps=("a",)),
        Task("c", _mk("c"), deps=("a",)),
    ]
    res = LocalExecutor(dag, _ctx(tmp_path, events=events)).run()
    assert res["status"] == SUCCESS
    assert events[0] == "a"
    assert set(events[1:]) == {"b", "c"}


def test_local_executor_retries_then_succeeds(tmp_path):
    attempts = {"n": 0}

    def flaky(ctx: Context) -> dict:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("暂时失败")
        return {"n_out": 1}

    dag = DAG("d")
    dag.tasks = [Task("a", flaky, retries=2)]
    res = LocalExecutor(dag, _ctx(tmp_path)).run()
    assert res["status"] == SUCCESS
    assert res["tasks"]["a"]["_attempt"] == 3


def test_local_executor_records_failed_and_stops_downstream(tmp_path):
    events: list[str] = []

    def boom(ctx: Context) -> dict:
        events.append("boom")
        raise RuntimeError("炸了")

    dag = DAG("d")
    dag.tasks = [Task("a", boom, retries=1), Task("b", _mk("b"), deps=("a",))]
    res = LocalExecutor(dag, _ctx(tmp_path, events=events)).run()
    assert res["status"] == FAILED
    assert res["failed_task"] == "a"
    assert "b" not in events, "上游失败后下游不许跑"


def test_local_executor_timeout_is_detected(tmp_path):
    def slow(ctx: Context) -> dict:
        time.sleep(1.2)
        return {}

    dag = DAG("d")
    dag.tasks = [Task("a", slow, timeout_s=0.1)]
    res = LocalExecutor(dag, _ctx(tmp_path)).run()
    assert res["status"] == FAILED
    led = RunLedger(tmp_path / "l.duckdb")
    assert led.task_states("r1")["a"] == TIMEOUT


def test_local_executor_resume_skips_success(tmp_path):
    led = RunLedger(tmp_path / "l.duckdb")
    events: list[str] = []
    led.start_run("r1", "d", "2026-09-22")
    led.start_task("r1", "a", 1)
    led.finish_task("r1", "a", SUCCESS)
    dag = DAG("d")
    dag.tasks = [Task("a", _mk("a")), Task("b", _mk("b"), deps=("a",))]
    res = LocalExecutor(dag, _ctx(tmp_path, led, events=events)).run(resume=True)
    assert res["status"] == SUCCESS
    assert res["tasks"]["a"]["_status"] == SKIPPED
    assert events == ["b"], "已成功的阶段不该重跑"


def test_local_executor_only_runs_subset_without_dependency_check(tmp_path):
    """`--only` 是 Airflow 的单阶段调用：上游由 Airflow 负责，本执行器不拦。"""
    events: list[str] = []
    dag = DAG("d")
    dag.tasks = [Task("a", _mk("a")), Task("b", _mk("b"), deps=("a",))]
    res = LocalExecutor(dag, _ctx(tmp_path, events=events)).run(only=("b",))
    assert res["status"] == SUCCESS
    assert events == ["b"]


# ---------------------------------------------------------------------------
# Airflow 导出
# ---------------------------------------------------------------------------


def _chain_dag() -> DAG:
    dag = DAG("demo")
    dag.tasks = [
        Task("ods", _mk("ods"), retries=1, description="落地"),
        Task("dwd", _mk("dwd"), deps=("ods",), retries=2, timeout_s=60.0, description="明细"),
        Task("ads", _mk("ads"), deps=("dwd",), timeout_s=30.0, description="宽表"),
    ]
    return dag


def test_airflow_source_is_valid_and_dag_id_is_not_doubled():
    dag = _chain_dag()
    src = to_airflow_source(dag, dag_id="mm_curation_platform", schedule="@daily")
    assert validate_airflow_source(src, dag) == []
    assert "dag_id='mm_curation_platform'" in src
    assert "mm_curation_mm_curation_platform" not in src
    assert "{{ run_id }}" in src and "{{ ds }}" in src


def test_airflow_default_dag_id_uses_dag_name():
    dag = DAG("mm_curation_platform")
    dag.tasks = [Task("a", _mk("a"))]
    src = to_airflow_source(dag)
    assert "dag_id='mm_curation_platform'" in src


def test_airflow_export_has_no_self_loop_on_start():
    """回归：`__start__` 曾依赖自身（`_ops['__start__'] >> _ops['__start__']`）。"""
    dag = _chain_dag()
    src = to_airflow_source(dag)
    assert validate_airflow_source(src, dag) == []
    assert '"__start__": {\n        "deps": [],' in src
    assert "'__start__']\n        _ops['__start__']" not in src


# --- 负例：每个校验项都必须能抓到"被改坏" ---
#
# 用**结构化改写**而不是字符串替换：初版的这几条用例做了 `src.replace("...")`，
# 结果因为 `json.dumps(indent=4)` 的缩进对不上，替换静默失效 →
# 用例"通过"了，但通过的原因是**什么都没改**（校验器当然报 0 问题）。
# 这是测试自己制造的假绿，所以这里改成：解析出 TASKS 字面量 → 改 → 回写，
# 并**强制断言改确实生效**（`broken != src`），否则用例直接失败。


def _mutate_tasks(src: str, fn) -> str:
    import ast
    import json

    tree = ast.parse(src)
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "TASKS"
    )
    data = ast.literal_eval(node.value)
    fn(data)
    lines = src.splitlines(keepends=True)
    lit = "TASKS = " + json.dumps(data, ensure_ascii=False, indent=4, sort_keys=True) + "\n"
    return "".join(lines[: node.lineno - 1]) + lit + "".join(lines[node.end_lineno :])


def test_validator_catches_missing_stage():
    dag = _chain_dag()
    src = to_airflow_source(dag)
    broken = _mutate_tasks(src, lambda d: d.pop("ads"))
    assert broken != src, "改写没生效，负例无效"
    problems = validate_airflow_source(broken, dag)
    assert problems and any("阶段集合不一致" in p for p in problems), problems


def test_validator_catches_dependency_rewrite():
    dag = _chain_dag()
    src = to_airflow_source(dag)
    broken = _mutate_tasks(src, lambda d: d["dwd"].update({"deps": ["ads"]}))
    assert broken != src, "改写没生效，负例无效"
    problems = validate_airflow_source(broken, dag)
    assert problems, "dwd 的依赖被改成 ads 后必须报问题"
    assert any("依赖被改写" in p for p in problems), problems


def test_validator_catches_self_loop():
    dag = _chain_dag()
    src = to_airflow_source(dag)
    broken = _mutate_tasks(src, lambda d: d["__start__"].update({"deps": ["__start__"]}))
    assert broken != src, "改写没生效，负例无效"
    problems = validate_airflow_source(broken, dag)
    assert any("自环" in p for p in problems), problems
    # 这一条是当初漏掉的缺陷：`__start__` 依赖自身时，初版校验器报 0 问题
    assert "依赖图有环" in " ".join(problems) or any("环" in p for p in problems)


def test_validator_catches_cycle():
    dag = _chain_dag()
    src = to_airflow_source(dag)
    # ods 依赖 ads，而 ads → dwd → ods 成环
    broken = _mutate_tasks(src, lambda d: d["ods"].update({"deps": ["ads"]}))
    assert broken != src, "改写没生效，负例无效"
    problems = validate_airflow_source(broken, dag)
    assert any("环" in p for p in problems), problems


def test_validator_catches_unknown_dependency():
    dag = _chain_dag()
    src = to_airflow_source(dag)
    broken = _mutate_tasks(src, lambda d: d["dwd"].update({"deps": ["nope"]}))
    assert broken != src, "改写没生效，负例无效"
    problems = validate_airflow_source(broken, dag)
    assert any("不在任务集合里" in p for p in problems), problems


def test_validator_catches_syntax_error():
    problems = validate_airflow_source("def (:", _chain_dag())
    assert problems and "不是合法 Python" in problems[0]


def test_validator_catches_non_literal_tasks():
    src = """from airflow import DAG
TASKS = dict()
with DAG(dag_id='x') as dag:
    pass
"""
    problems = validate_airflow_source(src, _chain_dag())
    assert problems and any("找不到 TASKS 字面量" in p for p in problems), problems
