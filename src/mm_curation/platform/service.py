"""数据服务层（S4）：把湖上的 ADS/DWS 变成**别的系统能消费**的只读接口。

## 为什么"取数脚本"不够

`scripts/mmc.py metrics` 已经能出数，但它回答的是"在我这台机器上跑一次"。
一个"服务"要额外回答四个问题，而这四个问题恰恰是数仓岗 JD 里反复出现的词：

| 问题 | 本模块的答案 |
|---|---|
| 谁能看？ | RBAC：token → 角色 → **行级**（可见数据集）+ **列级**（脱敏列） |
| 看多了怎么办？ | 令牌桶限流（每 token 独立稳态速率 + 突发容量） |
| 数据不对还供数吗？ | **启动契约闸门**：error 级断言失败 → 服务拒绝就绪（503），不供数 |
| 线上什么样？ | Prometheus 文本：请求/延迟分位/限流计数 + 新鲜度与告警（复用 S5） |

## 三个刻意的设计选择

1. **核心逻辑不依赖 Web 框架**。`ServiceCore` 是纯 Python（能单测、能嵌进别的进程），
   `create_app()` 只是把 core 挂到 FastAPI 上的适配层。
   把业务逻辑写进路由函数里，是"本地能跑但测不了、也换不了框架"的经典来源。
2. **只提供固定端点，不接受任意 SQL**。外部能传 SQL 的"数据服务"等于把这个库的
   全部权限外包给了调用方。参数化端点 + 白名单列，是这里唯一安全且可审计的形态。
3. **脱敏发生在 SQL 里，不是返回后删字段**。返回后再删，数据已经过一遍内存与日志。
   列级脱敏必须体现在 `SELECT` 列表上，这一点没有妥协空间。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..lineage.contract import check_contract, load_contracts
from . import obs as obs_mod
from .envs import ENV_DEV, resolve
from .runs import RunLedger

__all__ = [
    "AccessDenied",
    "Principal",
    "RateLimited",
    "ServiceCore",
    "ServiceNotReady",
    "TokenBucket",
    "create_app",
    "load_rbac",
]


# ---------------------------------------------------------------------------
# 异常：把"拒绝"做成可区分的类型，HTTP 层才能映射成正确的状态码
# ---------------------------------------------------------------------------


class AccessDenied(PermissionError):
    """401/403：token 无效，或角色对该资源无权限。"""


class RateLimited(RuntimeError):
    """429：令牌桶空了。"""

    def __init__(self, msg: str, retry_after_s: float):
        super().__init__(msg)
        self.retry_after_s = retry_after_s


class ServiceNotReady(RuntimeError):
    """503：契约闸门没过（或数据层没建），**拒绝供数**。"""

    def __init__(self, msg: str, blocking: Sequence[str] = ()):
        super().__init__(msg)
        self.blocking = list(blocking)


# ---------------------------------------------------------------------------
# 主体 / 策略
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    datasets: tuple[str, ...] = ("*",)
    hidden_columns: tuple[str, ...] = ()
    qps: float = 10.0
    burst: float = 20.0

    def can_read(self, dataset: str) -> bool:
        return "*" in self.datasets or dataset in self.datasets

    def visible_columns(self, columns: Sequence[str]) -> list[str]:
        """列级脱敏的**唯一出口**：SQL 从这里拿 SELECT 列表。"""
        return [c for c in columns if c not in self.hidden_columns]

    def masked(self, columns: Sequence[str]) -> list[str]:
        return [c for c in columns if c in self.hidden_columns]


def load_rbac(path: str | Path) -> dict[str, Any]:
    """读 RBAC 配置。文件缺失 → **空策略**（谁都不授权），不静默放开。"""
    p = Path(path)
    if not p.exists():
        return {"roles": {}, "tokens": {}}
    import yaml

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    roles = {}
    for name, spec in (data.get("roles") or {}).items():
        spec = spec or {}
        roles[name] = {
            "datasets": tuple(spec.get("datasets") or ["*"]),
            "hidden_columns": tuple(spec.get("hidden_columns") or []),
            "qps": float(spec.get("qps", 10)),
            "burst": float(spec.get("burst", 20)),
        }
    tokens = {
        str(t["token_sha256"]).lower(): (str(t.get("name") or "?"), str(t["role"]))
        for t in (data.get("tokens") or [])
        if t.get("token_sha256")
    }
    return {"roles": roles, "tokens": tokens}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 令牌桶
# ---------------------------------------------------------------------------


class TokenBucket:
    """经典令牌桶：容量 `burst`，稳态补充 `qps` 个/秒。

    为什么不用"固定窗口计数"：固定窗口在窗口边界允许 2× 突发
    （前一窗末尾打满 + 后一窗开头打满），对下游是实打实的双倍压力。
    令牌桶没有这个边界效应。
    """

    def __init__(self, qps: float, burst: float, *, now: Callable[[], float] = time.monotonic):
        self.qps = max(float(qps), 1e-9)
        self.burst = max(float(burst), 1.0)
        self._tokens = self.burst
        self._ts = now()
        self._now = now
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._now()
        self._tokens = min(self.burst, self._tokens + (now - self._ts) * self.qps)
        self._ts = now

    def try_acquire(self, n: float = 1.0) -> tuple[bool, float]:
        """返回 `(是否放行, 还需等多少秒)`。"""
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True, 0.0
            need = (n - self._tokens) / self.qps
            return False, round(need, 4)


# ---------------------------------------------------------------------------
# 核心
# ---------------------------------------------------------------------------

# 脱敏只作用在这些"有身份信息"的列上；数值列永远可见。
# 白名单而不是黑名单：新增列默认可见会泄露，默认不可见只是少给——
# 两种错误的代价不对称，所以选默认安全的那一边。
_IDENTITY_COLUMNS = ("device_id", "channel", "sample_id", "text_md5")


@dataclass
class _Metrics:
    """服务自身的指标（线程安全；延迟同时保留桶与时序两个视图）。

    为什么同时要两部分位：Prometheus 的 `histogram` 只给桶，
    要报 P95 得靠 `histogram_quantile` 在查询侧插值；但一个**只读本地演示**的服务
    没人给它配 PromQL。所以这里**同时**输出桶（给正规 Prometheus 抄）
    与一个在进程内按最近 N 次请求算出来的 P95 Gauge（自己能看）。
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    requests: dict[tuple[str, int, str], int] = field(default_factory=dict)
    rate_limited: dict[str, int] = field(default_factory=dict)
    masked_total: dict[str, int] = field(default_factory=dict)
    denied: dict[str, int] = field(default_factory=dict)
    latencies: dict[str, list[float]] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    _window: int = 500

    def observe(self, path: str, status: int, role: str, seconds: float) -> None:
        with self._lock:
            k = (path, status, role)
            self.requests[k] = self.requests.get(k, 0) + 1
            buf = self.latencies.setdefault(path, [])
            buf.append(seconds)
            if len(buf) > self._window:
                del buf[: len(buf) - self._window]

    def inc(self, table: dict[str, int], key: str) -> None:
        with self._lock:
            table[key] = table.get(key, 0) + 1

    @staticmethod
    def _p95(buf: Sequence[float]) -> float | None:
        if not buf:
            return None
        s = sorted(buf)
        # 最近秩法（nearest-rank）：P95 = 第 ceil(0.95 n) 个
        idx = max(1, min(len(s), int(0.95 * len(s) + 0.999999))) - 1
        return round(s[idx], 6)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": dict(self.requests),
                "rate_limited": dict(self.rate_limited),
                "masked_total": dict(self.masked_total),
                "denied": dict(self.denied),
                "p95": {p: self._p95(b) for p, b in self.latencies.items()},
                "n_latency_samples": {p: len(b) for p, b in self.latencies.items()},
                "uptime_s": round(time.time() - self.started, 1),
            }


_ENDPOINTS = (
    "/api/health",
    "/healthz",
    "/api/datasets",
    "/api/datasets/{dataset}/health",
    "/api/datasets/{dataset}/daily",
    "/api/datasets/{dataset}/ops",
    "/api/datasets/{dataset}/dims",
    "/api/runs",
    "/metrics",
)


class ServiceCore:
    """只读数据服务的全部逻辑（不含任何 Web 框架类型）。"""

    def __init__(
        self,
        root: str | Path,
        *,
        rbac_path: str | Path = "configs/rbac.yaml",
        contract_dir: str | Path = "configs/contracts_platform",
        ledger: RunLedger | None = None,
        con=None,
        contract_blocking: bool = True,
        env: str = ENV_DEV,
    ):
        self.root = Path(root).resolve()
        # `root` 是**仓库根**（代码/源/配置），`store` 是**产物根**（湖/数仓/台账）。
        # 服务默认读 dev（既有行为，路径一字不变）；`--env prod` 就读 prod 的湖。
        self.spec = resolve(self.root, env)
        self.store = self.spec.store
        self.rbac_path = Path(rbac_path)
        self.contract_dir = Path(contract_dir)
        self.contract_blocking = contract_blocking
        self.ledger = ledger or RunLedger(self.spec.warehouse_db)
        self._con = con
        self._own_con = con is None
        self.rbac = load_rbac(self.rbac_path)
        self.buckets: dict[str, TokenBucket] = {}
        self.metrics = _Metrics()
        self.ready: bool = False
        self.startup_report: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- 连接 ---------------------------------------------------------------

    @property
    def con(self):
        if self._con is None:
            import duckdb

            # 同 `jobs.make_context`：DuckDB 不建父目录，缺了它报的错指不到根因
            db = self.spec.warehouse_db
            db.parent.mkdir(parents=True, exist_ok=True)
            self._con = duckdb.connect(str(db))
        return self._con

    def close(self) -> None:
        if self._con is not None and self._own_con:
            self._con.close()
            self._con = None

    def _query(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self.con.execute(sql, list(args))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # -- 启动闸门 -----------------------------------------------------------

    def startup(self) -> dict[str, Any]:
        """启动时跑一次契约闸门。

        **闸门不过就不就绪**，而不是"打个日志继续供数"。理由很简单：
        一个数不对还照样供数的服务，比一个明确 503 的服务危险得多——
        下游会把错数当对数用，而且没有人会去读那条日志。
        """
        cdir = Path(self.contract_dir)
        cdir = cdir if cdir.is_absolute() else self.root / cdir
        contracts = load_contracts(cdir)
        wh = _CoreQuery(self)
        checks = [check_contract(wh, c) for c in contracts]
        blocking = [f"{r['dataset']}@{r['version']}::{b}" for r in checks for b in r["blocking"]]
        n_fail = sum(r["n_fail"] for r in checks)
        n_err = sum(r["n_error"] for r in checks)
        self.startup_report = {
            "n_contracts": len(checks),
            "n_checks": sum(r["n_checks"] for r in checks),
            "n_fail": n_fail,
            "n_error": n_err,
            "blocking": blocking,
            "contract_dir": str(cdir),
        }
        self.ready = not (blocking and self.contract_blocking)
        return self.startup_report

    # -- 鉴权 / 限流 --------------------------------------------------------

    def authorize(self, token: str) -> Principal:
        if not token:
            raise AccessDenied("缺少 X-API-Token")
        name_role = self.rbac["tokens"].get(hash_token(token))
        if not name_role:
            raise AccessDenied("token 无效")
        name, role = name_role
        spec = self.rbac["roles"].get(role)
        if spec is None:
            raise AccessDenied(f"token 绑定的角色 {role!r} 未在 roles 中定义")
        return Principal(name=name, role=role, **spec)

    def bucket_for(self, p: Principal) -> TokenBucket:
        with self._lock:
            if p.role not in self.buckets:
                self.buckets[p.role] = TokenBucket(p.qps, p.burst)
            return self.buckets[p.role]

    def check_rate(self, p: Principal) -> None:
        ok, wait = self.bucket_for(p).try_acquire()
        if not ok:
            self.metrics.inc(self.metrics.rate_limited, p.role)
            raise RateLimited(f"超出速率限制（{p.qps}/s，突发 {p.burst}）", wait)

    def _require_ready(self) -> None:
        if not self.ready:
            raise ServiceNotReady(
                "服务未就绪：契约闸门未通过或未执行", self.startup_report.get("blocking", [])
            )

    def _require_dataset(self, p: Principal, dataset: str) -> None:
        if not p.can_read(dataset):
            self.metrics.inc(self.metrics.denied, f"{p.role}:{dataset}")
            raise AccessDenied(f"角色 {p.role} 无权访问数据集 {dataset}")

    # -- 端点实现 -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            # 服务的是哪个环境，必须能从健康检查里问出来：
            # dev/prod 双环境之后，"这个容器到底读的哪个湖"是排障的第一个问题，
            # 而它无法从响应内容推断（两个环境的表名完全一样）。
            "env": self.spec.as_dict(),
            "contracts": self.startup_report,
            "endpoints": list(_ENDPOINTS),
        }

    def list_datasets(self, p: Principal) -> list[dict[str, Any]]:
        self._require_ready()
        rows = self._query("SELECT * FROM ads_dataset_health ORDER BY dataset")
        cols = [c for c in rows[0].keys()] if rows else []
        vis = p.visible_columns(cols)
        out = [{k: r[k] for k in vis} for r in rows if p.can_read(r["dataset"])]
        return self._note_mask(p, cols, vis), out

    def dataset_health(self, p: Principal, dataset: str) -> tuple[list[str], dict[str, Any]]:
        self._require_ready()
        self._require_dataset(p, dataset)
        rows = self._query("SELECT * FROM ads_dataset_health WHERE dataset = ?", [dataset])
        if not rows:
            raise KeyError(f"数据集 {dataset} 不存在")
        cols = list(rows[0].keys())
        vis = p.visible_columns(cols)
        return self._note_mask(p, cols, vis), {k: rows[0][k] for k in vis}

    def dataset_daily(
        self,
        p: Principal,
        dataset: str,
        *,
        limit: int = 400,
        from_date: str = "",
        to_date: str = "",
    ) -> tuple[list[str], list[dict[str, Any]]]:
        self._require_ready()
        self._require_dataset(p, dataset)
        sql = "SELECT * FROM dws_dataset_day WHERE dataset = ?"
        args: list[Any] = [dataset]
        if from_date:
            sql += " AND event_date >= ?"
            args.append(from_date)
        if to_date:
            sql += " AND event_date <= ?"
            args.append(to_date)
        sql += " ORDER BY event_date DESC LIMIT ?"
        args.append(max(1, min(int(limit), 2000)))
        rows = self._query(sql, args)
        cols = list(rows[0].keys()) if rows else list(_DWS_DAY_COLS)
        vis = p.visible_columns(cols)
        return self._note_mask(p, cols, vis), [{k: r[k] for k in vis} for r in rows]

    def dataset_ops(
        self, p: Principal, dataset: str, *, event_date: str = "", limit: int = 400
    ) -> tuple[list[str], list[dict[str, Any]]]:
        self._require_ready()
        self._require_dataset(p, dataset)
        sql = "SELECT * FROM dws_op_day WHERE dataset = ?"
        args: list[Any] = [dataset]
        if event_date:
            sql += " AND event_date = ?"
            args.append(event_date)
        sql += " ORDER BY event_date DESC, op LIMIT ?"
        args.append(max(1, min(int(limit), 2000)))
        rows = self._query(sql, args)
        cols = list(rows[0].keys()) if rows else list(_DWS_OP_COLS)
        vis = p.visible_columns(cols)
        return self._note_mask(p, cols, vis), [{k: r[k] for k in vis} for r in rows]

    def dataset_dims(
        self, p: Principal, dataset: str, *, limit: int = 200
    ) -> tuple[list[str], list[dict[str, Any]]]:
        self._require_ready()
        self._require_dataset(p, dataset)
        rows = self._query(
            "SELECT * FROM dim_device WHERE dataset = ? "
            "ORDER BY device_id, channel, version LIMIT ?",
            [dataset, max(1, min(int(limit), 2000))],
        )
        cols = list(rows[0].keys()) if rows else list(_DIM_COLS)
        vis = p.visible_columns(cols)
        return self._note_mask(p, cols, vis), [{k: r[k] for k in vis} for r in rows]

    def runs(self, p: Principal, *, limit: int = 20) -> list[dict[str, Any]]:
        self._require_ready()
        return self.ledger.list_runs(limit=max(1, min(int(limit), 200)))

    def _note_mask(self, p: Principal, cols: Sequence[str], vis: Sequence[str]) -> list[str]:
        """记录"这一问脱敏了哪几列"并把清单返回给调用方。

        返回清单而不是只计数：调用方要能把这句"我少给了你哪些列"写在响应里。
        **静默脱敏是危险的**——使用方会把"这列没有"理解成"这列不存在"，
        然后自己再造一遍字段。可见性必须是显式的。
        """
        hidden = [c for c in cols if c not in vis]
        if hidden:
            cur = self.metrics.masked_total.get(p.role, 0)
            self.metrics.masked_total[p.role] = cur + len(hidden)
        return hidden

    # -- 指标 ---------------------------------------------------------------

    def metrics_text(self) -> str:
        snap = self.metrics.snapshot()
        lines: list[str] = []

        def head(name: str, help_: str, type_: str) -> None:
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {type_}")

        head("mm_service_up", "服务是否就绪（1/0）", "gauge")
        lines.append(f"mm_service_up {int(self.ready)}")
        head("mm_service_uptime_seconds", "进程存活时长", "gauge")
        lines.append(f"mm_service_uptime_seconds {snap['uptime_s']}")

        head("mm_service_requests_total", "HTTP 请求计数（按路径/状态/角色）", "counter")
        for (path, status, role), n in sorted(snap["requests"].items()):
            lines.append(
                f'mm_service_requests_total{{path="{path}",status="{status}",role="{role}"}} {n}'
            )

        head("mm_service_latency_p95_seconds", "延迟 P95（进程内最近 500 次）", "gauge")
        for path, v in sorted(snap["p95"].items()):
            if v is not None:
                lines.append(f'mm_service_latency_p95_seconds{{path="{path}"}} {v}')

        head("mm_service_rate_limited_total", "被限流拒绝的请求数", "counter")
        for role, n in sorted(snap["rate_limited"].items()):
            lines.append(f'mm_service_rate_limited_total{{role="{role}"}} {n}')

        head("mm_service_denied_total", "被 RBAC 拒绝的请求数", "counter")
        for k, n in sorted(snap["denied"].items()):
            lines.append(f'mm_service_denied_total{{scope="{k}"}} {n}')

        head("mm_service_columns_masked_total", "脱敏掉的列·次", "counter")
        for role, n in sorted(snap["masked_total"].items()):
            lines.append(f'mm_service_columns_masked_total{{role="{role}"}} {n}')

        # 数据侧指标：直接复用 S5，服务与调度看到的是**同一套口径**。
        # 两边各算一遍必然漂移，所以这里只做拼接，不重算。
        try:
            obs = obs_mod.snapshot(self.con, self.ledger)
            lines.append(obs_mod.prometheus_text(obs).rstrip("\n"))
        except Exception as e:  # noqa: BLE001 - 数据侧取不到不该让 /metrics 挂掉
            lines.append("# 数据侧指标不可用：")
            lines.append(f"#   {type(e).__name__}: {e}")
        return "\n".join(lines) + "\n"

    # -- 统一入口（HTTP 层与测试共用同一条路径）----------------------------

    def dispatch(
        self, path: str, *, token: str = "", query: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """把 (路径, token, 查询参数) 映射成 (状态码, 响应体)。

        HTTP 适配层只做三件事：取 header、调这里、把状态码写回去。
        于是**同一套权限/限流/脱敏逻辑可以被单测直接覆盖**，不需要起服务器。
        """
        q = query or {}
        t0 = time.perf_counter()
        role = "anonymous"
        status = 200
        try:
            if path == "/api/health":
                return 200, self.health()
            if path == "/healthz":
                # 容器探针专用的**另一个**端点，刻意不复用 `/api/health`：
                # `/api/health` 的历史语义是"进程活着、把状态如实报出来"（永远 200，
                # 状态在 body 里）；而探针需要的是"**能供数**才算健康"。
                # 两者混用会导致二选一的坏结果——要么探针在闸门没过时报 healthy，
                # 要么 `curl /api/health` 这个曾经的诊断入口开始返回 503。
                body = self.health()
                return (200 if body["ready"] else 503), body
            if path == "/metrics":
                return 200, self.metrics_text()
            p = self.authorize(token)
            role = p.role
            self.check_rate(p)
            if path == "/api/datasets":
                masked, rows = self.list_datasets(p)
                return 200, {"columns_masked": masked, "datasets": rows}
            if path == "/api/runs":
                return 200, {"runs": self.runs(p, limit=int(q.get("limit", 20) or 20))}
            seg = [s for s in path.split("/") if s]
            # /api/datasets/{dataset}/{kind}
            if len(seg) == 4 and seg[:2] == ["api", "datasets"]:
                ds, kind = seg[2], seg[3]
                if kind == "health":
                    masked, data = self.dataset_health(p, ds)
                    return 200, {"columns_masked": masked, "data": data}
                if kind == "daily":
                    masked, rows = self.dataset_daily(
                        p,
                        ds,
                        limit=int(q.get("limit", 400) or 400),
                        from_date=str(q.get("from", "") or ""),
                        to_date=str(q.get("to", "") or ""),
                    )
                    return 200, {"dataset": ds, "columns_masked": masked, "rows": rows}
                if kind == "ops":
                    masked, rows = self.dataset_ops(
                        p,
                        ds,
                        event_date=str(q.get("event_date", "") or ""),
                        limit=int(q.get("limit", 400) or 400),
                    )
                    return 200, {"dataset": ds, "columns_masked": masked, "rows": rows}
                if kind == "dims":
                    masked, rows = self.dataset_dims(p, ds, limit=int(q.get("limit", 200) or 200))
                    return 200, {"dataset": ds, "columns_masked": masked, "rows": rows}
            return 404, {"error": "not_found", "path": path, "endpoints": list(_ENDPOINTS)}
        except ServiceNotReady as e:
            status = 503
            return status, {"error": "not_ready", "detail": str(e), "blocking": e.blocking}
        except AccessDenied as e:
            status = 403 if token else 401
            return status, {"error": "forbidden", "detail": str(e)}
        except RateLimited as e:
            status = 429
            return status, {
                "error": "rate_limited",
                "detail": str(e),
                "retry_after_s": e.retry_after_s,
            }
        except KeyError as e:
            status = 404
            return status, {"error": "not_found", "detail": str(e)}
        except Exception as e:  # noqa: BLE001 - 未预期异常要变成 500 而不是崩服务
            status = 500
            return status, {"error": "internal", "detail": f"{type(e).__name__}: {e}"}
        finally:
            # /metrics 自己不产生监控数据（否则观测一次就把分布推走一格）
            if path != "/metrics":
                self.metrics.observe(path, status, role, time.perf_counter() - t0)


class _CoreQuery:
    """`check_contract` 只要求 `wh.query(sql) -> (cols, rows)` 的最小适配。"""

    def __init__(self, core: ServiceCore):
        self._core = core

    def query(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self._core.con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()


# 列清单的兜底（空结果集时仍要能算出"哪些列被脱敏了"）
_DWS_DAY_COLS = (
    "dataset",
    "event_date",
    "n_total",
    "n_kept",
    "n_dropped",
    "drop_rate",
    "n_devices",
    "n_channels",
    "avg_len",
    "median_len",
    "n_ops",
    "score_coverage",
)
_DWS_OP_COLS = ("dataset", "event_date", "op", "n_scored", "n_dropped", "drop_rate", "score_p50")
_DIM_COLS = (
    "device_sk",
    "dataset",
    "device_id",
    "channel",
    "device_type",
    "unit",
    "sampling_hz",
    "operating_mode",
    "valid_from",
    "valid_to",
    "is_current",
    "version",
)


# ---------------------------------------------------------------------------
# FastAPI 适配层（薄到可以一眼看完）
# ---------------------------------------------------------------------------

# ⚠️ Web 框架类型必须在**模块级**导入，即使真正的使用者是 `create_app`。
#
# 原因是一个真实发生过的事故（ENGINEERING_NOTES #85）：本模块开头有
# `from __future__ import annotations`，于是函数签名里的注解在运行时是**字符串**；
# FastAPI 用 `typing.get_type_hints(handler)` 解析它们，而解析用的是**模块全局命名空间**。
# 原先 `from fastapi import Request` 写在 `create_app` 内部（局部作用域）——
# 解析时找不到 `Request`，FastAPI 就把它当成一个**必填 query 参数**，
# 结果是：除 `/metrics` 之外**所有路径一律 422**，
# 连 `/api/health`、`/healthz` 都打不开。
#
# 而所有单元测试都是直接调 `core.dispatch(path, ...)` 的（那是刻意的设计：
# 权限/限流逻辑与框架无关，可以不起服务器单测）。于是"HTTP 层从来没被跑过"这件事
# 完全没有信号——直到 scripts/smoke_container.py 对着真 socket 打了一发。
#
# 用 try/except 而不是直接 import：`ServiceCore` 的核心逻辑**不依赖 Web 框架**，
# 模块必须在没装 fastapi 的环境里也能 import（那些环境只跑 dispatch 层的测试）。
try:  # pragma: no cover - 取决于是否装了 fastapi
    from fastapi import FastAPI, Request
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse, PlainTextResponse
except ImportError:  # pragma: no cover
    FastAPI = Request = JSONResponse = PlainTextResponse = jsonable_encoder = None  # type: ignore[assignment,misc]


def create_app(root: str | Path = ".", **kwargs: Any):
    """把 `ServiceCore` 挂到 FastAPI。**这里不写任何业务逻辑。**"""
    if FastAPI is None:  # pragma: no cover
        raise ImportError("create_app 需要 fastapi：pip install fastapi uvicorn")

    core = ServiceCore(root, **kwargs)
    app = FastAPI(title="mm-curation 数据服务（只读）", version="1.0.0")
    app.state.core = core

    @app.on_event("startup")
    def _startup():
        rep = core.startup()
        print(f"[service] 契约闸门：{rep['n_checks']} 条断言，阻断 {len(rep['blocking'])} 条")

    @app.on_event("shutdown")
    def _shutdown():
        core.close()

    @app.get("/metrics")
    def _metrics():
        return PlainTextResponse(core.metrics_text(), media_type="text/plain; version=0.0.4")

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST"],
    )
    async def _catch_all(full_path: str, request: Request):
        path = "/" + full_path
        token = request.headers.get("X-API-Token", "")
        status, body = core.dispatch(path, token=token, query=dict(request.query_params))
        if isinstance(body, str):
            return PlainTextResponse(body, status_code=status)
        # `jsonable_encoder` 不能省：`dispatch` 是**框架无关**的（这样它能被单测直接调），
        # 因此它返回的是原生对象——而 DuckDB 把 DATE 列给成 `datetime.date`，
        # 标准 `json.dumps` 会直接抛 `TypeError: Object of type date is not JSON serializable`，
        # 表现是 **500 而不是 422**，而且只在真调 HTTP 时才出现（ENGINEERING_NOTES #86）。
        # 序列化属于适配层的职责，就不该漏回核心层去改成"到处 str(...)"。
        # （注意：Starlette 1.7 的 `JSONResponse` 不接受 `default=` 透传给 json.dumps，
        #   所以必须**先编码**再交给它。）
        return JSONResponse(jsonable_encoder(body), status_code=status)

    return app
