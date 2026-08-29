"""serving metrics 测试：计数/直方图渲染 + 端到端 /metrics 端点。"""

from __future__ import annotations

from mm_curation.serving.api import create_app
from mm_curation.serving.metrics import Metrics


def test_metrics_render_format():
    m = Metrics()
    m.observe_request("/api/search", 200, 0.03)
    m.observe_request("/api/search", 200, 0.20)
    m.observe_request("/api/search", 404)  # 异常请求不计延迟（与中间件语义一致）
    m.inc("ingest_accepted", 3)
    text = m.render()
    assert 'mm_requests_total{path="/api/search",status="200"} 2' in text
    assert 'mm_requests_total{path="/api/search",status="404"} 1' in text
    # 直方图累积语义：0.03 与 0.20 都 <=0.25，le=0.05 桶只有 1
    assert 'mm_request_latency_seconds_bucket{path="/api/search",le="0.05"} 1' in text
    assert 'mm_request_latency_seconds_bucket{path="/api/search",le="0.25"} 2' in text
    assert 'mm_business_total{kind="ingest_accepted"} 3' in text
    assert "mm_uptime_seconds" in text


def test_metrics_endpoint_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import mm_curation.serving.api as api_mod

    class FakeSearcher:
        name = "fake_idx"
        n_items = 1
        store = []

        def search_by_text(self, q, top_k=10):
            return []

    def fake_load(root, name):
        if name == "fake_idx":
            return FakeSearcher()
        raise KeyError(f"索引不存在: {name}")

    class M:
        name = "fake_idx"
        n_items = 1
        built_at = "2026-01-01T00:00:00+00:00"
        source_jsonl = "x"

    monkeypatch.setattr(api_mod, "load_searcher", fake_load)
    monkeypatch.setattr(api_mod, "list_indexes", lambda root: [M()])
    client = TestClient(create_app("fake_root"))

    assert client.get("/api/health").status_code == 200
    client.post("/api/search", json={"query": "一只猫", "index": "fake_idx", "top_k": 3})
    client.post("/api/search", json={"query": "x", "index": "nope", "top_k": 3})

    body = client.get("/metrics").text
    assert 'mm_requests_total{path="/api/health",status="200"}' in body
    assert 'mm_requests_total{path="/api/search",status="200"} 1' in body
    assert 'mm_requests_total{path="/api/search",status="404"} 1' in body
    assert "mm_request_latency_seconds_bucket" in body
