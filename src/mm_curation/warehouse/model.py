"""四层数据模型（DuckDB）：raw → stg → marts → metrics。

## 分层与数仓概念的对应

| 层 | 表 | 内容 | 数仓类比 | 别名视图 |
|---|---|---|---|---|
| L0 | `raw_samples` | 原始语料，一字不改 | ODS | `ods_samples` |
| L1 | `stg_samples` / `stg_scores` | 清洗后样本+决策+算子分 | DWD | `dwd_samples` / `dwd_scores` |
| L2 | `marts_*` | 聚合 | DWS | `dws_dataset_profile` 等 |
| L3 | `metrics` | 指标字典定义的核心指标 | ADS | `ads_metrics` |

两套名字都建出来，是为了让 `SELECT * FROM ods_samples` 这种数仓术语也能直接跑
（JD 里写的是 ODS/DWD/DWS/ADS），但**唯一的实现只有一套**——名字多不是模型多。

## 两条纪律（沿用项目既有约定）

1. **JSONL 一律 `read_text().split("\\n")`**，禁用 `splitlines()`（U+2028 陷阱，笔记 #44）。
2. **覆盖率算不出来就是算不出来**：分数缺失的行不进 `stg_scores`，
   L2 的 `score_coverage` 单独成列——不把「没评」当成「评了且通过」。
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Iterator

from .sources import SOURCES, Source, available, resolve

SCHEMA_VERSION = 1

_CJK_LO = 0x4E00
_CJK_HI = 0x9FFF


def count_han(text: str) -> int:
    """汉字计数（CJK 统一表意文字主区；扩展区在本项目语料里可忽略）。"""
    if not text:
        return 0
    return sum(1 for ch in text if _CJK_LO <= ord(ch) <= _CJK_HI)


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """读 JSONL（禁用 splitlines）。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            yield json.loads(line)


def _score_items(meta: dict[str, Any]) -> list[tuple[str, float]]:
    """从 meta 抽取 `score:<op>` 项。非数值项跳过（不伪造分数）。"""
    out: list[tuple[str, float]] = []
    for k, v in meta.items():
        if k.startswith("score:") and isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append((k[len("score:") :], float(v)))
    return out


def _first_op(value: Any) -> str | None:
    """`dropped_by` 的规范：字符串或列表（批量算子多因）。取第一个，保留原值。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value)


def read_samples(root: str | Path, src: Source, *, limit: int = 0) -> list[dict[str, Any]]:
    """把一个数据源读成扁平的行（L0/L1 共用）。"""
    rows: list[dict[str, Any]] = []
    for i, d in enumerate(iter_jsonl(resolve(root, src))):
        if limit and i >= limit:
            break
        text = d.get("text") or ""
        meta = d.get("meta") or {}
        rows.append(
            {
                "dataset": src.name,
                "id": str(d.get("id", "")),
                "modality": d.get("modality") or src.modality or "unknown",
                "text_len": len(text),
                "chars_han": count_han(text),
                "has_image": 1 if d.get("image_path") else 0,
                "is_kept": 1 if src.kind != "dropped" else 0,
                "dropped_by": None if src.kind != "dropped" else _first_op(d.get("dropped_by")),
                "scores": _score_items(meta),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS meta_info (key VARCHAR PRIMARY KEY, value VARCHAR);

CREATE OR REPLACE TABLE raw_samples (
    dataset VARCHAR, id VARCHAR, modality VARCHAR,
    text_len BIGINT, chars_han BIGINT, has_image INTEGER
);

CREATE OR REPLACE TABLE stg_samples (
    dataset VARCHAR, id VARCHAR, modality VARCHAR,
    text_len BIGINT, chars_han BIGINT, has_image INTEGER,
    is_kept INTEGER, dropped_by VARCHAR
);

CREATE OR REPLACE TABLE stg_scores (
    dataset VARCHAR, id VARCHAR, op VARCHAR, score DOUBLE
);

CREATE OR REPLACE TABLE marts_dataset_profile (
    dataset VARCHAR, n_total BIGINT, n_kept BIGINT, n_dropped BIGINT,
    drop_rate DOUBLE, n_ops BIGINT, score_coverage DOUBLE, modalities VARCHAR
);

CREATE OR REPLACE TABLE marts_funnel_stage (
    dataset VARCHAR, op VARCHAR, seq BIGINT, n_scored BIGINT, n_dropped BIGINT,
    drop_rate DOUBLE, score_min DOUBLE, score_p50 DOUBLE, score_max DOUBLE, score_mean DOUBLE
);

CREATE OR REPLACE TABLE marts_modality_quality (
    dataset VARCHAR, modality VARCHAR, n_total BIGINT, n_kept BIGINT,
    drop_rate DOUBLE, avg_len DOUBLE
);

CREATE OR REPLACE TABLE metrics (
    dataset VARCHAR, name VARCHAR, value DOUBLE, denominator DOUBLE, status VARCHAR
);
"""

# 数仓术语别名（只建视图，不复制数据）
_ALIAS_DDL = """
CREATE OR REPLACE VIEW ods_samples      AS SELECT * FROM raw_samples;
CREATE OR REPLACE VIEW dwd_samples      AS SELECT * FROM stg_samples;
CREATE OR REPLACE VIEW dwd_scores       AS SELECT * FROM stg_scores;
CREATE OR REPLACE VIEW dws_dataset_profile  AS SELECT * FROM marts_dataset_profile;
CREATE OR REPLACE VIEW dws_funnel_stage     AS SELECT * FROM marts_funnel_stage;
CREATE OR REPLACE VIEW dws_modality_quality AS SELECT * FROM marts_modality_quality;
CREATE OR REPLACE VIEW ads_metrics      AS SELECT * FROM metrics;
"""


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


class Warehouse:
    """数仓句柄：构建 + 查询。

    DuckDB 是**可选依赖**：没装时 `connect()` 抛带安装提示的错，不静默退化
    （静默退化会让「SQL 层能跑」这个 claim 变成假的）。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        try:
            import duckdb
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "数仓层需要 duckdb：`pip install duckdb`"
                "（SQL 层是可选能力，未安装时明确报这个错而不是假装成功）"
            ) from e
        return duckdb.connect(str(self.path))

    # -- 构建 ---------------------------------------------------------------

    def build(self, root: str | Path, *, limit: int = 0) -> dict[str, Any]:
        """从注册表构建全部四层。返回构建报告（含跳过的数据源）。"""
        root = Path(root)
        used: list[str] = []
        skipped: list[str] = []
        raw_rows: list[tuple] = []
        stg_rows: list[tuple] = []
        score_rows: list[tuple] = []

        for src in SOURCES:
            if not available(root, src):
                skipped.append(f"{src.name}:{src.kind}（文件缺失或为空）")
                continue
            rows = read_samples(root, src, limit=limit)
            if not rows:
                skipped.append(f"{src.name}:{src.kind}（0 行）")
                continue
            used.append(f"{src.name}:{src.kind}")
            for r in rows:
                base = (r["dataset"], r["id"], r["modality"],
                        r["text_len"], r["chars_han"], r["has_image"])
                if src.kind == "raw":
                    raw_rows.append(base)
                else:
                    stg_rows.append(base + (r["is_kept"], r["dropped_by"]))
                for op, val in r["scores"]:
                    score_rows.append((r["dataset"], r["id"], op, val))

        con = self.connect()
        con.execute(_DDL)
        self._insert(con, "raw_samples", raw_rows)
        self._insert(con, "stg_samples", stg_rows)
        self._insert(con, "stg_scores", score_rows)
        self._build_marts(con)
        con.execute(_ALIAS_DDL)
        con.execute("INSERT OR REPLACE INTO meta_info VALUES ('schema_version', ?)",
                    [str(SCHEMA_VERSION)])
        con.execute("INSERT OR REPLACE INTO meta_info VALUES ('sources_used', ?)",
                    [",".join(used)])
        con.close()

        return {
            "db": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "layers": {
                "ods": ["raw_samples", "ods_samples"],
                "dwd": ["stg_samples", "stg_scores", "dwd_samples", "dwd_scores"],
                "dws": ["marts_dataset_profile", "marts_funnel_stage", "marts_modality_quality"],
                "ads": ["metrics", "ads_metrics"],
            },
            "sources_used": used,
            "sources_skipped": skipped,
            "n_raw": len(raw_rows),
            "n_stg": len(stg_rows),
            "n_scores": len(score_rows),
        }

    @staticmethod
    def _insert(con, table: str, rows: list[tuple]) -> None:
        if not rows:
            return
        n = len(rows[0])
        con.executemany(f"INSERT INTO {table} VALUES ({','.join(['?'] * n)})", rows)

    def _build_marts(self, con) -> None:
        con.execute(
            """
            INSERT INTO marts_dataset_profile
            SELECT
                s.dataset,
                COUNT(*)                                     AS n_total,
                SUM(s.is_kept)                               AS n_kept,
                COUNT(*) - SUM(s.is_kept)                    AS n_dropped,
                (COUNT(*) - SUM(s.is_kept)) * 1.0 / COUNT(*)  AS drop_rate,
                (SELECT COUNT(DISTINCT op) FROM stg_scores g WHERE g.dataset = s.dataset),
                COALESCE((SELECT COUNT(*) FROM stg_scores g WHERE g.dataset = s.dataset)
                         * 1.0 / NULLIF(COUNT(*) *
                           (SELECT COUNT(DISTINCT op) FROM stg_scores g2
                            WHERE g2.dataset = s.dataset), 0),
                         0.0),
                STRING_AGG(DISTINCT s.modality, '|')
            FROM stg_samples s
            GROUP BY s.dataset
            """
        )
        con.execute(
            """
            INSERT INTO marts_funnel_stage
            WITH ops AS (
                SELECT DISTINCT dataset, op FROM (
                    SELECT dataset, op FROM stg_scores
                    UNION ALL
                    SELECT dataset, dropped_by AS op FROM stg_samples WHERE dropped_by IS NOT NULL
                )
            ),
            -- 打分侧与丢弃侧分别聚合再左连接：相关子查询写不长，且同类逻辑重复五遍
            sc AS (
                SELECT dataset, op, COUNT(*) AS n FROM stg_scores GROUP BY 1, 2
            ),
            dp AS (
                SELECT dataset, dropped_by AS op, COUNT(*) AS n
                FROM stg_samples WHERE dropped_by IS NOT NULL GROUP BY 1, 2
            )
            SELECT
                o.dataset, o.op,
                -- 展示序号，不是真实漏斗级序（级序只在运行时存在，判决书里才有权威 seq）
                ROW_NUMBER() OVER (PARTITION BY o.dataset ORDER BY o.op),
                COALESCE(sc.n, 0),
                COALESCE(dp.n, 0),
                -- 没打分 = 没有分母 → 判脏率记 NULL 而不是 0（与记分卡同一口径）
                CASE WHEN COALESCE(sc.n, 0) = 0 THEN NULL
                     ELSE COALESCE(dp.n, 0) * 1.0 / sc.n END,
                NULL, NULL, NULL, NULL
            FROM ops o
            LEFT JOIN sc ON sc.dataset = o.dataset AND sc.op = o.op
            LEFT JOIN dp ON dp.dataset = o.dataset AND dp.op = o.op
            """
        )
        rows = con.execute(
            "SELECT dataset, op, score FROM stg_scores ORDER BY dataset, op"
        ).fetchall()
        agg: dict[tuple[str, str], list[float]] = {}
        for ds, op, sc in rows:
            if sc is not None:
                agg.setdefault((ds, op), []).append(float(sc))
        for (ds, op), vals in agg.items():
            con.execute(
                """UPDATE marts_funnel_stage
                   SET score_min=?, score_p50=?, score_max=?, score_mean=?
                   WHERE dataset=? AND op=?""",
                [min(vals), _pct(vals, 0.5), max(vals),
                 statistics.fmean(vals) if vals else None, ds, op],
            )
        con.execute(
            """
            INSERT INTO marts_modality_quality
            SELECT dataset, modality, COUNT(*), SUM(is_kept),
                   (COUNT(*) - SUM(is_kept)) * 1.0 / COUNT(*), AVG(text_len)
            FROM stg_samples GROUP BY dataset, modality
            """
        )

    # -- 查询 ---------------------------------------------------------------

    def query(self, sql: str) -> tuple[list[str], list[tuple]]:
        con = self.connect()
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            return cols, cur.fetchall()
        finally:
            con.close()

    def tables(self) -> list[str]:
        _, rows = self.query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        )
        return [r[0] for r in rows]


def build_warehouse(root: str | Path, out: str | Path, *, limit: int = 0) -> dict[str, Any]:
    return Warehouse(out).build(root, limit=limit)
