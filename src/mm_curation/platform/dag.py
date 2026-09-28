"""声明式编排（S3）：**编排定义只有一份，执行引擎可替换**。

## 为什么不直接把逻辑写进 Airflow DAG

`dags/multimodal_curation_dag.py` 的现状说明了这个问题的代价：它是
`schedule=None` 的手动 DAG、硬编码容器路径 `/opt/airflow/scripts/*.py`、
**编排的还是早期脚本而不是现在的数仓链路**。根因不是"没写 DAG"，
而是**编排逻辑和引擎耦合**：DAG 里塞 bash 命令，于是本机（无 Docker、无 Airflow）
一步都验不了，验不了的东西自然就腐烂。

本模块把两者分开：

| 关注点 | 在哪 |
|---|---|
| 「有哪些阶段、谁依赖谁、重试几次、超时多少」 | `Task` / `DAG`（纯数据） |
| 「怎么执行一个阶段」 | `LocalExecutor`（可本机跑，真出数） |
| 「Airflow 长什么样」 | `to_airflow_source()`（生成 DAG 文件，CI 里结构校验） |

于是：**同一份定义，本机能跑通、CI 能校验、Airflow 能接管**。

## 三条如实说明

1. **超时是"检测"不是"强杀"**：任务在同一个进程的线程里跑，线程无法被强杀。
   超时后本执行器把该阶段记 `TIMEOUT` 并中断后续阶段，但**不保证底层工作立刻停止**。
   真正的强杀需要进程级隔离（容器 / K8s pod），那是 S6 的范畴，不是这里假装有的。
2. **断点续跑靠台账**：`--resume` 跳过的是"这次 run_id 下已 `SUCCESS` 的阶段"。
   隔了一次运行不算——因为**参数可能变了**，跨运行的续跑需要参数指纹比对，本版未做。
3. **Airflow 路径只能靠 CI 验**：本机拉不到 `registry-1.docker.io`（已记录），
   所以生成出来的 DAG 由 CI 做**结构与依赖校验**，容器里真跑留给 CI。
"""

from __future__ import annotations

import ast
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .runs import FAILED, SKIPPED, SUCCESS, TIMEOUT, RunLedger

# 任务函数签名：接收一个上下文，返回报告 dict（可含 n_in/n_out/fingerprint/…）
TaskFn = Callable[["Context"], dict[str, Any]]


@dataclass
class Task:
    name: str
    fn: TaskFn
    deps: tuple[str, ...] = ()
    retries: int = 0
    timeout_s: float = 0.0
    description: str = ""


@dataclass
class Context:
    """一次运行的上下文（阶段函数能拿到的全部东西）。"""

    run_id: str
    batch_date: str
    root: Path
    ledger: RunLedger
    con: Any  # platform.duckdb 连接（能读湖上 parquet 与维表）
    lake: Any
    # 产物根（环境隔离点）。`None` = 与 `root` 相同，即 dev 的既有行为。
    # ⚠️ 为什么不是"把 root 换成 store"就行：`root` 还要用来读源（data/raw）与
    # 配置（configs），那两样**每个环境共用同一份**（同一份代码、同一个 git_sha），
    # 混成一个字段就没法既隔离产物、又共用源码。见 `envs` 模块文档。
    store: Path | None = None
    datasets: tuple[str, ...] = ()
    event_dates: tuple[str, ...] = ()
    verbose: bool = True
    # 阶段函数的可选开关（incremental / limit / contract_blocking / slo …）。
    # 放进一个 dict 而不是给每个开关加字段：开关会越来越多，
    # 而「阶段函数只认一个 ctx」这条约定不该随开关数量变化。
    extra: dict[str, Any] = field(default_factory=dict)

    def store_root(self) -> Path:
        """产物写到哪（dev 下与 `root` 相同）。"""
        return self.store or self.root

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)


@dataclass
class DAG:
    name: str
    tasks: list[Task] = field(default_factory=list)

    def task(self, name: str) -> Task:
        for t in self.tasks:
            if t.name == name:
                return t
        raise KeyError(f"未定义的阶段 {name!r}；可用：{[t.name for t in self.tasks]}")

    def order(self) -> list[Task]:
        """Kahn 拓扑排序。**发现环就报错**——静默按声明顺序执行会让依赖形同虚设。"""
        indeg = {t.name: len(t.deps) for t in self.tasks}
        for t in self.tasks:
            for d in t.deps:
                self.task(d)  # 依赖必须存在
        queue = [t.name for t in self.tasks if indeg[t.name] == 0]
        out: list[Task] = []
        while queue:
            name = queue.pop(0)
            out.append(self.task(name))
            for t in self.tasks:
                if name in t.deps:
                    indeg[t.name] -= 1
                    if indeg[t.name] == 0:
                        queue.append(t.name)
        if len(out) != len(self.tasks):
            stuck = [t.name for t in self.tasks if t not in out]
            raise ValueError(f"DAG {self.name!r} 存在环：{stuck}")
        return out

    def as_dict(self) -> dict[str, Any]:
        """结构化视图（供导出、文档、CI 校验共用）。"""
        return {
            "name": self.name,
            "tasks": [
                {
                    "name": t.name,
                    "deps": list(t.deps),
                    "retries": t.retries,
                    "timeout_s": t.timeout_s,
                    "description": t.description,
                }
                for t in self.order()
            ],
        }


class LocalExecutor:
    """本机执行器：按依赖序跑，落台账，支持重试 / 断点续跑 / 单阶段执行。"""

    def __init__(self, dag: DAG, ctx: Context):
        self.dag = dag
        self.ctx = ctx

    # -- 内部 ---------------------------------------------------------------

    def _invoke(self, task: Task) -> dict[str, Any]:
        """跑一个阶段，返回报告。超时在**线程外**判定（见模块头说明 1）。"""
        if task.timeout_s <= 0:
            return task.fn(self.ctx) or {}
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(task.fn, self.ctx)
            try:
                return fut.result(timeout=task.timeout_s)
            except FutureTimeout as e:  # pragma: no cover - 取决于机器负载
                raise TimeoutError(f"阶段 {task.name} 超过 {task.timeout_s}s") from e

    def _run_one(self, task: Task, seq: int) -> dict[str, Any]:
        ledger = self.ctx.ledger
        last_err = ""
        for attempt in range(1, task.retries + 2):
            ledger.start_task(self.ctx.run_id, task.name, seq, attempt=attempt)
            t0 = time.perf_counter()
            try:
                rep = self._invoke(task)
                ledger.finish_task(
                    self.ctx.run_id,
                    task.name,
                    SUCCESS,
                    attempt=attempt,
                    n_in=int(rep.get("n_in") or 0),
                    n_out=int(rep.get("n_out") or 0),
                    bytes_scanned=int(rep.get("bytes_scanned") or 0),
                    bytes_written=int(rep.get("bytes_written") or 0),
                    fingerprint=str(rep.get("fingerprint") or ""),
                    metrics=rep,
                )
                rep = dict(rep)
                rep["_status"] = SUCCESS
                rep["_attempt"] = attempt
                rep["_duration_s"] = round(time.perf_counter() - t0, 3)
                return rep
            except Exception as e:  # noqa: BLE001 - 阶段失败要变台账而不是崩栈
                last_err = f"{type(e).__name__}: {e}"
                status = TIMEOUT if isinstance(e, TimeoutError) else FAILED
                ledger.finish_task(
                    self.ctx.run_id,
                    task.name,
                    status,
                    attempt=attempt,
                    error=last_err + "\n" + traceback.format_exc()[-800:],
                )
                self.ctx.log(f"    [retry {attempt}/{task.retries + 1}] {task.name}: {last_err}")
                if status == TIMEOUT:
                    break
        raise RuntimeError(f"阶段 {task.name} 失败：{last_err}")

    # -- 对外 ---------------------------------------------------------------

    def run(self, *, resume: bool = False, only: Sequence[str] = ()) -> dict[str, Any]:
        """执行整条 DAG（或只用 `only` 指定若干阶段，供 Airflow 单阶段调用）。"""
        ledger = self.ctx.ledger
        if only:
            plan = [self.dag.task(n) for n in only]
        else:
            plan = self.dag.order()

        already = ledger.task_states(self.ctx.run_id) if resume else {}
        results: dict[str, Any] = {}
        dispatched: set[str] = set()
        t0 = time.perf_counter()

        for seq, task in enumerate(plan, start=1):
            if not only:
                missing = [d for d in task.deps if d not in dispatched]
                if missing:
                    ledger.finish_run(self.ctx.run_id, FAILED, f"依赖未完成：{missing}")
                    raise RuntimeError(f"阶段 {task.name} 的上游未完成：{missing}")
            if resume and already.get(task.name) == SUCCESS:
                self.ctx.log(f"  [skip] {task.name}（本次运行已成功）")
                ledger.finish_task(self.ctx.run_id, task.name, SKIPPED)
                dispatched.add(task.name)
                results[task.name] = {"_status": SKIPPED}
                continue
            self.ctx.log(f"  [run ] {task.name}  {task.description}")
            try:
                results[task.name] = self._run_one(task, seq)
            except Exception as e:  # noqa: BLE001
                ledger.finish_run(self.ctx.run_id, FAILED, str(e))
                self.ctx.log(f"  [FAIL] {task.name}: {e}")
                return {
                    "run_id": self.ctx.run_id,
                    "status": FAILED,
                    "failed_task": task.name,
                    "duration_s": round(time.perf_counter() - t0, 3),
                    "tasks": results,
                }
            dispatched.add(task.name)
            r = results[task.name]
            self.ctx.log(
                f"         ok  n_in={r.get('n_in', '-')} n_out={r.get('n_out', '-')} "
                f"fp={(str(r.get('fingerprint') or '-'))[:12]}  {r.get('_duration_s')}s"
            )

        ledger.finish_run(self.ctx.run_id, SUCCESS)
        return {
            "run_id": self.ctx.run_id,
            "status": SUCCESS,
            "duration_s": round(time.perf_counter() - t0, 3),
            "tasks": results,
        }


# ---------------------------------------------------------------------------
# Airflow 导出（生成源码文本；结构与依赖由 CI 校验，容器里真跑留给 CI）
# ---------------------------------------------------------------------------

_AIRFLOW_TEMPLATE = '''"""自动生成，请勿手改（说明见 docs/PLATFORM.md）。

生成物是**适配器**，不是第二个事实源：阶段、依赖、重试、超时全部来自
`src/mm_curation/platform/jobs.py`。改这里会在下次导出时被覆盖。

⚠️ 单阶段语义：每个 BashOperator 调 `--only <阶段>`，是**独立进程**。
进程级隔离带来的正是"超时可强杀、重试可隔离"这两件事；
代价是 `job_runs` 那行由非 `--only` 的运行写入，Airflow 轨只留 `task_runs` 明细
（`jobs.run_platform` 的注释解释了为什么不在这条路上写 job_runs）。
"""

from __future__ import annotations

import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

TASKS = {tasks_json}

with DAG(
    dag_id={dag_id!r},
    description="mm-curation 数据平台链路（ODS→DIM→DWD→DWS→ADS→契约/观测）",
    start_date=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    schedule={schedule!r},
    catchup=True,
    max_active_runs=1,
    default_args={{"retries": 0}},
    tags=["platform", "warehouse"],
) as dag:
    _ops = {{}}
    for _name, _spec in TASKS.items():
        _ops[_name] = BashOperator(
            task_id=_name,
            bash_command=(
                "python -m mm_curation.cli platform run "
                "--only " + _name + " --run-id {{{{ run_id }}}} --batch-date {{{{ ds }}}}"
            ),
            retries=_spec["retries"],
            execution_timeout=datetime.timedelta(seconds=_spec["timeout_s"])
            if _spec["timeout_s"] else None,
            doc_md=_spec["description"],
        )
    for _name, _spec in TASKS.items():
        for _dep in _spec["deps"]:
            _ops[_dep] >> _ops[_name]
'''


def to_airflow_source(dag: DAG, *, dag_id: str = "", schedule: str = "@daily") -> str:
    """把 DAG 定义渲染成 Airflow DAG 源码。

    用 `BashOperator` 调 `mmc platform run --only <阶段>`，
    每个阶段是**独立进程**——这正是 Airflow 能给出"进程级超时与重试"的原因，
    也是本地执行器做不到强杀的那件事（模块头说明 1）。
    """
    spec = dag.as_dict()
    tasks: dict[str, dict[str, Any]] = {}
    for t in spec["tasks"]:
        # 无上游的阶段挂到 __start__，避免 Airflow 里出现孤立任务
        tasks[t["name"]] = {
            "deps": list(t["deps"]) or ["__start__"],
            "retries": t["retries"],
            "timeout_s": t["timeout_s"],
            "description": t["description"],
        }
    tasks["__start__"] = {"deps": [], "retries": 0, "timeout_s": 0, "description": "起点"}
    # ⚠️ 这里**不能**再写 "deps 为空就挂 __start__" 的兜底循环：
    # 那会把 `__start__` 自己也改成 `deps=["__start__"]`（自环），
    # 生成出 `_ops['__start__'] >> _ops['__start__']`。
    # 上面那行 `list(t["deps"]) or ["__start__"]` 已经覆盖了普通阶段，无需第二次。
    return _AIRFLOW_TEMPLATE.format(
        tasks_json=json.dumps(tasks, ensure_ascii=False, indent=4, sort_keys=True),
        # 默认 dag_id 用 dag.name 本身：它已经是 `mm_curation_*` 形式，
        # 再套一层 `mm_curation_` 前缀会生成 `mm_curation_mm_curation_platform`。
        dag_id=dag_id or dag.name,
        schedule=schedule,
    )


def validate_airflow_source(src: str, dag: DAG) -> list[str]:
    """结构校验生成物：**不导入 airflow**（CI 里没装它），只做 AST 与字面量检查。

    校验四件事：① 能解析；② 阶段集合一致；③ 每个依赖都在任务集合里；
    ④ **没有自环**。

    第 ④ 条是补上的：初版只查 ①②③，于是在一个 `__start__ -> __start__` 自环的
    生成物上**报了 0 个问题**——门禁把"有环"这种最容易炸的东西放过去了。
    这正是"跑了门禁的子集等于没跑"那类假绿：检查项看着有，被检查的集合不全。
    """
    problems: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"生成物不是合法 Python：{e}"]
    literal: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "TASKS":
                    try:
                        literal = ast.literal_eval(node.value)
                    except ValueError:
                        problems.append("TASKS 不是字面量，无法静态校验")
    if not literal:
        problems.append("生成物里找不到 TASKS 字面量")
        return problems
    gen = set(literal) - {"__start__"}
    want = {t.name for t in dag.tasks}
    if gen != want:
        problems.append(f"阶段集合不一致：多 {sorted(gen - want)} 少 {sorted(want - gen)}")
    for name, spec in literal.items():
        for dep in spec["deps"]:
            if dep not in literal:
                problems.append(f"{name} 的依赖 {dep} 不在任务集合里")
            if dep == name:
                problems.append(f"{name} 依赖自身（自环）")
    # 环检测：不限自环，`a->b->a` 同样让 Airflow 建不出 DAG
    order: list[str] = []
    indeg = {n: sum(1 for d in s["deps"] if d in literal) for n, s in literal.items()}
    queue = [n for n, k in indeg.items() if k == 0]
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for n, s in literal.items():
            if cur in s["deps"]:
                indeg[n] -= 1
                if indeg[n] == 0:
                    queue.append(n)
    if len(order) != len(literal):
        problems.append(f"依赖图有环：{sorted(set(literal) - set(order))}")
    for name in want:
        if name in literal:
            declared = dag.task(name).deps
            if declared and list(literal[name]["deps"]) != list(declared):
                got = list(literal[name]["deps"])
                problems.append(f"{name} 依赖被改写：期望 {list(declared)} 实际 {got}")
    return problems
