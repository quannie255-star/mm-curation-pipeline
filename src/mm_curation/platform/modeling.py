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
    new_attrs  VARCHAR
);
"""

# 首批跑批时湖上还没有 `dims/` 分区，而 DuckDB 扫一个没有文件的 glob 会抛
# `IO Error: No files found that match the pattern`（**不是**返回空表）。
# 所以「上批状态为空」要显式造一张**带 schema 的空表**。
#
# ⚠️ **列序必须显式指定**（#97 的第二个坑）：从湖上读时`dataset` 是 Hive 分区列，
# DuckDB 把它放在**文件列之后**；而这里手写的空表把 `dataset` 放在第2 位。
# 两边列序不同 → `INSERT INTO _dim SELECT ...`（无列名清单，按位置插入）会把
# `unit`（'K'）塞进 `sampling_hz`（DOUBLE），报
# `Conversion Error: Could not convert string 'K' to DOUBLE`。
# 所以：读、写、空表初值三处**都用同一份显式列名清单**，不靠位置对齐。
_DIM_COLS = (
    "device_sk, dataset, device_id, channel, device_type, unit, "
    "sampling_hz, operating_mode, valid_from, valid_to, is_current, version"
)
_DIM_CHG_COLS = "device_sk, dataset, device_id, channel, changed_at, version, old_attrs, new_attrs"
_DIM_EMPTY_SQL = {
    "dim_device": (
        "SELECT CAST(NULL AS VARCHAR) AS device_sk, CAST(NULL AS VARCHAR) AS dataset, "
        "CAST(NULL AS VARCHAR) AS device_id, CAST(NULL AS VARCHAR) AS channel, "
        "CAST(NULL AS VARCHAR) AS device_type, CAST(NULL AS VARCHAR) AS unit, "
        "CAST(NULL AS DOUBLE) AS sampling_hz, CAST(NULL AS VARCHAR) AS operating_mode, "
        "CAST(NULL AS DATE) AS valid_from, CAST(NULL AS DATE) AS valid_to, "
        "CAST(NULL AS BOOLEAN) AS is_current, CAST(NULL AS INTEGER) AS version"
    ),
    "dim_device_changes": (
        "SELECT CAST(NULL AS VARCHAR) AS device_sk, CAST(NULL AS VARCHAR) AS dataset, "
        "CAST(NULL AS VARCHAR) AS device_id, CAST(NULL AS VARCHAR) AS channel, "
        "CAST(NULL AS DATE) AS changed_at, CAST(NULL AS INTEGER) AS version, "
        "CAST(NULL AS VARCHAR) AS old_attrs, CAST(NULL AS VARCHAR) AS new_attrs"
    ),
}


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

    **维表落湖（2026-10-05，`ENGINEERING_NOTES` #97）**：SCD-2 算完后把两张表整表写进
    `data/lake/dims/`，库里的 `dim_device` / `dim_device_changes` 随后被 `refresh_views`
    建成**指向湖上 Parquet 的视图**（不再是 BASE TABLE）。

    为什么要改：原先维表只活在 DuckDB 的 BASE TABLE 里，而 `promote` 只搬
    `data/lake/**` 的 Parquet 分区 + 重建视图——**维表两边都不沾**。后果是
    晋升报`ok=True`、契约闸门 10/10 全绿，服务的 `/api/datasets/{ds}/dims`
    在 prod 上仍然 500（`Table with name dim_device does not exist`）。
    实测：dev 库 16 个对象 / prod 库 14 个，缺的正好是这两张维表；7 个端点里1 个 500。

    为什么不是「promote 多搬一步」：那样维表仍然**不在对账指纹里、不在契约覆盖范围内**，
    下一个 BASE TABLE 还会再漏一次。落湖之后它和事实表受同一套闸门管。

    **实现上为什么用 TEMP 工作表**：SCD-2 是增量的，要读上一批的维表状态来封口/开新版本。
    落湖后库里的 `dim_device` 是**视图**，DuckDB 视图不可 UPDATE。所以上一批状态直接
    **从湖上 Parquet 读**（不依赖库里的对象形态），SCD-2 在 `_dim` / `_dim_chg` 两张
    TEMP 表上算完再整表落湖。首批跑时湖上还没有 `dims/` 分区，读出来是空表——
    那是正确初值，不是错误。
    """
    # 把上批状态从湖上读进 TEMP 表（首批为空 = 正确初值）
    con.execute("DROP TABLE IF EXISTS _dim")
    con.execute("DROP TABLE IF EXISTS _dim_chg")
    for tmp, table, cols in (
        ("_dim", "dim_device", _DIM_COLS),
        ("_dim_chg", "dim_device_changes", _DIM_CHG_COLS),
    ):
        if lake.has("dims", table):
            # 显式列名：湖上`dataset` 是 Hive 分区列，DuckDB 放在文件列**之后**，
            # 与手写空表的列序不同 → 无列名清单的 INSERT 会把 unit('K') 塞进 sampling_hz。
            con.execute(
                f"CREATE TEMP TABLE {tmp} AS "
                f"SELECT {cols} FROM ({lake.scan_sql('dims', table=table)})"
            )
        else:
            # 首批：湖上还没有 dims/ 分区。**建带 schema 的空表**而不是跳过——
            # 跳过会让后面的 `max(version)` 报「表不存在」，把「初值」伪装成「故障」。
            con.execute(
                f"CREATE TEMP TABLE {tmp} AS "
                f"SELECT {cols} FROM ({_DIM_EMPTY_SQL[table]}) WHERE false"
            )

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
        UPDATE _dim AS d
        SET valid_to = o.first_seen, is_current = FALSE
        FROM _obs o
        WHERE d.dataset = o.dataset AND d.device_id = o.device_id AND d.channel = o.channel
          AND d.is_current
          AND ({_attr_expr("d")} IS DISTINCT FROM {_attr_expr("o")})
        """
    )

    # 2) 开新版本：当前版本缺失的自然键
    con.execute(
        f"""
        INSERT INTO _dim ({_DIM_COLS})
        SELECT md5(concat_ws('|', o.dataset, o.device_id, o.channel,
                            cast(v.next_version as varchar))),
               o.dataset, o.device_id, o.channel,
               o.device_type, o.unit, o.sampling_hz, o.operating_mode,
               o.first_seen, NULL, TRUE, v.next_version
        FROM _obs o
        JOIN (
            SELECT o2.dataset, o2.device_id, o2.channel,
                   coalesce((SELECT max(d.version) FROM _dim d
                             WHERE d.dataset = o2.dataset AND d.device_id = o2.device_id
                               AND d.channel = o2.channel), 0) + 1 AS next_version
            FROM _obs o2
        ) v ON v.dataset = o.dataset AND v.device_id = o.device_id AND v.channel = o.channel
        WHERE NOT EXISTS (
            SELECT 1 FROM _dim d2
            WHERE d2.dataset = o.dataset AND d2.device_id = o.device_id
              AND d2.channel = o.channel AND d2.is_current
        )
        """
    )

    # 3) 变更审计（幂等：device_sk 是版本级唯一键）
    con.execute(
        f"""
        INSERT INTO _dim_chg ({_DIM_CHG_COLS})
        SELECT d.device_sk, d.dataset, d.device_id, d.channel, d.valid_to, d.version,
               {_attr_expr("d")},
               (SELECT {_attr_expr("n")}
                FROM _dim n
                WHERE n.dataset = d.dataset AND n.device_id = d.device_id
                  AND n.channel = d.channel AND n.version = d.version + 1)
        FROM _dim d
        WHERE d.is_current = FALSE AND d.valid_to IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM _dim_chg c WHERE c.device_sk = d.device_sk)
        """
    )

    n_dim = con.execute("SELECT count(*) FROM _dim").fetchone()[0]
    n_cur = con.execute("SELECT count(*) FROM _dim WHERE is_current").fetchone()[0]
    n_chg = con.execute("SELECT count(*) FROM _dim_chg").fetchone()[0]

    # -- 整表落湖（2026-10-05，#97）------------------------------------------------
    # 顺序要紧：先把 `_dim` / `_dim_chg` 落湖，`refresh_views` 随后才能把库里的
    # `dim_device` 建成指向湖的视图。这里读的是 TEMP 工作表（SCD-2 真正的算完结果）。
    lake_rep = _dump_dims_to_lake(con, lake)

    return {
        "n_dim_rows": int(n_dim),
        "n_current": int(n_cur),
        "n_versions": int(n_dim),
        "n_changes": int(n_chg),
        "n_observed_keys": int(con.execute("SELECT count(*) FROM _obs").fetchone()[0]),
        "lake": lake_rep,
    }


def _dim_lake_schema() -> dict[str, Any]:
    """维表落湖时**显式固定**的 Parquet schema（#97）。

    为什么必须显式：`_infer_schema` 对「这批全是 NULL 的列」一律判`string`。
    维表 `valid_to` 恰好天生如此——SCD-2 里「还没被封口的当前版本」`valid_to`
    全是 NULL，于是首批落湖它是 `string`，第二批（出现了封口版本）变成 `date32`：
    **同一张表两批不同 schema**。后果不是「类型不好看」，而是下游 SQL 直接崩：

        Cannot compare values of type DATE and type VARCHAR

    （`build_dwd` 里的有效期区间 join 就会撞上。）
    显式 schema 让 Parquet 的物理类型与库里 `_DIM_DDL` 的声明一致。
    """
    import pyarrow as pa

    return {
        "dim_device": pa.schema(
            [
                pa.field("device_sk", pa.string()),
                pa.field("dataset", pa.string()),
                pa.field("device_id", pa.string()),
                pa.field("channel", pa.string()),
                pa.field("device_type", pa.string()),
                pa.field("unit", pa.string()),
                pa.field("sampling_hz", pa.float64()),
                pa.field("operating_mode", pa.string()),
                pa.field("valid_from", pa.date32()),
                pa.field("valid_to", pa.date32()),
                pa.field("is_current", pa.bool_()),
                pa.field("version", pa.int64()),
            ]
        ),
        "dim_device_changes": pa.schema(
            [
                pa.field("device_sk", pa.string()),
                pa.field("dataset", pa.string()),
                pa.field("device_id", pa.string()),
                pa.field("channel", pa.string()),
                pa.field("changed_at", pa.date32()),
                pa.field("version", pa.int64()),
                pa.field("old_attrs", pa.string()),
                pa.field("new_attrs", pa.string()),
            ]
        ),
    }


def _dim_rows(con, table: str, cols: str) -> list[dict[str, Any]]:
    """按 `cols` 的**显式顺序**读 TEMP 工作表。

    显式顺序而不是 `SELECT *`：`lake.write` 用第一行的 key 定列序，
    而 TEMP 表的列序来自湖上 Parquet（Hive 分区列在末尾）。两者不一致时
    写出去的 Parquet 列序会变，虽然 DuckDB 按名字读不受影响，但「同一批输入
    重复写产物逐字节一致」这个幂等前提就破了（列序变了压缩布局就变了）。
    """
    cur = con.execute(f"SELECT {cols} FROM {table}")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _dump_dims_to_lake(con, lake: Lake) -> dict[str, Any]:
    """把 SCD-2 两张表整表写进 `data/lake/dims/`，**按 dataset 单键分区**。

    读的是 TEMP 工作表 `_dim` / `_dim_chg`（`build_dims` 里的 SCD-2 算完的结果），
    不是库里的 `dim_device`——落湖后后者已是视图，读它会绕回湖上再读一遍。

    不用 `(dataset, event_date)`：维表是**状态**不是**事件**，一个设备的当前版本
    只应有一行；按事件日分区会让同一设备在每个事件日各存一份全量快照。
    `valid_from` / `valid_to` 已经是列，事件日语义不丢。
    """
    rep: dict[str, Any] = {}
    schemas = _dim_lake_schema()
    for table, src, cols in (
        ("dim_device", "_dim", _DIM_COLS),
        ("dim_device_changes", "_dim_chg", _DIM_CHG_COLS),
    ):
        rows = _dim_rows(con, src, cols)
        rep[table] = lake.write(
            "dims",
            rows,
            table=table,
            partition_by=("dataset",),
            replace=True,
            schema=schemas[table],
        )
    rep["n_rows"] = rep["dim_device"]["n_rows"] + rep["dim_device_changes"]["n_rows"]
    return rep


def dim_summary(con) -> dict[str, Any]:
    # ⚠️ **不要在这里执行 `_DIM_DDL`**（#97）：落湖后 `dim_device` 是**指向湖上
    # Parquet 的视图**，`CREATE TABLE IF NOT EXISTS` 撞上同名视图会让 DuckDB 报
    # `Catalog Error`，而服务启动路径会调本函数。维表在老库里可能是 BASE TABLE、
    # 在新库里是视图——本函数**只读**，两种形态都能查，所以不需要建表。
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
    # 维表**直接读湖上**（#97），不读库里的 `dim_device`：dwd 是本批的产物，
    # 维表也是本批刚落湖的，读湖保证两者口径同源；而库里的对象在不同环境下
    # 形态不同（旧库 BASE TABLE / 新库 VIEW），读库会让同一段 SQL 在 dev 与
    # prod 上行为不一致——那正是 #97 的原始故障。
    dims_sql = (
        f"SELECT {_DIM_COLS} FROM ({lake.scan_sql('dims', table='dim_device')})"
        if lake.has("dims", "dim_device")
        else f"SELECT {_DIM_COLS} FROM ({_DIM_EMPTY_SQL['dim_device']}) WHERE false"
    )

    win_sql = f"""
        SELECT o.dataset, try_cast(o.event_date AS DATE) AS event_date,
               o.sample_id, o.device_id, o.channel,
               o.text_len, o.chars_han, o.has_image, o.is_kept, o.dropped_by,
               d.device_sk, d.version AS device_version
        FROM ({ods}) o
        LEFT JOIN ({dims_sql}) d
          ON d.dataset = o.dataset AND d.device_id = o.device_id AND d.channel = o.channel
         AND try_cast(o.event_date AS DATE) >= try_cast(d.valid_from AS DATE)
         AND (d.valid_to IS NULL
              OR try_cast(o.event_date AS DATE) < try_cast(d.valid_to AS DATE))
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
    # SCD-2 维表（#97）：落湖后建成视图，`promote` 搬湖即搬维表。
    # `hive_partitioning=true` 会把dataset 变成列，所以下面不必再 select 它。
    "dim_device": ("dims", "dim_device", "*"),
    "dim_device_changes": ("dims", "dim_device_changes", "*"),
}

# 某层还没有任何 Parquet 时，空视图要**投影出正确的列**（`SELECT *` 只在真有表时合法）。
# 列名不是猜的：每条都注明它从哪来，且真库 `describe <view>` 与此一致（2026-10-05 实点）。
# ⚠️ 改动这些列名时必须同步改真库口径，否则「空态能查」只在首批成立、
# 第二批（有数据后列变了）就崩——那比直接报错更难查。
_EMPTY_VIEW_SQL = {
    "dim_device": f"SELECT {_DIM_COLS} FROM ({_DIM_EMPTY_SQL['dim_device']}) WHERE false",
    "dim_device_changes": (
        f"SELECT {_DIM_CHG_COLS} FROM ({_DIM_EMPTY_SQL['dim_device_changes']}) WHERE false"
    ),
    # 来源：`build_ods` 写的 `ods_samples` 列清单（与 _VIEWS["ods_all"] 那串一致）
    "ods_all": (
        "SELECT CAST(NULL AS VARCHAR) AS dataset, CAST(NULL AS VARCHAR) AS event_date, "
        "CAST(NULL AS VARCHAR) AS modality, CAST(NULL AS VARCHAR) AS sample_id, "
        "CAST(NULL AS VARCHAR) AS device_id, CAST(NULL AS VARCHAR) AS device_type, "
        "CAST(NULL AS VARCHAR) AS channel, CAST(NULL AS VARCHAR) AS unit, "
        "CAST(NULL AS DOUBLE) AS sampling_hz, CAST(NULL AS VARCHAR) AS operating_mode, "
        "CAST(NULL AS VARCHAR) AS window_start, CAST(NULL AS VARCHAR) AS window_end, "
        "CAST(NULL AS BIGINT) AS text_len, CAST(NULL AS BIGINT) AS chars_han, "
        "CAST(NULL AS BOOLEAN) AS has_image, CAST(NULL AS VARCHAR) AS text_md5, "
        "CAST(NULL AS BOOLEAN) AS is_kept, CAST(NULL AS VARCHAR) AS dropped_by WHERE false"
    ),
    # 来源：`build_dwd` 的 `win_sql` 投影
    "dwd_window": (
        "SELECT CAST(NULL AS VARCHAR) AS sample_id, CAST(NULL AS VARCHAR) AS device_id, "
        "CAST(NULL AS VARCHAR) AS channel, CAST(NULL AS BIGINT) AS text_len, "
        "CAST(NULL AS BIGINT) AS chars_han, CAST(NULL AS BOOLEAN) AS has_image, "
        "CAST(NULL AS BOOLEAN) AS is_kept, CAST(NULL AS VARCHAR) AS dropped_by, "
        "CAST(NULL AS VARCHAR) AS device_sk, CAST(NULL AS BIGINT) AS device_version, "
        "CAST(NULL AS VARCHAR) AS dataset, CAST(NULL AS DATE) AS event_date WHERE false"
    ),
    # 来源：`build_dws` 的 `day_sql` 投影
    "dws_dataset_day": (
        "SELECT CAST(NULL AS BIGINT) AS n_total, CAST(NULL AS BIGINT) AS n_kept, "
        "CAST(NULL AS BIGINT) AS n_dropped, CAST(NULL AS DOUBLE) AS drop_rate, "
        "CAST(NULL AS BIGINT) AS n_devices, CAST(NULL AS BIGINT) AS n_channels, "
        "CAST(NULL AS DOUBLE) AS avg_len, CAST(NULL AS DOUBLE) AS median_len, "
        "CAST(NULL AS BIGINT) AS n_ops, CAST(NULL AS DOUBLE) AS score_coverage, "
        "CAST(NULL AS VARCHAR) AS dataset, CAST(NULL AS VARCHAR) AS event_date WHERE false"
    ),
    # 来源：`build_ads` 的 `ads_dataset_health` 投影
    "ads_dataset_health": (
        "SELECT CAST(NULL AS BIGINT) AS n_total, CAST(NULL AS BIGINT) AS n_kept, "
        "CAST(NULL AS BIGINT) AS n_dropped, CAST(NULL AS DOUBLE) AS drop_rate, "
        "CAST(NULL AS DATE) AS first_event_date, CAST(NULL AS DATE) AS last_event_date, "
        "CAST(NULL AS BIGINT) AS n_partitions, CAST(NULL AS BIGINT) AS n_undated_partitions, "
        "CAST(NULL AS BIGINT) AS freshness_days, CAST(NULL AS BIGINT) AS n_devices, "
        "CAST(NULL AS BIGINT) AS n_channels, CAST(NULL AS DOUBLE) AS avg_len, "
        "CAST(NULL AS BIGINT) AS n_ops, CAST(NULL AS DOUBLE) AS score_coverage, "
        "CAST(NULL AS VARCHAR) AS dataset WHERE false"
    ),
    # 来源：`_explode_scores` 的输出列
    "dwd_op_score": (
        "SELECT CAST(NULL AS VARCHAR) AS sample_id, CAST(NULL AS VARCHAR) AS op, "
        "CAST(NULL AS DOUBLE) AS score, CAST(NULL AS VARCHAR) AS dataset, "
        "CAST(NULL AS VARCHAR) AS event_date WHERE false"
    ),
    # 来源：`build_dws` 的 `op_sql` 投影
    "dws_op_day": (
        "SELECT CAST(NULL AS VARCHAR) AS op, CAST(NULL AS BIGINT) AS n_scored, "
        "CAST(NULL AS BIGINT) AS n_dropped, CAST(NULL AS DOUBLE) AS drop_rate, "
        "CAST(NULL AS DOUBLE) AS score_p50, CAST(NULL AS VARCHAR) AS dataset, "
        "CAST(NULL AS VARCHAR) AS event_date WHERE false"
    ),
}


def refresh_views(con, lake: Lake) -> list[str]:
    """在 platform 库里重建指向湖上 Parquet 的视图。

    **先 DROP 再 CREATE**：2026-10-05 起 `dim_device` / `dim_device_changes` 也在这份
    名单里（#97），而它们在**旧库里是 BASE TABLE**——`CREATE OR REPLACE VIEW` 撞上同名
    实表会报 `Catalog Error: Existing object is of type Table`，视图根本建不出来。
    这也是「同名不同形态」的历史包袱：库升级前跑过一次 `build_dims` 的库必须能被
    幂等刷新，否则服务启动时直接崩。
    """
    made: list[str] = []
    for name, (layer, table, cols) in _VIEWS.items():
        _drop_any(con, name)
        if not lake.has(layer, table):
            # 该层还没有任何 Parquet（首批、或只跑了子集阶段）→ 建**空视图**。
            # 直接 `CREATE VIEW … read_parquet(空glob)` 会抛
            # `IO Error: No files found that match the pattern`，而那会让
            # 「还没跑」和「跑挂了」变成同一个错误——排查时最贵的那种歧义。
            #
            # 已知 schema 的层用 `WHERE false` 零行派生，列名与真表一致；
            # 未知的层退化成单列占位（服务启动要能在「只跑了 ods」的库上活下来）。
            empty_sql = _EMPTY_VIEW_SQL.get(name)
            if empty_sql is None:
                con.execute(
                    f"CREATE VIEW {name} AS SELECT CAST(NULL AS VARCHAR) AS _empty WHERE false"
                )
            else:
                con.execute(f"CREATE VIEW {name} AS {empty_sql}")
            made.append(name)
            continue
        con.execute(f"CREATE VIEW {name} AS {lake.scan_sql(layer, table=table, columns=cols)}")
        made.append(name)
    for name, sql in _ALIAS_VIEWS.items():
        _drop_any(con, name)
        con.execute(f"CREATE VIEW {name} AS {sql}")
        made.append(name)
    return made


def _drop_any(con, name: str) -> None:
    """把 `name` 上的**任何**对象（视图 / 基表 / 临时表）删掉，不报「不存在」。

    为什么不能直接 `DROP VIEW IF EXISTS` + `DROP TABLE IF EXISTS`（#97 真实库实测）：
    DuckDB 的 `DROP VIEW IF EXISTS x` 在 `x` 是 **BASE TABLE** 时**不**静默跳过，
    而是抛 `Catalog Error: Existing object x is of type Table, trying to drop type View`
    —— 也就是说「先 VIEW 后 TABLE」这个看起来最稳的顺序，在旧库（维表是实表）
    上必然炸。而旧库正是**库升级后第一次跑**要面对的东西。

    所以先查 `information_schema` 看它到底是什么类型，再发对应的 DROP。
    """
    row = con.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_name = ? AND table_schema = current_schema()",
        [name],
    ).fetchone()
    if row is None:
        return
    kind = "VIEW" if str(row[0]).upper().endswith("VIEW") else "TABLE"
    con.execute(f"DROP {kind} IF EXISTS {name}")


_ALIAS_VIEWS = {
    "ods_samples": "SELECT * FROM ods_all",
    "dwd_samples": "SELECT * FROM dwd_window",
    "dwd_scores": "SELECT * FROM dwd_op_score",
    "dws_dataset_profile": (
        "SELECT dataset, sum(n_total) n_total, sum(n_kept) n_kept, sum(n_dropped) n_dropped, "
        "       sum(n_dropped)*1.0/nullif(sum(n_total),0) drop_rate, count(*) n_partitions "
        "FROM dws_dataset_day GROUP BY dataset"
    ),
    "ads_metrics": "SELECT * FROM ads_dataset_health",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _rows(con, sql: str) -> list[dict[str, Any]]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
