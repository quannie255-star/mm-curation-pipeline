"""上传文件的真实性检查 —— 前端不能「来什么都收」。

外行最常见的失败不是算子跑不动，而是**文件格式不对**：
CSV 带BOM、JSONL 每行不是 JSON、图片路径写错相对根目录、
必填字段全空。这类问题必须在**跑之前**说清楚，否则用户等 20 分钟
才看到一句 `KeyError: 'text'`。

⚠️ 判据纪律：本模块的每个检查都必须是**真会拒的**。
所以 `tests/test_studio_upload.py` 里每条检查都有一个
「喂它一份**合法**文件必须放行」的对照 —— 只有这一侧，
才能证明拒的不是格式的普遍情况而是真的问题。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path

# 单独命名是为了避开「换行/制表符写进字符串字面量」这类编辑事故：
# 一旦它们变成真字符，文件会直接语法报错（比默默算错更容易发现）。
_NL = "\n"
_TSV = "\t"


@dataclass
class CheckResult:
    ok: bool
    n_rows: int
    n_bad_json: int
    missing_fields: dict[str, int]
    empty_fields: dict[str, int]
    notes: list[str]
    fatal: str = ""   # 非空= 直接拒绝的原因


def _sniff_delimiter(sample: str) -> str:
    """猜 CSV 分隔符：看哪个符号出现得多。

    不做这件事的后果很具体：Excel 导出的中文 CSV 常用逗号，
    而从数据库导的常用制表符或分号 ——猜错会把整行当成一个字段，
    然后报「缺text 字段」，用户完全看不懂。
    """
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # 退而求其次：数哪个符号出现最多
        counts = {d: sample.count(d) for d in ",;\t|"}
        best = max(counts, key=lambda k: counts[k])
        return best if counts[best] > 0 else ","


def _iter_records(path: Path, n_probe: int = 500):
    """读前 n_probe 行，返回 (行字典列表, 坏行数, 文件格式)。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    suffix = path.suffix.lower()
    rows: list[dict] = []
    bad = 0
    if suffix in (".csv", ".tsv"):
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return [], 0, "csv"
        delim = "\t" if suffix == ".tsv" else _sniff_delimiter("\n".join(lines[:20]))
        for row in csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delim):
            rows.append({(k or "").strip(): (v or "") for k, v in row.items()})
            if len(rows) >= n_probe:
                break
        return rows, bad, "csv"
    # 默认按 JSONL 处理
    for ln in text.splitlines()[:n_probe]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1   # 每行必须是对象；数组/标量无法与一条样本对应
    return rows, bad, "jsonl"


def check_upload(
    path: Path,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...] = (),
    image_root: Path | None = None,
    n_probe: int = 500,
) -> CheckResult:
    """检查一个上传文件是否能喂给指定场景。

    `image_root` 给了才会检查图片路径是否真的存在 ——
    图文场景必须给，否则「图缺失」要到跑清洗时才会暴露。
    """
    notes: list[str] = []
    if not path.exists():
        return CheckResult(False, 0, 0, {}, {}, notes, f"文件不存在：{path.name}")
    if path.stat().st_size == 0:
        return CheckResult(False, 0, 0, {}, {}, notes, "文件是空的（0 字节）")

    rows, bad_json, fmt = _iter_records(path, n_probe)
    if not rows:
        if fmt == "csv":
            return CheckResult(False, 0, 0, {}, {}, notes,
                               "CSV 里没读到数据行（只有表头？）")
        return CheckResult(False, 0, bad_json, {}, {}, notes,
                           f"前 {n_probe} 行里没有一行能解析成 JSON 对象"
                           + ("（看起来像 CSV，请用 .csv 后缀上传）"
                              if bad_json and path.suffix.lower() not in (".csv", ".tsv")
                              else ""))

    if bad_json:
        notes.append(f"前 {len(rows) + bad_json} 行里有 {bad_json} 行不是合法 JSON 对象，"
                     f"这些行会被跳过")

    # 必填字段：区分「键不存在」与「键存在但全空」—— 后者是另一种病
    missing: dict[str, int] = {}
    empty: dict[str, int] = {}
    for f in required_fields:
        absent = sum(1 for r in rows if f not in r)
        if absent:
            missing[f] = absent
        else:
            blanks = sum(1 for r in rows if not str(r.get(f, "")).strip())
            if blanks == len(rows):
                empty[f] = blanks
    if missing:
        detail = "、".join(f"`{k}`（{v} 行没有这个字段）" for k, v in sorted(missing.items()))
        return CheckResult(False, len(rows), bad_json, missing, empty, notes,
                           f"缺少必填字段：{detail}。"
                           f"每行都必须有这些字段（顺序无所谓，键名要一致）。")
    if empty:
        detail = "、".join(f"`{k}`" for k in sorted(empty))
        return CheckResult(False, len(rows), bad_json, missing, empty, notes,
                           f"字段 {detail} 在所有 {len(rows)} 行里都是空值 —— "
                           f"这通常意味着列名对不上，或数据真的没采到。")

    # 图片路径：只在给了根目录时真去查
    if "image_path" in required_fields:
        if image_root is None:
            notes.append("未提供图片根目录，跳过图片存在性检查")
        else:
            n_missing_img = 0
            probe = rows[:200]
            for r in probe:
                p = Path(str(r.get("image_path", "")))
                full = p if p.is_absolute() else (image_root / p)
                if not full.exists():
                    n_missing_img += 1
            if n_missing_img == len(probe):
                return CheckResult(False, len(rows), bad_json, missing, empty, notes,
                                   f"抽查 {len(probe)} 行的图片，**全部找不到**。"
                                   f"请确认 image_path 是相对「图片根目录」的路径，"
                                   f"且图片确实打包上传了。")
            if n_missing_img:
                notes.append(f"抽查 {len(probe)} 行里有 {n_missing_img} 行图片找不到"
                             f"（清洗时会被丢弃，不影响其它样本）")

    # 未知字段：只是提示，不拦。
    # CSV 要**看表头列名**而不是数据行的键 —— DictReader 遇到多余列会塞
    # `None` 键（restkey），而短行会补 None 值，只看行键会漏掉或误报。
    declared = set(required_fields) | set(optional_fields)
    seen_cols = {k for r in rows for k in r if isinstance(k, str) and k}
    if fmt == "csv":
        all_lines = [ln for ln in path.read_text(
            encoding="utf-8-sig", errors="replace").splitlines() if ln.strip()]
        if all_lines:
            delim = _TSV if path.suffix.lower() == ".tsv" else _sniff_delimiter(
                _NL.join(all_lines[:20]))
            header = next(csv.reader([all_lines[0]], delimiter=delim), [])
            seen_cols = {h.strip() for h in header if h and h.strip()}
    extra = sorted(seen_cols - declared)
    if extra:
        notes.append(f"文件里这些字段用不上（会被保留但不参与清洗判定）：{extra}")
    if any(not isinstance(k, str) for r in rows for k in r):
        notes.append("有些行的**列数比表头多**，多出来的内容会被丢弃 —— "
                     "检查是否有逗号或换行没转义")

    # 总行数：**CSV 要减掉表头行**，否则 n_rows 比真实数据多 1
    # （实测栽过：2 条数据被报成 3 条，前端会显示错的数据量）
    lines = [ln for ln in path.read_text(
        encoding="utf-8-sig", errors="replace").splitlines() if ln.strip()]
    total_rows = max(0, len(lines) - 1) if fmt == "csv" else len(lines)
    return CheckResult(True, total_rows, bad_json, missing, empty, notes)
