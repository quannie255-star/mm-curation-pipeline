#!/usr/bin/env python
"""「平台运行态」公网演示服务 —— **只依赖 Python 标准库**。

## 为什么不用 FastAPI / Streamlit

沙箱只暴露一个 HTTP 端口，装不装得起来是未知的。`http.server` 是标准库，
零安装、零原生 wheel、秒起，而演示页要干的事只有「读JSON、返回JSON、渲染表格」。
为一个看表格的页面拖进一整套 Web 框架，是把部署风险换了个方向——**框架越重，
「能不能在别人机器上跑起来」越不确定**。真要分析能力，仓库里的 DuckDB 路径仍然完整。

## 它是什么，不是什么

**是**：prod 环境一次真实跑批结果的**只读快照**的浏览器（数据由
`scripts/export_ops_snapshot.py` 导出到 `data/`）。

**不是**：连着生产库的活服务。页面顶部常驻一行「快照时间」——
让看的人知道自己在看哪个时间点的数据。**把快照说成实时，是这类演示最容易被识破的谎**。

## 端点

| 路径 | 用途 |
|---|---|
| `/` | 演示页（单文件 HTML，内嵌 JS） |
| `/api/health` | 存活 + 快照清单摘要 |
| `/api/manifest` | 快照元信息（导出时间、表清单、行数） |
| `/api/datasets` | 数据集健康（对应 `/api/datasets` 的真实服务） |
| `/api/datasets/<name>/daily` | 日趋势 |
| `/api/datasets/<name>/dims` | 维表明细 |
| `/api/runs` | 运行台账 |
| `/data/<file>.json` | 原始快照（可核对） |

端点形状与真实 `ServiceCore` 保持一致（`{masked_cols, rows}`），
这样**页面上的代码与本地跑真实服务时几乎一样**——差异只在数据来源
（快照 vs 活库），而那一行差异是显式写在页面上的。

##跑法

    PORT=8080 python3 serve.py     # 部署环境
    python serve.py --port 8080    # 本地

必须监听 `0.0.0.0` 并读 `PORT` 环境变量——沙箱的反向代理只认这一种。
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
INDEX = HERE / "index.html"
MANIFEST = DATA / "snapshot_manifest.json"

# 维表明细一次最多给多少行——4479 行全丢给浏览器没意义，且会让页面卡住。
# 这个数写在页面上（不藏着），让人知道「这里被截断了」。
DIMS_PAGE_MAX = 200


# 快照是**只读**的（进程生命周期内不会变），所以解析一次就够。
# 不加缓存的后果不是慢，而是同一个响应里两次 `load()` 拿到两个不同对象——
# 那种不一致只在特定时序下出现，极难复现。
_CACHE: dict[str, dict] = {}


class SnapshotMissing(FileNotFoundError):
    """快照表不存在。**只带表名**——不带路径，避免本机目录结构进公网响应。"""


def as_records(payload: dict) -> list[dict]:
    """`{columns, rows}` -> `[dict, ...]`，并**丢弃全空列**。

    丢掉全空列是因为快照里有些列在**所有行**上都是 `null`。留着它们，页面上会出现
    一整列空白——看的人会以为「这个字段没算出来」，而不是「这个字段在这里不适用」。

    只丢**全空**列，不丢**部分**空的：实测 `ads_dataset_health` 里
    `freshness_days` 只有 6/8 行有值、`score_coverage` 只有 2/8，
    但它们在有值的行上是有效信息，整列丢掉会白扔数据。部分空的列照常返回，
    由页面把`null` 渲染成 `—`。
    """
    if "records" in payload:
        return payload["records"]
    cols = payload["columns"]
    rows = payload["rows"]
    keep = [i for i in range(len(cols)) if any(r[i] is not None for r in rows)]
    # `keep` 为空时必须返回 `[]` 而不是 `[{}]`——否则页面会渲染出「一行全空的记录」，
    # 看起来像「查到了数据但字段都缺」。这种假行比没有行更坏：
    # 变异测试实测到的（造一张全null 的表，原实现返回 `[{}]`）。
    if not keep:
        return []
    return [{cols[i]: r[i] for i in keep} for r in rows]


def load(name: str) -> dict:
    """读一张快照表，顺带把`records` 算好存进同一份 payload（供 as_records 复用）。"""
    if name in _CACHE:
        return _CACHE[name]
    p = DATA / f"{name}.json"
    if not p.exists():
        # 传**表名**而不是路径：FileNotFoundError 的 args[0] 是 errno（如 2），
        # 而 str(exc) 会带本机绝对路径。两者都不能直接回给公网。
        raise SnapshotMissing(name)
    payload = json.loads(p.read_text(encoding="utf-8"))
    payload["records"] = as_records(payload)
    _CACHE[name] = payload
    return payload


def load_manifest() -> dict:
    if not MANIFEST.exists():
        raise SnapshotMissing("snapshot_manifest.json")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _load_raw(name: str) -> dict:
    """读一个快照文件，**不做** records 转换（claims_rendered 没有 columns/rows）。"""
    p = DATA / f"{name}.json"
    if not p.exists():
        raise SnapshotMissing(name)
    return json.loads(p.read_text(encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "mmc-ops-demo/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 演示服务内容随发布更新，缓存会让人看到旧数字
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def log_message(self, fmt: str, *args: object) -> None:
        # 默认实现打到 stderr 且**不换行**，多行响应时日志会连成一片
        sys.stderr.write(f"[web] {fmt % args}\n")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/health":
                m = load_manifest()
                self._json(
                    {
                        "status": "ok",
                        "mode": "snapshot",
                        "exported_at": m["exported_at"],
                        "tables": len(m["tables"]),
                        "total_rows": m["total_rows"],
                    }
                )
            elif path == "/api/manifest":
                self._json(load_manifest())
            elif path == "/api/claims":
                # 对外数字：**构建期现算**进claims_rendered.json，页面只读不算。
                # 这样网页上的每个数字仍只有一处来源（claims.json + 评测报告），
                # 不需要让沙箱里的服务去读仓库里的文档/报告。
                self._json(_load_raw("claims_rendered"))
            elif path == "/api/datasets":
                self._json(self._datasets())
            elif path == "/api/runs":
                recs = as_records(load("job_runs"))
                recs.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
                self._json({"masked_cols": [], "rows": recs})
            elif m := re.fullmatch(r"/api/datasets/([\w.-]+)/daily", path):
                self._json(self._daily(m.group(1)))
            elif m := re.fullmatch(r"/api/datasets/([\w.-]+)/dims", path):
                self._json(self._dims(m.group(1)))
            elif m := re.fullmatch(r"/data/([\w.-]+\.json)", path):
                # 走 `load()` 而不是直接 read_text：这样缺文件时抛的是
                # `SnapshotMissing`（能被上面的 except 接住并说清是哪个表），
                # 直接 read_text 抛的是原生 FileNotFoundError，会掉进兜底变500。
                # 响应里只给 columns/rows——内部缓存字段 `records` 不外泄。
                raw = load(m.group(1)[:-5])
                self._json({k: v for k, v in raw.items() if k in ("columns", "rows")})
            else:
                self._json({"error": "not found", "path": path}, 404)
        except SnapshotMissing as exc:
            # 说清是「哪个表没导出」而不是笼统 500——后者会让人以为是程序 bug。
            self._json({"error": f"snapshot table missing: {exc}"}, 503)
        except Exception as exc:  # pragma: no cover - 兜底，页面要拿到可读原因
            # 只回**异常类型**，不回 str(exc)：多数异常的 str 里带本机绝对路径
            # （FileNotFoundError / PermissionError / KeyError on绝对路径…），
            # 公网服务不该把C:\Users\<用户名>\ 这样的结构送出去。
            self._json({"error": f"internal error: {type(exc).__name__}"}, 500)

    def _datasets(self) -> dict:
        recs = as_records(load("ads_dataset_health"))
        recs.sort(key=lambda r: -int(r.get("n_total") or 0))
        return {"masked_cols": [], "rows": recs}

    def _daily(self, ds: str) -> dict:
        """日趋势。返回形状与真实服务一致；`truncated` 恒为 False——275 行全给。

        刻意**不做分页**：这里的数据量小到没有分页的必要，而一旦引入分页，
        页面就得处理「第 2 页」这种状态——多一个状态就多一处能悄悄出错的地方。
        """
        recs = [r for r in as_records(load("dws_dataset_day")) if r.get("dataset") == ds]
        recs.sort(key=lambda r: str(r.get("event_date") or ""))
        return {"masked_cols": [], "rows": recs, "truncated": False, "n_total": len(recs)}

    def _dims(self, ds: str) -> dict:
        recs = [r for r in as_records(load("dim_device")) if r.get("dataset") == ds]
        recs.sort(key=lambda r: str(r.get("device_id") or ""))
        return {
            "masked_cols": [],
            "rows": recs[:DIMS_PAGE_MAX],
            "truncated": len(recs) > DIMS_PAGE_MAX,
            "n_total": len(recs),
            "page_max": DIMS_PAGE_MAX,
        }


def main() -> int:
    port = int(os.environ.get("PORT", "0"))
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    if not port:
        port = 8080

    needed = (INDEX, MANIFEST, DATA / "claims_rendered.json")
    missing = [str(p.relative_to(HERE)) for p in needed if not p.exists()]
    if missing:
        print(
            f"[FAIL] 缺文件：{missing}\n先跑 python -X utf8 scripts/export_ops_snapshot.py",
            file=sys.stderr,
        )
        return 1

    # 0.0.0.0 而不是 localhost：沙箱的反向代理从容器外访问，只绑本地等于关上门
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    m = load_manifest()
    print(f"[web]快照导出于 {m['exported_at']} / {m['total_rows']} 行，监听 0.0.0.0:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
