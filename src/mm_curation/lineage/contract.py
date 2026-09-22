"""数据契约（Data Contract）：数据集对外承诺的 schema 与断言，可进 CI 阻断。

## 契约 vs 断言工具

Great Expectations / Soda 也是「写规则 + 跑校验」，差别在两点：

1. **契约带版本与 owner**，破坏性变更要改 `version` ——
   规则散落在 notebook 里时，「谁改了什么」无从追溯。
2. **契约能进 CI 阻断**（`severity: error` 的条目失败即 exit 1），
   不是「跑一下看看」——这正是本项目已有的数据 CI 纪律在 schema 层的延伸。

## 字段规则如何落到 SQL

字段规则不是靠 Python 遍历数据（那样大数据集下会全表拉取），
而是**编译成 SQL 让数据库算**：

| 规则 | 编译结果 |
|---|---|
| `required` | `COUNT(*) FILTER (WHERE col IS NULL OR col = '')` |
| `unique` | `COUNT(*) - COUNT(DISTINCT col)` |
| `allowed` | `COUNT(*) FILTER (WHERE col NOT IN (...))` |
| `min_len` / `max_len` | `LENGTH(CAST(col AS VARCHAR)) < n` —— **只对文本列有意义** |
| `min_value` / `max_value` | `col < n` —— 数值列用这个 |

（上表省略了统一的 `SELECT COUNT(*) FROM t WHERE dataset = ... AND <条件>` 外壳，
 每条规则都是「期望为 0 的计数」——能报出坏了多少条，而不是只说 true/false。）

## ⚠️ min_len 不能用在数值列上（笔记 #79）

`min_len` 的实现是 `LENGTH(CAST(col AS VARCHAR))`，对数值列比较的是**数字的位数**
而不是数值大小。在 `text_len` 上写 `min_len: 10` 会得到 214/214 全挂——
因为 214 篇文章的字符数都是 3~5 位数，**没有一篇的"位数"达到 10**。
这个错误不会报错、只会安静地判 FAIL，是最难发现的那一类。
对策：数值列一律用 `min_value` / `max_value`，两者语义不重叠、不互相兜底。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

DEFAULT_CONTRACT_DIR = "configs/contracts"
CONTRACT_SCHEMA_VERSION = 1

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_ERROR = "ERROR"  # 规则本身跑不起来（SQL 错 / 表不存在）


@dataclass
class CheckResult:
    name: str
    status: str
    actual: Any
    expect: Any
    severity: str
    sql: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "actual": self.actual,
            "expect": self.expect,
            "severity": self.severity,
        }


@dataclass
class Contract:
    dataset: str
    version: int
    owner: str
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    table: str = "stg_samples"

    @property
    def key(self) -> str:
        return f"{self.dataset}@v{self.version}"


def load_contract(path: str | Path) -> Contract:
    if yaml is None:  # pragma: no cover
        raise RuntimeError("需要 pyyaml：`pip install pyyaml`")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not data.get("dataset"):
        raise ValueError(f"契约缺少 dataset 字段：{path}")
    return Contract(
        dataset=str(data["dataset"]),
        version=int(data.get("version", 0)),
        owner=str(data.get("owner") or "unassigned"),
        fields=dict(data.get("fields") or {}),
        checks=list(data.get("checks") or []),
        table=str(data.get("table") or "stg_samples"),
    )


def load_contracts(dirpath: str | Path = DEFAULT_CONTRACT_DIR) -> list[Contract]:
    p = Path(dirpath)
    if not p.exists():
        return []
    return [load_contract(f) for f in sorted(p.glob("*.yaml"))]


def _q(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def compile_field_checks(c: Contract) -> list[tuple[str, str, Any, str]]:
    """把字段规则编译成 (name, sql, expect, severity)。

    返回的是「期望为 0 的计数型断言」——计数型比布尔型好在能报出**坏了多少条**，
    便于判断是全面崩坏还是个别脏数据。
    """
    out: list[tuple[str, str, Any, str]] = []
    t = c.table
    where = f"WHERE dataset = {_q(c.dataset)}"
    for col, rule in (c.fields or {}).items():
        rule = rule or {}
        if rule.get("required"):
            out.append(
                (
                    f"{col}.required",
                    f"SELECT COUNT(*) FROM {t} {where}"
                    f" AND ({col} IS NULL OR CAST({col} AS VARCHAR) = '')",
                    0,
                    "error",
                )
            )
        if rule.get("unique"):
            out.append(
                (
                    f"{col}.unique",
                    f"SELECT COUNT(*) - COUNT(DISTINCT {col}) FROM {t} {where}",
                    0,
                    "error",
                )
            )
        if rule.get("allowed"):
            vals = ", ".join(_q(v) for v in rule["allowed"])
            out.append(
                (
                    f"{col}.allowed",
                    f"SELECT COUNT(*) FROM {t} {where} AND CAST({col} AS VARCHAR) NOT IN ({vals})",
                    0,
                    "error",
                )
            )
        if rule.get("min_len") is not None:
            n = int(rule["min_len"])
            out.append(
                (
                    f"{col}.min_len",
                    f"SELECT COUNT(*) FROM {t} {where} AND LENGTH(CAST({col} AS VARCHAR)) < {n}",
                    0,
                    "error",
                )
            )
        if rule.get("max_len") is not None:
            n = int(rule["max_len"])
            out.append(
                (
                    f"{col}.max_len",
                    f"SELECT COUNT(*) FROM {t} {where} AND LENGTH(CAST({col} AS VARCHAR)) > {n}",
                    0,
                    "warn",
                )
            )
        # 数值区间：不 CAST、不 LENGTH——数值列用 min_len 比的是位数不是大小（笔记 #79）
        if rule.get("min_value") is not None:
            out.append(
                (
                    f"{col}.min_value",
                    f"SELECT COUNT(*) FROM {t} {where} AND {col} < {float(rule['min_value'])}",
                    0,
                    "error",
                )
            )
        if rule.get("max_value") is not None:
            out.append(
                (
                    f"{col}.max_value",
                    f"SELECT COUNT(*) FROM {t} {where} AND {col} > {float(rule['max_value'])}",
                    0,
                    "warn",
                )
            )
    return out


def check_contract(wh, c: Contract) -> dict[str, Any]:
    """跑一个契约的全部断言。"""
    items: list[CheckResult] = []
    for name, sql, expect, sev in compile_field_checks(c):
        items.append(_run(wh, name, sql, expect, sev))
    for chk in c.checks:
        items.append(
            _run(
                wh,
                str(chk.get("name") or "unnamed"),
                str(chk.get("sql") or ""),
                chk.get("expect", 0),
                str(chk.get("severity") or "error"),
            )
        )
    n_fail = sum(1 for i in items if i.status == STATUS_FAIL)
    n_err = sum(1 for i in items if i.status == STATUS_ERROR)
    blocking = [
        i.name for i in items if i.status in (STATUS_FAIL, STATUS_ERROR) and i.severity == "error"
    ]
    return {
        "dataset": c.dataset,
        "version": c.version,
        "owner": c.owner,
        "ok": not blocking,
        "n_checks": len(items),
        "n_fail": n_fail,
        "n_error": n_err,
        "blocking": blocking,
        "checks": [i.to_dict() for i in items],
    }


def _run(wh, name: str, sql: str, expect: Any, severity: str) -> CheckResult:
    if not sql.strip():
        return CheckResult(name, STATUS_ERROR, None, expect, severity, sql)
    try:
        _, rows = wh.query(sql)
        actual = rows[0][0] if rows and rows[0] else None
    except Exception as e:  # noqa: BLE001 - 规则跑挂必须变成 ERROR 而不是崩溃
        return CheckResult(name, STATUS_ERROR, f"{type(e).__name__}: {e}", expect, severity, sql)
    if actual is None:
        status = STATUS_ERROR
    else:
        try:
            status = STATUS_PASS if float(actual) == float(expect) else STATUS_FAIL
        except (TypeError, ValueError):
            status = STATUS_PASS if actual == expect else STATUS_FAIL
    return CheckResult(name, status, actual, expect, severity, sql)
