"""embedding 模块：Chinese-CLIP 编码（检索索引与语义去重复用）。"""

from .clip_encoder import ClipEncoder, get_encoder

__all__ = ["ClipEncoder", "get_encoder"]
