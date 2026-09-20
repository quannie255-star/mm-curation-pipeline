#!/usr/bin/env python
"""W2-6 三口径抽取对照实验：同一份原始 HTML，三个抽取器各抽一次。

**为什么这份对照在 W2 之前做不出来**：老的 `web_sources.extract_article` 抓完
立刻出 text、原始 HTML 从不落盘，于是「换个抽取器会不会更好」这个问题
**没有输入可回放**。本脚本第 2 步（`store.put`）就是 W2-1 的兑现——落盘之后，
同一份 HTML 可以被 N 个抽取器反复抽，对照才成立。

口径（全部落在同一份 HTML 上，不重抓）：

| 指标 | 含义 |
|---|---|
| `n_ok` / `status_counts` | 各档成功/失败数，失败细分 none / no_paragraphs / too_short |
| `n_chars` p10/p50/p90 | 成功页面的正文长度分布（判断抽多了还是抽少了） |
| `coverage_vs_news_cn` | 同页该档字数 ÷ 站点抽取器字数（中位数）；<1 偏漏，>1 可能混入噪声 |
| `rescue_rate` | 站点抽取器失败、该档成功的比例（tier2 兜底器存在的唯一理由） |
| `loss_rate` | 站点抽取器成功、该档失败的比例（换成该档的代价） |
| `boilerplate_para_ratio` | 段落含导航/版权/推荐标记的比例——**代理指标，非人工质量判定** |

**诚实边界（写在这里而不是藏在脚注）**：

- 本报告**不含人工质量判定**。`extract_review_sample.jsonl` 是给人工抽检用的
  对照表（30 篇并排），抽检结论是独立输入，不由本脚本代填。
- `trafilatura` 未安装时该档记 `unavailable` 并**从分母中剔除**。把「没跑」算成
  「跑输了」是这类报告最常见的假结论。

用法：`python scripts/eval_extract_ablation.py --max-docs 60`
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_curation.data.connectors import CHINANEWS  # noqa: E402
from mm_curation.data.web_sources import can_fetch, fetch  # noqa: E402
from mm_curation.extract import (  # noqa: E402
    MIN_CHARS_DEFAULT,
    RawDocStore,
    available_extractors,
    get_extractor,
)

STATUS_LABEL = {
    "ok": "成功",
    "none": "抽不出",
    "no_paragraphs": "段落全空",
    "too_short": "正文过短",
    "unavailable": "依赖未装",
}

#: 代理指标用的噪声标记——只说明「这段像导航/版权」，不等于「这段没价值」
NOISE_MARKERS = ("责任编辑", "版权", "相关新闻", "推荐阅读", "首页", "转载", "来源：")

REVIEW_N = 30


def _evaluate(extractor, html: str) -> dict:
    """跑一档，压成可比的一行。长度政策与链条一致（`MIN_CHARS_DEFAULT`）。"""
    art = extractor.extract(html)
    if art is None:
        return {"status": "none", "n_chars": 0, "n_paragraphs": 0, "title": "", "text": "",
                "paragraphs": ()}
    if not art.paragraphs:
        return {
            "status": "no_paragraphs",
            "n_chars": 0,
            "n_paragraphs": 0,
            "title": art.title,
            "text": "",
            "paragraphs": (),
        }
    return {
        "status": "too_short" if art.n_chars < MIN_CHARS_DEFAULT else "ok",
        "n_chars": art.n_chars,
        "n_paragraphs": len(art.paragraphs),
        "title": art.title,
        "text": art.text(),
        "paragraphs": art.paragraphs,
    }


def _squash(text: str) -> str:
    """反转义 + 抹掉全部空白，再做包含判断。

    **反转义这一步不能省**：`heuristic` 会把 `Fijo&#322;ek` 还原成 `Fijołek`，而
    `news_cn` 不做（保持历史落库文本逐字不变）。不归一的话，同一个词会被判成
    「两边都有但字面不同」，从而把覆盖率**系统性低估**——那会得出「兜底器漏抽」
    的假结论。修这一处之前，实测该口径把 11% 的基准段错判成了未覆盖。
    """
    return re.sub(r"\s+", "", html_mod.unescape(text))


def _noise_ratio(text: str) -> float:
    paras = [p for p in text.split("\n") if p.strip()]
    if not paras:
        return 0.0
    hit = sum(1 for p in paras if any(m in p for m in NOISE_MARKERS))
    return round(hit / len(paras), 4)


def _pct(values: list[float], q: float) -> float:
    """分位数（近邻法，不插值）——样本量小时插值会造出并不存在的精度。"""
    if not values:
        return 0.0
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
    return float(xs[idx])


def _summarize(rows: list[dict], name: str) -> dict:
    """汇总一档。

    `unavailable` 从分母剔除：把「依赖没装、压根没跑」算成「跑输了」，是这类
    对照报告最常见的假结论。它只以计数形式出现在失败细分里。
    """
    cells = [r["extractors"][name] for r in rows if name in r["extractors"]]
    counts: dict[str, int] = {}
    for c in cells:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    usable = [c for c in cells if c["status"] != "unavailable"]
    ok = [c for c in usable if c["status"] == "ok"]
    lens = [c["n_chars"] for c in ok]
    return {
        "n_evaluated": len(usable),
        "n_unavailable": counts.get("unavailable", 0),
        "n_ok": len(ok),
        "status_counts": {STATUS_LABEL.get(k, k): v for k, v in sorted(counts.items())},
        "n_chars_p10": _pct(lens, 0.10),
        "n_chars_p50": _pct(lens, 0.50),
        "n_chars_p90": _pct(lens, 0.90),
        "n_paragraphs_p50": _pct([c["n_paragraphs"] for c in ok], 0.50),
        "noise_para_ratio_p50": _pct([_noise_ratio(c["text"]) for c in ok], 0.50),
    }


def _usable(r: dict, *names: str) -> bool:
    return all(
        n in r["extractors"] and r["extractors"][n]["status"] != "unavailable" for n in names
    )


def _cross(rows: list[dict], base: str, other: str) -> dict:
    """交叉口径：base 的失败/成功在 other 上的表现。

    分母为 0 时返回 None 而不是 0——0 会被读成「救回率 0%」，None 才会被读成
    「没有样本」。任一档 unavailable 的页面整页排除：那一页给不出可比的两个数。
    """
    both = [r for r in rows if _usable(r, base, other)]
    base_fail = [r for r in both if r["extractors"][base]["status"] != "ok"]
    base_ok = [r for r in both if r["extractors"][base]["status"] == "ok"]
    rescued = [r for r in base_fail if r["extractors"][other]["status"] == "ok"]
    lost = [r for r in base_ok if r["extractors"][other]["status"] != "ok"]
    pair = [
        r["extractors"][other]["n_chars"] / r["extractors"][base]["n_chars"]
        for r in base_ok
        if r["extractors"][other]["status"] == "ok" and r["extractors"][base]["n_chars"] > 0
    ]

    # 段落级覆盖口径——把「缺字」与「多抽」分开。**这是本脚本最有信息量的两个数**：
    # `_squash` 后做包含判断，因此站点档把行内 <a> 剥掉造成的缺字（基准段是另一档
    # 段的子串）算「被覆盖」，而侧栏推荐位那种凭空多出来的才算「多抽」。
    n_base_p = n_base_covered = n_other_p = n_other_extra = 0
    for r in base_ok:
        o = r["extractors"][other]
        if o["status"] != "ok":
            continue
        bp = [_squash(p) for p in r["extractors"][base]["paragraphs"] if _squash(p)]
        op = [_squash(p) for p in o["paragraphs"] if _squash(p)]
        n_base_p += len(bp)
        n_other_p += len(op)
        n_base_covered += sum(1 for p in bp if any(p in q for q in op))
        n_other_extra += sum(1 for q in op if not any(p in q for p in bp))

    return {
        "n_base_fail": len(base_fail),
        "n_rescued": len(rescued),
        "n_base_ok": len(base_ok),
        "n_lost": len(lost),
        "rescue_rate": round(len(rescued) / len(base_fail), 4) if base_fail else None,
        "loss_rate": round(len(lost) / len(base_ok), 4) if base_ok else None,
        "coverage_vs_base_p50": round(statistics.median(pair), 4) if pair else None,
        "n_base_paragraphs": n_base_p,
        "base_covered_rate": round(n_base_covered / n_base_p, 4) if n_base_p else None,
        "n_other_paragraphs": n_other_p,
        "other_extra_rate": round(n_other_extra / n_other_p, 4) if n_other_p else None,
    }


def _evaluate_all(html: str, usable: list[str], missing: list[str]) -> dict:
    per = {n: _evaluate(get_extractor(n)(), html) for n in usable}
    for n in missing:
        per[n] = {
            "status": "unavailable",
            "n_chars": 0,
            "n_paragraphs": 0,
            "title": "",
            "text": "",
            "paragraphs": (),
        }
    return per


def _collect_from_store(raw_root: Path, limit: int) -> list[dict]:
    """**离线重跑**：只读已落盘的 RawDoc，一次网络请求都不发。

    这是 W2-1 最直观的红利——指标口径想改就改，代价是零次重抓。
    也正因为抽取的输入被固定下来了，「换了指标后结论变了」不会与「这次抓到的
    页面不一样」混在一起。
    """
    store = RawDocStore(raw_root)
    names = available_extractors()
    usable = [n for n in names if get_extractor(n).available()]
    missing = [n for n in names if not get_extractor(n).available()]
    refs = sorted(store.iter_refs(), key=lambda r: str(r.get("fetched_at", "")))
    rows: list[dict] = []
    for ref in refs[:limit] if limit else refs:
        doc = store.get(str(ref["sha256"]))
        if doc is None:
            continue
        rows.append(
            {
                "url": doc.url,
                "rawdoc": doc.sha256,
                "n_html_chars": len(doc.text),
                "extractors": _evaluate_all(doc.text, usable, missing),
            }
        )
    return rows


def _collect(max_docs: int, delay: float, raw_root: Path) -> tuple[list[dict], list[str], int]:
    store = RawDocStore(raw_root)
    refs, listing_failures = CHINANEWS.discover(fetch)
    names = available_extractors()
    usable = [n for n in names if get_extractor(n).available()]
    missing = [n for n in names if not get_extractor(n).available()]

    rows: list[dict] = []
    n_fetch_fail = 0
    for ref in refs[:max_docs]:
        if not can_fetch(ref.url):
            continue
        time.sleep(delay)
        html = fetch(ref.url)
        if not html:
            n_fetch_fail += 1
            continue
        sha = store.put(html, url=ref.url, meta={"source": "chinanews"}).sha256
        rows.append(
            {
                "url": ref.url,
                "rawdoc": sha,
                "n_html_chars": len(html),
                "extractors": _evaluate_all(html, usable, missing),
            }
        )
    return rows, listing_failures, n_fetch_fail


def _write_review(rows: list[dict], path: Path, names: list[str]) -> int:
    """人工抽检对照表：等间隔取样（确定性可复现），各档并排、各截 400 字。"""
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0
    step = max(1, len(rows) // REVIEW_N)
    picked = rows[::step][:REVIEW_N]
    with path.open("w", encoding="utf-8") as f:
        for r in picked:
            f.write(
                json.dumps(
                    {
                        "url": r["url"],
                        "rawdoc": r["rawdoc"],
                        "review": {n: r["extractors"][n]["text"][:400] for n in names},
                        "review_status": {n: r["extractors"][n]["status"] for n in names},
                        "human_verdict": "",  # 待人工填：clean / mixed / garbage
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(picked)


def _table_rows(per: dict, tiers: dict) -> list[str]:
    out = []
    for n, d in per.items():
        fail = "、".join(f"{k} {v}" for k, v in d["status_counts"].items() if k != "成功") or "—"
        out.append(
            f"| `{n}` | {tiers[n]} | {d['n_evaluated']} | **{d['n_ok']}** | {fail} | "
            f"{d['n_chars_p10']:.0f} / {d['n_chars_p50']:.0f} / {d['n_chars_p90']:.0f} | "
            f"{d['n_paragraphs_p50']:.0f} | {d['noise_para_ratio_p50']:.1%} |"
        )
    return out


def _cross_rows(cross: dict) -> list[str]:
    out = []
    for n, c in cross.items():
        rr = "—" if c["rescue_rate"] is None else f"**{c['rescue_rate']:.1%}**"
        lr = "—" if c["loss_rate"] is None else f"{c['loss_rate']:.1%}"
        cv = "—" if c["coverage_vs_base_p50"] is None else f"{c['coverage_vs_base_p50']:.3f}"
        bc = "—" if c["base_covered_rate"] is None else f"{c['base_covered_rate']:.1%}"
        oe = "—" if c["other_extra_rate"] is None else f"**{c['other_extra_rate']:.1%}**"
        out.append(
            f"| `{n}` | {c['n_base_fail']} | {c['n_rescued']} | {rr} | "
            f"{c['n_base_ok']} | {c['n_lost']} | {lr} | {cv} | {bc} | {oe} |"
        )
    return out


def _render_md(p: dict) -> str:
    st = p["rawdoc_stats"]
    lines = [
        "# 三口径抽取对照实验（V6 W2-6）",
        "",
        f"- 生成时间：{p['generated_at']}（模式 {p['mode']}）",
        f"- 语料：中国新闻网滚动新闻，**样本 {p['n_docs']} 篇**"
        f"（抓取失败 {p['n_fetch_fail']} 篇，索引页失败 {len(p['listing_failures'])} 个）",
        f"- 原始 HTML 存档：`{p['rawdoc_root']}`"
        f"（{st['n_docs']} 份 / {st['n_bytes_raw']} 字节原文 → {st['n_bytes_stored']} 字节落盘，"
        f"压缩比 {st['compression_ratio']}）",
        f"- 长度政策：正文短于 {p['min_chars']} 字记 `too_short`（与链条同一口径）",
        "",
        "> 同一份原始 HTML 被各档各抽一次——RawDoc 落盘（W2-1）之后才可能做的对照。",
        "",
        "## 各档结果",
        "",
        "| 档位 | tier | 评估页数 | 成功 | 失败细分 | 正文字数 p10/p50/p90 | 段数 p50"
        " | 噪声段占比 p50 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += _table_rows(p["per_extractor"], p["tiers"])
    if p["unavailable"]:
        listed = "、".join(f"`{n}`" for n in p["unavailable"])
        lines += [
            "",
            f"> ⚠️ 未参与对照：{listed}（依赖未安装）。`n_evaluated` 已把它们剔除"
            "——「没跑」不等于「跑输了」。",
        ]

    lines += [
        "",
        "## 交叉口径（基准 = `news_cn`，站点专用档）",
        "",
        "| 对照档 | 基准失败页 | 其中被救回 | 救回率 | 基准成功页 | 其中被丢 | 丢失率"
        " | 同页字数比 p50 | 基准段被覆盖 | 多抽段占比 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += _cross_rows(p["cross"])

    lines += [
        "",
        "## 阅读方式（避免把代理指标当结论）",
        "",
        "- **救回率**回答「tier2 兜底器值不值得存在」；"
        "**丢失率 / 字数比**回答「能不能直接把兜底器设成默认」。",
        "- **基准段被覆盖**与**多抽段占比**是一对，必须一起读：前者低说明基准**漏抽**，"
        "后者高说明对照档**多抽**。只看字数比会把这两种完全不同的毛病混成一个数。",
        "- 段落包含判断前会做了反转义 + 抹空白（两档归一后再比）。"
        "不归一的话，兜底器还原 `&#322;` 而成的字面差异会被误判成「基准段未被覆盖」，"
        "把覆盖率系统性低估——修这一处时实测它曾把 11% 的基准段错判为未覆盖。",
        "- **基准段被覆盖率不足 100% 不等于兜底器漏抽**：抽查逐条归因后，剩余缺口主要来自"
        "基准档自己——它的 `<a>` 剥离正则会删掉段落**内部**的行内链接文本（导语里的「中新社」"
        "署名就这么整批消失），并且一个跨段的 `<a>` 会把多段内容拼成一段，"
        "使基准段落在原文里都不是连续子串。要看「有没有真漏内容」，得看原始 HTML，"
        "不能只看这个比值。",
        "- `n_chars` 更大**不等于**抽得更好：把导航、推荐位、版权声明一起抽进来同样会让字数变多。"
        "所以字数比必须**与噪声段占比、多抽段占比一起读**。",
        "- `noise_para_ratio` 是**关键词代理**，不是人工判定的准确率；真实质量结论需人工抽检。",
        f"- 本报告**未包含人工质量判定**：`{p['review_path']}` 是 {p['review_n']} 篇的三档并排"
        "对照表，`human_verdict` 一列留空待填。",
        "",
        "## 复现",
        "",
        "```bash",
        f"python scripts/eval_extract_ablation.py --max-docs {p['n_docs']}",
        "# 离线重跑（只读已存档原文，零网络请求——改指标不必重抓）",
        f"python scripts/eval_extract_ablation.py --from-rawdoc --max-docs {p['n_docs']}",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="三口径抽取对照实验（W2-6）")
    ap.add_argument("--max-docs", type=int, default=60, help="最多采集/对照多少篇")
    ap.add_argument("--delay", type=float, default=1.0, help="逐篇限速秒数（对源站客气）")
    ap.add_argument("--rawdoc-root", default="data/raw/html")
    ap.add_argument("--out", default="data/reports/extract_ablation")
    ap.add_argument(
        "--from-rawdoc",
        action="store_true",
        help="离线重跑：只读已存档原文，不发任何网络请求（改指标时用这个）",
    )
    args = ap.parse_args()

    if args.from_rawdoc:
        rows = _collect_from_store(Path(args.rawdoc_root), args.max_docs)
        listing_failures, n_fetch_fail, mode = [], 0, "offline(rawdoc)"
    else:
        rows, listing_failures, n_fetch_fail = _collect(
            args.max_docs, args.delay, Path(args.rawdoc_root)
        )
        mode = "crawl"
    names = list(available_extractors())
    reviewed = [n for n in names if rows and rows[0]["extractors"][n]["status"] != "unavailable"]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    review_path = out.parent / "extract_review_sample.jsonl"
    n_review = _write_review(rows, review_path, reviewed)

    payload = {
        "stage": "V6-W2-6",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": mode,
        "source": CHINANEWS.spec(),
        "n_docs": len(rows),
        "n_fetch_fail": n_fetch_fail,
        "listing_failures": listing_failures,
        "min_chars": MIN_CHARS_DEFAULT,
        "rawdoc_root": args.rawdoc_root,
        "rawdoc_stats": RawDocStore(args.rawdoc_root).stats(),
        "tiers": {n: get_extractor(n).tier for n in names},
        "unavailable": [n for n in names if not get_extractor(n).available()],
        "per_extractor": {n: _summarize(rows, n) for n in names},
        "cross": {
            n: _cross(rows, "news_cn", n)
            for n in names
            if n != "news_cn" and get_extractor(n).available()
        },
        "review_path": str(review_path),
        "review_n": n_review,
        "human_verdict": "待人工抽检填写",
    }
    out.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out.with_suffix(".md").write_text(_render_md(payload), encoding="utf-8")

    print(f"样本 {len(rows)} 篇 → {out}.json / {out}.md")
    print(f"抽检对照表 {review_path}（{n_review} 篇，human_verdict 待填）")
    for n in names:
        d = payload["per_extractor"][n]
        print(
            f"  {n:12s} tier{payload['tiers'][n]}  "
            f"成功 {d['n_ok']}/{d['n_evaluated']}  {d['status_counts']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
