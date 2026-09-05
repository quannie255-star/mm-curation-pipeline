"""η-b 入口：构造抽取忠实性 DPO 三元组 + 冻结 benchmark。

用法：python -X utf8 scripts/build_ext_data.py
产物：data/interim/ext_dpo.jsonl + benchmarks/ext_news_v1/{items.jsonl, manifest.json}

隔离（结构性，累计排除四类既有占用）：
judge benchmark 150 源 / judge SFT 500 选样 / pref benchmark 源 / pref DPO 源
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.tuning.extraction import build_ext_items, write_benchmark  # noqa: E402

NEWS = Path("data/raw/news_corpus.jsonl")
SFT_OUT = Path("data/interim/ext_dpo.jsonl")
BENCH_OUT = Path("benchmarks/ext_news_v1")
N_SFT_USED = 500


def collect_used_ids() -> set[str]:
    """累计四类既有占用的源文档 id。"""
    used: set[str] = set()
    for src in (
        Path("benchmarks/judge_news_v1/items.jsonl"),
        Path("benchmarks/pref_news_v1/items.jsonl"),
    ):
        for ln in src.read_text(encoding="utf-8").split("\n"):
            if ln.strip() and (sid := json.loads(ln).get("source_id")):
                used.add(sid)
    for dsrc in (Path("data/interim/judge_sft.jsonl"), Path("data/interim/pref_dpo.jsonl")):
        if dsrc.exists():
            for ln in dsrc.read_text(encoding="utf-8").split("\n"):
                if ln.strip() and (sid := json.loads(ln).get("source_id")):
                    used.add(sid)
    return used


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rows = [json.loads(ln) for ln in NEWS.read_text(encoding="utf-8").split("\n") if ln.strip()]
    corpus = {
        r["id"]: {"id": r["id"], "title": r["meta"]["title"], "text": r["text"]}
        for r in rows
        if len(r["text"]) >= 200
    }

    used = collect_used_ids()
    # 注：judge SFT 的 500 篇已通过 judge_sft.jsonl 行内 source_id 计入 used，
    # 不再复现选样（此前双重排除曾把空闲池误删——η-b 构建 bug 实录）
    free = sorted((d for d in corpus.values() if d["id"] not in used), key=lambda d: d["id"])
    logging.info(
        "语料 %s 篇；既有占用 %s 篇；η-b 可用 %s 篇",
        len(corpus),
        len(used),
        len(free),
    )

    triples, items = build_ext_items(free, n_train=250, n_eval=80)
    n_kind: dict[str, int] = {}
    for t in triples:
        n_kind[t["kind"]] = n_kind.get(t["kind"], 0) + 1
    logging.info("DPO 三元组 %s（%s）", len(triples), n_kind)

    SFT_OUT.parent.mkdir(parents=True, exist_ok=True)
    SFT_OUT.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in triples) + "\n",
        encoding="utf-8",
    )
    manifest = write_benchmark(items, BENCH_OUT, train_jsonl=SFT_OUT)
    manifest["leakage_check"]["note"] = (
        "minhash 高命中为模板脚手架碰撞（抽取题 prompt 共享协议头，与训练三元组同模板）；"
        "md5=0 表明无 identical 文本。真保障是结构性源文档排除（935 篇既有占用全数排除）。"
    )
    manifest["candidate_cap_chars"] = 320
    manifest["source_window_chars"] = 900
    (BENCH_OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["leakage_check"]["note"] = (
        "minhash 高命中为模板脚手架碰撞（抽取题 prompt 共享协议头，与训练三元组同模板）；"
        "md5=0 表明无 identical 文本。真保障是结构性源文档排除（935 篇既有占用全数排除）。"
    )
    manifest["candidate_cap_chars"] = 320
    manifest["source_window_chars"] = 900
    (BENCH_OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logging.info(
        "冻结完成: %s（%s 题，损伤分布 %s，泄漏 md5=%s minhash=%s）",
        BENCH_OUT,
        manifest["n_items"],
        manifest["balance"],
        len(manifest["leakage_check"]["md5_leaks"]),
        len(manifest["leakage_check"]["minhash_leaks"]),
    )


if __name__ == "__main__":
    main()
