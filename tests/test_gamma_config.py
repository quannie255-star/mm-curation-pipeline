"""γ 阶段配置层测试：runtime 键（local|ray）解析与执行器选择。

Ray 执行器本身的行为等价性在 packages/curation-eval/tests/test_ray_executor.py
（importorskip 守卫）；这里只测配置校验与本地路线，不启动 ray 集群。
"""

from __future__ import annotations

import pytest
import yaml

from mm_curation.pipeline.config import PipelineConfig
from mm_curation.pipeline.runner import get_executor


def _write_config(tmp_path, runtime):
    cfg = {
        "name": "gamma_rt",
        "dataset": {"raw_jsonl": "data/raw/x.jsonl"},
        "output": {"dir": "data/processed/x"},
        "operators": [{"op": "doc_length", "params": {"min": 10}}],
    }
    if runtime is not None:
        cfg["runtime"] = runtime
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return PipelineConfig.from_yaml(path)


def test_runtime_default_local(tmp_path):
    assert _write_config(tmp_path, None).runtime == "local"


def test_runtime_ray_accepted(tmp_path):
    assert _write_config(tmp_path, "ray").runtime == "ray"


def test_runtime_unknown_rejected(tmp_path):
    with pytest.raises(ValueError, match="runtime"):
        _write_config(tmp_path, "dask")


def test_get_executor_local():
    from curation_eval import LocalSequentialExecutor

    assert isinstance(get_executor("local"), LocalSequentialExecutor)
