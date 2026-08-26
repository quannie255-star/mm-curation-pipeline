"""索引查询器：加载落盘三件套，提供文搜图 / 图搜图（design 2.1 的查询侧）。

职责边界：只做"向量 -> top-k 行号 -> 样本元数据"，HTTP 语义（校验/状态码/
base64 解码）属于 serving 层。encoder 经 clip_encoder.get_encoder() 获取，
测试 monkeypatch 同一入口。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..embedding import clip_encoder
from .store import IndexManifest

logger = logging.getLogger(__name__)


@dataclass
class SearchHit:
    row: int
    id: str
    score: float  # 余弦相似度 [-1, 1]
    image_path: str
    caption: str
    labels: dict

    def to_dict(self) -> dict:
        return {
            "row": self.row,
            "id": self.id,
            "score": round(self.score, 4),
            "image_path": self.image_path,
            "caption": self.caption,
            "labels": self.labels,
        }


class IndexSearcher:
    """单个索引的只读查询器。进程内可同时持有多个（脏/净对比）。"""

    def __init__(self, index_dir: str | Path):
        self.dir = Path(index_dir)
        self.manifest = IndexManifest.load(self.dir)
        import faiss

        self.index = faiss.read_index(str(self.dir / self.manifest.faiss_index))
        self.store = [
            json.loads(line)
            for line in (self.dir / self.manifest.image_store).read_text("utf-8").splitlines()
        ]
        if self.index.ntotal != len(self.store):
            raise RuntimeError(
                f"索引 {self.manifest.name} 损坏：faiss {self.index.ntotal} 行"
                f" != store {len(self.store)} 行"
            )
        if self.manifest.is_stale():
            logger.warning(
                "索引 %s 已过期（上游 %s 晚于构建时间），结果可能滞后",
                self.manifest.name,
                self.manifest.source_jsonl,
            )

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def n_items(self) -> int:
        return self.index.ntotal

    def _search_vec(self, vec, top_k: int) -> list[SearchHit]:
        return self.search_many_by_vectors([vec], top_k)[0]

    def search_many_by_vectors(self, vectors, top_k: int) -> list[list[SearchHit]]:
        """批量向量检索（评测模块的入口：全部查询一次 FAISS 调用，
        避免逐条走文本编码路径）。返回与 vectors 等长的命中列表。"""
        import numpy as np

        if len(vectors) == 0:
            return []
        top_k = min(top_k, self.n_items)
        scores, rows = self.index.search(np.asarray(vectors, dtype="float32"), top_k)
        out: list[list[SearchHit]] = []
        for score_row, row_row in zip(scores, rows):
            hits = []
            for score, row in zip(score_row, row_row):
                if row < 0:  # FAISS 对无效/不足结果返回 -1
                    continue
                meta = self.store[row]
                hits.append(
                    SearchHit(
                        row=int(row),
                        id=meta["id"],
                        score=float(score),
                        image_path=meta["image_path"],
                        caption=meta["caption"],
                        labels=meta.get("labels", {}),
                    )
                )
            out.append(hits)
        return out

    def search_by_text(self, query: str, top_k: int = 10) -> list[SearchHit]:
        vec = clip_encoder.get_encoder().encode_texts([query])[0]
        return self._search_vec(vec, top_k)

    def search_by_image(self, image, top_k: int = 10) -> list[SearchHit]:
        """image 接受 PIL Image / 图像字节 / 路径。"""
        img = self._to_pil(image)
        vec = clip_encoder.get_encoder().encode_image_object(img)
        return self._search_vec(vec, top_k)

    @staticmethod
    def _to_pil(image):
        from PIL import Image

        if isinstance(image, Image.Image):  # Pillow 12 已移除 Image.isImageType
            return image.convert("RGB")
        if isinstance(image, (bytes, bytearray)):
            from io import BytesIO

            return Image.open(BytesIO(image)).convert("RGB")
        return Image.open(image).convert("RGB")


def list_indexes(indexes_root: str | Path) -> list[IndexManifest]:
    """扫描索引根目录，返回全部可用索引清单（/api/indexes 的数据源）。"""
    root = Path(indexes_root)
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if (d / "manifest.json").exists():
            out.append(IndexManifest.load(d))
    return out


def load_searcher(indexes_root: str | Path, name: str) -> IndexSearcher:
    """按名加载；不存在时抛 KeyError（服务层转 404）。"""
    d = Path(indexes_root) / name
    if not (d / "manifest.json").exists():
        raise KeyError(
            f"索引不存在: {name}（可用: {[m.name for m in list_indexes(indexes_root)]}）"
        )
    return IndexSearcher(d)
