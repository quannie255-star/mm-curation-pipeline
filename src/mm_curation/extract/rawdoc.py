"""RawDoc：原始 HTML 的内容寻址存档（V6 W2-1）。

**为什么要有这一层**（这是真根因，不是"顺手补个功能"）：
`data/web_sources.py` 抓完 HTML 立刻抽成 text，**原始 HTML 从不落盘**。后果三条：

1. **换抽取器无法重放**——同一份网页只抽一次，抽取器的对照实验做不了；
2. **抽取层没有可比基线**——"新抽取器更好"没有可回放的输入去证明；
3. **笔记 #65 无法复现**——那次"爬虫空白膨胀 → `chinese_ratio` 误杀 778 篇"的
   现场只活在报告文字里，原始 HTML 已经不在了，事后只能靠复述。

内容寻址（sha256）顺带给三个红利：

- **去重免费**：同一份 HTML 抓十次只落一份盘（`n_docs` 与抓取次数解耦）；
- **可溯源**：判决书的 `input_fingerprint` 可以一路指回这里；
- **可校验**：读出时重算 sha 与路径比对，**损坏立刻暴露而不是静默降级**。

落盘布局：`<root>/<sha[:2]>/<sha>.html.gz` + 同名 `.meta.json` 边车。
边车存在的唯一理由：列目录时不该为了看一眼 url 就 gunzip 全部正文。

原子写：先写 `.tmp` 再 `os.replace`——中断不会留下半个文件（笔记 #2 的教训）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1


class RawDocCorrupted(RuntimeError):
    """存盘内容与它的 sha256 不再匹配——宁可炸，也不要静默给出坏正文。"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawDoc:
    """一份原始文档的存档记录。

    `text` 是**未经任何清洗**的原文（通常是 HTML）。这一层刻意不做任何加工——
    凡是加工都属于抽取层，加工过早会让"抽取器的选择"变成不可撤销的决定。
    """

    sha256: str
    urls: tuple[str, ...]
    fetched_at: str
    text: str
    meta: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        """首次观测到该内容的 URL（边车里 urls 是列表，这里取第一个）。"""
        return self.urls[0] if self.urls else ""

    @property
    def n_bytes(self) -> int:
        return len(self.text.encode("utf-8"))


class RawDocStore:
    """内容寻址的原始文档库。默认根目录 `data/raw/html`（已被 .gitignore 覆盖）。"""

    def __init__(self, root: str | Path = "data/raw/html") -> None:
        self.root = Path(root)

    # ---------- 路径 ----------

    def dir_for(self, sha: str) -> Path:
        self._check_sha(sha)
        return self.root / sha[:2]

    def path_for(self, sha: str) -> Path:
        return self.dir_for(sha) / f"{sha}.html.gz"

    def meta_path_for(self, sha: str) -> Path:
        return self.dir_for(sha) / f"{sha}.meta.json"

    @staticmethod
    def _check_sha(sha: str) -> None:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError(f"sha256 必须是 64 位小写十六进制，得 {sha!r}")

    # ---------- 写 ----------

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def put(
        self,
        text: str,
        *,
        url: str,
        fetched_at: str | None = None,
        meta: dict | None = None,
    ) -> RawDoc:
        """存入一份原文；同内容重复存入是幂等的（只累积 urls，不重写正文）。"""
        sha = sha256_of_text(text)
        fetched_at = fetched_at or utc_now_iso()
        body, meta_p = self.path_for(sha), self.meta_path_for(sha)

        if not body.exists():
            self._atomic_write(body, gzip.compress(text.encode("utf-8"), 6))

        ref = self.ref(sha)
        urls = list(ref["urls"]) if ref else []
        if url and url not in urls:
            urls.append(url)
        first_seen = ref["fetched_at"] if ref else fetched_at
        merged_meta = {**(ref["meta"] if ref else {}), **(meta or {})}
        self._atomic_write(
            meta_p,
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "sha256": sha,
                    "urls": urls,
                    "fetched_at": first_seen,
                    "n_bytes": len(text.encode("utf-8")),
                    "meta": merged_meta,
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )
        return RawDoc(
            sha256=sha,
            urls=tuple(urls),
            fetched_at=first_seen,
            text=text,
            meta=merged_meta,
        )

    # ---------- 读 ----------

    def exists(self, sha: str) -> bool:
        return self.path_for(sha).exists() and self.meta_path_for(sha).exists()

    def ref(self, sha: str) -> dict | None:
        """只读边车（不解压正文）——列目录/统计走这里。"""
        mp = self.meta_path_for(sha)
        if not mp.exists():
            return None
        try:
            row = json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return row if isinstance(row, dict) else None

    def get(self, sha: str) -> RawDoc | None:
        """读回原文；内容与 sha 不符时抛 `RawDocCorrupted`（不静默降级）。"""
        ref = self.ref(sha)
        body = self.path_for(sha)
        if ref is None or not body.exists():
            return None
        try:
            text = gzip.decompress(body.read_bytes()).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError) as e:
            raise RawDocCorrupted(f"{sha[:10]} 正文解压/解码失败: {e}") from e
        if sha256_of_text(text) != sha:
            raise RawDocCorrupted(
                f"{sha[:10]} 内容哈希与路径不符——存档损坏或被改写"
            )
        return RawDoc(
            sha256=sha,
            urls=tuple(ref.get("urls") or []),
            fetched_at=str(ref.get("fetched_at", "")),
            text=text,
            meta=dict(ref.get("meta") or {}),
        )

    def iter_refs(self) -> Iterator[dict]:
        """遍历全部边车（按 sha 排序，保证可复现）。"""
        if not self.root.exists():
            return
        for mp in sorted(self.root.glob("*/*.meta.json")):
            try:
                row = json.loads(mp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(row, dict) and "sha256" in row:
                yield row

    def __len__(self) -> int:
        return sum(1 for _ in self.iter_refs())

    def stats(self) -> dict:
        """库体积与压缩比——RawDoc 层唯一的"数字"，用于证明它没白占盘。"""
        n_docs = 0
        n_bytes_raw = 0
        n_bytes_stored = 0
        for ref in self.iter_refs():
            n_docs += 1
            n_bytes_raw += int(ref.get("n_bytes") or 0)
            body = self.path_for(str(ref["sha256"]))
            if body.exists():
                n_bytes_stored += body.stat().st_size
        return {
            "schema_version": SCHEMA_VERSION,
            "n_docs": n_docs,
            "n_bytes_raw": n_bytes_raw,
            "n_bytes_stored": n_bytes_stored,
            "compression_ratio": (
                round(n_bytes_stored / n_bytes_raw, 4) if n_bytes_raw else 0.0
            ),
        }
