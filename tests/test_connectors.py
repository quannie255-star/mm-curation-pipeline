"""源接入协议单测（V6 W2-5）：三个 connector + 编排等价性。

**全部离线**：connector 的活是"文本 → URL 列表"，网络入口 `fetch_fn` 是显式参数，
所以协议的正确性不依赖联网。

本文件最重要的一条是 `test_ingest_matches_legacy_crawl_byte_for_byte`：
把老的 `web_sources.crawl` 与新的 `ingest()` 跑在**同一个假 fetch** 上，
断言两份 JSONL **逐字节相同**。"源接入协议化没改变既有语料"这句话，
在这里是一个可执行的断言，而不是一句说明。
"""

from __future__ import annotations

import json

import pytest

from mm_curation.data import connectors as C
from mm_curation.data import web_sources as ws
from mm_curation.data.connectors import (
    CHINANEWS,
    IngestStats,
    ListingParseError,
    RssConnector,
    SitemapConnector,
    SourceConnector,
    SourceRef,
    StaticListingConnector,
    ingest,
)

# ---------- 夹具 ----------

BODY = (
    "这是第一段正文内容，长度超过二十个字符的阈值线，讲述铺轨启动的消息。"
    "这是第二段正文，同样满足长度过滤条件，介绍项目背景与线路走向等。"
)


def article_html(h1: str) -> str:
    """两篇文章用**不同标题**，这样等价对照既验 id 拼法也验正文拼接。"""
    return f"""
<html><head><title>站点标题</title></head><body>
<h1>{h1}</h1>
<div class="left_zw">
  <p>{BODY}</p>
  <p>再补一段正文，把总长度稳稳推过链条的八十字符闸门，避免偶发落选。</p>
</div>
<div id="backtop"></div>
</body></html>
"""


ARTICLE = article_html("标题测试：沪渝蓉高铁铺轨")
ARTICLE_2 = article_html("标题测试：另一篇内容不同的稿子")

LISTING = """
<html><body><ul>
  <li><a href="/gn/2026/09-03/10689481.shtml">甲</a></li>
  <li><a href="/cj/2026/09-03/10689486.shtml">乙</a></li>
  <li><a href="/aboutus/staff/x.html">员工页（不该被收集）</a></li>
  <li><a href="/gn/2026/09-03/10689481.shtml">甲重复</a></li>
</ul></body></html>
"""

A1 = "https://www.chinanews.com.cn/gn/2026/09-03/10689481.shtml"
A2 = "https://www.chinanews.com.cn/cj/2026/09-03/10689486.shtml"
LISTING_TPL = "https://www.chinanews.com.cn/scroll-news/news{n}.html"

RSS2 = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>甲篇标题</title>
    <link>https://example.com/a/1</link>
    <pubDate>Sat, 20 Sep 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>乙篇标题</title>
    <guid isPermaLink="true">https://example.com/a/2</guid>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>甲</title>
    <link rel="self" href="https://example.com/self-not-an-article"/>
    <link rel="alternate" href="https://example.com/a/1"/>
  </entry>
  <entry>
    <title>乙</title>
    <link href="https://example.com/a/2"/>
  </entry>
</feed>
"""

SITEMAP_URLSET = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a/1</loc></url>
  <url><loc>https://example.com/a/2</loc></url>
</urlset>
"""

SITEMAP_INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sm-1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sm-2.xml</loc></sitemap>
</sitemapindex>
"""


def _site_connector(**kw) -> StaticListingConnector:
    base = {
        "name": "chinanews",
        "listing_template": LISTING_TPL,
        "link_re": r'href="(/[^"]+/\d{4}/[\d-]+/\d+\.shtml)"',
        "base_url": "https://www.chinanews.com.cn",
        "max_listing_pages": 2,
        "extractor": "news_cn",
        "id_prefix": "news",
    }
    return StaticListingConnector(**{**base, **kw})


def _site_fetch(url: str) -> str | None:
    if url.startswith("https://www.chinanews.com.cn/scroll-news/"):
        page = url.rsplit("/news", 1)[-1].removesuffix(".html")
        return LISTING if page == "1" else "<html><body>本页无文章</body></html>"
    if url == A1:
        return ARTICLE
    if url == A2:
        return ARTICLE_2  # 内容不同 → 内容寻址下会落两份，顺便验去重没误伤
    return None


def _allow_all(_url: str) -> bool:
    return True


# ===================== 静态列表页 =====================


def test_static_listing_urls_paging():
    assert _site_connector(max_listing_pages=3).listing_urls() == (
        LISTING_TPL.format(n=1),
        LISTING_TPL.format(n=2),
        LISTING_TPL.format(n=3),
    )


def test_static_listing_template_requires_page_placeholder():
    with pytest.raises(ValueError, match=r"\{n\}"):
        StaticListingConnector(name="x", listing_template="https://e.com/list.html")


def test_static_listing_parse_is_raw_and_discover_dedupes():
    """`parse_listing` 忠实返回索引页内容（含重复），去重归 `discover`。

    把去重放在 `discover` 而不是 `parse_listing`，是为了让"索引页长什么样"
    可被原样观察——否则想统计"这个源的列表页重复率"就没数了。
    """
    conn = _site_connector()
    raw = conn.parse_listing(LISTING, url=LISTING_TPL.format(n=1))
    assert [r.url for r in raw] == [A1, A2, A1]  # 员工页被链接正则排除
    assert all(r.meta == {} for r in raw)  # 静态列表页拿不到额外信息，不硬编

    refs, _ = conn.discover(_site_fetch)
    assert [r.url for r in refs] == [A1, A2]  # 去重保序


def test_static_listing_raises_when_no_link_matches():
    """反爬页/改版必须**报错**，不能伪装成"今天没有新文章"。"""
    with pytest.raises(ListingParseError):
        _site_connector().parse_listing("<html><body>访问过于频繁</body></html>", url="u")


def test_discover_reports_failed_listing_pages_but_keeps_others():
    def flaky(url: str):
        return LISTING if url.endswith("news1.html") else None

    refs, failures = _site_connector(max_listing_pages=2).discover(flaky)
    assert [r.url for r in refs] == [A1, A2]
    assert len(failures) == 1 and "索引页不可达" in failures[0]


# ===================== RSS / Atom =====================


def test_rss2_reads_link_text_and_meta():
    refs = RssConnector(name="rss_a", feed_url="https://e.com/feed").parse_listing(RSS2, url="u")
    assert [r.url for r in refs] == ["https://example.com/a/1", "https://example.com/a/2"]
    assert refs[0].meta["title"] == "甲篇标题"
    assert refs[0].meta["published"].startswith("Sat, 20 Sep 2026")
    assert "published" not in refs[1].meta  # 没给就不编


def test_atom_prefers_rel_alternate_over_rel_self():
    refs = RssConnector(name="atom_a", feed_url="https://e.com/feed").parse_listing(ATOM, url="u")
    assert [r.url for r in refs] == ["https://example.com/a/1", "https://example.com/a/2"]


def test_feed_without_entries_raises():
    with pytest.raises(ListingParseError, match="item/entry"):
        RssConnector(name="r1", feed_url="u").parse_listing("<rss><channel/></rss>", url="u")


def test_xml_rejects_doctype_declaration():
    """实体膨胀攻击靠 DOCTYPE 声明实体；本环境无 defusedxml，用拒收顶上。"""
    bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel/></rss>'
    with pytest.raises(ListingParseError, match="DOCTYPE"):
        RssConnector(name="r2", feed_url="u").parse_listing(bomb, url="u")


def test_xml_rejects_oversized_document(monkeypatch):
    monkeypatch.setattr(C, "MAX_XML_CHARS", 100)
    with pytest.raises(ListingParseError, match="上限"):
        SitemapConnector(name="s1", sitemap_url="u").parse_listing("<urlset>" + "x" * 200, url="u")


def test_malformed_xml_raises_listing_parse_error():
    with pytest.raises(ListingParseError, match="XML 解析失败"):
        RssConnector(name="r3", feed_url="u").parse_listing("<rss><channel>", url="u")


# ===================== Sitemap =====================


def test_sitemap_urlset_returns_document_urls():
    refs, failures = SitemapConnector(name="s2", sitemap_url="https://e.com/sm.xml").discover(
        lambda u: SITEMAP_URLSET
    )
    assert [r.url for r in refs] == ["https://example.com/a/1", "https://example.com/a/2"]
    assert failures == []


def test_sitemap_index_expands_children_up_to_limit():
    """不设上限等于把对方全站拉一遍——那是攻击行为不是采集。"""
    seen: list[str] = []

    def fetch(url: str):
        seen.append(url)
        return SITEMAP_INDEX if url.endswith("sm.xml") else SITEMAP_URLSET

    conn = SitemapConnector(name="s3", sitemap_url="https://e.com/sm.xml", max_sitemaps=1)
    refs, failures = conn.discover(fetch)
    assert seen == ["https://e.com/sm.xml", "https://example.com/sm-1.xml"]
    assert [r.url for r in refs] == ["https://example.com/a/1", "https://example.com/a/2"]
    assert failures == []
    assert SitemapConnector.is_index(SITEMAP_INDEX) and not SitemapConnector.is_index(
        SITEMAP_URLSET
    )


def test_sitemap_records_child_and_root_failures():
    conn = SitemapConnector(name="s4", sitemap_url="https://e.com/sm.xml", max_sitemaps=2)
    refs, failures = conn.discover(lambda u: SITEMAP_INDEX if u.endswith("sm.xml") else None)
    assert refs == [] and len(failures) == 2

    refs, failures = conn.discover(lambda u: None)
    assert refs == [] and failures == ["https://e.com/sm.xml: sitemap 不可达"]


def test_sitemap_without_loc_raises():
    with pytest.raises(ListingParseError, match="loc"):
        SitemapConnector(name="s5", sitemap_url="u").parse_listing("<urlset/>", url="u")


# ===================== 协议与声明 =====================


def test_connector_requires_nonempty_name():
    """空 name 必须在构造期就炸——报告里没法归因的源等于没接。"""
    with pytest.raises(TypeError, match="非空的 name"):
        RssConnector(name="", feed_url="https://e.com/feed")


def test_connector_rejects_unknown_parameter():
    with pytest.raises(TypeError):
        SitemapConnector(name="s6", sitemap_url="u", not_a_real_option=1)


def test_spec_round_trips_through_from_spec():
    """`spec()` 写出去、`from_spec()` 读回来必须**完全不变**。

    先有 `spec()` 没 `from_spec()` 时，`spec()` 只是一份给人看的说明书；
    加上反向还原，它才真是一条配置。这条测试也是 `spec()` 漏字段的守卫
    ——早先它漏了 `extractor`，按 spec 重建会把抽取器悄悄掉回默认链。
    """
    for conn in (
        CHINANEWS,
        RssConnector(name="r", feed_url="https://e.com/feed", extractor="heuristic"),
        SitemapConnector(name="s", sitemap_url="https://e.com/sm.xml", max_sitemaps=3),
    ):
        spec = conn.spec()
        assert json.loads(json.dumps(spec, ensure_ascii=False)) == spec  # 可序列化
        assert C.from_spec(spec).spec() == spec  # 往返无损

    spec = CHINANEWS.spec()
    assert spec["kind"] == "StaticListingConnector"
    assert spec["max_listing_pages"] == 40 and spec["extractor"] == "news_cn"
    assert spec["id_prefix"] == "news" and spec["base_url"].startswith("https://www.chinanews")
    with pytest.raises(KeyError, match="未知的源类型"):
        C.from_spec({"kind": "NoSuchConnector", "name": "x"})


def test_extractor_chain_is_single_stage_when_declared():
    """源声明了抽取器就单级——否则"换源"与"换抽取器"两个变量会互相污染。"""
    single = _site_connector().extractor_chain
    assert [type(e).name for e in single.extractors] == ["news_cn"]

    auto = _site_connector(extractor="").extractor_chain
    assert [type(e).name for e in auto.extractors] == ["news_cn", "trafilatura", "heuristic"]


def test_doc_id_keeps_legacy_prefix():
    assert _site_connector().doc_id(A1).startswith("news")
    assert len(_site_connector().doc_id(A1)) == len("news") + 10


def test_source_ref_and_stats_shapes():
    assert SourceRef("u").meta == {}
    row = IngestStats("s", n_new=1, listing_failures=["x"]).to_row()
    assert row["connector"] == "s" and row["n_new"] == 1 and row["listing_failures"] == ["x"]
    assert isinstance(_site_connector(), SourceConnector)


# ===================== 编排：与旧 crawl 逐字节等价（核心） =====================


def test_ingest_matches_legacy_crawl_byte_for_byte(tmp_path, monkeypatch):
    """**本文件最重要的一条**：源接入协议化没有改变既有语料。

    老 `crawl` 与新 `ingest` 跑在同一个假 fetch 上，两份 JSONL 必须逐字节相同
    ——包含 `id` 的拼法、`meta` 的键序、正文拼接的换行数。任何一处口径漂移都会
    在这里炸，而不是等到某天发现语料悄悄变了。
    """
    monkeypatch.setattr(ws, "fetch", _site_fetch)
    monkeypatch.setattr(ws, "can_fetch", _allow_all)

    legacy = tmp_path / "legacy.jsonl"
    ws.crawl(legacy, max_docs=10, delay=0.0, max_listing_pages=2, rawdoc_root=tmp_path / "raw_a")

    modern = tmp_path / "modern.jsonl"
    store = C.RawDocStore(tmp_path / "raw_b")
    stats = ingest(
        _site_connector(),
        out_jsonl=modern,
        store=store,
        fetch_fn=_site_fetch,
        can_fetch_fn=_allow_all,
        max_docs=10,
        delay=0.0,
    )

    a, b = legacy.read_bytes(), modern.read_bytes()
    assert a and a == b
    assert stats.n_new == 2 and stats.n_candidates == 2 and stats.n_fail == 0
    # 两篇内容不同 → 内容寻址下正好落两份；若是同一份内容，这里会是 1（去重免费）
    assert stats.n_archived == 2 and len(store) == 2


def test_ingest_is_idempotent_and_records_stats(tmp_path):
    out = tmp_path / "c.jsonl"
    first = ingest(
        _site_connector(),
        out_jsonl=out,
        fetch_fn=_site_fetch,
        can_fetch_fn=_allow_all,
        delay=0.0,
    )
    assert first.n_new == 2

    before = out.read_bytes()
    second = ingest(
        _site_connector(),
        out_jsonl=out,
        fetch_fn=_site_fetch,
        can_fetch_fn=_allow_all,
        delay=0.0,
    )
    assert second.n_new == 0 and second.n_skip == 2
    assert out.read_bytes() == before  # 幂等：重跑不追加


def test_ingest_counts_unreachable_and_short_documents(tmp_path):
    """失败要分得清是"抓不到"还是"抽不出"——所以两条分别记。"""

    def fetch(url: str):
        if url.startswith("https://www.chinanews.com.cn/scroll-news/"):
            return LISTING if url.endswith("news1.html") else "<html><body>无</body></html>"
        if url == A1:
            return "<html><body>没有正文容器</body></html>"  # 抽不出
        if url == A2:
            return None  # 抓不到
        return None

    stats = ingest(
        _site_connector(max_listing_pages=1),
        out_jsonl=tmp_path / "d.jsonl",
        fetch_fn=fetch,
        can_fetch_fn=_allow_all,
        delay=0.0,
    )
    assert stats.n_candidates == 2 and stats.n_new == 0 and stats.n_fail == 2


def test_ingest_honours_robots_and_max_docs(tmp_path):
    out = tmp_path / "e.jsonl"
    stats = ingest(
        _site_connector(max_listing_pages=1),
        out_jsonl=out,
        fetch_fn=_site_fetch,
        can_fetch_fn=lambda u: u != A1,  # A1 被 robots 拒
        delay=0.0,
    )
    assert stats.n_new == 1 and stats.n_skip == 0
    rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [r["meta"]["url"] for r in rows] == [A2]
    assert rows[0]["meta"]["source"] == "chinanews"

    capped = tmp_path / "f.jsonl"
    assert (
        ingest(
            _site_connector(max_listing_pages=1),
            out_jsonl=capped,
            fetch_fn=_site_fetch,
            can_fetch_fn=_allow_all,
            max_docs=1,
            delay=0.0,
        ).n_new
        == 1
    )


def test_ingest_propagates_listing_meta_under_namespaced_key(tmp_path):
    """Feed 带的标题/时间放在 `meta.listing` 下，不与会占用顶层 meta 的键混在一层。"""
    feed_conn = RssConnector(
        name="feed_x",
        feed_url="https://e.com/feed",
        extractor="heuristic",
        id_prefix="feed",
    )
    out = tmp_path / "g.jsonl"

    def feed_fetch(url: str):
        if url.endswith("/feed"):
            return RSS2
        return ARTICLE if url.endswith(("/1", "/2")) else None

    stats = ingest(
        feed_conn,
        out_jsonl=out,
        fetch_fn=feed_fetch,
        can_fetch_fn=_allow_all,
        delay=0.0,
    )
    assert stats.n_new == 2
    row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert row["meta"]["listing"]["title"] == "甲篇标题"
    assert row["meta"]["source"] == "feed_x" and row["id"].startswith("feed")
