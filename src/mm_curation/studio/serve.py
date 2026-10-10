"""Studio 的 HTTP 服务（只用 Python 标准库，不加新依赖）。

启动：
    python -m mm_curation.studio.serve
然后浏览器打开 http://127.0.0.1:8765

⚠️ 为什么用 `http.server` 而不是 FastAPI/Flask：
外行用户跑起来不该先 `pip install` 一堆东西。标准库够用，
而且**依赖越少，本地工具越不容易起不来**。

安全边界（本地单用户工具，但仍然要守住）：
- 只监听 127.0.0.1，不对外网暴露；
- 会话 id 校验字符集（防路径穿越）；
- 上传按流分块写，超 2GB 立刻中止；
- 静态文件从白名单目录读，不接受任意路径。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import backend
from .backend import (
    ALLOWED_SUFFIX,
    MAX_UPLOAD_BYTES,
    UPLOAD_ROOT,
    StudioError,
    do_check,
    list_scenarios,
    session_dir,
    start_dataset,
    start_funnel,
)

LOG = logging.getLogger("studio.serve")
STATIC = Path(__file__).resolve().parent / "static"
BOUNDARY = "----mmstudio7f3c9a"


class Handler(BaseHTTPRequestHandler):
    server_version = "MMStudio/1.0"

    # ── 基础响应helpers ──
    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, message: str, hint: str = "", code: int = 400) -> None:
        self._json({"ok": False, "error": message, "hint": hint}, code)

    def _static(self, name: str) -> None:
        # 白名单：只允许已知文件名，不接受用户传的任意路径
        if name not in ("index.html", "app.js", "style.css"):
            self._err("没有这个页面", code=404)
            return
        p = STATIC / name
        if not p.exists():
            self._err(f"页面文件缺失：{name}（安装不完整）", code=500)
            return
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}[p.suffix]
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── 路由 ──
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self._static("index.html")
            elif u.path == "/app.js":
                self._static("app.js")
            elif u.path == "/style.css":
                self._static("style.css")
            elif u.path == "/api/scenarios":
                self._json({"ok": True, "scenarios": list_scenarios()})
            elif u.path == "/api/job":
                job = backend.get_job((q.get("id") or [""])[0])
                self._json({"ok": True, "job": job.as_dict()})
            elif u.path == "/api/preview":
                # 采样预览清洗结果里的若干条（让用户眼见为实）
                self._preview(q)
            else:
                self._err("没有这个接口", code=404)
        except StudioError as e:
            self._err(e.message, e.hint)
        except Exception as e:  # noqa: BLE001
            LOG.exception("GET %s 失败", u.path)
            self._err(f"服务器出错：{type(e).__name__}", "请把这条信息反馈给开发者", 500)

    def _preview(self, q: dict) -> None:
        session = (q.get("session") or [""])[0]
        limit = min(int((q.get("limit") or ["8"])[0]), 50)
        src = session_dir(session) / "cleaned" / "cleaned.jsonl"
        if not src.exists():
            raise StudioError("还没有清洗结果", "请先跑完清洗步骤")
        out = []
        with open(src, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = str(rec.get("text") or rec.get("caption") or "")
                img = rec.get("image_path")
                out.append({
                    "id": str(rec.get("id", ""))[:40],
                    "text": text[:300],
                    "image_rel": str(img) if img else None,
                })
        self._json({"ok": True, "rows": out})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        try:
            if u.path == "/api/upload":
                self._upload()
            elif u.path == "/api/check":
                body = self._read_json()
                self._json(do_check(
                    body["session"], body["filename"], body["scenario"],
                    bool(body.get("images_uploaded")),
                ))
            elif u.path == "/api/funnel":
                body = self._read_json()
                self._json({"ok": True, **start_funnel(
                    body["session"], body["filename"], body["scenario"],
                    int(body.get("limit") or 0),
                )})
            elif u.path == "/api/dataset":
                body = self._read_json()
                self._json({"ok": True, **start_dataset(
                    body["session"], body["name"],
                    int(body.get("pack_block_size") or 512),
                    float(body.get("val_ratio") or 0.1),
                    float(body.get("test_ratio") or 0.1),
                    int(body.get("max_tokens") or 4096),
                )})
            elif u.path == "/api/reset":
                body = self._read_json()
                d = session_dir(body["session"])
                shutil.rmtree(d / "cleaned", ignore_errors=True)
                self._json({"ok": True})
            else:
                self._err("没有这个接口", code=404)
        except StudioError as e:
            self._err(e.message, e.hint)
        except KeyError as e:
            self._err(f"请求缺字段：{e}", "刷新页面重试")
        except Exception as e:  # noqa: BLE001
            LOG.exception("POST %s 失败", u.path)
            self._err(f"服务器出错：{type(e).__name__}: {e}", code=500)

    # ── 上传：走 multipart，**流式写盘** ──
    def _upload(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._err("没有收到文件", "请重新选择文件")
            return
        if length > MAX_UPLOAD_BYTES:
            self._err("文件太大（超过 2GB）", "请先切分或抽样后再上传")
            return
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data; boundary=" not in ctype:
            self._err("上传格式不对（需要 multipart/form-data）")
            return
        boundary = ctype.split("boundary=")[-1].strip().strip('"').encode()
        # 分块读入（有上限），再解析 multipart。
        # 不整段 `rfile.read(length)` 是为了不因一个大文件吃掉几 GB 内存。
        rfile = self.rfile
        remaining = length
        buf = bytearray()
        while remaining > 0:
            chunk = rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            buf.extend(chunk)
            remaining -= len(chunk)
        session, files, _ = _parse_multipart(bytes(buf), boundary)
        if not session or not files:
            self._err("没解析出文件", "请重新选择文件")
            return
        sdir = session_dir(session)
        saved = {}
        for field, (name, blob) in files.items():
            if field == "data":
                dest = sdir / Path(name).name
                dest.write_bytes(blob)
                saved["data"] = dest.name
            elif field == "images":
                zdir = sdir / "images"
                if zdir.exists():
                    shutil.rmtree(zdir, ignore_errors=True)
                zdir.mkdir(parents=True, exist_ok=True)
                try:
                    with zipfile.ZipFile(__import__("io").BytesIO(blob)) as z:
                        for m in z.infolist():
                            #防 zip 滑出目标目录（Zip Slip）
                            tgt = (zdir / m.filename).resolve()
                            if not str(tgt).startswith(str(zdir.resolve())):
                                continue
                            if m.is_dir():
                                tgt.mkdir(parents=True, exist_ok=True)
                            else:
                                tgt.parent.mkdir(parents=True, exist_ok=True)
                                tgt.write_bytes(z.read(m))
                except zipfile.BadZipFile:
                    self._err("图片包不是合法的 zip", "请用 zip 打包图片目录后再上传")
                    return
                saved["images"] = True
        if "data" not in saved:
            self._err("没收到数据文件", "请重新选择文件")
            return
        self._json({"ok": True, **saved,
                    "images_uploaded": bool(saved.get("images"))})

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise StudioError("请求是空的")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:  # 降噪
        LOG.debug("%s - %s", self.address_string(), fmt % args)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff._-]{1,120}$")


def _parse_multipart(body: bytes, boundary: bytes):
    """极简 multipart 解析：拿 session 字段与文件字段。"""
    delim = b"--" + boundary
    session = None
    files: dict[str, tuple[str, bytes]] = {}
    for part in body.split(delim):
        if not part or part in (b"--\r\n", b"--"):
            continue
        part = part.lstrip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        raw_head, payload = part.split(b"\r\n\r\n", 1)
        payload = payload.rstrip(b"\r\n")
        head = raw_head.decode("utf-8", "replace")
        m = re.search(r'name="([^"]*)"', head)
        if not m:
            continue
        field = m.group(1)
        fm = re.search(r'filename="([^"]*)"', head)
        if fm:
            name = fm.group(1)
            if field == "data" and _SAFE_NAME.match(name) and \
                    Path(name).suffix.lower() in ALLOWED_SUFFIX:
                files[field] = (name, payload)
        elif field == "session":
            try:
                session = payload.decode("utf-8").strip()
            except UnicodeDecodeError:
                session = None
    return session, files, bool(files.get("images"))


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m mm_curation.studio.serve",
        description="多模态数据清洗 Studio（本地网页界面）",
        epilog="启动后浏览器会自动打开；数据只留在本机。",
    )
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认只监听本机；改成 0.0.0.0 会让同网段的人也能访问）")
    ap.add_argument("--port", type=int, default=8765, help="端口（被占用时换一个）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host}:{a.port}"
    print(f"\n  多模态数据清洗 Studio 已启动\n  请在浏览器打开：{url}\n")
    print(f"  数据目录：{UPLOAD_ROOT}（只在本机，不上传到任何服务器）")
    if a.host != "127.0.0.1":
        print(f"  ⚠ 你正在监听 {a.host}，同网段的其他人也能访问 —— 仅在可信网络下这么做")
    print("  按 Ctrl+C 停止\n")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
