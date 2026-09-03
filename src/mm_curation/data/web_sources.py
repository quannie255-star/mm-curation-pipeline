"""网页数据获取器（V3 ζ1）：新闻源爬取 → Sample 协议 JSONL。

设计约束（PRD 八·风险表）：
- 解析与站点结构解耦：容器/链接 pattern 是配置，换源=换配置
- 合规：遵守 robots.txt（urllib.robotparser）+ 限速 + 明体面的 UA
- 幂等：已抓取的 URL 跳过（重跑不重复，断点续爬）
- 正文获取宽进严出：段落级粗滤（长度阈值）即可，深度清洗是漏斗的职责

首战源：中国新闻网滚动新闻（静态 HTML，robots Allow: /）。
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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) mm-curation-personal-tuner/0.1"

LISTING_URL = "https://www.chinanews.com.cn/scroll-news/news{n}.html"
ARTICLE_RE = re.compile(r'href="(/[^"]+/\d{4}/[\d-]+/\d+\.shtml)"')
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
PARA_MIN_CHARS = 20  # 段落粗滤：滤「顶部」/导航/分享按钮（深度清洗归漏斗）

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

    结构变体防御（实测图集页 /tp/ 的「阅读推荐」会把推荐标题包在 <a> 内的
    <p> 里混进容器）：先剥 <a> 块再取 <p>；结束标记取多候选中最早出现者。
    """
    i = html.find("left_zw")
    if i == -1:
        return None
    ends = [html.find(m, i) for m in ('id="backtop"', 'class="ydtj"', '<div class="share')]
    ends = [p for p in ends if p != -1]
    seg = html[i : min(ends) if ends else i + 80_000]
    seg = re.sub(r"<a\s[^>]*>.*?</a>", "", seg, flags=re.S)  # 剥掉链接块（推荐位/导航）
    paras = [TAG_RE.sub("", p).strip() for p in re.findall(r"<p[^>]*>(.*?)</p>", seg, re.S)]
    paras = [p for p in paras if len(p) >= PARA_MIN_CHARS]
    if not paras:
        return None
    t = TITLE_RE.search(html)
    title = TAG_RE.sub("", t.group(1)).strip() if t else ""
    return {"title": title, "paragraphs": paras}


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
    out_jsonl: Path, *, max_docs: int = 2000, delay: float = 1.0, max_listing_pages: int = 40
) -> int:
    """主循环：列表页 → 文章链接 → 逐篇抓取入库（限速+幂等+robots）。"""
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    seen_urls = load_seen_urls(out_jsonl)
    n_new, n_skip, n_fail = 0, 0, 0

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
            art = extract_article(html) if html else None
            if not art or len("".join(art["paragraphs"])) < 80:
                n_fail += 1
                continue
            append(
                {
                    "id": "news" + hashlib.md5(url.encode()).hexdigest()[:10],
                    "text": art["title"] + "\n\n" + "\n".join(art["paragraphs"]),
                    "meta": {"url": url, "title": art["title"], "source": "chinanews"},
                }
            )
            seen_urls.add(url)
            n_new += 1
    logging.info("新增 %s / 跳过已存在 %s / 解析失败 %s", n_new, n_skip, n_fail)
    return n_new
