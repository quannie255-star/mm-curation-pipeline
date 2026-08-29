"""serving 层可观测性：Prometheus 文本格式的计数器与延迟直方图。

手写暴露格式而非引 prometheus_client 依赖——暴露面只有一个 /metrics
端点时，30 行标准格式比一个新依赖划算（依赖决策记录在案）。
"""

from __future__ import annotations

import threading
import time

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class Metrics:
    """线程安全的极简指标集：请求计数 + 延迟直方图 + 业务计数。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, int], int] = {}  # (path, status) -> count
        self._latency: dict[str, list[int]] = {  # path -> bucket counts
            p: [0] * (len(LATENCY_BUCKETS) + 1) for p in ("/api/search", "/api/ingest")
        }
        self._counters: dict[str, int] = {}
        self._started = time.time()

    def observe_request(self, path: str, status: int, seconds: float | None = None) -> None:
        with self._lock:
            self._requests[(path, status)] = self._requests.get((path, status), 0) + 1
            if seconds is not None and path in self._latency:
                for i, bound in enumerate(LATENCY_BUCKETS):
                    if seconds <= bound:
                        self._latency[path][i] += 1
                        break
                else:
                    self._latency[path][-1] += 1

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def render(self) -> str:
        with self._lock:
            lines = [
                "# HELP mm_uptime_seconds 进程存活时长",
                "# TYPE mm_uptime_seconds gauge",
                f"mm_uptime_seconds {round(time.time() - self._started, 1)}",
            ]
            lines += ["# HELP mm_requests_total HTTP 请求计数", "# TYPE mm_requests_total counter"]
            for (path, status), n in sorted(self._requests.items()):
                lines.append(f'mm_requests_total{{path="{path}",status="{status}"}} {n}')
            lines += [
                "# HELP mm_request_latency_seconds 延迟分布（桶上限累积）",
                "# TYPE mm_request_latency_seconds histogram",
            ]
            for path, buckets in self._latency.items():
                cumulative = 0
                for bound, n in zip(LATENCY_BUCKETS + (float("inf"),), buckets):
                    cumulative += n
                    lines.append(
                        f'mm_request_latency_seconds_bucket{{path="{path}",le="{bound}"}} '
                        f"{cumulative}"
                    )
            lines += [
                "# HELP mm_business_total 业务计数（ingest 接受/拒绝）",
                "# TYPE mm_business_total counter",
            ]
            for name, n in sorted(self._counters.items()):
                lines.append(f'mm_business_total{{kind="{name}"}} {n}')
            return "\n".join(lines) + "\n"
