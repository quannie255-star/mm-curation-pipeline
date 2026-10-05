"""湖上分层存储：Parquet + Hive 风格分区（S2 的存储面）。

## 为什么换掉"单个 duckdb 文件 + `CREATE OR REPLACE TABLE`"

原实现每次构建把全部行读进一个 DuckDB 文件重建——「分层」只是
`CREATE VIEW ods_x AS SELECT * FROM raw_x` 的**别名**，同一张物理表两个名字。
分层要成立，必须每层**独立落盘**，即：

| 要求 | 现在 |
|---|---|
| 独立的物理产物 | `data/lake/<层>/<表>/…/*.parquet` |
| 独立的分区键 | `dataset=<x>/event_date=<y>`（Hive 风格，任何引擎都认） |
| 独立的更新粒度 | 只重写被触碰到的那几个分区 |

**路径模型是三层：层 → 表 → 分区。** 层是"加工深度"（ods/dwd/dws/ads），
表是层内的具体事实（`dwd` 层下有 `dwd_window` 与 `dwd_op_score` 两张表）。
把表名塞进层名（例如把 `dwd_op_score` 当层）会立刻把路径约定搞乱——
这是实做时踩到的第一个坑，所以路径模型在 `_base()` 里收口，只允许一个出口。

## 不自己造轮子的地方

- **列存与分区**：`COPY … TO (FORMAT PARQUET, PARTITION_BY (…))` 直接用 DuckDB 原生能力；
- **分区裁剪**：读侧用 `read_parquet(glob, hive_partitioning=true)`，
  分区键自动成为列，`WHERE event_date='…'` 即触发裁剪——不需要自建元数据索引；
- **类型推断**：交给 pyarrow，不自写 schema 推导。

## 一条诚实说明

ODS 层存的是**样本级结构化元数据 + 内容指纹**（`text_md5`），**不存原文**。
理由：ODS 在这里的目的是"可分区、可对账、可追溯"，而不是"可复制原文"——
原文仍由源文件与 `extract/rawdoc.py` 的内容寻址归档承担，复制一份进列存
只会让仓库体积翻倍而不增加任何可验证性。这条取舍写在 `docs/PLATFORM.md`。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover —— 只为类型注解，运行时不 import pyarrow
    import pyarrow as pa

LAYERS = ("ods", "dims", "dwd", "dws", "ads")
"""湖上的层。

`dims` 是 2026-10-05 加进来的（见 `ENGINEERING_NOTES` #97）：SCD-2 维表原先只存在于
DuckDB 的 BASE TABLE 里，而 `promote` 只搬 `data/lake/**` 的 Parquet 分区 + 重建视图
——**维表两边都不沾**，于是晋升后服务的 `/api/datasets/{ds}/dims` 端点在 prod 上必然 500，
而契约闸门因为只查 ods/dwd/dws/ads 一层，10/10 全绿也抓不到。

维表上湖不是为了"更好看"，是为了让它进入**晋升的对账指纹**与**契约的覆盖范围**：
只有落成 Parquet，它才和事实表受同一套闸门管。
"""

DEFAULT_PARTITION = ("dataset", "event_date")
"""默认分区键（ODS/DWD/DWS/ADS 按事件日切）。

`dims` 层不用它：维表是**状态**不是**事件**，按 `dataset` 单键分区即可
（见 `modeling.build_dims` 落湖时传的 `partition_by`）。
"""

HIVE_DEFAULT_PARTITION = "__HIVE_DEFAULT_PARTITION__"
"""分区值为空时 DuckDB 落盘的目录名（Hive 生态的既定约定，不是我们发明的）。

阅读这个目录名会有点丑，但**约定优于自创**：任何引擎（Spark / Trino / Hive）
读到它都知道"这里是空分区"，自创一个 `null` 反而要额外解释。
"""


def hive_value(v: Any) -> str:
    """分区值的目录名表示（必须与 DuckDB 的写法一致，否则删不掉旧分区）。"""
    return HIVE_DEFAULT_PARTITION if v is None else str(v)


@dataclass(frozen=True)
class PartitionStat:
    dataset: str
    event_date: str
    n_files: int
    bytes: int


def _infer_schema(names: Sequence[str], rows: Sequence[dict[str, Any]]):
    """用 pyarrow 推断列类型；**全 None 的列一律 string**。

    全 None 列若让 pyarrow 推断会得到 `null` 类型，写 Parquet 会失败——
    而"这一列这次全是空"是完全正常的事（例如某天的样本都没有 image_path）。

    日期/时间也要显式处理：它们既不是 int 也不是 str，
    落到兜底分支会被当成 string，然后在 `from_pylist` 里抛
    `ArrowTypeError: Expected bytes, got a 'datetime.date' object`。
    """
    import datetime as _dt

    import pyarrow as pa

    fields = []
    for n in names:
        nn = [r.get(n) for r in rows if r.get(n) is not None]
        if not nn:
            fields.append(pa.field(n, pa.string()))
        elif all(isinstance(v, bool) for v in nn):
            fields.append(pa.field(n, pa.bool_()))
        elif all(isinstance(v, int) and not isinstance(v, bool) for v in nn):
            fields.append(pa.field(n, pa.int64()))
        elif all(isinstance(v, _dt.datetime) for v in nn):
            fields.append(pa.field(n, pa.timestamp("us")))
        elif all(isinstance(v, _dt.date) for v in nn):
            fields.append(pa.field(n, pa.date32()))
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in nn):
            fields.append(pa.field(n, pa.float64()))
        else:
            fields.append(pa.field(n, pa.string()))
    return pa.schema(fields)


class Lake:
    """`data/lake/` 的读写句柄。所有层共用一套路径约定。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    # -- 路径 ---------------------------------------------------------------

    def _base(self, layer: str, table: str = "") -> Path:
        """层 → 表 的唯一路径出口（三层：层 / 表 / 分区）。"""
        if layer not in LAYERS:
            raise ValueError(f"未知层 {layer!r}；合法值 {LAYERS}")
        base = self.root / layer
        return base / table if table else base

    def layer_dir(self, layer: str, table: str = "") -> Path:
        return self._base(layer, table)

    def parquet_glob(self, layer: str, table: str = "") -> str:
        return (self._base(layer, table) / "**" / "*.parquet").as_posix()

    def has(self, layer: str, table: str = "") -> bool:
        """该层/表在湖上是否**已有 Parquet 落盘**（路径级判断，不读数据）。

        为什么要它：DuckDB 扫一个没有文件的 glob 会抛
        `IO Error: No files found that match the pattern`，而不是返回空表。
        首批跑批时 `dims/` 还没有任何分区，上批状态必须是「空」而不是「报错」——
        这两者语义完全不同，所以要显式判一次，而不是靠try/except 吞异常。
        """
        base = self._base(layer, table)
        return base.exists() and any(base.rglob("*.parquet"))

    def part_dir(self, layer: str, values: Sequence[str], table: str = "") -> Path:
        """`<层>/<表>/dataset=X/event_date=Y`。"""
        d = self._base(layer, table)
        for key, val in zip(DEFAULT_PARTITION, values):
            d = d / f"{key}={val}"
        return d

    # -- 发现 ---------------------------------------------------------------

    def files(
        self, layer: str, table: str = "", dataset: str = "", event_date: str = ""
    ) -> list[Path]:
        """按分区过滤后的 parquet 文件清单（**路径级过滤，不读数据**）。

        用路径解析而不是 glob 模式：分区值里可能有 `-`、`__HIVE_DEFAULT_PARTITION__`
        这类字符串，拼 glob 容易写出既匹配不到又看不出错的模式。
        """
        base = self._base(layer, table)
        if not base.exists():
            return []
        want = {"dataset": dataset, "event_date": event_date}
        out: list[Path] = []
        for f in base.rglob("*.parquet"):
            kv = dict(p.split("=", 1) for p in f.relative_to(base).parts if "=" in p)
            if all(not v or kv.get(k) == v for k, v in want.items()):
                out.append(f)
        return sorted(out)

    def partitions(self, layer: str, table: str = "") -> list[PartitionStat]:
        """按 (dataset, event_date) 汇总文件数与字节数——**分区裁剪的证据来源**。"""
        agg: dict[tuple[str, str], PartitionStat] = {}
        base = self._base(layer, table)
        if not base.exists():
            return []
        for f in base.rglob("*.parquet"):
            kv = dict(p.split("=", 1) for p in f.relative_to(base).parts if "=" in p)
            key = (kv.get("dataset", "?"), kv.get("event_date", "?"))
            prev = agg.get(key)
            size = f.stat().st_size
            n_files = (prev.n_files if prev else 0) + 1
            n_bytes = (prev.bytes if prev else 0) + size
            agg[key] = PartitionStat(key[0], key[1], n_files, n_bytes)
        return [agg[k] for k in sorted(agg)]

    def partition_values(self, layer: str, table: str = "", key: str = "event_date") -> set[str]:
        """该层已有哪些分区值（**只读目录名，不读数据**）。

        这是增量作业"要处理哪些分区"的唯一依据：
        `上游分区集合 − 本层分区集合 = 待处理分区`。
        比查水位线表更可靠——水位线是账，分区目录是事实，两者不一致时以事实为准。
        空分区（`__HIVE_DEFAULT_PARTITION__`）不在此返回，由 `has_undated()` 单独回答。
        """
        base = self._base(layer, table)
        if not base.exists():
            return set()
        out: set[str] = set()
        prefix = f"{key}="
        for d in base.rglob(f"{key}=*"):
            if d.is_dir():
                val = d.name[len(prefix) :]
                if val and val != HIVE_DEFAULT_PARTITION:
                    out.add(val)
        return out

    def has_undated(self, layer: str, table: str = "") -> bool:
        """是否存在空分区（事件时间取不到的记录）。"""
        base = self._base(layer, table)
        if not base.exists():
            return False
        return any(d.is_dir() for d in base.rglob(f"event_date={HIVE_DEFAULT_PARTITION}"))

    def stats(
        self, layer: str, table: str = "", dataset: str = "", event_date: str = ""
    ) -> dict[str, Any]:
        fs = self.files(layer, table, dataset, event_date)
        return {
            "layer": layer,
            "table": table or "*",
            "dataset": dataset or "*",
            "event_date": event_date or "*",
            "n_files": len(fs),
            "bytes": sum(f.stat().st_size for f in fs),
            "partitions": len({f.parent.as_posix() for f in fs}),
        }

    # -- 写入 ---------------------------------------------------------------

    def _clear(
        self,
        layer: str,
        table: str,
        rows: Sequence[dict[str, Any]],
        partition_by: Sequence[str],
    ):
        """写前清掉目标分区目录：**分区重写必须是替换，不是追加**。"""
        seen = {tuple(hive_value(r.get(k)) for k in partition_by) for r in rows}
        base = self._base(layer, table)
        for values in seen:
            d = base
            for key, val in zip(partition_by, values):
                d = d / f"{key}={val}"
            if d.exists():
                shutil.rmtree(d)

    def write(
        self,
        layer: str,
        rows: Sequence[dict[str, Any]],
        *,
        table: str = "",
        partition_by: Sequence[str] = DEFAULT_PARTITION,
        replace: bool = True,
        schema: pa.Schema | None = None,
    ) -> dict[str, Any]:
        """把一批行写成按 `partition_by` 分区的 Parquet 数据集。

        返回写入报告（行数 / 分区数 / 文件数 / 字节数 / 分区清单）。
        `replace=True`（默认）先清目标分区目录再写 → **幂等**：
        同一批输入重复写，产物逐字节一致，指纹必然相同。

        `schema` 显式给列类型。**什么时候必须给**：某一列在这批里恰好全是
        NULL 时，`_infer_schema` 会把它定成 `string`（它分不清「这列是字符串」
        和「这列碰巧没值」），下一批有值了又变成 `date32`——**同一张表两批
        不同 schema**，Parquet 读回来类型不一致，下游 SQL 会在类型比较上报
        `Cannot compare values of type DATE and type VARCHAR`。
        维表的 `valid_to` / `changed_at` 就有这个特性（SCD-2 里「还没封口的
        版本」`valid_to` 全 NULL），所以 `modeling._dump_dims_to_lake` 显式传 schema。
        """
        if not rows:
            return {
                "layer": layer,
                "table": table or "*",
                "n_rows": 0,
                "n_files": 0,
                "bytes": 0,
                "partitions": [],
            }
        names = list(rows[0].keys())
        schema = schema if schema is not None else _infer_schema(names, rows)

        import duckdb
        import pyarrow as pa

        if replace:
            self._clear(layer, table, rows, partition_by)

        target = self._base(layer, table)
        target.mkdir(parents=True, exist_ok=True)
        tbl = pa.Table.from_pylist([{k: r.get(k) for k in names} for r in rows], schema=schema)
        con = duckdb.connect()
        try:
            con.register("src", tbl)
            cols = ", ".join(partition_by)
            con.execute(
                f"COPY (SELECT * FROM src) TO '{target.as_posix()}' "
                f"(FORMAT PARQUET, PARTITION_BY ({cols}), OVERWRITE_OR_IGNORE, COMPRESSION ZSTD)"
            )
        finally:
            con.close()

        # 写入后按实际落盘的文件统计（不拿内存里的行数当字节数）
        touched = {tuple(hive_value(r.get(k)) for k in partition_by) for r in rows}
        n_files = 0
        n_bytes = 0
        for values in touched:
            d = self._base(layer, table)
            for key, val in zip(partition_by, values):
                d = d / f"{key}={val}"
            if d.exists():
                for f in d.glob("*.parquet"):
                    n_files += 1
                    n_bytes += f.stat().st_size
        return {
            "layer": layer,
            "table": table or "*",
            "n_rows": len(rows),
            "n_files": n_files,
            "bytes": n_bytes,
            "partitions": sorted("|".join(v) for v in touched),
        }

    # -- 读取 ---------------------------------------------------------------

    def scan_sql(
        self,
        layer: str,
        *,
        table: str = "",
        dataset: str = "",
        event_date: str = "",
        where: str = "",
        columns: str = "*",
    ) -> str:
        """生成读取该层的一段 SQL。

        `hive_partitioning=true` 让 `dataset` / `event_date` 自动成为列，
        因此下面的 `WHERE` 就是**分区裁剪**，不是全表过滤。
        """
        glob = self.parquet_glob(layer, table)
        preds = []
        if dataset:
            preds.append(f"dataset = '{dataset}'")
        if event_date:
            preds.append(f"event_date = '{event_date}'")
        if where:
            preds.append(f"({where})")
        clause = f" WHERE {' AND '.join(preds)}" if preds else ""
        return f"SELECT {columns} FROM read_parquet('{glob}', hive_partitioning=true){clause}"

    def exists(self, layer: str, table: str = "") -> bool:
        return bool(self.files(layer, table))

    def prune_report(
        self, layer: str, dataset: str, event_date: str, *, table: str = ""
    ) -> dict[str, Any]:
        """分区裁剪的**可证伪证据**：命中字节 vs 全层字节。

        只报"扫了几个文件"是不够的——要证明的是"少扫了多少字节"，
        因为代价由字节数决定，不由文件数决定。
        """
        hit = self.stats(layer, table, dataset, event_date)
        total = self.stats(layer, table)
        # ⚠️ **0 命中不许报成「少扫 100%」**。
        #
        # 原式 `1 - matched/layer_bytes` 在 `matched == 0` 时给出 `1.0`，
        # 也就是「这个过滤条件什么都没匹配到」被渲染成一个**极漂亮的性能数字**。
        # 而 0 命中在真实使用里几乎总是**参数写错**（把表名当 dataset 传、
        # 或事件日不存在）——恰恰是最该报错的时候，却给了最好的读数。
        # 这与项目一贯的口径同源：**没有分母 / 没有命中时返回 `None`，
        # 不返回一个好看的数**（同 `obs.pipeline_health` 的分母为 0 → None）。
        n_matched = hit["n_files"]
        if n_matched == 0:
            ratio = None
            hint = (
                "没有匹配到任何分区：检查 dataset / event_date / table 是否写对。"
                "「少扫 100%」是一个误导性读数，故此处不给裁剪率。"
            )
        else:
            ratio = round(1 - hit["bytes"] / total["bytes"], 6) if total["bytes"] else None
            hint = ""
        return {
            "layer": layer,
            "table": table or "*",
            "dataset": dataset,
            "event_date": event_date,
            "matched_bytes": hit["bytes"],
            "matched_files": n_matched,
            "layer_bytes": total["bytes"],
            "layer_files": total["n_files"],
            "prune_ratio": ratio,
            "hint": hint,
        }


def default_lake(repo_root: str | Path) -> Lake:
    return Lake(Path(repo_root) / "data" / "lake")


def total_bytes(paths: Iterable[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.exists())
