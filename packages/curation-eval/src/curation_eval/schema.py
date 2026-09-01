"""数据质量协议的最小处理单元：Sample（V2 泛化版）。

结构不变量（构造期校验，只管结构不管质量——质量是算子的职责）：
- id 非空；modality 必须在 MODALITY_FIELDS 登记
- image_path 与 modality 一致性：有图自动推断为 image_caption（免漏标）；
  显式声明 image_caption 但无图 → ValueError
- text 允许为空串：空文本正是文本质量算子要抓的对象

序列化：to_dict 只写新键（text）；from_dict 永久兼容 v1 落盘的 caption 键，
并静默丢弃未知键（如 dropped.jsonl 的 dropped_by）——与 v1 容错行为一致。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from dataclasses import fields as dc_fields
from typing import Any

# 模态 → 该模态样本必然可用的字段。开放扩展：新模态在此登记。
MODALITY_FIELDS: dict[str, frozenset[str]] = {
    "image_caption": frozenset({"text", "image_path"}),
    "text_article": frozenset({"text"}),
}


@dataclass
class Sample:
    id: str
    text: str = ""
    image_path: str | None = None
    modality: str = "text_article"
    meta: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Sample.id 不得为空")
        if self.modality not in MODALITY_FIELDS:
            raise ValueError(f"未知 modality: {self.modality!r}，已知: {sorted(MODALITY_FIELDS)}")
        if self.image_path is not None and self.modality != "image_caption":
            self.modality = "image_caption"  # 有图自动推断，免漏标
        if self.image_path is None and self.modality == "image_caption":
            raise ValueError("modality=image_caption 要求 image_path 非空")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Sample":
        data = dict(d)
        legacy = data.pop("caption", None)
        if legacy is not None and not data.get("text"):
            data["text"] = legacy  # v1 落盘兼容
        known = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
