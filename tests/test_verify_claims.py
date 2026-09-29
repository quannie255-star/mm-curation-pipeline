"""verify_claims 单测：指针解析 / 漂移判定 / 指纹校验 / claims 注册表完整性。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "scripts",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from verify_claims import (  # noqa: E402
    check_claim,
    check_facade,
    check_facades,
    check_fingerprints,
    count_literal,
    render_value,
    resolve,
)


def test_resolve_dot_and_index_paths():
    doc = {"a": {"b": [10, 20]}, "c": 1}
    assert resolve(doc, "a.b.1") == 20
    assert resolve(doc, "a.b[0]") == 10
    assert resolve(doc, "c") == 1


def test_check_claim_pass_and_drift(tmp_path):
    f = tmp_path / "report.json"
    f.write_text(json.dumps({"gate": {"recall": 0.98}}), encoding="utf-8")
    ok = check_claim(
        {
            "id": "x",
            "file": "report.json",
            "pointer": "gate.recall",
            "expected": 0.98,
            "tol": 0.001,
            "comparator": "approx",
        },
        repo=tmp_path,
    )
    assert ok["status"] == "pass"
    drift = check_claim(
        {
            "id": "x",
            "file": "report.json",
            "pointer": "gate.recall",
            "expected": 0.90,
            "tol": 0.001,
            "comparator": "approx",
        },
        repo=tmp_path,
    )
    assert drift["status"] == "drift" and drift["current"] == 0.98


def test_check_claim_missing_and_historical(tmp_path):
    missing = check_claim(
        {
            "id": "x",
            "file": "nope.json",
            "pointer": "a.b",
            "expected": 1,
            "tol": 0,
            "comparator": "approx",
        },
        repo=tmp_path,
    )
    assert missing["status"] == "missing"
    hist = check_claim(
        {"id": "h", "comparator": "historical", "file": None, "pointer": None, "expected": -0.017},
        repo=tmp_path,
    )
    assert hist["status"] == "historical"


def test_fingerprint_drift_detection(tmp_path):
    f = tmp_path / "data.jsonl"
    f.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    import hashlib

    md5 = hashlib.md5(f.read_bytes()).hexdigest()
    registry = {
        "meta": {
            "data_fingerprints": {
                "data.jsonl": {"md5": md5, "rows": 2},
            }
        }
    }
    assert check_fingerprints(registry, repo=tmp_path)[0]["status"] == "pass"
    registry["meta"]["data_fingerprints"]["data.jsonl"]["rows"] = 3
    assert check_fingerprints(registry, repo=tmp_path)[0]["status"] == "drift"


def test_registry_all_pointers_resolve_in_repo():
    """注册表完整性——**必须能在 CI 上跑**（CI 没有 `data/reports/`，它是生成物、已忽略）。

    旧版直接断言「报告必须存在」，于是这个测试**只在跑过生成步骤的机器上绿**、在任何
    新克隆/CI 上红（ENGINEERING_NOTES #92）。改为：①字段完整性（与报告在盘无关，CI 可查）；
    ②报告**在盘**时才校验指针可解析（否则 skip 该条——缺失是「没生成」不是「数字错」）。
    """
    registry = json.loads((_ROOT / "docs" / "claims.json").read_text(encoding="utf-8"))
    n_with_report = 0
    for claim in registry["claims"]:
        if claim.get("comparator") == "historical" or not claim.get("file"):
            continue
        for key in ("file", "pointer", "expected", "comparator"):
            assert claim.get(key) is not None, f"{claim['id']} 缺字段 {key}"
        if not (_ROOT / claim["file"]).exists():
            continue  # 生成报告未落盘（CI/新克隆）——不是数字错，跳过指针校验
        r = check_claim(claim, repo=_ROOT)
        assert r["status"] != "pointer-broken", f"{claim['id']} 指针断裂"
        n_with_report += 1
    # 本机有报告时至少要真的校验到一条，避免「全部跳过」式假绿
    if any((_ROOT / c["file"]).exists() for c in registry["claims"] if c.get("file")):
        assert n_with_report >= 1


# ---------------------------------------------------------------------------
# 门面（facades）：文档数字 ↔ 来源一致性（M2：把「人工对齐」变成「CI 拦截」）
# ---------------------------------------------------------------------------


def test_count_literal_boundaries():
    """字面量统计必须按独立 token 计数（前后不能还是数字 / 小数点）。"""
    assert count_literal("a 0.556 b 0.556", "0.556") == 2
    assert count_literal("噪声 10.556", "0.556") == 0  # 前一位是数字
    assert count_literal("0.5561", "0.556") == 0  # 后一位是数字
    assert count_literal("267 / 670", "67") == 0
    assert count_literal("616 + 67 = 683", "616") == 1
    assert count_literal("616 + 67 = 683", "67") == 1


def test_render_value():
    assert render_value(0.555836, ".3f") == "0.556"
    assert render_value(0.0123, ".2f", 100, "%") == "1.23%"
    assert render_value(683, "d") == "683"


def _mini_registry():
    return {
        "baselines": {"tests_total": 683},
        "claims": [{"id": "c", "expected": 0.555836}],
        "facades": [],
    }


def test_check_facade_pass_registry_stale_doc_stale(tmp_path):
    (tmp_path / "d.md").write_text("结果 0.556 提升", encoding="utf-8")
    reg = _mini_registry()
    base = {
        "id": "f",
        "doc": "d.md",
        "source": {"claim": "c"},
        "fmt": ".3f",
        "scale": 1,
        "suffix": "",
        "literal": "0.556",
    }
    assert check_facade(base, reg, repo=tmp_path)["status"] == "pass"
    stale_reg = check_facade({**base, "literal": "0.600"}, reg, repo=tmp_path)
    assert stale_reg["status"] == "registry-stale"
    # 文档里写成 10.556：边界不该把它当成 0.556 -> doc-stale
    (tmp_path / "d.md").write_text("结果 10.556 提升", encoding="utf-8")
    assert check_facade(base, reg, repo=tmp_path)["status"] == "doc-stale"


def test_check_facade_source_missing(tmp_path):
    reg = _mini_registry()
    fac = {"id": "f", "doc": "d.md", "source": {"claim": "nope"}, "fmt": ".3f", "literal": "0.556"}
    assert check_facade(fac, reg, repo=tmp_path)["status"] == "source-missing"


def test_real_registry_facades_all_pass_and_mutation_goes_red():
    """集成 + 变异测试：真实 claims.json 的门面必须全绿；把来源改坏必须变红。"""
    registry = json.loads((_ROOT / "docs" / "claims.json").read_text(encoding="utf-8"))
    assert registry.get("facades"), "claims.json 缺少 facades 注册（M2）"
    bad = [f["id"] for f in check_facades(registry, repo=_ROOT) if f["status"] != "pass"]
    assert not bad, f"门面未通过：{bad}"

    # 变异①：把目标门面引用的来源值改掉 -> 该门面必须 registry-stale（必须红）
    mut = json.loads(json.dumps(registry))
    target = mut["facades"][0]
    cid = target["source"].get("claim")
    if cid is not None:
        for c in mut["claims"]:
            if c["id"] == cid:
                c["expected"] = 999.0
    else:
        mut["baselines"][target["source"]["baseline"]] = 999
    st = {f["id"]: f["status"] for f in check_facades(mut, repo=_ROOT)}
    assert st[target["id"]] == "registry-stale"
