"""训练/分析可直接消费的数据集制品生产（训练数据生产系统的产物层）。

两层：
- **构建层**（`build.py`）：JSONL 清洗产物 → tokenize → 切分 → 泄漏检查
  → packing → Parquet shard + manifest。
- **配方层**（`train_recipe.py`）：manifest → 训练配置 → 真训一次 → 结果。
  配方层存在的理由是「数据集必须对**值不值得训**负责」——
  只报「保留率 97.67%」的是清洗系统，报「用 recipe R 训出 val loss P」
  的才是训练数据生产系统。
"""

from .build import (
    SCHEMA_VERSION,
    DatasetBuilder,
    DatasetManifest,
    assign_split,
    estimate_jaccard,
    iter_jsonl,
    minhash_signature,
    pack_sequences,
    text_sha,
)
from .train_recipe import (
    RecipeResult,
    TinyCausalLM,
    TrainConfig,
    logits_bytes,
    run_recipe,
    train_config_from_manifest,
)

# ⚠️ `__all__` **只能有一份**。踩过：写了两份，后者覆盖前者，
# 上面那份里的名字在 `from ... import *` 时静默消失（不报错）。
__all__ = [
    # 构建层
    "SCHEMA_VERSION",
    "DatasetBuilder",
    "DatasetManifest",
    "assign_split",
    "estimate_jaccard",
    "iter_jsonl",
    "minhash_signature",
    "pack_sequences",
    "text_sha",
    # 训练配方层
    "RecipeResult",
    "TinyCausalLM",
    "TrainConfig",
    "logits_bytes",
    "run_recipe",
    "train_config_from_manifest",
]
