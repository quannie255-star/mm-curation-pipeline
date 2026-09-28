"""分层物化与维度建模（S2 的模型面）。

## 分层落成「物理分层」

| 层 | 产物 | 更新节奏 | 保留 |
|---|---|---|---|
| ODS | `data/lake/ods/dataset=<x>/event_date=<y>/*.parquet` | 按事件日分区幂等重写 | 全量历史 |
| DWD | `…/dwd/…` 明细 + 算子分 | 同上（只重算被触碰的分区） | 与 ODS 对齐 |
| DWS | `…/dws/…` 日粒度聚合 | 按分区重算（可随时重建） | 与 ODS 对齐 |
| ADS | `…/ads/…` 面向应用的健康宽表 | 全量重建（小） | 只留最近一版 |

**维度表例外**：`dim_*` 放在 `data/warehouse/platform.duckdb` 里做成**可 MERGE 的表**。
这不是偷懒——维表小、需要原地 upsert 与 SCD-2 版本管理；
事实表大、只追加、按分区替换。**「维表进库、事实进湖」是真实数仓的通行分工**，
硬把维表也做成 parquet 反而要自己实现 upsert。

## 维度建模（最小但真的）

- `dim_device`：自然键 `(dataset, device_id, channel)`，属性 `device_type/unit/sampling_hz/
  operating_mode`，**SCD-2**（`valid_from` / `valid_to` / `is_current` / `version`）。
  代理键 `device_sk` = md5(自然键 + version)——**代理键必须能随版本变化**，
  否则"历史行"与"当前行"会共用一个键，SCD-2 就白做了。
- `dim_device_changes`：SCD-2 的**变更审计日志**（哪一版在哪天因属性变化被封口）。
- `fact_*` 即 DWD 明细：`dwd_window`（窗级事实）+ `dwd_op_score`（窗 × 算子事实）。
- `dwd_window` 通过 **有效期区间 join** 关联到当时的维版本——这是 SCD-2 唯一真正的用处：
  回答"这条事实发生的时候，那个设备的属性是什么"。

## 主题域的诚实口径

不假装有 GMV / 留存。本项目的主题域是「**数据集 × 设备 × 通道 × 事件日**」，
因为这是这份数据里真实存在的分析维度。对外表述见 `docs/PLATFORM.md`。

## ⚠️ 一个真实的类型陷阱（实测踩到）

Hive 分区的列类型由 DuckDB **按目录值推断**。第一版给"取不到事件时间"的记录
写了 `event_date=unknown` 这个分区值——于是**整列被推断成 VARCHAR**，
`o.event_date >= d.valid_from`（DATE）直接抛
`Cannot compare values of type VARCHAR and type DATE`。

修法**不是**把 `unknown` 换成假日期（那会把"我不知道它是哪天的"伪装成你知道），
而是两条一起做：

1. 取不到就写 **NULL** → 目录名落成 Hive 约定的 `__HIVE_DEFAULT_PARTITION__`，
   列的推断类型回到 DATE；
2. 关联处显式 `try_cast(o.event_date AS DATE)`，解析不了的为 NULL →
   **自然退出** SCD-2 有效期关联，并被 `ads.n_undated_partitions` 如实计数。

一行代码，一个口径。`docs/ENGINEERING_NOTES.md` 有这条的完整记录。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator, Sequence

from .lake import Lake
from .registry import (
    LAKE_SOURCES,
    UNKNOWN_DATE,
    LakeSource,
    available,
    event_date_of,
    resolve,
)
from .runs import fingerprint_rows

_CJK_LO = 0x4E00
_CJK_HI = 0x9FFF

# 维表属性列（顺序即 SCD-2 的"属性元组"，任一列变化即视为一次版本变更）
DIM_ATTRS = ("device_type", "unit", "sampling_hz", "operating_mode")


def count_han(text: str) -> int:
    if not text:
        return 0
    return sum(1 for ch in text if _CJK_LO <= ord(ch) <= _CJK_HI)


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """逐行读 JSONL。按文件迭代（只切 `\\n`），**禁用 `splitlines()`**（笔记 #44）。"""
    p = Path(path)
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _scores(meta: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in meta.items():
        if k.startswith("score:") and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k[len("score:") :]] = float(v)
    return out


def _first_op(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v or None
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else None
    return str(v)


def ods_row(rec: dict[str, Any], src: LakeSource) -> dict[str, Any]:
    """源记录 → ODS 行（**不做任何清洗**，只做扁平化与指纹）。

    事件时间取不到时写 **NULL**（而不是字符串 `"unknown"`）。
    原因见模块头的类型陷阱：任何一个非日期字符串都会把整列推断成 VARCHAR。
    落盘后这个分区目录名是 Hive 约定的 `__HIVE_DEFAULT_PARTITION__`。
    """
    meta = rec.get("meta") or {}
    text = rec.get("text") or ""
    ed = event_date_of(rec)
    return {
        "dataset": src.dataset,
        "modality": rec.get("modality") or src.modality or "unknown",
        "sample_id": str(rec.get("id") or ""),
        "event_date": None if ed == UNKNOWN_DATE else ed,
        "device_id": str(meta.get("device_id") or ""),
        "device_type": str(meta.get("device_type") or ""),
        "channel": str(meta.get("channel") or ""),
        "unit": str(meta.get("unit") or ""),
        "sampling_hz": meta.get("sampling_hz"),
        "operating_mode": str(meta.get("operating_mode") or ""),
        "window_start": str(meta.get("window_start") or ""),
        "window_end": str(meta.get("window_end") or ""),
        "text_len": len(text),
        "chars_han": count_han(text),
        "has_image": 1 if rec.get("image_path") else 0,
        "text_md5": hashlib.md5(text.encode("utf-8")).hexdigest(),
        "is_kept": 0 if src.kind == "dropped" else 1,
        "dropped_by": None if src.kind != "dropped" else _first_op(rec.get("dropped_by")),
        "scores_json": json.dumps(_scores(meta), sort_keys=True) if _scores(meta) else None,
    }


# ---------------------------------------------------------------------------
# ODS
# ---------------------------------------------------------------------------


def build_ods(
    lake: Lake,
    root: str | Path,
    *,
    datasets: Sequence[str] = (),
    limit: int = 0,
    event_dates: Sequence[str] = (),
    skip_dates: Sequence[str] = (),
    keep_undated: bool = True,
) -> dict[str, Any]:
    """把源 jsonl 落成按事件日分区的 Parquet（ODS 层）。

    `skip_dates` = 湖上已有的分区 → 这批记录直接丢弃（**只重写新分区**）。

    ⚠️ **如实说明读取侧的边界**：jsonl 源是按行顺序的平面文件，
    没有分区目录可以下推，所以"跳过"只能发生在**解析之后**——
    读取侧的 I/O 收益为零，收益只在写入侧（不重写已有分区）。
    想拿到读取侧的收益，源侧本身就得是分区表（Parquet / Hive / Iceberg）。
    这是源格式决定的，不是实现偷懒。
    """
    root = Path(root)
    want_dates = set(event_dates)
    want_undated = UNKNOWN_DATE in want_dates
    skip = set(skip_dates)
    used: list[str] = []
    skipped: list[str] = []
    rows: list[dict[str, Any]] = []
    per_dataset: dict[str, int] = {}
    n_skipped_records = 0

    for src in LAKE_SOURCES:
        if datasets and src.dataset not in datasets:
            continue
        if not available(root, src):
            skipped.append(f"{src.name}（文件缺失或为空）")
            continue
        n0 = len(rows)
        for i, rec in enumerate(iter_jsonl(resolve(root, src))):
            if limit and i >= limit:
                break
            r = ods_row(rec, src)
            if want_dates:
                keep = r["event_date"] in want_dates or (r["event_date"] is None and want_undated)
                if not keep:
                    continue
            elif skip:
                ed = r["event_date"]
                if ed is not None and ed in skip:
                    n_skipped_records += 1
                    continue
                if ed is None and not keep_undated:
                    n_skipped_records += 1
                    continue
            rows.append(r)
        if len(rows) > n0:
            used.append(src.name)
            per_dataset[src.dataset] = per_dataset.get(src.dataset, 0) + (len(rows) - n0)

    rep = lake.write("ods", rows, table="ods_samples")
    rep.update(
        {
            "sources_used": used,
            "sources_skipped": skipped,
            "per_dataset": per_dataset,
            "n_skipped_records": n_skipped_records,
            "fingerprint": fingerprint_rows(rows),
        }
    )
    return rep


# ---------------------------------------------------------------------------
# 维度：SCD-2
# ---------------------------------------------------------------------------

_DIM_DDL = """
CREATE TABLE IF NOT EXISTS dim_device (
    device_sk     VARCHAR PRIMARY KEY,
    dataset       VARCHAR,
    device_id     VARCHAR,
    channel       VARCHAR,
    device_type   VARCHAR,
    unit          VARCHAR,
    sampling_hz   DOUBLE,
    operating_mode VARCHAR,
    valid_from    DATE,
    valid_to      DATE,
    is_current    BOOLEAN,
    version       INTEGER
);

CREATE TABLE IF NOT EXISTS dim_device_changes (
    device_sk   VARCHAR PRIMARY KEY,
    dataset     VARCHAR,
    device_id   VARCHAR,
    channel     VARCHAR,
    changed_at  DATE,
    version     INTEGER,
    old_attrs   VARCHAR,
    new_attrs   VARCHAR
);
"""


def _attr_expr(prefix: str = "") -> str:
    """属性元组的 SQL 表达式（`attr1|attr2|attr3|attr4`）。

    **必须按表别名限定列名**：`unit` / `sampling_hz` 在 `dim_device` 与观测集里
    同名，不限定就是 `Ambiguous reference`——用字符串 replace 拼前缀会漏掉
    除首个列以外的所有列（这正是第一版写错的地方）。所以做成函数，逐列加前缀。
    """
    q = f"{prefix}." if prefix else ""
    cols = (
        f"coalesce({q}device_type,'')",
        f"coalesce({q}unit,'')",
        f"coalesce(cast({q}sampling_hz as varchar),'')",
        f"coalesce({q}operating_mode,'')",
    )
    return " || '|' || ".join(cols)


def build_dims(
    con, lake: Lake, *, datasets: Sequence[str] = (), event_dates: Sequence[str] = ()
) -> dict[str, Any]:
    """SCD-2 维表增量维护：**先闭旧版本（MERGE/UPSERT），再开新版本**。

    两条 SQL 各司其职，顺序不能反：
    1. 关闭「属性已变化」的当前版本（把 `valid_to` 封到**变化发生的那一天**，
       `is_current=false`）——这一步用 `UPDATE … FROM` 表达 upsert 语义；
    2. 给「没有当前版本」的自然键插入新版本（`version = 历史最大 + 1`，
       代理键随之改变）。
    3. 把刚被封口的版本写进变更审计表（幂等：按 `device_sk` 去重）。

    **不追求"一次 MERGE 搞定"**：DuckDB 的 `MERGE` 只能对命中的行做 UPDATE 或
    对未命中的行做 INSERT，而 SCD-2 需要"命中且属性变化时**同时**封旧 + 开新"，
    那是两条语句。为了迁就一个语法而把语义写歪，是本末倒置。

    ⚠️ **已如实记录的边界**：同一批次内同一自然键若发生**多次**属性变化，
    本实现只保留首次观测到的属性（`arg_min`）。要支持多次变化必须按事件日
    逐步迭代。真实数据实测（212 个事件日 × 8 通道）**变化次数为 0**，
    因此这条限制在当前数据上不会被触发，但它是真限制，写在这里而不是藏着。
    """
    con.execute(_DIM_DDL)

    conds = ["channel <> ''"]  # 没有通道/设备的记录不该产生维行（如文本语料）
    if datasets:
        conds.append("dataset IN (" + ",".join(f"'{d}'" for d in datasets) + ")")
    if event_dates:
        conds.append("event_date IN (" + ",".join(f"'{d}'" for d in event_dates) + ")")

    # 本批次观测到的 (自然键 → 首见日 + 属性)
    obs_sql = f"""
        SELECT dataset, device_id, channel,
               min(event_date)                       AS first_seen,
               arg_min(device_type, event_date)      AS device_type,
               arg_min(unit, event_date)             AS unit,
               arg_min(sampling_hz, event_date)      AS sampling_hz,
               arg_min(operating_mode, event_date)   AS operating_mode
        FROM ({lake.scan_sql("ods", table="ods_samples", where=" AND ".join(conds))})
        GROUP BY dataset, device_id, channel
    """
    con.execute("CREATE OR REPLACE TEMP TABLE _obs AS " + obs_sql)

    # 1) 封口：属性与当前版本不同 → valid_to = 本批首见日
    con.execute(
        f"""
        UPDATE dim_device AS d
        SET valid_to = o.first_seen, is_current = FALSE
        FROM _obs o
        WHERE d.dataset = o.dataset AND d.device_id = o.device_id AND d.channel = o.channel
          AND d.is_current
          AND ({_attr_expr("d")} IS DISTINCT FROM {_attr_expr("o")})
        """
    )

    # 2) 开新版本：当前版本缺失的自然键
    con.execute(
        """
        INSERT INTO dim_device
        SELECT md5(concat_ws('|', o.dataset, o.device_id, o.channel,
                            cast(v.next_version as varchar))),
               o.dataset, o.device_id, o.channel,
               o.device_type, o.unit, o.sampling_hz, o.operating_mode,
               o.first_seen, NULL, TRUE, v.next_version
        FROM _obs o
        JOIN (
            SELECT o2.dataset, o2.device_id, o2.channel,
                   coalesce((SELECT max(d.version) FROM dim_device d
                             WHERE d.dataset = o2.dataset AND d.device_id = o2.device_id
                               AND d.channel = o2.channel), 0) + 1 AS next_version
            FROM _obs o2
        ) v ON v.dataset = o.dataset AND v.device_id = o.device_id AND v.channel = o.channel
        WHERE NOT EXISTS (
            SELECT 1 FROM dim_device d2
            WHERE d2.dataset = o.dataset AND d2.device_id = o.device_id
              AND d2.channel = o.channel AND d2.is_current
        )
        """
    )

    # 3) 变更审计（幂等：device_sk 是版本级唯一键）
    con.execute(
        f"""
        INSERT INTO dim_device_changes
        SELECT d.device_sk, d.dataset, d.device_id, d.channel, d.valid_to, d.version,
               {_attr_expr("d")},
               (SELECT {_attr_expr("n")}
                FROM dim_device n
                WHERE n.dataset = d.dataset AND n.device_id = d.device_id
                  AND n.channel = d.channel AND n.version = d.version + 1)
        FROM dim_device d
        WHERE d.is_current = FALSE AND d.valid_to IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM dim_device_changes c WHERE c.device_sk = d.device_sk)
        """
    )

    n_dim = con.execute("SELECT count(*) FROM dim_device").fetchone()[0]
    n_cur = con.execute("SELECT count(*) FROM dim_device WHERE is_current").fetchone()[0]
    n_chg = con.execute("SELECT count(*) FROM dim_device_changes").fetchone()[0]
    return {
        "n_dim_rows": int(n_dim),
        "n_current": int(n_cur),
        "n_versions": int(n_dim),
        "n_changes": int(n_chg),
        "n_observed_keys": int(con.execute("SELECT count(*) FROM _obs").fetchone()[0]),
    }


def dim_summary(con) -> dict[str, Any]:
    con.execute(_DIM_DDL)
    rows = con.execute(
        "SELECT dataset, count(*) AS n_versions, "
        "       sum(CASE WHEN is_current THEN 1 ELSE 0 END) AS n_current "
        "FROM dim_device GROUP BY dataset ORDER BY dataset"
    ).fetchall()
    chg = con.execute(
        "SELECT dataset, count(*), min(changed_at), max(changed_at) "
        "FROM dim_device_changes GROUP BY dataset ORDER BY dataset"
    ).fetchall()
    return {
        "by_dataset": [
            {"dataset": r[0], "n_versions": int(r[1]), "n_current": int(r[2])} for r in rows
        ],
        "changes": [
            {"dataset": r[0], "n_changes": int(r[1]), "first": str(r[2]), "last": str(r[3])}
            for r in chg
        ],
    }


# ---------------------------------------------------------------------------
# DWD：明细事实
# ---------------------------------------------------------------------------


def build_dwd(
    con, lake: Lake, *, datasets: Sequence[str] = (), event_dates: Sequence[str] = ()
) -> dict[str, Any]:
    """ODS → DWD 明细事实：窗级事实 + 窗×算子事实，并**按有效期区间**关联维版本。"""
    ds_cond = ("dataset IN (" + ",".join(f"'{d}'" for d in datasets) + ")") if datasets else ""
    ed_cond = (
        ("event_date IN (" + ",".join(f"'{d}'" for d in event_dates) + ")") if event_dates else ""
    )
    conds = " AND ".join(c for c in (ds_cond, ed_cond) if c)
    ods = lake.scan_sql("ods", table="ods_samples", where=conds)

    win_sql = f"""
        SELECT o.dataset, try_cast(o.event_date AS DATE) AS event_date,
               o.sample_id, o.device_id, o.channel,
               o.text_len, o.chars_han, o.has_image, o.is_kept, o.dropped_by,
               d.device_sk, d.version AS device_version
        FROM ({ods}) o
        LEFT JOIN dim_device d
          ON d.dataset = o.dataset AND d.device_id = o.device_id AND d.channel = o.channel
         AND try_cast(o.event_date AS DATE) >= d.valid_from
         AND (d.valid_to IS NULL OR try_cast(o.event_date AS DATE) < d.valid_to)
    """
    rows = _rows(con, win_sql)
    rep_w = lake.write("dwd", rows, table="dwd_window")
    rep_w["table"] = "dwd_window"

    # 窗 × 算子事实：`scores_json` 的展开放在 Python 做。
    # 为什么不在 SQL 里 unnest：`json_keys` 与 `json_extract` 的并行展开依赖
    # 具体 DuckDB 版本的函数签名，写死了就是版本脆弱点；而这只是个
    # 「一列 JSON 拆成多行」的机械变换，Python 里几行写得又清楚又好测。
    rows_scored = _rows(
        con,
        f"SELECT dataset, event_date, sample_id, scores_json "
        f"FROM ({ods}) WHERE scores_json IS NOT NULL",
    )
    rep_s = lake.write("dwd", _explode_scores(rows_scored), table="dwd_op_score")
    rep_s["table"] = "dwd_op_score"

    return {
        "n_in": rep_w["n_rows"],
        "n_out": rep_w["n_rows"],
        "n_scores": rep_s["n_rows"],
        "partitions": rep_w["partitions"],
        "bytes_written": rep_w["bytes"] + rep_s["bytes"],
        # 未关联到维表的行**必须拆成两类**，否则这个数字没有诊断价值：
        #   no_channel —— 文本/图像/FHIR 样本本来就没有设备通道（按设计不建维行）；
        #                 实测它也吞掉了 metropt3 里 3 条 channel 为空的畸形窗口，
        #                 所以叫 no_channel（如实描述事实）而不是 non_sensor（会撒谎）。
        #   unmatched_sensor —— 有通道却没匹配上维版本，**这是真问题**。
        # 合成一个数上报，等于把"设计如此"和"出错了"混成同一个指标。
        "n_no_channel": sum(1 for r in rows if not r.get("channel")),
        "n_unmatched_sensor": sum(1 for r in rows if r.get("channel") and not r.get("device_sk")),
        "fingerprint": fingerprint_rows(rows),
        "tables": [rep_w, rep_s],
    }


def _explode_scores(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 `scores_json` 展开成「窗 × 算子」事实行。"""
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            scores = json.loads(r["scores_json"])
        except (TypeError, ValueError):
            continue
        for op, val in sorted(scores.items()):
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                out.append(
                    {
                        "dataset": r["dataset"],
                        "event_date": r["event_date"],
                        "sample_id": r["sample_id"],
                        "op": str(op),
                        "score": float(val),
                    }
                )
    return out


def _count_none(rows: Sequence[dict[str, Any]], key: str) -> int:
    """统计某列为空的**样本级**条数（诊断用，不参与口径）。"""
    return sum(1 for r in rows if r.get(key) in (None, ""))


# ---------------------------------------------------------------------------
# DWS：聚合层（按分区重算）
# ---------------------------------------------------------------------------


def build_dws(
    con, lake: Lake, *, datasets: Sequence[str] = (), event_dates: Sequence[str] = ()
) -> dict[str, Any]:
    """DWD → DWS：按 (dataset × event_date) 与 (dataset × event_date × op) 聚合。

    覆盖率与丢脏率都**分列上报**：没有打分的窗不进分母（算不出来就是算不出来）。
    """
    conds = []
    if datasets:
        conds.append("dataset IN (" + ",".join(f"'{d}'" for d in datasets) + ")")
    if event_dates:
        conds.append("event_date IN (" + ",".join(f"'{d}'" for d in event_dates) + ")")
    c = " AND ".join(conds)
    win = lake.scan_sql("dwd", table="dwd_window", where=c)
    sc = lake.scan_sql("dwd", table="dwd_op_score", where=c)

    day_sql = f"""
        WITH w AS (
            SELECT dataset, event_date,
                   count(*)                                            AS n_total,
                   sum(is_kept)                                        AS n_kept,
                   count(DISTINCT device_id)                           AS n_devices,
                   count(DISTINCT channel)                             AS n_channels,
                   avg(text_len)                                       AS avg_len,
                   median(text_len)                                    AS median_len
            FROM ({win}) GROUP BY dataset, event_date
        ), s AS (
            SELECT dataset, event_date, count(DISTINCT op) AS n_ops,
                   count(*) AS n_scores
            FROM ({sc}) GROUP BY dataset, event_date
        )
        SELECT w.dataset, w.event_date, w.n_total, w.n_kept,
               w.n_total - w.n_kept                       AS n_dropped,
               (w.n_total - w.n_kept) * 1.0 / w.n_total    AS drop_rate,
               w.n_devices, w.n_channels, w.avg_len, w.median_len,
               coalesce(s.n_ops, 0)                        AS n_ops,
               coalesce(s.n_scores, 0) * 1.0
                 / nullif(w.n_total * coalesce(s.n_ops, 0), 0) AS score_coverage
        FROM w LEFT JOIN s ON s.dataset = w.dataset AND s.event_date = w.event_date
    """
    rows_day = _rows(con, day_sql)
    rep_day = lake.write("dws", rows_day, table="dws_dataset_day")

    op_sql = f"""
        WITH w AS (
            SELECT dataset, event_date, sample_id, is_kept, dropped_by
            FROM ({win})
        ), s AS (
            SELECT dataset, event_date, sample_id, op, score FROM ({sc})
        ), scored AS (
            SELECT dataset, event_date, op, count(*) AS n_scored, median(score) AS score_p50
            FROM s GROUP BY dataset, event_date, op
        ), dropped AS (
            SELECT dataset, event_date, dropped_by AS op, count(*) AS n_dropped
            FROM w WHERE dropped_by IS NOT NULL GROUP BY dataset, event_date, dropped_by
        )
        SELECT coalesce(sc.dataset, dp.dataset)     AS dataset,
               coalesce(sc.event_date, dp.event_date) AS event_date,
               coalesce(sc.op, dp.op)               AS op,
               coalesce(sc.n_scored, 0)             AS n_scored,
               coalesce(dp.n_dropped, 0)            AS n_dropped,
               CASE WHEN coalesce(sc.n_scored, 0) = 0 THEN NULL
                    ELSE coalesce(dp.n_dropped, 0) * 1.0 / sc.n_scored END AS drop_rate,
               sc.score_p50
        FROM scored sc FULL OUTER JOIN dropped dp
          ON sc.dataset = dp.dataset AND sc.event_date = dp.event_date AND sc.op = dp.op
    """
    rows_op = _rows(con, op_sql)
    rep_op = lake.write("dws", rows_op, table="dws_op_day")

    return {
        "n_in": rep_day["n_rows"],
        "n_out": rep_day["n_rows"],
        "n_op_rows": rep_op["n_rows"],
        "partitions": rep_day["partitions"],
        "bytes_written": rep_day["bytes"] + rep_op["bytes"],
        "fingerprint": fingerprint_rows(rows_day),
        "tables": [{**rep_day, "table": "dws_dataset_day"}, {**rep_op, "table": "dws_op_day"}],
    }


# ---------------------------------------------------------------------------
# ADS：面向应用的宽表
# ---------------------------------------------------------------------------


def build_ads(con, lake: Lake, *, today: str = "") -> dict[str, Any]:
    """DWS → ADS 数据集健康宽表（全量重建：它很小，重建比增量维护便宜）。"""
    from datetime import date

    today = today or date.today().isoformat()
    day = lake.scan_sql("dws", table="dws_dataset_day")
    op = lake.scan_sql("dws", table="dws_op_day")
    sql = f"""
        WITH d AS (
            SELECT dataset,
                   sum(n_total)                      AS n_total,
                   sum(n_kept)                       AS n_kept,
                   sum(n_dropped)                    AS n_dropped,
                   min(try_cast(event_date AS DATE)) AS first_event_date,
                   max(try_cast(event_date AS DATE)) AS last_event_date,
                   count(*) FILTER (WHERE try_cast(event_date AS DATE) IS NULL)
                                                     AS n_undated_partitions,
                   max(n_devices)                    AS n_devices,
                   max(n_channels)                   AS n_channels,
                   count(*)                          AS n_partitions,
                   avg(avg_len)                      AS avg_len,
                   -- score_coverage 在真实数据上可能整列为 NULL（无 score 事件），
                   -- lake._infer_schema 的兜底会把全 NULL 列落成 VARCHAR（lake.py:66）；
                   -- 直接 avg(VARCHAR) 在 binder 阶段就抛错（路线 D 实测抓到，WorkBuddy
                   -- 的 platform__realdata__0002/0003 同因 FAILED）。try_cast 自愈旧分区。
                   avg(try_cast(score_coverage AS DOUBLE)) AS mean_score_coverage
            FROM ({day}) GROUP BY dataset
        ), o AS (
            SELECT dataset, count(DISTINCT op) AS n_ops,
                   sum(n_dropped) AS n_dropped_scored
            FROM ({op}) GROUP BY dataset
        )
        SELECT d.dataset, d.n_total, d.n_kept, d.n_dropped,
               d.n_dropped * 1.0 / nullif(d.n_total, 0)     AS drop_rate,
               d.first_event_date, d.last_event_date, d.n_partitions,
               d.n_undated_partitions,
               date_diff('day', d.last_event_date, DATE '{today}') AS freshness_days,
               d.n_devices, d.n_channels, d.avg_len,
               coalesce(o.n_ops, 0)                          AS n_ops,
               d.mean_score_coverage                         AS score_coverage
        FROM d LEFT JOIN o ON o.dataset = d.dataset
    """
    rows = _rows(con, sql)
    # ADS 是聚合结果，没有事件日这一级 → 只按数据集分区
    rep = lake.write("ads", rows, table="ads_dataset_health", partition_by=("dataset",))
    return {
        "n_in": len(rows),
        "n_out": len(rows),
        "partitions": rep["partitions"],
        "bytes_written": rep["bytes"],
        "fingerprint": fingerprint_rows(rows),
        "tables": [{**rep, "table": "ads_dataset_health"}],
    }


# ---------------------------------------------------------------------------
# 视图：把湖上的分区文件接进 platform.duckdb（一条 SQL 能查到全部层）
# ---------------------------------------------------------------------------

_VIEWS = {
    "ods_all": (
        "ods",
        "ods_samples",
        "dataset, event_date, modality, sample_id, device_id, device_type, channel, unit, "
        "sampling_hz, operating_mode, window_start, window_end, text_len, chars_han, "
        "has_image, text_md5, is_kept, dropped_by",
    ),
    "dwd_window": ("dwd", "dwd_window", "*"),
    "dwd_op_score": ("dwd", "dwd_op_score", "*"),
    "dws_dataset_day": ("dws", "dws_dataset_day", "*"),
    "dws_op_day": ("dws", "dws_op_day", "*"),
    "ads_dataset_health": ("ads", "ads_dataset_health", "*"),
}


def refresh_views(con, lake: Lake) -> list[str]:
    """在 platform 库里重建指向湖上 Parquet 的视图。"""
    made: list[str] = []
    for name, (layer, table, cols) in _VIEWS.items():
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS {lake.scan_sql(layer, table=table, columns=cols)}"
        )
        made.append(name)
    con.execute("CREATE OR REPLACE VIEW ods_samples AS SELECT * FROM ods_all")
    con.execute("CREATE OR REPLACE VIEW dwd_samples AS SELECT * FROM dwd_window")
    con.execute("CREATE OR REPLACE VIEW dwd_scores AS SELECT * FROM dwd_op_score")
    con.execute(
        "CREATE OR REPLACE VIEW dws_dataset_profile AS "
        "SELECT dataset, sum(n_total) n_total, sum(n_kept) n_kept, sum(n_dropped) n_dropped, "
        "       sum(n_dropped)*1.0/nullif(sum(n_total),0) drop_rate, count(*) n_partitions "
        "FROM dws_dataset_day GROUP BY dataset"
    )
    con.execute("CREATE OR REPLACE VIEW ads_metrics AS SELECT * FROM ads_dataset_health")
    made += ["ods_samples", "dwd_samples", "dwd_scores", "dws_dataset_profile", "ads_metrics"]
    return made


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _rows(con, sql: str) -> list[dict[str, Any]]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
