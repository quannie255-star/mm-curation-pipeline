"""COCO-CN 标注包的解析。

COCO-CN（AIMLab 人大，2022）是 COCO train2014 子集的人工中文标注数据集：
- 20,341 张图（train 18,341 / val 1,000 / test 1,000），全部来自 COCO train2014
- imageid.human-written-caption.txt：人工撰写中文 caption（\t 分隔，可一图多句）
- imageid.human-written-tags.txt：人工标注中文标签（空格分隔）
原始发布页：https://github.com/xingyaowu/COCO-CN；此处用的 HF 镜像副本：
https://hf-mirror.com/datasets/AIMClab-RUC/COCO-CN
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

COCO_CN_TAR_URL = (
    "https://hf-mirror.com/datasets/AIMClab-RUC/COCO-CN/resolve/main/coco-cn-version1805v1.1.tar.gz"
)

# 标注文件名 -> 解析后的字段名
_CAPTION_FILE = "imageid.human-written-caption.txt"
_TAGS_FILE = "imageid.human-written-tags.txt"
_SPLIT_FILES = {
    "train": "coco-cn_train.txt",
    "val": "coco-cn_val.txt",
    "test": "coco-cn_test.txt",
}


@dataclass
class CocoCnAnnotations:
    """按图像文件名索引的 COCO-CN 标注。

    key 形如 "COCO_train2014_000000296735"（与 COCO 官方文件名去掉 .jpg 一致），
    方便与图像 parquet 里的 path/image_id 做 join。
    """

    captions: dict[str, list[str]] = field(default_factory=dict)
    tags: dict[str, list[str]] = field(default_factory=dict)
    splits: dict[str, str] = field(default_factory=dict)  # 文件名 -> train/val/test

    def __len__(self) -> int:
        return len(self.splits)

    def image_names(self) -> list[str]:
        return sorted(self.splits)

    def merge(self, other: "CocoCnAnnotations") -> None:
        self.captions.update(other.captions)
        self.tags.update(other.tags)
        self.splits.update(other.splits)


def parse_coco_cn_tar(tar_path: str | Path) -> CocoCnAnnotations:
    """解析 COCO-CN tar.gz（跳过 macOS 的 ._ 资源文件）。"""
    ann = CocoCnAnnotations()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = Path(member.name).name
            if name.startswith("._"):  # macOS AppleDouble 资源文件（二进制，非 UTF-8）
                continue
            known = name in (_CAPTION_FILE, _TAGS_FILE) or name in _SPLIT_FILES.values()
            if not known:
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            text = io.TextIOWrapper(f, encoding="utf-8").read()
            if name == _CAPTION_FILE:
                for line in text.splitlines():
                    if "\t" not in line:
                        continue
                    img, caption = line.split("\t", 1)
                    img = img.split("#")[0]
                    ann.captions.setdefault(img, []).append(caption.strip())
            elif name == _TAGS_FILE:
                for line in text.splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        ann.tags[parts[0]] = parts[1:]
            elif name in _SPLIT_FILES.values():
                split = {v: k for k, v in _SPLIT_FILES.items()}[name]
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        ann.splits[line] = split
    if not ann.splits:
        raise ValueError(f"{tar_path} 中未找到 split 文件，包可能损坏")
    return ann
