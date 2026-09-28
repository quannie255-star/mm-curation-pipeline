"""verify_claims 单测：指针解析 / 漂移判定 / 指纹校验 / claims 注册表完整性。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "scripts",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from verify_claims import check_claim, check_fingerprints, resolve  # noqa: E402


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
    """注册表完整性：每条非历史 claim 的指针必须在当前报告里可解析。"""
    registry = json.loads((_ROOT / "docs" / "claims.json").read_text(encoding="utf-8"))
    for claim in registry["claims"]:
        if claim.get("comparator") == "historical" or not claim.get("file"):
            continue
        r = check_claim(claim, repo=_ROOT)
        assert r["status"] != "pointer-broken", f"{claim['id']} 指针断裂"
        assert r["status"] != "missing", f"{claim['id']} 报告缺失：{claim['file']}"
