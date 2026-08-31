"""P2+P3 测试：增量去重器三层判定 + 实时质量门（含 /api/ingest 集成）。"""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageDraw

from mm_curation.dedup_incremental import IncrementalDeduper


def _img_file(tmp_path, name, seed, size=(160, 120), fmt=None):
    """结构化图（渐变+形状）：pHash 依赖低频结构，纯色图会全部碰撞。"""
    import random

    rng = random.Random(seed)
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    base = (seed * 37 % 255, seed * 61 % 255, seed * 89 % 255)
    for x in range(size[0]):
        draw.line([(x, 0), (x, size[1])], fill=tuple(int(c * x / size[0]) for c in base))
    for _ in range(3):
        x0, y0 = rng.randint(0, size[0] - 40), rng.randint(0, size[1] - 40)
        draw.rectangle(
            (x0, y0, x0 + rng.randint(20, 60), y0 + rng.randint(15, 40)),
            fill=tuple(rng.randint(0, 255) for _ in range(3)),
        )
    p = tmp_path / name
    img.save(p, fmt) if fmt else img.save(p)
    return str(p)


CAPTIONS = [
    "一只金毛犬在夕阳下的海滩上奔跑",
    "城市夜景中的霓虹灯广告牌特写",
    "厨房料理台上摆满了新鲜的蔬菜水果",
]


def test_incremental_dedup_three_layers(tmp_path):
    d = IncrementalDeduper()
    a = _img_file(tmp_path, "a.png", 1)
    assert not d.check_and_add("a", a, CAPTIONS[0]).is_duplicate

    # md5：同文件
    assert d.check_and_add("a2", a, CAPTIONS[1]).is_duplicate is True
    # pHash：重编码版（PNG->JPEG 内容相同视觉）
    b = _img_file(tmp_path, "b.jpg", 1)  # 同 seed 同内容，JPEG 重编码
    v = d.check_and_add("b", b, CAPTIONS[1])
    assert v.is_duplicate and v.method == "phash_near" and v.duplicate_of == "a"
    # MinHash：不同图（颜色不同）+ 近似重复 caption
    near = CAPTIONS[0] + "，一只金毛犬在夕阳下的海滩上奔跑"
    c = _img_file(tmp_path, "c.png", 7)
    v = d.check_and_add("c", c, near)
    assert v.is_duplicate and v.method == "minhash_lsh"
    # 全新的图 + 文：保留
    e = _img_file(tmp_path, "e.png", 9)
    assert not d.check_and_add("e", e, CAPTIONS[2]).is_duplicate
    assert len(d) == 2  # a / e（b、c 判重未入索引）


def test_incremental_dedup_short_caption_skips_lsh(tmp_path):
    d = IncrementalDeduper()
    a = _img_file(tmp_path, "a.png", 1)
    b = _img_file(tmp_path, "b.png", 7)
    d.check_and_add("a", a, "短句")  # <8 字不入 LSH
    v = d.check_and_add("b", b, "短句")
    assert not v.is_duplicate  # 图不同文太短 -> 不误杀


def test_quality_gate_and_ingest_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mm_curation.serving.api import create_app

    # 判重状态已 journal 持久化：测试必须把 journal 指到临时目录隔离，
    # 否则上一轮运行写入的样本会让本轮首条 ingest 直接判重（真实踩坑）
    monkeypatch.setenv("MM_DEDUP_JOURNAL", str(tmp_path / "journal.jsonl"))
    app = create_app("fake_root")
    client = TestClient(app)

    def _b64(seed=None, color=None):
        # 通过用例必须用结构化图：纯色图 Laplacian 方差≈0 会被 blur 阈值正确拦截
        buf = io.BytesIO()
        if seed is not None:
            img = Image.new("RGB", (200, 150))
            draw = ImageDraw.Draw(img)
            for x in range(200):
                draw.line([(x, 0), (x, 150)], fill=(x % 256, seed * 60 % 256, 150))
            draw.rectangle((30, 30, 120, 90), fill=(250, 100, 60))
        else:
            img = Image.new("RGB", (200, 150), color)
        img.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()

    # 高质量长 caption -> 通过；同图再发 -> 判重
    r1 = client.post(
        "/api/ingest", json={"image": _b64(seed=2), "caption": CAPTIONS[0], "id": "s1"}
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["quality"]["passed"] is True
    assert "text_length" in body["quality"]["scores"]
    assert body["dedup"]["is_duplicate"] is False
    assert body["accept"] is True

    r2 = client.post(
        "/api/ingest", json={"image": _b64(seed=2), "caption": CAPTIONS[1], "id": "s2"}
    )
    body2 = r2.json()
    assert body2["dedup"]["is_duplicate"] is True
    assert body2["accept"] is False

    # 低质 caption -> flags + 不通过（纯色图同时会被 blur 正确拦截）
    r3 = client.post(
        "/api/ingest", json={"image": _b64(color=(10, 10, 10)), "caption": "哈哈", "id": "s3"}
    )
    body3 = r3.json()
    assert "text_length" in body3["quality"]["flags"]
    assert body3["quality"]["passed"] is False and body3["accept"] is False

    # 非法 base64
    assert client.post("/api/ingest", json={"image": "!!!", "caption": "x"}).status_code == 422


def test_journal_replay_restores_dedup_state(tmp_path):
    """SPOF 修复验证：journal 持久化后，重启（新实例）三层判重状态不丢。"""
    journal = tmp_path / "journal.jsonl"
    d1 = IncrementalDeduper(journal=journal)
    a = _img_file(tmp_path, "j_a.png", 1)
    b = _img_file(tmp_path, "j_b.png", 9)
    assert not d1.check_and_add("j_a", a, CAPTIONS[0]).is_duplicate
    assert not d1.check_and_add("j_b", b, CAPTIONS[2]).is_duplicate
    assert journal.exists() and len(journal.read_text("utf-8").splitlines()) == 2

    d2 = IncrementalDeduper(journal=journal)  # 模拟重启后重放
    assert len(d2) == 2
    assert d2.check("j_a2", a, CAPTIONS[1]).is_duplicate  # md5 层存活
    near = CAPTIONS[0] + "，一只金毛犬在夕阳下的海滩上奔跑"
    c = _img_file(tmp_path, "j_c.png", 7)
    v = d2.check("j_c", c, near)
    assert v.is_duplicate and v.method == "minhash_lsh"  # LSH 层存活
    fresh = _img_file(tmp_path, "j_d.png", 11)
    assert not d2.check_and_add("j_d", fresh, CAPTIONS[1]).is_duplicate  # 新样本正常
    assert len(journal.read_text("utf-8").splitlines()) == 3


def test_journal_tolerates_corrupt_tail(tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        '{"id": "x", "md5": "aa", "phash": 123, "caption": "八个字以上的正常文本"}\n'
        '{"id": "y", "md5": "bb", "phash": 4',  # 模拟崩溃时的半行
        encoding="utf-8",
    )
    d = IncrementalDeduper(journal=journal)
    assert len(d) == 1  # 完整行重放，半行跳过
