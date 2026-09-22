"""指标字典：口径写进版本控制，数值可被 CI 校验。

## 为什么不是「把数字写进 README」

README 里的数字会腐烂（本项目已经烂过五次，见 DEV_PLAN 基线数字那段）。
指标字典把**口径本身**（SQL）放进版本控制，`verify` 用它重算并与基线比对——
口径变了 SQL 就变，数字变了重算就变，两类漂移都能被抓住。

## 每个指标必须回答三件事

- `definition`：业务定义（给人读）
- `denominator`：**分母是什么**（口径最容易腐烂的地方，必须显式写）
- `sql`：可执行的口径（给机器跑）

少任何一条，这个指标都不算定义完成。

## 求值约定

口径 SQL 返回四列 `dataset, dim, value, denominator`；
只返回两列时按 `(value, denominator)` 解析，dataset 记 `__global__`（兼容老口径）。
一个指标**可以产出多行**（每个数据集、每个算子一行）——
「一个指标只有一个数」是多数口径错误的根源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_METRICS_PATH = _ROOT / "configs" / "metrics.yaml"
BASELINE_PATH = _ROOT / "configs" / "metrics_baseline.json"
GLOBAL_DATASET = "__global__"


@dataclass
class MetricSpec:
    name: str
    definition: str
    denominator: str
    sql: str
    unit: str = "ratio"
    owner: str = "data-quality"
    baseline: float | None = None
    tolerance: float = 1e-9

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MetricSpec":
        missing = [k for k in ("name", "definition", "denominator", "sql") if not d.get(k)]
        if missing:
            raise ValueError(f"指标定义缺少必填字段 {missing}：{d.get('name', '?')}")
        return cls(
            name=str(d["name"]),
            definition=str(d["definition"]),
            denominator=str(d["denominator"]),
            sql=str(d["sql"]),
            unit=str(d.get("unit", "ratio")),
            owner=str(d.get("owner", "data-quality")),
            baseline=None if d.get("baseline") is None else float(d["baseline"]),
            tolerance=float(d.get("tolerance", 1e-9)),
        )


@dataclass
class MetricResult:
    name: str
    value: float | None
    denominator: float | None
    baseline: float | None
    delta: float | None
    ok: bool | None  # None = 无基线可比
    dataset: str = GLOBAL_DATASET
    dim: str = ""
    error: str = ""

    @property
    def key(self) -> str:
        """基线键：name|dataset|dim——同名指标在不同数据集上基线不同，不能互相覆盖。"""
        return "|".join([self.name, self.dataset, self.dim])

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dataset": self.dataset,
            "dim": self.dim,
            "value": self.value,
            "denominator": self.denominator,
            "baseline": self.baseline,
            "delta": self.delta,
            "ok": self.ok,
            "error": self.error,
        }


def load_metrics(path: str | Path = DEFAULT_METRICS_PATH) -> list[MetricSpec]:
    if yaml is None:
        raise RuntimeError("需要 pyyaml：pip install pyyaml")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [MetricSpec.from_dict(m) for m in data.get("metrics", [])]


def evaluate(
    con, spec: MetricSpec, baselines: dict[str, float] | None = None
) -> list[MetricResult]:
    """执行一条口径，返回逐行结果并与基线比对。"""
    try:
        cur = con.execute(spec.sql)
        cols = [str(d[0]).lower() for d in cur.description]
        rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 - 口径跑挂要变成结果而不是崩溃
        return [
            MetricResult(
                spec.name, None, None, spec.baseline, None, None, error=f"{type(e).__name__}: {e}"
            )
        ]
    if not rows:
        return [
            MetricResult(spec.name, None, None, spec.baseline, None, None, error="口径返回空行")
        ]
    if "value" not in cols:
        return [
            MetricResult(
                spec.name,
                None,
                None,
                spec.baseline,
                None,
                None,
                error=f"口径缺少 value 列（实际返回 {cols}）",
            )
        ]

    i_val = cols.index("value")
    i_den = cols.index("denominator") if "denominator" in cols else -1
    i_ds = cols.index("dataset") if "dataset" in cols else -1
    i_dim = cols.index("dim") if "dim" in cols else -1

    out: list[MetricResult] = []
    for row in rows:
        ds = GLOBAL_DATASET if i_ds < 0 else str(row[i_ds] or GLOBAL_DATASET)
        dim = "" if i_dim < 0 else str(row[i_dim] or "")
        value = None if row[i_val] is None else float(row[i_val])
        denom = None if (i_den < 0 or row[i_den] is None) else float(row[i_den])
        key = "|".join([spec.name, ds, dim])
        bl = spec.baseline if baselines is None else baselines.get(key, spec.baseline)
        if bl is None or value is None:
            out.append(MetricResult(spec.name, value, denom, bl, None, None, ds, dim))
        else:
            delta = value - bl
            out.append(
                MetricResult(
                    spec.name, value, denom, bl, delta, abs(delta) <= spec.tolerance, ds, dim
                )
            )
    return out


@dataclass
class VerifyReport:
    results: list[MetricResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """门禁是否成立。

        ⚠️ **口径跑挂（error）必须算失败**，不能只算「没可比基线」。
        早先的实现只判 `ok is not False`，而跑挂的行 `ok=None` →
        在空仓库上 7 条口径有 5 条根本没跑成，`ok` 却是 **True**，
        `--verify` 返回 0 —— 一个**什么都没验的成功**。
        这和 `format --check` 挂掉却让 CI 显示绿是同一类失效（笔记 #80）：
        **没跑的检查项必须显式失败，而不是沉默地通过。**
        """
        if self.errors:
            return False
        return all(r.ok is not False for r in self.results)

    @property
    def failures(self) -> list[MetricResult]:
        return [r for r in self.results if r.ok is False]

    @property
    def errors(self) -> list[MetricResult]:
        return [r for r in self.results if r.error]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "n_values": len(self.results),
            "n_failed": len(self.failures),
            "n_error": len(self.errors),
            "results": [r.to_dict() for r in self.results],
        }


def verify(
    con, specs: list[MetricSpec] | None = None, baselines: dict[str, float] | None = None
) -> VerifyReport:
    """跑全部口径并与基线比对。漂移或口径跑挂都体现在 ok 上。"""
    rep = VerifyReport()
    for s in specs if specs is not None else load_metrics():
        rep.results.extend(evaluate(con, s, baselines))
    return rep


# ---------------------------------------------------------------------------
# 基线冻结：基线单独存 JSON，**不回写 metrics.yaml**
# ——yaml.dump 会丢掉口径注释，而注释正是那个文件的价值所在。
# ---------------------------------------------------------------------------


def load_baselines(path: str | Path = BASELINE_PATH) -> dict[str, float]:
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: float(v) for k, v in (data.get("baselines") or {}).items()}


def freeze(
    con, specs: list[MetricSpec] | None = None, path: str | Path = BASELINE_PATH
) -> dict[str, Any]:
    """把当前口径的实际值冻成基线（首次建立或口径变更后重建）。"""
    specs = specs if specs is not None else load_metrics()
    out: dict[str, float] = {}
    for s in specs:
        for r in evaluate(con, s):
            if r.value is not None:
                out[r.key] = r.value
    payload = {
        "note": "由 scripts/mmc.py metrics --freeze 生成；口径变更需重冻",
        "n_baselines": len(out),
        "baselines": out,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
