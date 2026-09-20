"""源接入协议化（V6 W2-5）：把"站点耦合"从代码变成声明。

**修的是什么**：`web_sources.py` 里 `LISTING_URL` / `ARTICLE_RE` / `extract_article`
三处都硬编码了中国新闻网。想加第二个源，必须改代码。这不是"不够优雅"的抱怨——
它的真实代价是**换源成本无法估算**：新源到底要写几行、踩几个坑，只能在
真正动手那天才知道，于是"扩语料"永远排不到优先级前面。

这里把源拆成三个正交的部分：

| 关注点 | 归属 | 形态 |
|---|---|---|
| 去哪找链接（列表/Feed/Sitemap） | **`SourceConnector`** | 声明：模板 + 正则 / XML |
| 怎么抽正文 | `Extractor`（W2-1/2/3） | 三级链按 tier 竞争 |
| 原始 HTML 存哪 | `RawDocStore`（W2-1） | 内容寻址 gzip |

于是 `SourceConnector` 只剩下两件必须由站点知识回答的事：
`listing_urls()`（要抓哪些索引页）与 `parse_listing()`（索引页里哪些是正文链接）。

**三个实现覆盖三类真实源**：

- `StaticListingConnector`——静态滚动/栏目列表页（正则抽相对链接），chinanews 属此类；
- `RssConnector`——RSS 2.0 / Atom（`xml.etree` 解析），多数新闻站都提供，**这是 MVP 之外
  最省事的扩源路径**；
- `SitemapConnector`——`sitemap.xml` / `sitemapindex`，用于"我只想按时间捞一批 URL，
  不要站点导航噪声"的场景。

**离线可测**：三个 connector 都只做"HTML/XML 文本 → URL 列表"的纯变换，
网络入口（`fetch_fn`）是显式参数。因此协议的正确性完全不依赖联网——
`tests/test_connectors.py` 用假 fetch 就能验，并额外做一次**与旧 `crawl` 的逐字节等价**
对照（见 `test_ingest_matches_legacy_crawl_byte_for_byte`）。

**XML 安全**：抓来的 feed/sitemap 是不可信输入。`xml.etree` 默认不展开外部实体，
但对"十亿笑声"这类实体膨胀攻击无防护，而本环境没有 `defusedxml`。所以这里用两条
廉价护栏顶上：**拒绝任何含 DOCTYPE 的文档**（实体声明必须先有 DOCTYPE）+ **限制
输入长度**。真要吃不受信 XML 的场合应当换 `defusedxml`，这一点写在这里免得被忘掉。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mm_curation.extract import MIN_CHARS_DEFAULT, ExtractionChain, RawDocStore, get_extractor
from mm_curation.extract import default_chain as default_extract_chain

from .web_sources import can_fetch as default_can_fetch
from .web_sources import fetch as default_fetch
from .web_sources import load_seen_urls

#: 抓取函数签名：`(url) -> str | None`（失败返回 None，不抛）
Fetcher = Callable[[str], "str | None"]
#: robots 判定签名：`(url) -> bool`
RobotChecker = Callable[[str], bool]

#: 单个 XML 文档上限——拒绝超大输入（实体膨胀攻击的第一道闸）
MAX_XML_CHARS = 8_000_000


class ListingParseError(ValueError):
    """索引页/Feed/Sitemap 解析失败。

    故意**抛**而不是返回空列表：空列表会伪装成"这个源今天没有新文章"，
    让限流、反爬页、结构改版三种完全不同的问题长得一模一样。
    """


@dataclass(frozen=True)
class SourceRef:
    """一条候选文档。`meta` 放列表页上顺手拿到的信息（发布时间/栏目），可留空。"""

    url: str
    meta: dict = field(default_factory=dict)


class SourceConnector(ABC):
    """源协议：只回答"去哪找链接"，其余一概不管。

    **配置在实例上，不在类上**：`name` / `extractor` / `delay` 都是类级默认值，
    构造时可逐个覆盖。这样"加一个源"是**构造一个对象**，不是写一个新的类——
    源的数量应当受限于站点数，而不是受限于我们愿意写多少个 subclass。
    """

    #: 源名（进 `meta.source`，也是 `doc_id_prefix` 的默认值）
    name: str = ""
    #: 文档 id 前缀；`crawl` 用 "news" + md5[:10]，这里显式声明以便对齐旧产物
    id_prefix: str = ""
    #: 默认抽取器名；空串表示"不指定，走抽取链竞争"
    extractor: str = ""
    #: 逐文档限速（秒）；对源站客气，也是反爬的基本礼貌
    delay: float = 1.0

    def __init__(self, *, name: str = "", **overrides) -> None:
        if name:
            self.name = name
        if not self.name:
            raise TypeError(f"{type(self).__name__} 需要非空的 name（报告里要按源归因）")
        for k, v in overrides.items():
            if not hasattr(type(self), k):
                raise TypeError(f"{type(self).__name__} 不认识参数 {k!r}")
            setattr(self, k, v)

    @property
    def doc_id_prefix(self) -> str:
        return self.id_prefix or self.name

    @property
    def extractor_chain(self) -> ExtractionChain:
        """本源的抽取链：指定了 `extractor` 就单级，否则走全局默认链。

        **为什么不是无条件用默认链**：指定单级才能让"换源"与"换抽取器"两个变量
        分开可测；一刀切走链条会让源之间的对比混入抽取器差异。
        """
        if self.extractor:
            return ExtractionChain(
                [get_extractor(self.extractor)()], min_chars=MIN_CHARS_DEFAULT
            )
        return default_extract_chain()

    @abstractmethod
    def listing_urls(self) -> tuple[str, ...]:
        """要抓的索引页 URL（列表页 / feed / sitemap）。"""

    @abstractmethod
    def parse_listing(self, text: str, *, url: str) -> list[SourceRef]:
        """索引页文本 → 候选文档（**去重保序**由 `discover` 统一负责）。"""

    def discover(self, fetch_fn: Fetcher) -> tuple[list[SourceRef], list[str]]:
        """跑一遍索引页，返回 `(候选列表, 失败说明)`。

        失败说明单独返回而不是只打日志：抓取规模、限流命中率是**要进报告的**，
        不能只活在 stderr 里。
        """
        out: list[SourceRef] = []
        failures: list[str] = []
        for url in self.listing_urls():
            text = fetch_fn(url)
            if not text:
                failures.append(f"{url}: 索引页不可达")
                continue
            try:
                out.extend(self.parse_listing(text, url=url))
            except ListingParseError as e:
                failures.append(f"{url}: {e}")
        return _dedupe(out), failures

    def doc_id(self, url: str) -> str:
        """文档 id：`<前缀> + md5(url)[:10]`——与 `web_sources.crawl` 的历史口径一致。"""
        return self.doc_id_prefix + hashlib.md5(url.encode()).hexdigest()[:10]

    def spec(self) -> dict:
        """可序列化的源声明——"换源=换配置"的落点。

        **必须包含 `extractor` / `id_prefix` / `delay`**：漏掉它们的话，按 spec
        重建出来的源会**悄悄换掉抽取器**（比如从 `news_cn` 掉回默认链）。
        这类漂移不会报错、只会让语料慢慢变形，是最难察觉的一种。
        """
        return {
            "name": self.name,
            "kind": type(self).__name__,
            "extractor": self.extractor,
            "id_prefix": self.id_prefix,
            "delay": self.delay,
            **self._spec_fields(),
        }

    def _spec_fields(self) -> dict:
        return {}


def _dedupe(refs: list[SourceRef]) -> list[SourceRef]:
    """按 URL 去重、保序（先出现的赢，因为索引页顺序通常就是时间序）。"""
    seen: set[str] = set()
    out: list[SourceRef] = []
    for r in refs:
        if r.url and r.url not in seen:
            seen.add(r.url)
            out.append(r)
    return out


# ===================== 实现一：静态列表页 =====================


class StaticListingConnector(SourceConnector):
    """静态滚动/栏目列表页：URL 模板 + 链接正则。

    `listing_template` 里用 `{n}` 占位页码；`link_re` 的第 1 个捕获组是文章链接
    （相对路径会与 `base_url` 拼接）。

    `base_url` 为空时要求 `link_re` 直接捕获绝对 URL——两种都支持，因为
    静态站的写法两种都有，让调用方按现场事实选，而不是我们猜。
    """

    link_re: str = r'href="(/[^"]+\.html)"'

    def __init__(
        self,
        *,
        name: str,
        listing_template: str,
        link_re: str = "",
        base_url: str = "",
        max_listing_pages: int = 1,
        start_page: int = 1,
        extractor: str = "",
        id_prefix: str = "",
        delay: float = 1.0,
    ) -> None:
        super().__init__(name=name, extractor=extractor, id_prefix=id_prefix, delay=delay)
        if "{n}" not in listing_template:
            raise ValueError("listing_template 必须含 {n} 页码占位（单页源用 {n}=1 亦可）")
        self.listing_template = listing_template
        self.link_re = link_re or type(self).link_re
        self.base_url = base_url
        self.max_listing_pages = max_listing_pages
        self.start_page = start_page

    def listing_urls(self) -> tuple[str, ...]:
        return tuple(
            self.listing_template.format(n=p)
            for p in range(self.start_page, self.start_page + self.max_listing_pages)
        )

    def parse_listing(self, text: str, *, url: str) -> list[SourceRef]:
        found = re.compile(self.link_re).findall(text)
        if not found:
            raise ListingParseError("未匹配到任何文章链接（结构改版或被反爬页替换？）")
        refs = []
        for hit in found:
            link = hit if isinstance(hit, str) else hit[0]
            refs.append(SourceRef(url=link if self.base_url == "" else self.base_url + link))
        return refs

    def _spec_fields(self) -> dict:
        return {
            "listing_template": self.listing_template,
            "link_re": self.link_re,
            "base_url": self.base_url,
            "max_listing_pages": self.max_listing_pages,
            "start_page": self.start_page,
        }


# ===================== 实现二：RSS / Atom =====================


def _parse_xml(text: str, *, what: str) -> ET.Element:
    """带两道护栏的 XML 解析（见模块 docstring 的"XML 安全"）。"""
    if len(text) > MAX_XML_CHARS:
        raise ListingParseError(f"{what} 超过 {MAX_XML_CHARS} 字符上限，拒绝解析")
    if "<!doctype" in text[:4096].lower():
        raise ListingParseError(f"{what} 含 DOCTYPE 声明，为确保安全拒绝解析")
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise ListingParseError(f"{what} XML 解析失败: {e}") from e


def _local(tag: str) -> str:
    """去命名空间取本地名——RSS/Atom/Sitemap 三套命名空间写法各异，按名匹配最稳。"""
    return tag.rsplit("}", 1)[-1]


def _first_text(elem: ET.Element, *names: str) -> str:
    for child in elem:
        if _local(child.tag) in names:
            return (child.text or "").strip()
    return ""


def _locs_of(root: ET.Element) -> list[str]:
    """取出全部 `<loc>` 文本（保序）——`urlset` 与 `sitemapindex` 共用同一形状。"""
    return [
        (e.text or "").strip()
        for e in root.iter()
        if _local(e.tag) == "loc" and (e.text or "").strip()
    ]


class RssConnector(SourceConnector):
    """RSS 2.0 / Atom：一条 feed 就是一批候选，无需翻页。

    链接取值规则（按优先级，两者都覆盖 RSS 与 Atom 的主流写法）：
    1. `<link>` 的**文本**（RSS 2.0）；
    2. `<link href="...">`（Atom），优先 `rel="alternate"`，其次无 `rel`；
    3. `<guid isPermaLink="true">` 的文本（RSS 站常见的兜底）。
    """

    def __init__(
        self,
        *,
        name: str,
        feed_url: str,
        extractor: str = "",
        id_prefix: str = "",
        delay: float = 1.0,
    ) -> None:
        super().__init__(name=name, extractor=extractor, id_prefix=id_prefix, delay=delay)
        self.feed_url = feed_url

    def listing_urls(self) -> tuple[str, ...]:
        return (self.feed_url,)

    def parse_listing(self, text: str, *, url: str) -> list[SourceRef]:
        root = _parse_xml(text, what="feed")
        entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
        if not entries:
            raise ListingParseError("feed 里没有 item/entry（不是 feed？或换了格式？）")
        refs = []
        for e in entries:
            link = _entry_link(e)
            if link:
                refs.append(
                    SourceRef(
                        url=link,
                        meta={
                            k: v
                            for k, v in (
                                ("title", _first_text(e, "title")),
                                ("published", _first_text(e, "pubDate", "published", "updated")),
                            )
                            if v
                        },
                    )
                )
        return refs

    def _spec_fields(self) -> dict:
        return {"feed_url": self.feed_url}


def _entry_link(e: ET.Element) -> str:
    href_fallback = ""
    for child in e:
        if _local(child.tag) != "link":
            continue
        text = (child.text or "").strip()
        if text:  # RSS 2.0：<link>https://…</link>
            return text
        href = (child.get("href") or "").strip()
        rel = (child.get("rel") or "alternate").lower()
        if href and rel == "alternate":
            return href
        href_fallback = href_fallback or href
    return href_fallback or _first_text(e, "guid")


# ===================== 实现三：Sitemap =====================


class SitemapConnector(SourceConnector):
    """`sitemap.xml` / `sitemapindex`：按时间捞 URL，绕开站点导航噪声。

    `sitemapindex` 会指向若干子 sitemap，`discover` 会展开它们（受
    `max_sitemaps` 限制——不设上限等于把对方站点全量拉一遍，那是攻击行为不是采集）。
    """

    def __init__(
        self,
        *,
        name: str,
        sitemap_url: str,
        max_sitemaps: int = 1,
        extractor: str = "",
        id_prefix: str = "",
        delay: float = 1.0,
    ) -> None:
        super().__init__(name=name, extractor=extractor, id_prefix=id_prefix, delay=delay)
        self.sitemap_url = sitemap_url
        self.max_sitemaps = max_sitemaps

    def listing_urls(self) -> tuple[str, ...]:
        return (self.sitemap_url,)

    def parse_listing(self, text: str, *, url: str) -> list[SourceRef]:
        locs = _locs_of(_parse_xml(text, what="sitemap"))
        if not locs:
            raise ListingParseError("sitemap 里没有 <loc>")
        return [SourceRef(url=loc) for loc in locs]

    @staticmethod
    def is_index(text: str) -> bool:
        """是 `sitemapindex`（loc 指向子 sitemap）还是 `urlset`（loc 就是文档）？"""
        return _local(_parse_xml(text, what="sitemap").tag) == "sitemapindex"

    def discover(self, fetch_fn: Fetcher) -> tuple[list[SourceRef], list[str]]:
        """展开 sitemapindex → 子 sitemap → 文档 URL（受 `max_sitemaps` 限制）。

        只解析根文档一次：`is_index` 与 `parse_listing` 各解析一遍属于白做功，
        而且解析两遍意味着两条可能不一致的失败路径。
        """
        failures: list[str] = []
        root_text = fetch_fn(self.sitemap_url)
        if not root_text:
            return [], [f"{self.sitemap_url}: sitemap 不可达"]
        try:
            root = _parse_xml(root_text, what="sitemap")
        except ListingParseError as e:
            return [], [f"{self.sitemap_url}: {e}"]

        locs = _locs_of(root)
        if not locs:
            return [], [f"{self.sitemap_url}: sitemap 里没有 <loc>"]

        if _local(root.tag) != "sitemapindex":  # urlset：loc 就是文档 URL
            return _dedupe([SourceRef(url=loc) for loc in locs]), failures

        out: list[SourceRef] = []
        for child_url in locs[: self.max_sitemaps]:
            text = fetch_fn(child_url)
            if not text:
                failures.append(f"{child_url}: 子 sitemap 不可达")
                continue
            try:
                out.extend(self.parse_listing(text, url=child_url))
            except ListingParseError as e:
                failures.append(f"{child_url}: {e}")
        return _dedupe(out), failures

    def _spec_fields(self) -> dict:
        return {"sitemap_url": self.sitemap_url, "max_sitemaps": self.max_sitemaps}


# ===================== 编排：discover → fetch → archive → extract → JSONL =====================


@dataclass
class IngestStats:
    """一次采集的账目——**每个数都要能解释差异**，不能只有一个总数。"""

    connector: str
    n_pages_failed: int = 0
    n_candidates: int = 0
    n_new: int = 0
    n_skip: int = 0
    n_fail: int = 0
    n_archived: int = 0
    listing_failures: list[str] = field(default_factory=list)

    def to_row(self) -> dict:
        return {
            "connector": self.connector,
            "n_pages_failed": self.n_pages_failed,
            "n_candidates": self.n_candidates,
            "n_new": self.n_new,
            "n_skip": self.n_skip,
            "n_fail": self.n_fail,
            "n_archived": self.n_archived,
            "listing_failures": list(self.listing_failures),
        }


def ingest(
    connector: SourceConnector,
    *,
    out_jsonl: Path,
    store: RawDocStore | None = None,
    fetch_fn: Fetcher = default_fetch,
    can_fetch_fn: RobotChecker = default_can_fetch,
    max_docs: int = 2000,
    delay: float | None = None,
    chain: ExtractionChain | None = None,
) -> IngestStats:
    """协议化的采集主循环：与 `web_sources.crawl` 同源同口径，但源是声明出来的。

    `chain` 不传时用 `connector.extractor_chain`（指定了单抽取器则单级，
    否则走全局默认链）。**限速、robots、幂等、80 字闸门** 四项行为与旧 `crawl`
    逐项对齐，因此两者在同一个假 fetch 下应当产出逐字节相同的 JSONL
    ——这条等价由 `test_ingest_matches_legacy_crawl_byte_for_byte` 锁住。

    `meta` 里刻意**不放 UA / 抓取时间**：UA 是 fetch 的关注点、时间在
    `RawDoc` 边车里已经有了。文档 meta 里塞运行期信息会让"同一份内容两次采集
    是否等价"这个比较失去意义，也会直接破坏与旧产物的逐字节等价。
    """
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    chain = chain or connector.extractor_chain
    delay = connector.delay if delay is None else delay
    seen_urls = load_seen_urls(out_jsonl)

    refs, listing_failures = connector.discover(fetch_fn)
    stats = IngestStats(
        connector=connector.name,
        n_pages_failed=len(listing_failures),
        n_candidates=len(refs),
        listing_failures=listing_failures,
    )

    for ref in refs:
        if stats.n_new >= max_docs:
            break
        if ref.url in seen_urls:
            stats.n_skip += 1
            continue
        if not can_fetch_fn(ref.url):
            logging.info("robots 禁止，跳过 %s", ref.url)
            continue
        if delay:
            time.sleep(delay)
        html = fetch_fn(ref.url)
        raw_sha = _archive(store, html, ref=ref, connector=connector, stats=stats)
        art, _attempts = chain.extract(html) if html else (None, [])
        if art is None:
            stats.n_fail += 1
            continue

        meta: dict = {
            "url": ref.url,
            "title": art.title,
            "source": connector.name,
        }
        if raw_sha:
            meta["rawdoc"] = raw_sha
        if ref.meta:
            meta["listing"] = dict(ref.meta)
        _append(
            out_jsonl,
            {"id": connector.doc_id(ref.url), "text": art.text(), "meta": meta},
        )
        seen_urls.add(ref.url)
        stats.n_new += 1

    logging.info(
        "源 %s：新增 %s / 跳过 %s / 失败 %s / 存档 %s（候选 %s，索引页失败 %s）",
        connector.name,
        stats.n_new,
        stats.n_skip,
        stats.n_fail,
        stats.n_archived,
        stats.n_candidates,
        stats.n_pages_failed,
    )
    return stats


def _archive(
    store: RawDocStore | None,
    html: str | None,
    *,
    ref: SourceRef,
    connector: SourceConnector,
    stats: IngestStats,
) -> str:
    """存原文；**存档失败只 warning**——它是增值项，不是采集的前置条件。"""
    if not html or store is None:
        return ""
    try:
        sha = store.put(html, url=ref.url, meta={"source": connector.name}).sha256
    except OSError as e:
        logging.warning("原文存档失败 %s: %s", ref.url, e)
        return ""
    stats.n_archived += 1
    return sha


def _append(out_jsonl: Path, row: dict) -> None:
    with out_jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ===================== 预置源 =====================

#: 中国新闻网滚动新闻——与 `web_sources.crawl` 的历史口径逐字对齐
#: （同模板、同正则、同 id 前缀 "news"、同单抽取器 news_cn）
CHINANEWS = StaticListingConnector(
    name="chinanews",
    listing_template="https://www.chinanews.com.cn/scroll-news/news{n}.html",
    link_re=r'href="(/[^"]+/\d{4}/[\d-]+/\d+\.shtml)"',
    base_url="https://www.chinanews.com.cn",
    max_listing_pages=40,
    extractor="news_cn",
    id_prefix="news",
)


_KINDS: dict[str, type[SourceConnector]] = {
    "StaticListingConnector": StaticListingConnector,
    "RssConnector": RssConnector,
    "SitemapConnector": SitemapConnector,
}


def from_spec(spec: dict) -> SourceConnector:
    """按 `spec()` 重建源——「换源=换配置」的另一半。

    只有 `spec()` 能写出去、`from_spec()` 能读回来，源才真的是一条配置；
    否则 `spec()` 只是一份给人看的说明，加源的代价照旧是改代码。
    """
    kind = spec.get("kind")
    if kind not in _KINDS:
        raise KeyError(f"未知的源类型 {kind!r}；已知 {sorted(_KINDS)}")
    return _KINDS[kind](**{k: v for k, v in spec.items() if k != "kind"})
