"""FAISS 向量索引构建与落盘（索引层，docs/design_tables.md 1.1/1.2）。

选型：IndexFlatIP + 归一化向量 = 精确余弦检索。万级样本下 Flat 暴力检索
延迟毫秒级且零召回损失，不引入 IVF/HNSW 的近似误差；扩到百万级时换
IndexIVFFlat/IndexHNSWFlat 并保持本模块对外接口不变（升级路径已隔离在此）。

落盘三件套（data/indexes/<name>/）：
- faiss.index    向量索引（行号 ↔ store.jsonl 的 row 严格对齐）
- store.jsonl    样本元数据（id/图路径/text/labels）
- manifest.json  索引清单（来源、规模、维度、构建时间）
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..embedding import clip_encoder
from ..operators.base import Sample

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class IndexManifest:
    name: str
    source_jsonl: str
    n_items: int
    dim: int
    faiss_index: str = "faiss.index"
    image_store: str = "store.jsonl"
    metric: str = "cosine"
    built_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def save(self, dir_path: Path) -> None:
        (dir_path / "manifest.json").write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, dir_path: str | Path) -> "IndexManifest":
        return cls(**json.loads((Path(dir_path) / "manifest.json").read_text("utf-8")))

    def is_stale(self) -> bool:
        """上游 source_jsonl 在建索引之后被重写（流转表: ready -> stale）。"""
        src = Path(self.source_jsonl)
        if not src.exists():
            return True
        rebuilt = datetime.fromisoformat(self.built_at).timestamp()
        return src.stat().st_mtime > rebuilt


class IndexBuilder:
    """编码 -> 建索引 -> 落盘。encoder 可注入（测试用假编码器）。"""

    def __init__(self, encoder=None):
        self._encoder = encoder

    def _get_encoder(self):
        return self._encoder or clip_encoder.get_encoder()

    def build(
        self, samples: list[Sample], name: str, out_dir: str | Path, source_jsonl: str | Path
    ) -> IndexManifest:
        if not samples:
            raise ValueError("样本列表为空，拒绝构建空索引")
        import faiss
        import numpy as np

        vectors = self._get_encoder().encode_images([s.image_path for s in samples])
        if len(vectors) != len(samples):
            raise RuntimeError(f"编码数 {len(vectors)} != 样本数 {len(samples)}")
        if not np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4):
            raise ValueError("向量未归一化，IndexFlatIP 的余弦语义不成立")

        out_dir = Path(out_dir) / name
        out_dir.mkdir(parents=True, exist_ok=True)
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors.astype("float32"))
        faiss.write_index(index, str(out_dir / "faiss.index"))

        with open(out_dir / "store.jsonl", "w", encoding="utf-8") as f:
            for row, s in enumerate(samples):
                f.write(
                    json.dumps(
                        {
                            "row": row,
                            "id": s.id,
                            "image_path": _relpath(s.image_path),
                            "text": s.text,
                            "labels": s.labels,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        manifest = IndexManifest(
            name=name,
            source_jsonl=str(source_jsonl),
            n_items=len(samples),
            dim=int(vectors.shape[1]),
        )
        manifest.save(out_dir)
        return manifest


def build_index(
    input_jsonl: str | Path, name: str, out_dir: str | Path = "data/indexes"
) -> IndexManifest:
    """CLI/DAG 入口：读样本 jsonl -> 构建索引 -> 返回清单。"""
    samples = [Sample.from_dict(json.loads(line)) for line in open(input_jsonl, encoding="utf-8")]
    return IndexBuilder().build(samples, name, out_dir, input_jsonl)


def _relpath(path: str) -> str:
    """图路径存相对路径（服务端静态资源按仓库根解析）。"""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)
