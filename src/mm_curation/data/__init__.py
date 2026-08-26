"""数据获取层：多源下载、镜像适配、格式统一。

设计动机（docs/JD_RESEARCH.md 差距 #5）：国内环境下 HuggingFace 直连超时、
COCO 官方源 DNS 不通是常态，下载链路必须内建「镜像端点 + UA + 重试 + 断点续传」，
这不是锦上添花，而是数据岗位每天面对的真实工程问题。
"""

from .download import download_seed_dataset
from .sources import CocoCnAnnotations, parse_coco_cn_tar

__all__ = ["CocoCnAnnotations", "parse_coco_cn_tar", "download_seed_dataset"]
