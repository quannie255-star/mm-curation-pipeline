"""benchmark 构建器（V3 ζ2）：域评测集的构建、防污染与版本冻结。

产品语义：benchmark 是「我 care 的能力」的可执行定义——版本冻结、来源
可追溯、与训练集物理隔离。judge 的可信度只对这个冻结版本负责。

独立性三原则（防 judge「背题」）：
1. seed 隔离：benchmark 的污染 seed 必须与训练集不同（构造期强制校验）
2. 配比隔离：损伤 kinds/rate 与训练集不同（manifest 双方互查）
3. 泄漏检查：md5 精确 + MinHash 近似，对训练集文件双重去重，结果进 manifest

产物：benchmarks/<name>/items.jsonl（{id,text,label,kind}）+ manifest.json
（版本/seed/来源指纹/损伤配比/泄漏检查结果/标注协议）。两者一起提交进仓
库——冻结的 benchmark 是产品资产，不是可再生数据。
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from curation_eval import ContaminationPlan, Sample


@dataclass
class BenchmarkSpec:
    """构建规格：全部进 manifest，构建后不可变（改规格 = 新版本）。"""

    name: str
    domain_desc: str  # 域描述（人读，进 manifest）
    n_clean: int = 150
    n_dirty: int = 150
    seed: int = 9000  # ★必须与训练集 seed 不同（build 时强制断言）
    train_seeds: tuple[int, ...] = ()  # 已知训练集 seed 列表（隔离校验用）
    kinds: dict[str, float] = field(
        default_factory=lambda: {
            "paragraph_repeat": 0.4,
            "boilerplate_inject": 0.3,
            "whitespace_pad": 0.3,
        }
    )

    def __post_init__(self) -> None:
        if not self.kinds:
            raise ValueError("kinds 为空")
        if self.seed in self.train_seeds:
            raise ValueError(
                f"benchmark seed {self.seed} 与训练集 seed 撞车——独立性原则 1；"
                "换一个 seed，并在 manifest 里登记训练集 seeds"
            )


def _fingerprint(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _minhash_keys(text: str, bands: int = 8, rows: int = 10) -> set[bytes]:
    """轻量 MinHash 签名（byte 4-gram，与 dedup_fast 同语义族），取 band 签名做近邻查重。"""
    import numpy as np

    data = text.encode("utf-8")[:600]
    arr = np.frombuffer(data.ljust(4, b"\x00"), dtype=np.uint8)
    if arr.size < 4:
        return set()
    win = np.lib.stride_tricks.sliding_window_view(arr, 4).astype(np.uint64)
    h = (win * np.uint64(2654435761)).sum(axis=1)
    rng = np.random.default_rng(7)
    a, b = rng.integers(1, 2**31, 80), rng.integers(0, 2**31, 80)
    sig = ((a[:, None] * h[None, :] + b[:, None]) % np.uint64(2147483647)).min(axis=1)
    return {sig[i * rows : (i + 1) * rows].tobytes() for i in range(bands)}


def _leak_check(items: list[dict], train_jsonl: Path | None) -> dict:
    """对训练集做 md5 + MinHash band 双重泄漏检查（结果进 manifest）。"""
    report: dict = {
        "train_file": str(train_jsonl) if train_jsonl else None,
        "md5_leaks": [],
        "minhash_leaks": [],
    }
    if train_jsonl is None or not train_jsonl.exists():
        report["note"] = "训练集尚未产出，泄漏检查随训练集构建后补跑（build_train 时强制）"
        return report
    train_rows = [
        json.loads(ln) for ln in train_jsonl.read_text(encoding="utf-8").split("\n") if ln.strip()
    ]

    def _row_text(r: dict) -> str:
        return r.get("text") or r.get("prompt") or ""

    md5s = {_fingerprint(_row_text(r)) for r in train_rows}
    band_keys: dict[bytes, str] = {}
    for n, r in enumerate(train_rows):
        for k in _minhash_keys(_row_text(r)):
            band_keys.setdefault(k, r.get("id", f"train{n}"))
    for it in items:
        it_text = _row_text(it)
        if _fingerprint(it_text) in md5s:
            report["md5_leaks"].append(it["id"])
            continue
        if any(k in band_keys for k in _minhash_keys(it_text)):
            report["minhash_leaks"].append(it["id"])
    return report


def build_benchmark(
    corpus: list[Sample],
    spec: BenchmarkSpec,
    out_dir: Path,
    *,
    train_jsonl: Path | None = None,
    images_out: Path | None = None,
) -> dict:
    """从域语料构建冻结 benchmark：干净采样 + 污染注入 + 泄漏检查 + 落盘。"""
    if len(corpus) < spec.n_clean:
        raise ValueError(f"域语料不足：需 {spec.n_clean}，只有 {len(corpus)}")
    rng = random.Random(spec.seed)
    pool = sorted(corpus, key=lambda s: s.id)  # 排序保采样确定性
    rng.shuffle(pool)
    clean = pool[: spec.n_clean]

    plan = ContaminationPlan(inject_rate=1.0, seed=spec.seed, kinds=spec.kinds)
    dirty_all, manifest_counts = plan.run(
        [Sample(id=s.id, text=s.text) for s in clean],
        images_out or Path("data/interim/bench_images"),
    )
    dirty = [s for s in dirty_all if s.labels.get("dirty")][: spec.n_dirty]

    items = [
        {
            "id": f"bm-{_fingerprint(s.text)[:10]}",
            "text": s.text,
            "label": "clean",
            "kind": "clean",
            "source_id": s.id,
        }
        for s in clean
    ] + [
        {
            "id": f"bm-{_fingerprint(s.text)[:10]}",
            "text": s.text,
            "label": "dirty",
            "kind": s.labels["dirty"],
            "source_id": None,
        }
        for s in dirty
    ]
    # 污染器是有放回选样：同一篇可能被同种损伤注入两次产生全同文本——
    # benchmark 里同一文本只保留一份（id 由文本 md5 派生，去重即去同 id）
    seen_ids, deduped = set(), []
    for it in items:
        if it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            deduped.append(it)
    items = deduped

    leak = _leak_check(items, train_jsonl)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "items.jsonl").write_text(
        "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "benchmark": spec.name,
        "version": "v1",
        "created": date.today().isoformat(),
        "domain": spec.domain_desc,
        "n_items": len(items),
        "label_balance": {
            "clean": sum(1 for it in items if it["label"] == "clean"),
            "dirty": sum(1 for it in items if it["label"] == "dirty"),
        },
        "contamination": {"seed": spec.seed, "kinds": spec.kinds, "counts": manifest_counts},
        "train_seed_isolation": {
            "benchmark_seed": spec.seed,
            "train_seeds": list(spec.train_seeds),
        },
        "leakage_check": leak,
        "label_protocol": "clean=域内正常文本；dirty=程序化注入损伤（kinds 见上）；"
        "judge 任务：二分类 keep/drop，κ 与 P/R 对本表结算",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
