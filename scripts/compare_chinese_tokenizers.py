"""中文 tokenizer 选型对比（现取判据，不拍脑袋）。

为什么要选：**tokenizer 决定数据集的成本结构**
- 压缩率（字符/token）低 →同样语料占更少显存/算力，但可能损失细节
- BERT 词级 vs byte-level BPE：前者vocab 小、序列长；后者序列短、覆盖全
- 是否需要 [CLS]/[SEP]（双向encoder 语义）对 causal LM 是**多余的 token**

⚠️ **两个候选都是本地缓存的**（HF 外网 SSL被挡，不能下新模型）：
- `uer/gpt2-chinese-cluecorpussmall`：BERT 词级，vocab 21128（名字叫gpt2 但实际是 BERT 结构）
- `Qwen/Qwen2.5-0.5B-Instruct`：byte-level BPE（vocab.json + merges.txt 齐全）

判据（全部现取，不预设结论）：
1. 压缩率 字符/token —— 越高越省
2. round-trip 保真度：decode(encode(t)) == t（**训练数据系统的硬要求**，
   解码不回来就不能用）
3. OOV/未登录字符数—— 越多越糟
4. 特殊 token 开销—— causal LM 不需要 [CLS]/[SEP]
5. 吞吐：编码速度（决定构建数据集要多久）

用法：python scripts/compare_chinese_tokenizers.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


CANDIDATES = [
    ("uer/gpt2-chinese-cluecorpussmall", "BERT 词级（vocab 21128）"),
    ("Qwen/Qwen2.5-0.5B-Instruct", "byte-level BPE（vocab 151k）"),
]

CORPORA = [
    pathlib.Path("data/raw/news_corpus.jsonl"),
    pathlib.Path("data/raw/finance_news/news_corpus.jsonl"),
]


def load_texts(limit: int = 1500) -> list[str]:
    out: list[str] = []
    for f in CORPORA:
        if not f.exists():
            continue
        for line in f.open(encoding="utf-8"):
            if not line.strip():
                continue
            t = json.loads(line).get("text") or ""
            if t:
                out.append(t)
            if len(out) >= limit:
                return out
    return out


def evaluate(name: str, tok, texts: list[str]) -> dict:
    # 1) 压缩率
    t0 = time.time()
    encs = tok(texts, add_special_tokens=False)["input_ids"]
    enc_s = time.time() - t0
    n_tok = sum(len(e) for e in encs)
    n_chr = sum(len(t) for t in texts)

    # 2) round-trip 保真（抽样 200 条，全量 decode 太慢）
    n_rt = min(200, len(texts))
    rt_ok = 0
    rt_bad: list[tuple[str, str]] = []
    for i in range(n_rt):
        got = tok.decode(encs[i], skip_special_tokens=True)
        if got == texts[i]:
            rt_ok += 1
        elif len(rt_bad) < 3:
            rt_bad.append((texts[i][:50], got[:50]))

    # 3) 特殊 token 开销：causal LM 不该被 [CLS]/[SEP] 吃掉位置
    specials = set(tok.all_special_tokens)
    n_special_id = sum(
        1 for e in encs[:500] for t in e if t in tok.all_special_ids
    )

    # 4) OOV：编码一批常用汉字，看有多少变成UNK
    probe = (
        "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出"
        "也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只"
        "如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意"
        "动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高已亲"
        "其进此话常与活正感"
    )
    unk = getattr(tok, "unk_token_id", None)
    n_unk = 0
    for ch in probe:
        ids = tok(ch, add_special_tokens=False)["input_ids"]
        if unk is not None and unk in ids:
            n_unk += 1

    return {
        "tokenizer": name,
        "vocab_size": int(getattr(tok, "vocab_size", 0)),
        "compression": round(n_chr / max(n_tok, 1), 3),
        "n_chars": n_chr,
        "n_tokens": n_tok,
        "roundtrip_rate": round(rt_ok / max(n_rt, 1), 4),
        "roundtrip_n": n_rt,
        "roundtrip_bad_examples": rt_bad,
        "special_token_count_in500": n_special_id,
        "special_tokens": sorted(specials),
        "n_unk_chars": n_unk,
        "encode_sec": round(enc_s, 2),
        "encode_docs_per_sec": round(len(texts) / max(enc_s, 1e-9), 1),
    }


def main() -> int:
    from transformers import AutoTokenizer

    texts = load_texts()
    print(f"真实语料 {len(texts)} 篇 | 字符 {sum(map(len, texts))}\n")

    rows: list[dict] = []
    for name, desc in CANDIDATES:
        try:
            tok = AutoTokenizer.from_pretrained(name)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {name}: {type(exc).__name__} {str(exc)[:120]}")
            continue
        r = evaluate(name, tok, texts)
        r["desc"] = desc
        rows.append(r)
        print(f"=== {name}  ({desc})")
        print(f"   压缩率       {r['compression']} 字符/token")
        print(f"   round-trip   {r['roundtrip_rate'] * 100:.1f}%  ({r['roundtrip_n']} 条抽样)")
        for a, b in r["roundtrip_bad_examples"]:
            print(f"      ✗ 原: {a!r}")
            print(f"        回: {b!r}")
        print(f"   特殊 token  {r['special_token_count_in500']} 个/500篇 "
              f"{r['special_tokens'][:6]}")
        print(f"   未登录汉字  {r['n_unk_chars']}")
        print(f"   编码速度{r['encode_docs_per_sec']} 篇/s\n")

    if not rows:
        print("无可用 tokenizer")
        return 1

    # ── 判据：先看硬门槛（round-trip 必须 1.0），再看成本 ──
    print("=" * 64)
    print("判读：")
    ok = [r for r in rows if r["roundtrip_rate"] == 1.0]
    if not ok:
        print("  ⚠️ **无一候选 round-trip 100%** → 都不能用于训练数据生产")
        print("    （解码不回来 = 语料被静默改写，这是硬门槛）")
    else:
        best = max(ok, key=lambda r: r["compression"])
        print(f"  ✅ round-trip 100% 的候选：{[r['tokenizer'] for r in ok]}")
        print(f"  → 压缩率最高：{best['tokenizer']} ({best['compression']} 字符/token)")
    print()
    print("  成本对比（同语料，占用显存/算力）：")
    for r in rows:
        print(f"    {r['tokenizer'][:42]:44s} {r['n_tokens']:>9d} token")

    rep = pathlib.Path("data/reports/tokenizer_benchmark.json")
    rep.write_text(
        json.dumps(
            {
                "n_texts": len(texts),
                "n_chars": sum(map(len, texts)),
                "candidates": rows,
                "note": "两个候选均为本地 HF 缓存（外网 SSL 被挡）。"
                "round-trip 是硬门槛；压缩率是成本判据。"
                "uer 实为 BERT 词级（含 [CLS]/[SEP]），对 causal LM 是多余开销。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已写 {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
