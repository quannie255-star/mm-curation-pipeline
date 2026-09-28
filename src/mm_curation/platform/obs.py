"""可观测性与告警收敛（S5）：让"数据系统坏没坏"变成一个能被告出来的数。

## 四类信号

| 信号 | 判据 | 数据来源 |
|---|---|---|
| 数据新鲜度 | `today - max(event_date) > slo_days` | `ads_dataset_health` |
| 行数异常（涨/跌） | 日行数超出**稳健限**（中位数 ± margin × 1.4826×MAD） | `dws_dataset_day` |
| 管道失败 | 最近一次**已终态**运行的终态不是 SUCCESS（在途的 RUNNING 不算） | `job_runs` |
| 维表未匹配 | 传感器行有通道却没关联上维版本（**真问题**） | `dwd_window` |

## 三个刻意的口径选择

1. **用中位数 + MAD，不用均值 + 3σ**。日窗数本身右偏（metropt3 实测 7~239），
   均值与标准差都会被极端日拉走 → 阈值最后会宽到什么都抓不到。
   口径直接复用 `operators/robust`（唯一真相源，笔记 #75 的教训）。
2. **告警必须收敛后再报**。原始信号按天产生，metropt3 有几十个异常日——
   把几十条丢给人看，接受者的实际反应是"关掉通知"，那等于没有告警。
   所以按 `(类型, 数据集)` 指纹收敛成一条，带 `n_signals` / `first` / `last`。
   「减少告警噪音」不是优化项，是告警能不能活下来的前提。
3. **"在途"不是一个结论**。`RUNNING` 既不是成功也不是失败，它是"还不知道"。
   而 `obs` 这个阶段**必然**在一次运行进行中执行——把它算进成功率或报成失败，
   等于让这条指标/告警每次运行都把自己判一遍罪，结果是"永远为红"，然后被静音。
   所以两处都按同一集合（SUCCESS/FAILED/TIMEOUT/SKIPPED）取终态，`RUNNING` 只在
   `n_running` 里如实计数。**分母为 0 时返回 `None` 而不是 `0`**：`0.0` 是"全都失败了"，
   `None` 是"还没有结论"，这两件事不能共用一个值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..operators.robust import MAD_TO_SIGMA, median
from .runs import FAILED, RUNNING, SKIPPED, SUCCESS, TIMEOUT

SEV_INFO = "info"
SEV_WARN = "warn"
SEV_CRIT = "crit"
_SEV_ORDER = {SEV_INFO: 0, SEV_WARN: 1, SEV_CRIT: 2}

DEFAULT_SLO = {"freshness_days": 7.0, "volume_margin": 3.0, "min_days_for_volume": 8}


@dataclass
class Signal:
    kind: str
    severity: str
    message: str
    dataset: str = ""
    at: str = ""
    value: Any = None
    limit: Any = None

    @property
    def fingerprint(self) -> str:
        """收敛指纹：**只按 (类型, 数据集)**，不按具体日期。

        按日期收敛等于没收敛（一天一条）；按类型 + 数据集收敛，
        才能把"这个数据集的这类问题"合成一件事。
        """
        return f"{self.kind}:{self.dataset or '*'}"

    def to_dict(self) -> dict[str, Any]:
        """⚠️ 产物**不能**直接 `Signal(**d)` 还原：里面含 `fingerprint`，
        而它是计算属性、不是构造参数。要还原请逐字段取（见 `collect_signals`）。"""
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "dataset": self.dataset,
            "at": self.at,
            "value": self.value,
            "limit": self.limit,
            "fingerprint": self.fingerprint,
        }


@dataclass
class Alert:
    fingerprint: str
    kind: str
    severity: str
    dataset: str
    n_signals: int
    first: str
    last: str
    sample: str
    values: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "kind": self.kind,
            "severity": self.severity,
            "dataset": self.dataset,
            "n_signals": self.n_signals,
            "first": self.first,
            "last": self.last,
            "sample": self.sample,
            "values": self.values[:20],
        }


def load_platform_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"slo": dict(DEFAULT_SLO), "datasets": {}}
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return {"slo": dict(DEFAULT_SLO), "datasets": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    slo = {**DEFAULT_SLO, **(data.get("slo") or {})}
    return {"slo": slo, "datasets": dict(data.get("datasets") or {})}


def freshness(
    con, *, slo_days: float = 7.0, today: str = "", per_dataset: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """每个数据集的**数据新鲜度**（事件时间滞后天数）。

    这是本层最有信息量的一个指标：它回答的是"**数据源还活着吗**"，
    而不是"我的任务跑成功了吗"。任务天天成功而数据停在两年前，
    是真实数据平台上最常见的静默故障。
    """
    today = today or date.today().isoformat()
    per_dataset = per_dataset or {}
    rows = con.execute(
        "SELECT dataset, last_event_date, freshness_days, n_total, n_undated_partitions "
        "FROM ads_dataset_health ORDER BY dataset"
    ).fetchall()
    out = []
    for ds, last, fresh, n_total, n_undated in rows:
        limit = float(per_dataset.get(ds, {}).get("freshness_days", slo_days))
        ok = None if fresh is None else (float(fresh) <= limit)
        out.append(
            {
                "dataset": ds,
                "last_event_date": str(last) if last else None,
                "freshness_days": None if fresh is None else float(fresh),
                "slo_days": limit,
                "ok": ok,
                "n_total": int(n_total or 0),
                "n_undated_partitions": int(n_undated or 0),
                "evaluated": fresh is not None,
            }
        )
    _ = today
    return out


def volume_anomalies(con, *, margin: float = 3.0, min_days: int = 8) -> list[dict[str, Any]]:
    """按事件日的行数异常（双侧稳健限）。

    返回的每一条都带 `limit_hi` / `limit_lo` / 稳健中心与尺度，
    这样看到的人能自己判断阈值合不合理——只给一个"异常"标签是没法复核的。
    """
    rows = con.execute(
        "SELECT dataset, event_date, n_total FROM dws_dataset_day "
        "WHERE event_date IS NOT NULL ORDER BY dataset, event_date"
    ).fetchall()
    by_ds: dict[str, list[tuple[str, float]]] = {}
    for ds, ed, n in rows:
        by_ds.setdefault(ds, []).append((str(ed), float(n)))

    out: list[dict[str, Any]] = []
    for ds, series in sorted(by_ds.items()):
        if len(series) < min_days:
            continue
        vals = [v for _, v in series]
        center = median(vals)
        scale = MAD_TO_SIGMA * median([abs(v - center) for v in vals])
        if scale <= 0:
            continue  # 零离散度 → 定不出限，记未评而不是猜（同 robust_limit 的口径）
        hi = center + margin * scale
        lo = max(0.0, center - margin * scale)
        for ed, v in series:
            if v > hi or v < lo:
                out.append(
                    {
                        "dataset": ds,
                        "event_date": ed,
                        "n_total": v,
                        "center": round(center, 3),
                        "scale": round(scale, 3),
                        "limit_hi": round(hi, 3),
                        "limit_lo": round(lo, 3),
                        "direction": "spike" if v > hi else "drop",
                    }
                )
    return out


def pipeline_health(ledger) -> dict[str, Any]:
    """运行台账汇总：成功率、耗时、失败阶段 Top N。

    ⚠️ **成功率只对"已终态"的运行计算**（SUCCESS/FAILED/TIMEOUT/SKIPPED 为分母，
    在途的 RUNNING 不算）。原因很具体：`obs` 阶段本身就在一次运行**进行中**执行，
    如果分母含 RUNNING，那这条指标每次都会把**自己**算成失败——
    实测就是这个数：首次跑通全链路时 `success_rate` 报 `0.0`，
    9 个阶段全 SUCCESS。一个"永远是 0"的健康指标比没有更坏：它会让人关掉告警。
    在途数量单独用 `n_running` 上报，不混进成功率。
    """
    con = ledger.connect()
    try:
        rows = con.execute(
            "SELECT status, count(*) FROM job_runs GROUP BY status ORDER BY status"
        ).fetchall()
        by_status = {r[0]: int(r[1]) for r in rows}
        n_total = sum(by_status.values())
        # 终态 = 已落地的结局；RUNNING 不算（见 docstring）
        terminal = (SUCCESS, FAILED, TIMEOUT, SKIPPED)
        n_terminal = sum(by_status.get(s, 0) for s in terminal)
        n_ok = by_status.get(SUCCESS, 0)
        recent = con.execute(
            "SELECT run_id, status, batch_date, duration_s, started_at "
            "FROM job_runs ORDER BY started_at DESC LIMIT 10"
        ).fetchall()
        fail = con.execute(
            "SELECT task_name, count(*) AS n FROM task_runs WHERE status='FAILED' "
            "GROUP BY task_name ORDER BY n DESC LIMIT 5"
        ).fetchall()
        dur = con.execute(
            "SELECT task_name, avg(duration_s), max(duration_s) FROM task_runs "
            "WHERE status='SUCCESS' GROUP BY task_name ORDER BY avg(duration_s) DESC"
        ).fetchall()
        return {
            "n_runs": n_total,
            "n_terminal": n_terminal,
            "n_running": by_status.get(RUNNING, 0),
            "by_status": by_status,
            "success_rate": round(n_ok / n_terminal, 6) if n_terminal else None,
            "recent": [
                {"run_id": r[0], "status": r[1], "batch_date": str(r[2]), "duration_s": r[3]}
                for r in recent
            ],
            "top_failures": [{"task": r[0], "n": int(r[1])} for r in fail],
            "task_durations": [
                {
                    "task": r[0],
                    "avg_s": round(float(r[1] or 0), 3),
                    "max_s": round(float(r[2] or 0), 3),
                }
                for r in dur
            ],
        }
    finally:
        con.close()


def dim_signals(con) -> list[Signal]:
    """维表未匹配（真问题）与 SCD-2 变更数。"""
    out: list[Signal] = []
    rows = con.execute(
        "SELECT dataset, count(*) FROM dwd_window "
        "WHERE channel <> '' AND device_sk IS NULL GROUP BY dataset ORDER BY dataset"
    ).fetchall()
    for ds, n in rows:
        if n:
            out.append(
                Signal(
                    "dim_unmatched",
                    SEV_CRIT,
                    f"{n} 条有通道的传感器行未关联到维版本（维表漏建或事件日落在版本区间外）",
                    dataset=ds,
                    value=int(n),
                    limit=0,
                )
            )
    return out


# ---------------------------------------------------------------------------
# 收敛
# ---------------------------------------------------------------------------


def converge(signals: Iterable[Signal]) -> list[Alert]:
    """按指纹把原始信号收敛成告警（**这是"告警能不能活下来"的关键一步**）。"""
    agg: dict[str, Alert] = {}
    for s in signals:
        fp = s.fingerprint
        a = agg.get(fp)
        if a is None:
            agg[fp] = Alert(
                fp, s.kind, s.severity, s.dataset or "*", 1, s.at, s.at, s.message, [s.value]
            )
            continue
        a.n_signals += 1
        if _SEV_ORDER[s.severity] > _SEV_ORDER[a.severity]:
            a.severity = s.severity
            a.sample = s.message
        if s.at and (not a.first or s.at < a.first):
            a.first = s.at
        if s.at and (not a.last or s.at > a.last):
            a.last = s.at
        a.values.append(s.value)

    def _key(a: Alert) -> tuple[int, int, str]:
        return (-_SEV_ORDER[a.severity], -a.n_signals, a.fingerprint)

    return sorted(agg.values(), key=_key)


def collect_signals(
    obs: dict[str, Any], *, slo_days: float = 7.0, volume_margin: float = 3.0
) -> list[Signal]:
    """把观测快照翻译成原始信号（未收敛）。"""
    sig: list[Signal] = []
    for fr in obs.get("freshness", []):
        if fr["evaluated"] and not fr["ok"] and fr["freshness_days"] is not None:
            sig.append(
                Signal(
                    "freshness_breach",
                    SEV_WARN,
                    f"数据停在 {fr['last_event_date']}，滞后 {fr['freshness_days']:.0f} 天"
                    f"（SLO {fr['slo_days']:.0f} 天）——源侧可能已停止供数",
                    dataset=fr["dataset"],
                    at=fr["last_event_date"] or "",
                    value=fr["freshness_days"],
                    limit=fr["slo_days"],
                )
            )
        if fr.get("n_undated_partitions"):
            sig.append(
                Signal(
                    "undated_partition",
                    SEV_INFO,
                    f"{fr['n_undated_partitions']} 个分区没有事件时间（事件时间未知 ≠ 今天）",
                    dataset=fr["dataset"],
                    value=fr["n_undated_partitions"],
                )
            )
    for va in obs.get("volume_anomalies", []):
        sig.append(
            Signal(
                f"volume_{va['direction']}",
                SEV_WARN,
                f"{va['event_date']} 行数 {va['n_total']:.0f} 超出稳健限 "
                f"[{va['limit_lo']:.0f}, {va['limit_hi']:.0f}]",
                dataset=va["dataset"],
                at=va["event_date"],
                value=va["n_total"],
                limit=va["limit_hi"] if va["direction"] == "spike" else va["limit_lo"],
            )
        )
    for d in obs.get("dim_unmatched", []):
        # ⚠️ 这里**不能**写 `Signal(**d)`：`d` 是 `Signal.to_dict()` 的产物，
        # 里面带 `fingerprint`，而它是 Signal 上的**计算属性**，
        # 不能当构造参数 → `TypeError: unexpected keyword argument 'fingerprint'`。
        #
        # 这个缺陷的真实后果值得记下来：它只在 `dim_unmatched` 非空时发作，
        # 也就是**只在"真出了问题"的那一次才崩**。真实数据跑通时
        # `n_unmatched_sensor == 0`，这条路径从未被执行过 ——
        # "告警链路恰好在唯一需要报警的时候挂掉"是最坏的一种 bug 形态。
        # 逐字段显式构造既修掉它，也不怕 `to_dict` 以后再加字段。
        sig.append(
            Signal(
                str(d.get("kind") or "dim_unmatched"),
                str(d.get("severity") or SEV_CRIT),
                str(d.get("message") or ""),
                dataset=str(d.get("dataset") or ""),
                at=str(d.get("at") or ""),
                value=d.get("value"),
                limit=d.get("limit"),
            )
        )
    # 管道失败信号：**只看已终态的运行，且只看最近那一次**。
    #
    # ⚠️ 这里曾同时踩两个坑——每一个都足以让这条 crit 告警沦为一响就没人看：
    # ① `recent` 里含**在途的 RUNNING**，而 `obs` 阶段本身就在一次运行**进行中**执行，
    #    于是"最近一次不是 SUCCESS"必然成立 → **每次跑批都报一条 crit 假告警**。
    #    这与 `pipeline_health` 的 success_rate 是**同一个** bug（把 RUNNING 算作失败），
    #    上次只修了成功率那一半，信号这一半留了下来——同一个根因换了个出口继续响。
    # ② 原实现遍历 `recent`（最近 10 条），把历史上每一次失败都各补一条新信号，
    #    消息里却写着"最近一次运行"——名实不符，且失败越多报得越多。
    #
    # 改法：登记顺序是 `started_at DESC`，取**最近一条终态**运行，只在它确实不是
    # SUCCESS 时报。终态集合与 `pipeline_health` 的分母**故意保持一致**——
    # 指标与告警对同一件事必须用同一个口径，否则两者会各自给出结论、互相打架。
    # 分工也因此清晰：**指标管历史（成功率把历史失败都算进去），告警管当下（只报最近一次终态）**。
    terminal = (SUCCESS, FAILED, TIMEOUT, SKIPPED)
    recent = obs.get("pipeline", {}).get("recent", [])
    latest_terminal = next((r for r in recent if r.get("status") in terminal), None)
    if latest_terminal is not None and latest_terminal.get("status") != SUCCESS:
        sig.append(
            Signal(
                "pipeline_failure",
                SEV_CRIT,
                f"最近一次已结束的运行 {latest_terminal['run_id']} "
                f"终态 {latest_terminal['status']}",
                at=str(latest_terminal.get("batch_date") or ""),
                value=latest_terminal["status"],
            )
        )
    return sig


# ---------------------------------------------------------------------------
# Prometheus 文本暴露
# ---------------------------------------------------------------------------


def _lbl(**kv: Any) -> str:
    if not kv:
        return ""
    inner = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in kv.items() if v != "")
    return "{" + inner + "}"


def prometheus_text(obs: dict[str, Any]) -> str:
    """标准 Prometheus 文本暴露格式（不引 prometheus_client：只有一个出口时，
    几十行标准格式比一个新依赖划算——与 `serving/metrics.py` 同一决策）。"""
    lines: list[str] = []

    def line(name: str, help_: str, type_: str) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {type_}")

    line("mm_dataset_freshness_days", "数据集事件时间滞后天数", "gauge")
    for fr in obs.get("freshness", []):
        if fr["freshness_days"] is not None:
            lbl = _lbl(dataset=fr["dataset"])
            lines.append(f"mm_dataset_freshness_days{lbl} {fr['freshness_days']}")

    line("mm_dataset_rows", "数据集总行数", "gauge")
    for fr in obs.get("freshness", []):
        lines.append(f"mm_dataset_rows{_lbl(dataset=fr['dataset'])} {fr['n_total']}")

    line("mm_dataset_freshness_slo_ok", "新鲜度是否满足 SLO（1/0，未评不输出）", "gauge")
    for fr in obs.get("freshness", []):
        if fr["ok"] is not None:
            lines.append(
                f"mm_dataset_freshness_slo_ok{_lbl(dataset=fr['dataset'])} {int(bool(fr['ok']))}"
            )

    ph = obs.get("pipeline", {})
    line("mm_pipeline_runs_total", "作业运行次数", "counter")
    for st, n in sorted((ph.get("by_status") or {}).items()):
        lines.append(f'mm_pipeline_runs_total{{status="{st}"}} {n}')
    line("mm_pipeline_success_rate", "作业成功率（无运行时不输出）", "gauge")
    if ph.get("success_rate") is not None:
        lines.append(f"mm_pipeline_success_rate {ph['success_rate']}")

    line("mm_task_duration_seconds", "阶段平均耗时", "gauge")
    for t in ph.get("task_durations", []):
        lines.append(f"mm_task_duration_seconds{_lbl(task=t['task'])} {t['avg_s']}")

    line("mm_alerts_active", "收敛后的活跃告警数（按类型）", "gauge")
    kinds: dict[str, int] = {}
    for a in obs.get("alerts", []):
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    for k, n in sorted(kinds.items()):
        lines.append(f'mm_alerts_active{{kind="{k}"}} {n}')
    line("mm_alerts_signals_raw", "收敛前的原始信号数", "gauge")
    lines.append(f"mm_alerts_signals_raw {obs.get('n_signals', 0)}")
    return "\n".join(lines) + "\n"


def snapshot(
    con,
    ledger,
    *,
    slo_days: float = 7.0,
    per_dataset: dict[str, Any] | None = None,
    volume_margin: float = 3.0,
    min_days_for_volume: int = 8,
) -> dict[str, Any]:
    """生成一份完整观测快照（给 CLI / 服务 / Prometheus 共用）。"""
    fresh = freshness(con, slo_days=slo_days, per_dataset=per_dataset)
    vol = volume_anomalies(con, margin=volume_margin, min_days=min_days_for_volume)
    pipe = pipeline_health(ledger)
    dim_unk = dim_signals(con)
    obs: dict[str, Any] = {
        "freshness": fresh,
        "volume_anomalies": vol,
        "pipeline": pipe,
        "dim_unmatched": [s.to_dict() for s in dim_unk],
    }
    signals = collect_signals(obs, slo_days=slo_days, volume_margin=volume_margin)
    alerts = converge(signals)
    obs["n_signals"] = len(signals)
    obs["n_alerts"] = len(alerts)
    obs["alerts"] = [a.to_dict() for a in alerts]
    obs["convergence"] = {
        "raw_signals": len(signals),
        "converged_alerts": len(alerts),
        "ratio": round(1 - len(alerts) / len(signals), 4) if signals else None,
        "rule": "按 (类型, 数据集) 指纹收敛；同因合并，报一条带 n_signals 的告警",
    }
    return obs


def write_snapshot(obs: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obs, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def summarize(obs: dict[str, Any]) -> str:
    """一行摘要（给 CLI 收尾打印）。"""
    conv = obs.get("convergence") or {}
    n_breach = sum(1 for f in obs.get("freshness", []) if f["ok"] is False)
    return (
        f"新鲜度破线 {n_breach}/{len(obs.get('freshness', []))}  "
        f"行数异常分区 {len(obs.get('volume_anomalies', []))}  "
        f"管道成功率 {obs.get('pipeline', {}).get('success_rate')}  "
        f"原始信号 {conv.get('raw_signals')} → 收敛告警 {conv.get('converged_alerts')}"
    )


def _unused(_: Sequence[Any]) -> None:  # pragma: no cover
    pass
