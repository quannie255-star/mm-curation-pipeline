"""数据下载层测试：COCO-CN 标注解析 + samples 统一输出（离线，合成数据）。"""

from __future__ import annotations

import io
import json
import tarfile

from PIL import Image

from mm_curation.data.download import emit_samples
from mm_curation.data.sources import CocoCnAnnotations, parse_coco_cn_tar


def _make_coco_cn_tar(path):
    """合成 COCO-CN tar.gz（结构照真实包：3 个 split 文件 + caption/tags）。"""
    files = {
        "coco-cn_train.txt": "COCO_train2014_000000000101\nCOCO_train2014_000000000102\n",
        "coco-cn_val.txt": "COCO_train2014_000000000103\n",
        "coco-cn_test.txt": "",
        "imageid.human-written-caption.txt": (
            "COCO_train2014_000000000101#0\t一只猫坐在沙发上\n"
            "COCO_train2014_000000000102#0\t两个人在骑马\n"
            "COCO_train2014_000000000103#0\t城市夜景\n"
            "COCO_train2014_000000000103#1\t夜晚的街道\n"
        ),
        "imageid.human-written-tags.txt": (
            "COCO_train2014_000000000101 猫 沙发\nCOCO_train2014_000000000102 马 骑行\n"
        ),
    }
    with tarfile.open(path, "w:gz") as tf:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"coco-cn-version/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        # macOS AppleDouble 资源文件（二进制非 UTF-8）：解析必须跳过（回归用例）
        junk = b"\x00\xa2\x00\x00\xd4"
        info = tarfile.TarInfo(name="coco-cn-version/._coco-cn_train.txt")
        info.size = len(junk)
        tf.addfile(info, io.BytesIO(junk))
    return path


def test_parse_coco_cn_tar(tmp_path):
    tar = _make_coco_cn_tar(tmp_path / "cn.tar.gz")
    ann = parse_coco_cn_tar(tar)
    assert len(ann) == 3
    assert ann.splits["COCO_train2014_000000000103"] == "val"
    assert ann.captions["COCO_train2014_000000000101"] == ["一只猫坐在沙发上"]
    assert ann.captions["COCO_train2014_000000000103"] == ["城市夜景", "夜晚的街道"]
    assert ann.tags["COCO_train2014_000000000101"] == ["猫", "沙发"]


def test_emit_samples(tmp_path):
    ann = CocoCnAnnotations(
        captions={
            "COCO_train2014_000000000101": ["一只猫坐在沙发上"],
            "COCO_train2014_000000000103": ["城市夜景", "夜晚的街道"],
            "COCO_train2014_000000000999": ["未下载的图"],
        },
        tags={"COCO_train2014_000000000101": ["猫"]},
        splits={
            "COCO_train2014_000000000101": "train",
            "COCO_train2014_000000000103": "val",
            "COCO_train2014_000000000999": "train",  # 图像不在磁盘上 → 跳过
        },
    )
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    for name in ("COCO_train2014_000000000101", "COCO_train2014_000000000103"):
        Image.new("RGB", (64, 48), (10, 20, 30)).save(images_dir / f"{name}.jpg", "JPEG")

    out_jsonl = tmp_path / "samples.jsonl"
    stats = emit_samples(ann, images_dir, out_jsonl)
    assert stats == {"kept": 2}
    lines = out_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["id"] == "COCO_train2014_000000000101"
    assert first["caption"] == "一只猫坐在沙发上"
    assert first["meta"]["tags"] == ["猫"] and first["meta"]["split"] == "train"
    assert first["labels"] == {}
    second = json.loads(lines[1])
    assert second["meta"]["extra_captions"] == ["夜晚的街道"]  # 一图多句：其余进 meta
