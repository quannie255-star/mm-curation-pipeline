"""索引层测试：构建器 + 查询器。

假编码器产出确定性单位向量（one-hot），FAISS 用真实索引——验证落盘三件套、
行号对齐、清单字段与 stale 判定，不依赖模型与 GPU。
QueryableEncoder 让文本/图像对象查询也有可精确断言的向量。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from mm_curation.index.store import IndexBuilder, IndexManifest, build_index


class OneHotEncoder:
    """第 i 个样本 -> 第 (i % d) 维 one-hot：检索结果可精确断言。"""

    def __init__(self, dim: int = 8):
        self.dim = dim

    def encode_images(self, paths):
        n = len(paths)
        v = np.zeros((n, self.dim), dtype="float32")
        v[np.arange(n), np.arange(n) % self.dim] = 1.0
        return v

    def encode_texts(self, texts):  # pragma: no cover - 构建器只用图像
        raise NotImplementedError


class QueryableEncoder(OneHotEncoder):
    """查询侧假编码器：按文本 / 按图像首像素定位 one-hot 维。"""

    def __init__(self, dim: int, text_to_dim: dict[str, int], color_to_dim: dict):
        super().__init__(dim)
        self.text_to_dim = text_to_dim
        self.color_to_dim = color_to_dim

    def _onehot(self, dim: int) -> np.ndarray:
        v = np.zeros(self.dim, dtype="float32")
        v[dim] = 1.0
        return v

    def encode_images(self, paths):
        return np.stack([self._onehot(i % self.dim) for i in range(len(paths))])

    def encode_texts(self, texts):
        return np.stack([self._onehot(self.text_to_dim[t]) for t in texts])

    def encode_image_object(self, image):
        return self._onehot(self.color_to_dim[image.getpixel((0, 0))])


@pytest.fixture
def samples(tmp_path):
    from PIL import Image

    out = []
    for i in range(6):
        # PNG 无损：查询测试按纯色像素定位，JPEG 有损会让颜色漂移 1 而失配
        p = tmp_path / f"img{i}.png"
        Image.new("RGB", (32, 32), (i * 40, 80, 120)).save(p)
        out.append({"id": f"s{i}", "image_path": str(p), "text": f"第{i}张图", "labels": {}})
    return out


def _to_samples(rows):
    from mm_curation.operators.base import Sample

    return [Sample.from_dict(r) for r in rows]


def test_build_produces_aligned_triple(tmp_path, samples):
    manifest = IndexBuilder(OneHotEncoder()).build(
        _to_samples(samples), "test_idx", tmp_path / "idx", "fake/source.jsonl"
    )

    d = tmp_path / "idx" / "test_idx"
    assert (d / "faiss.index").exists() and (d / "store.jsonl").exists()
    store = [
        json.loads(line) for line in (d / "store.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["row"] for r in store] == list(range(6))  # 行号严格对齐
    assert store[2]["id"] == "s2" and store[2]["text"] == "第2张图"
    assert manifest.n_items == 6 and manifest.dim == 8 and manifest.metric == "cosine"

    # FAISS 侧行号与 store 对齐：用 one-hot 查询应命中自身
    import faiss

    index = faiss.read_index(str(d / "faiss.index"))
    scores, rows = index.search(np.eye(8, dtype="float32")[:1], 1)
    assert rows[0][0] == 0  # 查询第 0 维 -> 第一个该维样本 s0


def test_build_rejects_unnormalized_vectors(tmp_path, samples):
    class Bad(OneHotEncoder):
        def encode_images(self, paths):
            return super().encode_images(paths) * 3.0  # 未归一化

    with pytest.raises(ValueError, match="归一化"):
        IndexBuilder(Bad()).build(_to_samples(samples), "bad", tmp_path, "src.jsonl")


def test_build_rejects_empty(tmp_path):
    with pytest.raises(ValueError, match="空"):
        IndexBuilder(OneHotEncoder()).build([], "empty", tmp_path, "src.jsonl")


def test_manifest_stale_detection(tmp_path):
    """is_stale 的纯逻辑测试：固定 built_at + 显式 epoch 控制 mtime，
    与真实时钟完全解耦（此前与 datetime.now/st_mtime 耦合出现过偶发失败）。"""
    import os

    src = tmp_path / "src.jsonl"
    src.write_text("{}", encoding="utf-8")
    built = 1_577_836_800  # 2020-01-01T00:00:00+00:00
    m = IndexManifest(
        name="x", source_jsonl=str(src), n_items=1, dim=8, built_at="2020-01-01T00:00:00+00:00"
    )
    os.utime(src, (built - 100, built - 100))  # 源早于索引
    assert not m.is_stale()
    os.utime(src, (built + 100, built + 100))  # 源晚于索引（上游重写过）
    assert m.is_stale()

    gone = IndexManifest(
        name="x",
        source_jsonl=str(tmp_path / "gone.jsonl"),
        n_items=1,
        dim=8,
        built_at="2020-01-01T00:00:00+00:00",
    )
    assert gone.is_stale()  # 源文件消失同样视为过期


def test_build_index_entrypoint(tmp_path, samples, monkeypatch):
    import mm_curation.index.store as store_mod

    src = tmp_path / "input.jsonl"
    src.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in samples), encoding="utf-8")
    monkeypatch.setattr(store_mod.clip_encoder, "get_encoder", lambda: OneHotEncoder())
    manifest = build_index(src, "cli_idx", tmp_path / "out")
    assert manifest.n_items == 6
    assert (tmp_path / "out" / "cli_idx" / "manifest.json").exists()


# ---------------- T2/T3: 查询器 ----------------


def _build_queryable(tmp_path, samples):
    """用 QueryableEncoder 建一个 6 样本索引：文本 t<i> -> 维 i；图像颜色 (i*40,80,120) -> 维 i。"""
    import mm_curation.index.searcher as searcher_mod
    import mm_curation.index.store as store_mod
    from mm_curation.index.searcher import IndexSearcher

    text_to_dim = {f"t{i}": i for i in range(6)}
    color_to_dim = {(i * 40, 80, 120): i for i in range(6)}
    enc = QueryableEncoder(dim=8, text_to_dim=text_to_dim, color_to_dim=color_to_dim)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(store_mod.clip_encoder, "get_encoder", lambda: enc)
    monkey.setattr(searcher_mod.clip_encoder, "get_encoder", lambda: enc)
    src = tmp_path / "src.jsonl"
    src.write_text("{}", encoding="utf-8")
    IndexBuilder(enc).build(_to_samples(samples), "q_idx", tmp_path / "idx", src)
    return IndexSearcher(tmp_path / "idx" / "q_idx"), monkey


def test_search_by_text_hits_aligned_row(tmp_path, samples):
    searcher, monkey = _build_queryable(tmp_path, samples)
    try:
        hits = searcher.search_by_text("t2", top_k=3)
        assert hits[0].id == "s2" and hits[0].row == 2
        assert hits[0].score == pytest.approx(1.0)
        assert len(hits) == 3  # top_k 生效（其余为 0 分但按序返回）
        assert hits[0].text == "第2张图"
    finally:
        monkey.undo()


def test_search_by_image_accepts_pil_bytes_path(tmp_path, samples):
    from PIL import Image

    searcher, monkey = _build_queryable(tmp_path, samples)
    try:
        target = samples[4]  # 颜色 (160, 80, 120) -> 维 4
        for form in ("pil", "bytes", "path"):
            if form == "pil":
                img = Image.open(target["image_path"])
            elif form == "bytes":
                img = open(target["image_path"], "rb").read()
            else:
                img = target["image_path"]
            hits = searcher.search_by_image(img, top_k=1)
            assert hits[0].id == "s4", f"{form} 输入未命中"
    finally:
        monkey.undo()


def test_top_k_clamped_to_index_size(tmp_path, samples):
    searcher, monkey = _build_queryable(tmp_path, samples)
    try:
        assert len(searcher.search_by_text("t0", top_k=100)) == 6
    finally:
        monkey.undo()


def test_list_and_load_indexes(tmp_path, samples):
    from mm_curation.index.searcher import list_indexes, load_searcher

    searcher, monkey = _build_queryable(tmp_path, samples)
    try:
        assert [m.name for m in list_indexes(tmp_path / "idx")] == ["q_idx"]
        got = load_searcher(tmp_path / "idx", "q_idx")
        assert got.n_items == 6 and got.name == "q_idx"
        with pytest.raises(KeyError, match="不存在"):
            load_searcher(tmp_path / "idx", "nope")
    finally:
        monkey.undo()
