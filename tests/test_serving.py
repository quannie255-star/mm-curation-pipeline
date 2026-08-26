"""L2 服务层测试：HTTP 语义（校验/状态码/响应形状），索引层全部 mock。

真实索引 + 真实 HTTP 的端到端验证在 T6（curl 归档）做。
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from mm_curation.index.searcher import SearchHit
from mm_curation.serving.api import create_app


class FakeSearcher:
    name = "fake_idx"
    n_items = 3

    def __init__(self, tmp_path):
        # static 白名单来自 store；挂一张真实小图供 /static 返回
        from PIL import Image

        img = tmp_path / "white.png"
        Image.new("RGB", (8, 8), (250, 250, 250)).save(img)
        self.store = [
            {
                "row": 0,
                "id": "s0",
                "image_path": "tests/white.png",
                "caption": "第一张",
                "labels": {},
            }
        ]
        self._img_abs = img

    def search_by_text(self, query, top_k=10):
        return [
            SearchHit(
                row=i,
                id=f"s{i}",
                score=1.0 - i * 0.1,
                image_path="tests/white.png",
                caption=f"结果{i}",
                labels={} if i else {"dirty": "watermark"},
            )
            for i in range(min(top_k, self.n_items))
        ]

    def search_by_image(self, image, top_k=10):
        return self.search_by_text("<image>", top_k)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import mm_curation.serving.api as api_mod

    fake = FakeSearcher(tmp_path)

    def fake_load(root, name):
        if name == "fake_idx":
            return fake
        raise KeyError(f"索引不存在: {name}")

    class FakeManifest:
        name = "fake_idx"
        n_items = 3
        built_at = "2026-08-22T00:00:00+00:00"
        source_jsonl = "x.jsonl"

    monkeypatch.setattr(api_mod, "load_searcher", fake_load)
    monkeypatch.setattr(api_mod, "list_indexes", lambda root: [FakeManifest()])
    # static 白名单路径解析到真实临时文件（REPO_ROOT 指到 pytest 会话目录）
    monkeypatch.setattr(api_mod, "REPO_ROOT", tmp_path.parent)
    fake.store[0]["image_path"] = str(fake._img_abs.relative_to(tmp_path.parent))
    return TestClient(create_app("fake_root")), fake, tmp_path


def test_health_and_indexes(client):
    c, _, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "index_ready": True}
    r = c.get("/api/indexes")
    assert r.status_code == 200
    assert r.json()[0]["name"] == "fake_idx" and r.json()[0]["n_items"] == 3


def test_search_by_text_shape_and_order(client):
    c, _, _ = client
    r = c.post("/api/search", json={"query": "一只猫", "index": "fake_idx", "top_k": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["index"] == "fake_idx" and body["took_ms"] >= 0
    scores = [h["score"] for h in body["results"]]
    assert scores == sorted(scores, reverse=True) and len(scores) == 2
    assert body["results"][0]["id"] == "s0" and "labels" in body["results"][0]


def test_search_by_base64_image(client):
    c, _, tmp_path = client
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG")
    r = c.post(
        "/api/search",
        json={"image": base64.b64encode(buf.getvalue()).decode(), "index": "fake_idx", "top_k": 1},
    )
    assert r.status_code == 200 and r.json()["results"][0]["id"] == "s0"


def test_search_validation_errors(client):
    c, _, _ = client
    base = {"index": "fake_idx"}
    assert c.post("/api/search", json={**base, "top_k": 1}).status_code == 422  # 二选一: 都缺
    assert (
        c.post("/api/search", json={**base, "query": "a", "image": "YQ==", "top_k": 1}).status_code
        == 422
    )  # 都给
    assert c.post("/api/search", json={**base, "query": "a", "top_k": 0}).status_code == 422  # 越界
    assert c.post("/api/search", json={**base, "query": "a", "top_k": 999}).status_code == 422
    assert c.post("/api/search", json={**base, "query": "", "top_k": 1}).status_code == 422  # 空串
    assert (
        c.post("/api/search", json={"query": "a", "index": "nope", "top_k": 1}).status_code == 404
    )


def test_static_whitelist_and_traversal(client):
    c, fake, tmp_path = client
    allowed = fake.store[0]["image_path"]
    assert c.get(f"/static/{allowed}").status_code == 200
    assert c.get("/static/../../etc/passwd").status_code == 404  # 穿越不在白名单
    assert c.get("/static/data/raw/images/unknown.jpg").status_code == 404  # 未登记文件
