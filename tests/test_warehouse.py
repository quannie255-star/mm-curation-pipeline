"""数仓层（DuckDB 四层模型）与指标字典的测试。

duckdb 是**可选依赖**：没装时整文件跳过（与 `test_ray_executor.py` 同一约定）。
这意味着 CI 上的测试总数会少——**基线数字要按有/无 duckdb 分别记**。
"""

from __future__ import annotations

import json

import pytest

duckdb = pytest.importorskip("duckdb")

from mm_curation.warehouse import metrics as M  # noqa: E402
from mm_curation.warehouse import model as W  # noqa: E402
from mm_curation.warehouse.sources import Source  # noqa: E402

# ---------------------------------------------------------------------------
# 造一份最小语料：cleaned 3 条 + dropped 1 条，带算子分
# ---------------------------------------------------------------------------


def _write(root, rel, rows):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    return p


def _sample(i, *, text="中文测试内容", scores=None, dropped_by=None):
    d = {"id": f"id{i}", "text": text, "meta": {}}
    for op, v in (scores or {}).items():
        d["meta"][f"score:{op}"] = v
    if dropped_by:
        d["dropped_by"] = dropped_by
    return d


@pytest.fixture()
def mini_root(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _write(
        root,
        "data/processed/t/cleaned.jsonl",
        [
            _sample(1, scores={"doc_length": 0.9, "perplexity": 0.8}),
            _sample(2, scores={"doc_length": 0.7}),
            _sample(3, scores={"perplexity": 0.4}),
        ],
    )
    _write(
        root,
        "data/processed/t/dropped.jsonl",
        [
            _sample(4, scores={"doc_length": 0.1}, dropped_by="doc_length"),
        ],
    )
    monkeypatch.setattr(
        W,
        "SOURCES",
        (
            Source(
                name="t",
                kind="cleaned",
                path="data/processed/t/cleaned.jsonl",
                run="t",
                modality="text_article",
            ),
            Source(
                name="t",
                kind="dropped",
                path="data/processed/t/dropped.jsonl",
                run="t",
                modality="text_article",
            ),
        ),
    )
    return root


def test_build_four_layers(mini_root, tmp_path):
    wh = W.Warehouse(tmp_path / "w.duckdb")
    rep = wh.build(mini_root)
    assert rep["n_stg"] == 4  # cleaned 3 + dropped 1
    assert rep["sources_skipped"] == []
    con = wh.connect()
    n_total, n_kept, n_dropped = con.execute(
        "SELECT n_total, n_kept, n_dropped FROM marts_dataset_profile"
    ).fetchone()
    assert (n_total, n_kept, n_dropped) == (4, 3, 1)

    # 两套命名都可用（数仓术语 ODS/DWD 与项目术语 raw/stg），实现只有一套
    assert con.execute("SELECT COUNT(*) FROM ods_samples").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM dwd_samples").fetchone()[0] == 4


def test_build_with_no_sources_reports_zero_not_fake_success(tmp_path, monkeypatch):
    """数据产物不入库 → 新克隆的仓库所有源都缺失。

    这时 `build` 必须如实报 `sources_skipped` 全量、行数为 0；
    **不能**把 0 行当成成功（下游会给出「口径返回空行」这种指向错误方向的报错）。
    """
    monkeypatch.setattr(
        W,
        "SOURCES",
        (Source(name="nope", kind="cleaned", path="does/not/exist.jsonl", run="nope"),),
    )
    wh = W.Warehouse(tmp_path / "empty.duckdb")
    rep = wh.build(tmp_path)
    assert rep["n_stg"] == 0 and rep["n_raw"] == 0
    assert rep["sources_used"] == []
    assert len(rep["sources_skipped"]) == 1
    assert "缺失或为空" in rep["sources_skipped"][0]

    con = wh.connect()
    # 空库上指标口径必然跑挂——这正是"必须显式失败"的场景
    spec = M.MetricSpec.from_dict(
        {
            "name": "m",
            "definition": "d",
            "denominator": "x",
            "sql": "SELECT dataset, 1.0 AS value, 10 AS denominator FROM marts_dataset_profile",
        }
    )
    rep2 = M.verify(con, [spec], None)
    assert rep2.errors, "空库上口径应报「返回空行」"
    assert not rep2.ok


def test_score_coverage_is_a_column_not_a_guess(mini_root, tmp_path):
    """4 条样本 × 2 个算子 = 理论 8 个分数，实际只有 5 个 → 覆盖率 0.625。

    （5 = id1 的 doc_length+perplexity、id2 的 doc_length、id3 的 perplexity、
      id4 的 doc_length。）覆盖率不达 100% 时必须如实报出来，
    不能用「缺失即通过」把它抹平。
    """
    wh = W.Warehouse(tmp_path / "w.duckdb")
    wh.build(mini_root)
    con = wh.connect()
    cov = con.execute(
        "SELECT score_coverage FROM marts_dataset_profile WHERE dataset='t'"
    ).fetchone()[0]
    assert abs(cov - 5 / 8) < 1e-9


def test_batch_dedup_op_drops_without_scoring(tmp_path, monkeypatch):
    """笔记 #78：只丢不打分的算子不能算「没评过」。

    md5_exact 一条分数都没留，但实丢 2 条——只认分数会把它判成空转，
    进而让 dedup_contribution 恒为 0、uniqueness 维度恒为 NOT_EVALUATED。
    """
    root = tmp_path / "repo"
    _write(root, "p/cleaned.jsonl", [_sample(1, scores={"doc_length": 0.9})])
    _write(
        root,
        "p/dropped.jsonl",
        [
            _sample(2, dropped_by="md5_exact"),
            _sample(3, dropped_by="md5_exact"),
        ],
    )
    monkeypatch.setattr(
        W,
        "SOURCES",
        (
            Source(name="p", kind="cleaned", path="p/cleaned.jsonl", run="p", modality="text"),
            Source(name="p", kind="dropped", path="p/dropped.jsonl", run="p", modality="text"),
        ),
    )
    wh = W.Warehouse(tmp_path / "w2.duckdb")
    wh.build(root)
    con = wh.connect()
    ns, nd = con.execute(
        "SELECT n_scored, n_dropped FROM marts_funnel_stage WHERE op='md5_exact'"
    ).fetchone()
    assert (ns, nd) == (0, 2)  # 只丢不打分——这是它的正常工作方式


# ---------------------------------------------------------------------------
# 指标字典
# ---------------------------------------------------------------------------

SPEC = {
    "name": "m",
    "definition": "测试口径",
    "denominator": "样本数",
    "sql": "SELECT 'ds' AS dataset, 'd' AS dim, 0.5 AS value, 10 AS denominator",
}


def test_evaluate_parses_by_column_name():
    """口径按**列名**解析，不按位置：加一列不该让分母和值对调。"""
    con = duckdb.connect()
    spec = M.MetricSpec.from_dict(SPEC)
    res = M.evaluate(con, spec)
    assert len(res) == 1
    r = res[0]
    assert (r.dataset, r.dim, r.value, r.denominator) == ("ds", "d", 0.5, 10.0)
    assert r.ok is None  # 无基线 → 不判漂移，也不假装通过


def test_baseline_key_includes_dataset_and_dim():
    """同名指标在不同数据集/维度上的基线不能互相覆盖。"""
    a = M.MetricResult("m", 1.0, 1, None, None, None, dataset="ds", dim="a")
    b = M.MetricResult("m", 1.0, 1, None, None, None, dataset="ds", dim="b")
    assert a.key != b.key


def test_freeze_then_verify_detects_drift(tmp_path, monkeypatch):
    con = duckdb.connect()
    spec = M.MetricSpec.from_dict(SPEC)
    payload = M.freeze(con, [spec], tmp_path / "b.json")
    assert payload["n_baselines"] == 1
    assert M.verify(con, [spec], M.load_baselines(tmp_path / "b.json")).ok

    # 口径没变、数值变了 → 必须被抓到
    drift = M.MetricSpec.from_dict(
        {
            **SPEC,
            "sql": "SELECT 'ds' AS dataset, 'd' AS dim, 0.9 AS value, 10 AS denominator",
        }
    )
    rep = M.verify(con, [drift], M.load_baselines(tmp_path / "b.json"))
    assert not rep.ok
    assert len(rep.failures) == 1


def test_broken_sql_becomes_error_not_crash():
    """口径跑挂要变成结果里的 error，不能让整条 CI 崩在一个错字上。"""
    con = duckdb.connect()
    spec = M.MetricSpec.from_dict({**SPEC, "sql": "SELECT * FROM 不存在的表"})
    res = M.evaluate(con, spec)
    assert len(res) == 1 and res[0].error
    assert res[0].value is None


def test_verify_fails_when_specs_cannot_run(tmp_path):
    """口径跑挂 = 门禁不成立（不是「没基线所以跳过」）。

    空仓库上 7 条口径会有多条跑挂。若 `ok` 只看 `ok is not False`
    （跑挂的行 ok=None），`--verify` 会返回 0 ——
    **一个什么都没验的成功**，和 format 门禁挂掉却显示 CI 绿是同一类失效（笔记 #80）。
    """
    con = duckdb.connect()
    good = M.MetricSpec.from_dict(SPEC)
    broken = M.MetricSpec.from_dict({**SPEC, "name": "bad", "sql": "SELECT * FROM 不存在的表"})
    rep = M.verify(con, [good, broken], None)
    assert rep.errors, "跑挂的口径必须体现在 errors 里"
    assert not rep.ok, "口径跑挂时 ok 必须为 False，否则 --verify 会假绿"
    assert M.verify(con, [good], None).ok, "全部跑成时才允许 ok=True"


def test_freeze_skips_broken_specs_instead_of_writing_none(
    tmp_path,
):
    """冻结基线时跑挂的口径不写进基线（写进去就是给自己埋一个无意义的基线）。"""
    con = duckdb.connect()
    good = M.MetricSpec.from_dict(SPEC)
    broken = M.MetricSpec.from_dict({**SPEC, "name": "bad", "sql": "SELECT * FROM 不存在的表"})
    payload = M.freeze(con, [good, broken], tmp_path / "b.json")
    assert payload["n_baselines"] == 1
    assert all(v is not None for v in payload["baselines"].values())


def test_spec_requires_definition_and_denominator():
    """少 definition 或 denominator 的指标不算定义完成——直接拒收。"""
    with pytest.raises(ValueError):
        M.MetricSpec.from_dict({"name": "x", "sql": "SELECT 1 AS value"})
