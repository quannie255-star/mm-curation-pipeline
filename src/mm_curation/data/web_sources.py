"""网页数据获取器（V3 ζ1）：新闻源爬取 → Sample 协议 JSONL。

设计约束（PRD 八·风险表）：
- 解析与站点结构解耦：容器/链接 pattern 是配置，换源=换配置
- 合规：遵守 robots.txt（urllib.robotparser）+ 限速 + 明体面的 UA
- 幂等：已抓取的 URL 跳过（重跑不重复，断点续爬）
- 正文获取宽进严出：段落级粗滤（长度阈值）即可，深度清洗是漏斗的职责

首战源：中国新闻网滚动新闻（静态 HTML，robots Allow: /）。

**V6 W2-1 变更**：抓到的原始 HTML 现在落盘到 `RawDocStore`（内容寻址 gzip），
`meta.rawdoc` 记下 sha256。这是笔记 #65 的真修复——此前原始 HTML 从不落盘，
导致"换抽取器"不可重放、"新抽取器更好"没有可回放的输入可证。存档失败只会
warning，**绝不拖垮抓取**（存档是增值，不是前置条件）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from urllib import robotparser

from mm_curation.extract import NEWS_CN, RawDocStore

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) mm-curation-personal-tuner/0.1"

LISTING_URL = "https://www.chinanews.com.cn/scroll-news/news{n}.html"
ARTICLE_RE = re.compile(r'href="(/[^"]+/\d{4}/[\d-]+/\d+\.shtml)"')

#: 原始 HTML 存档根目录（已被 .gitignore 覆盖）
RAWDOC_ROOT = "data/raw/html"

_robots_cache: dict[str, robotparser.RobotFileParser] = {}


def fetch(url: str, *, retries: int = 3, timeout: float = 15.0) -> str | None:
    """GET 一个 URL（UA + 重试 + 退避），失败返回 None（调用方记数跳过）。"""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:  # 网络/超时/4xx5xx 一视同仁
            if attempt == retries:
                logging.warning("fetch 失败 %s: %s", url, e)
                return None
            time.sleep(2 * attempt)


def can_fetch(url: str) -> bool:
    """robots.txt 合规检查（按 host 缓存解析器；robots 拉不到时保守允许——
    目标源已人工核验 Allow: /，此处是防未来换源的护栏）。"""
    host = url.split("/")[2]
    if host not in _robots_cache:
        rp = robotparser.RobotFileParser()
        rp.set_url(f"https://{host}/robots.txt")
        try:
            rp.read()
        except Exception:
            logging.warning("robots.txt 不可达 %s（保守允许，人工已核验目标源）", host)
        _robots_cache[host] = rp
    return _robots_cache[host].can_fetch(UA, url) or _robots_cache[host].can_fetch("*", url)


def extract_links(listing_html: str) -> list[str]:
    """从滚动新闻列表页提取文章相对链接（去重保序）。"""
    seen, out = set(), []
    for rel in ARTICLE_RE.findall(listing_html):
        if rel not in seen:
            seen.add(rel)
            out.append("https://www.chinanews.com.cn" + rel)
    return out


def extract_article(html: str) -> dict | None:
    """解析文章页：标题 + 正文段落（容器内 <p>，长度粗滤）。

    **V6 W2-4：实现已搬到 `extract.news_cn.NewsCnExtractor`（tier 0），这里只剩
    一层薄适配。** 搬走而不是复制，是为了让"逐字等价"成为结构保证而非测试负担：
    留两份实现的话，谁改一边忘了另一边就会静默改变既有语料。

    返回 `dict` 而不是 `ExtractedArticle`：这是**对外保留的旧签名**，
    `scripts/fetch_news_corpus.py` 等调用方不需要改。

    语义提醒：容器缺失与"段落全被 20 字粗滤掉"都会返回 None（历史行为，
    刻意保留）。因此抽取链会把这两种情况都记成 `no_container`。
    """
    art = NEWS_CN.extract(html)
    if art is None:
        return None
    return {"title": art.title, "paragraphs": list(art.paragraphs)}


def load_seen_urls(out_jsonl: Path) -> set[str]:
    """幂等：读取已有产物的 URL 集合（断点续爬）。"""
    if not out_jsonl.exists():
        return set()
    seen = set()
    for ln in out_jsonl.read_text(encoding="utf-8").split("\n"):
        if ln.strip():
            try:
                seen.add(json.loads(ln)["meta"]["url"])
            except (json.JSONDecodeError, KeyError):
                continue
    return seen


def crawl(
    out_jsonl: Path,
    *,
    max_docs: int = 2000,
    delay: float = 1.0,
    max_listing_pages: int = 40,
    rawdoc_root: str | Path | None = RAWDOC_ROOT,
) -> int:
    """主循环：列表页 → 文章链接 → 逐篇抓取入库（限速+幂等+robots+原文存档）。

    `rawdoc_root=None` 可关掉原文存档（只用来做对照，默认开）。

    **为什么这里只用站点抽取器、不走 `ExtractionChain`**：链条的 tier1/tier2
    会救回此前判失败的文章，那是**改变既有语料**的动作，必须配一次 A/B 对照
    （W2-6）才能上线。这里保持旧路径，让"既有语料一字不变"这条线不被顺手破坏。
    """
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    store = RawDocStore(rawdoc_root) if rawdoc_root else None
    seen_urls = load_seen_urls(out_jsonl)
    n_new, n_skip, n_fail, n_archived = 0, 0, 0, 0

    def append(row: dict):
        with out_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    for page in range(1, max_listing_pages + 1):
        if n_new >= max_docs:
            break
        listing = fetch(LISTING_URL.format(n=page))
        if listing is None:
            continue
        urls = extract_links(listing)
        logging.info("列表页 %s: %s 篇候选", page, len(urls))
        for url in urls:
            if n_new >= max_docs:
                break
            if url in seen_urls:
                n_skip += 1
                continue
            if not can_fetch(url):
                logging.info("robots 禁止，跳过 %s", url)
                continue
            time.sleep(delay)  # 限速：对源站客气
            html = fetch(url)
            raw_sha = ""
            if html and store is not None:
                try:
                    raw_sha = store.put(html, url=url, meta={"source": "chinanews"}).sha256
                    n_archived += 1
                except OSError as e:  # 存档是增值项，磁盘异常不该拖垮抓取
                    logging.warning("原文存档失败 %s: %s", url, e)
            art = extract_article(html) if html else None
            if not art or len("".join(art["paragraphs"])) < 80:
                n_fail += 1
                continue
            meta = {"url": url, "title": art["title"], "source": "chinanews"}
            if raw_sha:
                meta["rawdoc"] = raw_sha  # 回指 RawDocStore，判决书可一路溯源
            append(
                {
                    "id": "news" + hashlib.md5(url.encode()).hexdigest()[:10],
                    "text": art["title"] + "\n\n" + "\n".join(art["paragraphs"]),
                    "meta": meta,
                }
            )
            seen_urls.add(url)
            n_new += 1
    logging.info(
        "新增 %s / 跳过已存在 %s / 解析失败 %s / 原文存档 %s", n_new, n_skip, n_fail, n_archived
    )
    return n_new
