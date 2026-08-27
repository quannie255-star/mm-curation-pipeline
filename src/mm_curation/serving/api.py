"""FastAPI 检索服务（L2 服务层，docs/design_tables.md 2.1/1.3）。

职责边界：HTTP 语义（校验/状态码/统一响应/base64 解码/静态资源白名单）；
向量与索引逻辑全部委托已确认的 index 层。查询器按索引名惰性加载并缓存
（脏/净索引可同时在线，供对比实验与 Demo 切换）。

静态资源安全：只允许返回"某个已加载索引的 store 中登记过的文件"，
精确路径白名单（而非目录前缀），路径穿越天然无效。
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from ..index.searcher import IndexSearcher, SearchHit, list_indexes, load_searcher
from ..index.store import REPO_ROOT

logger = logging.getLogger(__name__)
DEFAULT_INDEXES_ROOT = "data/indexes"


class SearchRequest(BaseModel):
    """query 与 image 二选一（design 1.3 的校验规则）。"""

    query: Optional[str] = Field(default=None, min_length=1, max_length=256)
    image: Optional[str] = Field(default=None, description="base64 编码的图像")
    top_k: int = Field(default=10, ge=1, le=100)
    index: str = Field(min_length=1)

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.query is None) == (self.image is None):
            raise ValueError("query 与 image 必须二选一")
        return self


class Hit(BaseModel):
    row: int
    id: str
    score: float
    image_path: str
    caption: str
    labels: dict


class SearchResponse(BaseModel):
    index: str
    took_ms: float
    results: list[Hit]


def create_app(indexes_root: str | Path = DEFAULT_INDEXES_ROOT) -> FastAPI:
    app = FastAPI(title="mm-curation 检索服务", version="0.1.0")
    _searchers: dict[str, IndexSearcher] = {}
    _static_paths: dict[str, Path] = {}

    def _get_searcher(name: str) -> IndexSearcher:
        if name not in _searchers:
            try:
                _searchers[name] = load_searcher(indexes_root, name)
            except KeyError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
        return _searchers[name]

    def _known_static_paths() -> dict[str, Path]:
        """全部索引 store 里登记过的图像文件（惰性构建一次）。"""
        if not _static_paths:
            for m in list_indexes(indexes_root):
                try:
                    s = _get_searcher(m.name)
                except HTTPException:  # 损坏索引不阻塞其他索引
                    continue
                for meta in s.store:
                    _static_paths.setdefault(meta["image_path"], REPO_ROOT / meta["image_path"])
        return _static_paths

    @app.get("/api/health")
    def health():
        ready = bool(list_indexes(indexes_root))
        return {"status": "ok", "index_ready": ready}

    @app.get("/api/indexes")
    def indexes():
        return [
            {"name": m.name, "n_items": m.n_items, "built_at": m.built_at, "source": m.source_jsonl}
            for m in list_indexes(indexes_root)
        ]

    @app.post("/api/search", response_model=SearchResponse)
    def search(req: SearchRequest):
        searcher = _get_searcher(req.index)
        t0 = time.perf_counter()
        try:
            hits: list[SearchHit] = (
                searcher.search_by_text(req.query, req.top_k)
                if req.query is not None
                else _search_by_b64(searcher, req.image, req.top_k)
            )
        except binascii.Error as e:  # base64 非法
            raise HTTPException(status_code=422, detail=f"image 不是合法 base64: {e}") from e
        took_ms = (time.perf_counter() - t0) * 1000
        return SearchResponse(
            index=searcher.name,
            took_ms=round(took_ms, 2),
            results=[Hit(**h.to_dict()) for h in hits],
        )

    @app.get("/static/{file_path:path}")
    def static(file_path: str):
        abs_path = _known_static_paths().get(file_path)
        if abs_path is None or not abs_path.exists():
            raise HTTPException(status_code=404, detail="文件不在已登记的白名单内")
        return FileResponse(abs_path)

    _attach_ingest(app)
    return app


class IngestRequest(BaseModel):
    """实时质量门入参（P2+P3）：一条图文对到达即评分 + 判重。"""

    image: str = Field(description="base64 编码的图像")
    caption: Optional[str] = Field(default=None, max_length=256)
    id: Optional[str] = Field(default=None, description="调用方样本 id（判重溯源用）")


def _attach_ingest(app: FastAPI) -> None:
    """实时质量门端点。gate/deduper 首次调用时惰性构建（检测器权重缺失自动降级）。"""
    state: dict = {}

    def _deps():
        if "gate" not in state:
            from pathlib import Path as P

            from ..dedup_incremental import IncrementalDeduper
            from .quality_gate import QualityGate

            cfg_path = P("configs/pipeline.example.yaml")
            if cfg_path.exists():
                from ..pipeline import PipelineConfig

                gate = QualityGate.from_config(PipelineConfig.from_yaml(cfg_path))
            else:  # 配置缺失 → 空算子集降级（可诊断，不崩溃）
                gate = QualityGate(ops=[])
            state["gate"] = gate
            state["deduper"] = IncrementalDeduper()
        return state["gate"], state["deduper"]

    @app.post("/api/ingest")
    def ingest(req: IngestRequest):
        import os
        import tempfile
        import uuid

        try:
            raw = base64.b64decode(req.image, validate=True)
        except binascii.Error as e:
            raise HTTPException(status_code=422, detail=f"image 不是合法 base64: {e}") from e
        # 单样本算子以文件路径为输入约定，落临时文件后清理（v1 取舍，见 quality_gate 文档）
        tmp = Path(tempfile.gettempdir()) / f"mm_ingest_{uuid.uuid4().hex}.jpg"
        try:
            tmp.write_bytes(raw)
            gate, deduper = _deps()
            quality = gate.assess(str(tmp), req.caption or "")
            verdict = deduper.check_and_add(req.id or tmp.name, str(tmp), req.caption or "")
        finally:
            if tmp.exists():
                os.remove(tmp)
        return {
            "quality": quality,
            "dedup": verdict.to_dict(),
            "accept": quality["passed"] and not verdict.is_duplicate,
        }


def _search_by_b64(searcher: IndexSearcher, image_b64: str, top_k: int):
    raw = base64.b64decode(image_b64, validate=True)
    return searcher.search_by_image(raw, top_k)


app = create_app()  # uvicorn 入口：mm_curation.serving.api:app
