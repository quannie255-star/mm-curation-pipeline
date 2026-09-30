"""容器冒烟的数据准备**必须本地可验**：造数 → 跑链路 → SUCCESS。

`scripts/ci_seed_sources.py` 存在的理由见它自己的文档：CI 的 runner 上
`data/raw/**` 是空的（真数据不入库），所以容器冒烟得先自己造源。
造数逻辑如果内联在 workflow YAML 里，就只能"在 CI 上跑过才知道对不对"，
而它的失败会表现成"容器没起来"——指不到原因。

所以这里把那段逻辑拉到本地跑一遍：**这一步绿了，容器冒烟剩下的未知量
就只有"镜像能不能构建 / 容器能不能起"**，而那两件事本机确实做不到
（拉不到 registry-1.docker.io）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from mm_curation.platform import envs, jobs  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _load_seed_module():
    """按路径加载 `scripts/ci_seed_sources.py`。

    `scripts/` 不是包，所以不能 `import`；用 spec 从文件加载是这里唯一干净的写法
    （也让这个测试跑的就是 CI 会跑的那**一份**文件，没有复制粘贴出来的第二份）。
    """
    path = REPO / "scripts" / "ci_seed_sources.py"
    spec = importlib.util.spec_from_file_location("ci_seed_sources", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_seed_writes_both_sources_into_an_empty_root(tmp_path):
    seed = _load_seed_module()
    written = seed.seed(tmp_path)
    assert set(written) == {
        "raw/real/metropt3/windows.jsonl",
        "raw/news_corpus.jsonl",
        "processed/text_funnel/cleaned.jsonl",
        "processed/text_funnel/dropped.jsonl",
    }
    for rel in written:
        assert (tmp_path / "data" / rel).exists()


def test_seed_is_idempotent_and_does_not_rewrite(tmp_path):
    """幂等是它能在**本机**安全运行的前提（本机已有真源，跑它必须是空操作）。"""
    seed = _load_seed_module()
    seed.seed(tmp_path)
    target = tmp_path / "data" / "raw" / "news_corpus.jsonl"
    before = target.read_bytes()
    target.write_bytes(before + b"\n")
    grown = target.read_bytes()

    assert seed.seed(tmp_path) == [], "第二次不该再写任何文件"
    assert target.read_bytes() == grown, "已存在的文件不许被覆盖"


def test_empty_root_becomes_a_runnable_workspace(tmp_path, monkeypatch):
    """造完数就能跑通整条链路并晋升 —— 这正是 CI 容器冒烟要走的路径。"""
    monkeypatch.chdir(tmp_path)
    seed = _load_seed_module()
    seed.seed(tmp_path)

    res = jobs.run_platform(tmp_path, verbose=False)
    assert res["status"] == "SUCCESS", res
    # 只落地 2 个源 → 其余源必须走 SKIPPED 分支（那条分支因此每次都被覆盖）
    assert res["tasks"]["ods"]["_status"] == "SUCCESS"

    rep = envs.promote(tmp_path, from_env=envs.ENV_DEV, to_env=envs.ENV_PROD)
    assert rep["ok"] is True, rep["reason"]
    assert rep["dest"]["n_files"] > 0, "容器要读的 prod store 不能是空的"


def test_seed_rows_produce_partitioned_event_dates(tmp_path, monkeypatch):
    """事件时间必须真的解析出来——否则分区全落在 `__HIVE_DEFAULT_PARTITION__`，
    而那种数据的契约断言会**空洞地通过**（0 = 0），冒烟就绿得没有意义。"""
    monkeypatch.chdir(tmp_path)
    _load_seed_module().seed(tmp_path)
    res = jobs.run_platform(tmp_path, verbose=False)
    assert res["status"] == "SUCCESS"
    lake = envs.resolve(tmp_path, envs.ENV_DEV).lake_dir
    part_names = {p.name for p in lake.rglob("event_date=*")}
    assert "event_date=2020-04-01" in part_names
    assert "event_date=2026-09-06" in part_names
