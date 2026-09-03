"""阈值回归门（ε2）测试：基线匹配绿 / 漂移注入红 / 基线结构变化红。

用小规模语料（scale=0.1）走真扫描逻辑，不 mock 算子——门禁的价值
就在于锁真实行为，测试同理。图像依赖 imagehash、文本依赖 datasketch，
缺失即 skip（与 test_image_ray_equivalence 同守卫套路）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("datasketch")
pytest.importorskip("imagehash")

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "threshold_regression_gate", _ROOT / "scripts" / "threshold_regression_gate.py"
)
gate = importlib.util.module_from_spec(_spec)
sys.modules["threshold_regression_gate"] = gate
_spec.loader.exec_module(gate)

SCALE = 0.1


@pytest.fixture(scope="module")
def curves(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("thr_gate_corpus")
    return gate.scan_curves(SCALE, tmp)


def _baseline_from(curves: dict) -> dict:
    return {
        "meta": {"note": "test fixture"},
        "curves": {op: [dict(p) for p in pts] for op, pts in curves.items()},
    }


def test_gate_passes_on_matching_baseline(curves):
    problems = gate.compare(_baseline_from(curves), curves)
    assert problems == []


def test_gate_fails_on_recall_drift(curves):
    baseline = _baseline_from(curves)
    # 注入漂移：任取一点把主靶 recall 拉低 0.2（模拟上游实现劣化）
    op = next(iter(baseline["curves"]))
    pts = baseline["curves"][op]
    pts[0]["primary_recall"] = round((pts[0]["primary_recall"] or 0) - 0.2, 4)
    problems = gate.compare(baseline, curves)
    assert problems, "recall 漂移 0.2 必须被抓住"
    assert any("primary_recall" in p for p in problems)


def test_gate_fails_on_kill_rate_drift(curves):
    baseline = _baseline_from(curves)
    op = next(iter(baseline["curves"]))
    pts = baseline["curves"][op]
    pts[-1]["clean_kill_rate"] = round((pts[-1]["clean_kill_rate"] or 0) + 0.1, 4)
    problems = gate.compare(baseline, curves)
    assert any("clean_kill_rate" in p for p in problems)


def test_gate_fails_on_missing_threshold_point(curves):
    baseline = _baseline_from(curves)
    op = next(iter(baseline["curves"]))
    baseline["curves"][op] = baseline["curves"][op][1:]  # 丢一个阈值点
    problems = gate.compare(baseline, curves)
    assert any("不在基线中" in p for p in problems)


def test_corpus_labels_are_wellformed(tmp_path):
    """语料契约：干净样本无 labels，注入样本带 dirty 标——recall/误杀口径的前提。"""
    texts = gate.build_text_corpus(10, 4)
    clean = [s for s in texts if not s.labels]
    dirty = [s for s in texts if s.labels]
    assert len(clean) == 10 and len(dirty) == 4
    assert all(s.labels["dirty"] == "near_duplicate_text" for s in dirty)
    imgs = gate.build_image_corpus(6, 3, tmp_path)
    assert all(Path(s.image_path).exists() for s in imgs)
    assert all(s.labels["dirty"] == "near_duplicate_image" for s in imgs if s.labels)
