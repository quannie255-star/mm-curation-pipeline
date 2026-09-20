"""抽取层单测（V6 W2）：RawDoc 存档 / 兜底抽取 / 链条归因 / 收编逐字等价。

测试意图（不是"覆盖率"，是"锁住哪几条不许悄悄变的语义"）：

1. **RawDoc 存档**：幂等（同内容只落一份）、损坏必炸（不静默给坏正文）、
   原子写不留 `.tmp` 残渣；
2. **兜底抽取**：噪声剥离**确实发生**（靠 `removed` 计数，不是靠"输出里没这个词"）；
3. **链条归因**：`unavailable` / `no_container` / `no_paragraphs` / `too_short`
   四种失败原因必须分得开——分不开的话 W2-6 的阴性结果没法归因；
4. **逐字等价**：收编后的站点抽取器与既有语料落库文本一字不差；
5. **协议 fail-fast**：匿名抽取器 / 负数 tier 在注册期就炸。

**本文件不联网**：所有 HTML 都是内联夹具。真实语料上的三口径对照属于 W2-6。
"""

from __future__ import annotations

import gzip

import pytest

from mm_curation.data.web_sources import extract_article
from mm_curation.extract import (
    ExtractAttempt,
    ExtractedArticle,
    ExtractionChain,
    Extractor,
    HeuristicExtractor,
    NewsCnExtractor,
    RawDocCorrupted,
    RawDocStore,
    TrafilaturaExtractor,
    available_extractors,
    default_chain,
    extract_html,
    get_extractor,
    register_extractor,
    sha256_of_text,
    strip_noise,
    unregister_extractor,
)
from mm_curation.extract.news_cn import NEWS_CN

ARTICLE = """
<html><head><title>站点标题不该赢</title></head><body>
<h1>标题测试：沪渝蓉高铁铺轨</h1>
<div class="left_zw">
  <p>这是第一段正文内容，长度超过二十个字符的阈值线，讲述铺轨启动的消息。</p>
  <p>这是第二段正文，同样满足长度过滤条件，介绍项目背景与线路走向等。</p>
  <p>第三段补充正文，把整篇正文总长度推过链条八十字符的最小门槛线。</p>
  <p>顶部</p>
  <a href="/gn/1.shtml"><div class="ydtj_div_right"><p>推荐标题混入测试内容足够长</p></div></a>
</div>
<div id="backtop"></div>
<div class="ydtj"><p>容器之后的推荐区段落内容也应该完全不被采集进来才算对</p></div>
</body></html>
"""

EXPECTED_TITLE = "标题测试：沪渝蓉高铁铺轨"
EXPECTED_PARAS = [
    "这是第一段正文内容，长度超过二十个字符的阈值线，讲述铺轨启动的消息。",
    "这是第二段正文，同样满足长度过滤条件，介绍项目背景与线路走向等。",
    "第三段补充正文，把整篇正文总长度推过链条八十字符的最小门槛线。",
]


# ===================== 1. RawDocStore =====================


def test_rawdoc_put_get_roundtrip(tmp_path):
    store = RawDocStore(tmp_path / "html")
    html = "<html><body><p>原始内容</p></body></html>"
    doc = store.put(html, url="https://a/1", meta={"source": "chinanews"})

    assert doc.sha256 == sha256_of_text(html)
    assert doc.url == "https://a/1"
    assert doc.n_bytes == len(html.encode("utf-8"))
    # 路径布局：<sha[:2]>/<sha>.html.gz —— 分片是为了单目录别堆几万个文件
    assert store.path_for(doc.sha256).name == f"{doc.sha256}.html.gz"
    assert store.path_for(doc.sha256).parent.name == doc.sha256[:2]

    back = store.get(doc.sha256)
    assert back is not None
    assert back.text == html  # 一字不差：这一层刻意不做任何加工
    assert back.meta == {"source": "chinanews"}
    assert store.exists(doc.sha256) and len(store) == 1


def test_rawdoc_put_is_idempotent_and_accumulates_urls(tmp_path):
    store = RawDocStore(tmp_path / "html")
    html = "<p>同一份内容被两个 URL 抓到</p>"
    first = store.put(html, url="https://a/1", fetched_at="2026-09-20T00:00:00+00:00")
    second = store.put(html, url="https://a/2", meta={"second": True})

    assert first.sha256 == second.sha256
    assert len(store) == 1  # 内容寻址：抓十次只落一份
    back = store.get(first.sha256)
    assert back.urls == ("https://a/1", "https://a/2")  # 只累积，不重写正文
    assert back.fetched_at == "2026-09-20T00:00:00+00:00"  # 首次观测时间不被覆盖
    assert back.meta == {"second": True}


def test_rawdoc_detects_tampering(tmp_path):
    """把正文换成别的内容但不改路径 → 必须炸，不能静默返回坏正文。"""
    store = RawDocStore(tmp_path / "html")
    doc = store.put("原始内容", url="https://a/1")
    store.path_for(doc.sha256).write_bytes(gzip.compress(b"tampered", 6))

    with pytest.raises(RawDocCorrupted, match="内容哈希与路径不符"):
        store.get(doc.sha256)


def test_rawdoc_rejects_malformed_sha(tmp_path):
    store = RawDocStore(tmp_path / "html")
    for bad in ("", "abc", "Z" * 64, "A" * 64):  # 非 64 位 / 非小写十六进制
        with pytest.raises(ValueError):
            store.path_for(bad)


def test_rawdoc_missing_is_none_not_error(tmp_path):
    store = RawDocStore(tmp_path / "html")
    missing = sha256_of_text("从未存过")
    assert store.get(missing) is None
    assert store.ref(missing) is None
    assert not store.exists(missing)
    assert len(store) == 0
    assert store.stats()["n_docs"] == 0


def test_rawdoc_atomic_write_leaves_no_tmp(tmp_path):
    root = tmp_path / "html"
    store = RawDocStore(root)
    store.put("<p>内容</p>" * 50, url="https://a/1")
    assert not list(root.rglob("*.tmp"))  # 中断不留半个文件（笔记 #2）


def test_rawdoc_stats_reports_compression(tmp_path):
    """stats 是这一层唯一的"数字"——用来证明它没白占盘。"""
    store = RawDocStore(tmp_path / "html")
    store.put("<div class='left_zw'>重复内容</div>" * 200, url="https://a/1")
    st = store.stats()
    assert st["n_docs"] == 1
    assert st["n_bytes_raw"] == len(("<div class='left_zw'>重复内容</div>" * 200).encode("utf-8"))
    assert 0 < st["compression_ratio"] < 0.5  # HTML 冗余度高，压得下来


# ===================== 2. 兜底抽取器 =====================


def test_strip_noise_reports_actually_removed_blocks():
    """锁住"剥离确实发生"：只断言输出里没有 alert 是测不出剥离的。"""
    html = (
        "<html><body>"
        "<script>alert('被剥离的脚本')</script>"
        "<style>.x{color:red}</style>"
        "<nav>导航文字</nav>"
        "<!-- 注释 -->"
        "<p>真正文段落</p>"
        "</body></html>"
    )
    cleaned, removed = strip_noise(html)

    assert removed["script"] == 1
    assert removed["style"] == 1
    assert removed["nav"] == 1
    assert removed["comment"] == 1
    assert "alert" not in cleaned and "color:red" not in cleaned and "导航文字" not in cleaned
    assert "真正文段落" in cleaned


def test_strip_noise_does_not_eat_lookalike_tags():
    """`<form` 的块规则不能误伤 `<footer>`，反之亦然（`\\b` 边界的意义）。"""
    html = "<footer>页脚</footer><form>表单</form><p>正文</p>"
    cleaned, removed = strip_noise(html)
    assert removed == {"form": 1, "footer": 1}
    assert "页脚" not in cleaned and "表单" not in cleaned and "正文" in cleaned


def test_heuristic_extracts_paragraphs_title_and_unescapes():
    html = (
        "<html><body><h1>真正的标题</h1>"
        "<p>价格 &lt; 100 元，且这一段足够长可以留下。</p>"
        "<p>第二段正文，同样足够长，应当被保留下来。</p>"
        "<p>   </p>"
        "</body></html>"
    )
    art = HeuristicExtractor().extract(html)
    assert art is not None
    assert art.extractor == "heuristic"
    assert art.title == "真正的标题"
    assert art.paragraphs == (
        "价格 < 100 元，且这一段足够长可以留下。",
        "第二段正文，同样足够长，应当被保留下来。",
    )


def test_heuristic_falls_back_to_title_tag_when_no_h1():
    art = HeuristicExtractor().extract("<title>只有 title</title><p>正文段落足够长了吧</p>")
    assert art is not None and art.title == "只有 title"


def test_heuristic_br_fallback_when_no_p_tags():
    """老站点正文常是一堆 <br> 拼的；没有 <p> 不等于没有正文。"""
    html = "<body>第一行正文内容<div>第二行正文内容<br>第三行正文内容</div></body>"
    art = HeuristicExtractor().extract(html)
    assert art is not None
    assert art.paragraphs == ("第一行正文内容第二行正文内容", "第三行正文内容")


def test_heuristic_returns_none_when_no_structure():
    """既无 <p> 也无 <br> → None，链条会记 no_container。"""
    assert HeuristicExtractor().extract("纯文本没有任何标签") is None
    assert HeuristicExtractor().extract("   ") is None


def test_heuristic_empty_paragraphs_return_empty_tuple_not_none():
    """有段落标签但全被滤空 → 空 tuple（→ no_paragraphs），不是 None。

    这个区分很重要：「根本没有正文结构」和「有结构但没内容」修法完全不同。
    """
    art = HeuristicExtractor().extract("<body><p> </p><p></p></body>")
    assert art is not None
    assert art.paragraphs == ()


def test_heuristic_diagnose_shape():
    d = HeuristicExtractor.diagnose("<script>var a=1</script><h1>T</h1><p>正文内容够长了吧</p>")
    assert d["removed"] == {"script": 1}
    assert d["n_paragraphs"] == 1 and d["n_chars"] > 0 and d["title"] == "T"


# ===================== 3. 链条与归因 =====================


class _Fake(Extractor):
    name = "_fake"
    tier = 2

    def __init__(self, *, paragraphs=("正文段落够长了吧" * 15,), title="T", explode=False):
        self._paras, self._title, self._explode = paragraphs, title, explode

    def extract(self, html):
        if self._explode:
            return None
        return ExtractedArticle(self._title, tuple(self._paras), self.name)


class _FakeA(_Fake):
    name = "_fake_a"
    tier = 0


class _FakeB(_Fake):
    name = "_fake_b"
    tier = 2


class _FakeNeedsMissingDep(_Fake):
    name = "_fake_needs_dep"
    tier = 1
    requires = ("_wb_module_never_installed_xyz",)


def test_chain_records_unavailable_then_falls_through():
    """缺依赖不是异常，是一条可读的记录 + 继续下一级。"""
    chain = ExtractionChain([_FakeNeedsMissingDep(), _FakeB()])
    art, attempts = chain.extract("<p>x</p>")

    assert attempts[0].ok is False
    assert attempts[0].reason.startswith("unavailable(缺 _wb_module_never_installed_xyz")
    assert attempts[1].ok is True and art.extractor == "_fake_b"
    assert attempts[0].to_row()["extractor"] == "_fake_needs_dep"


def test_chain_first_tier_wins():
    """tier 升序、第一个达标者赢——后面的不再尝试（省算力也是可复现性的一部分）。"""
    chain = ExtractionChain([_FakeA(), _FakeB()])
    art, attempts = chain.extract("<p>x</p>")
    assert art.extractor == "_fake_a"
    assert len(attempts) == 1


def test_chain_distinguishes_failure_reasons():
    """四种原因必须分得开——分不开的话阴性结果没法归因。"""
    none_art = _Fake(explode=True)
    empty = _Fake(paragraphs=())
    short = _Fake(paragraphs=("太短",))

    cases = ((none_art, "no_container"), (empty, "no_paragraphs"), (short, "too_short"))
    for ex, expected in cases:
        art, attempts = ExtractionChain([ex]).extract("<p>x</p>")
        assert art is None
        assert attempts[0].reason == expected, expected
    # too_short 还带上实际长度，方便判"差多少"
    _, attempts = ExtractionChain([short]).extract("<p>x</p>")
    assert attempts[0].n_chars == 2


def test_chain_min_chars_is_adjustable():
    chain = ExtractionChain([_Fake(paragraphs=("短正文",))], min_chars=3)
    art, attempts = chain.extract("<p>x</p>")
    assert art is not None and attempts[0].ok


def test_attempt_and_article_to_row_shapes():
    assert ExtractAttempt("x", False, "too_short", 3).to_row() == {
        "extractor": "x",
        "ok": False,
        "reason": "too_short",
        "n_chars": 3,
    }
    row = ExtractedArticle("T", ("aa", "bbb"), "x").to_row()
    assert row == {"extractor": "x", "title": "T", "n_paragraphs": 2, "n_chars": 5}


def test_registry_sorted_by_tier_then_name():
    assert available_extractors() == ("news_cn", "trafilatura", "heuristic")


def test_default_chain_order_follows_tier():
    chain = default_chain()
    assert [type(e).name for e in chain.extractors] == ["news_cn", "trafilatura", "heuristic"]
    assert default_chain() is chain  # 无状态，缓存复用


def test_trafilatura_degradation_is_recorded():
    """本机/CI 未装 trafilatura 时必须记 unavailable 并继续兜底。

    装上时这条测试转为跳过——但机制本身由 `_fake_needs_dep` 那条无条件锁住。
    """
    if TrafilaturaExtractor.available():
        pytest.skip("本环境已装 trafilatura，走的是正常竞争路径")
    html = "".join(f"<p>第{i}段正文，再补一点内容凑够链条阈值</p>" for i in range(5))
    art, attempts = default_chain().extract(html)
    reasons = {a.extractor: a.reason for a in attempts}
    assert reasons["news_cn"] == "no_container"  # 没有 left_zw 容器
    assert reasons["trafilatura"] == "unavailable(缺 trafilatura)"
    assert art.extractor == "heuristic"  # 兜底器永远可用，链条不会空手而归


# ===================== 4. 收编的逐字等价 =====================


def test_news_cn_matches_frozen_expectation():
    """冻结字面量断言：两个入口都必须等于它，而不是互相比（互比会一起漂）。"""
    assert NewsCnExtractor.tier == 0 and NewsCnExtractor.available()
    art = NEWS_CN.extract(ARTICLE)
    assert art.title == EXPECTED_TITLE
    assert list(art.paragraphs) == EXPECTED_PARAS

    legacy = extract_article(ARTICLE)
    assert legacy == {"title": EXPECTED_TITLE, "paragraphs": EXPECTED_PARAS}


def test_min_chars_gate_lives_in_chain_not_in_extractor():
    """同一条 HTML，直调站点抽取器成功，走链条却可能因 min_chars 落选。

    这是**设计如此**：长度政策只在链条一处，抽取器不管长度。夹具正文若短于
    80 字，链条会记 tier0 `too_short` 并下探——`crawl` 里那道 80 字闸门
    与链条的 `MIN_CHARS_DEFAULT` 是同一个历史口径。
    """
    # 段落长度刻意落在 20~80 之间：过得了站点抽取器的 20 字粗滤，过不了链条的 80 字闸门
    short = (
        '<div class="left_zw"><p>'
        "只有一段正文，长度在四十字上下，能让站点抽取器收下但过不了闸门。"
        "</p></div>"
    )
    assert NEWS_CN.extract(short) is not None  # 直调：抽到了
    art, attempts = ExtractionChain([NEWS_CN]).extract(short)
    assert art is None and attempts[0].reason == "too_short"
    # 把闸门调低，同一份 HTML 就过——证明是政策问题不是抽取问题
    art, _ = ExtractionChain([NEWS_CN], min_chars=10).extract(short)
    assert art is not None


def test_news_cn_matches_legacy_on_rejects():
    assert extract_article("<html><body>没有正文容器</body></html>") is None
    assert NEWS_CN.extract("<html><body>没有正文容器</body></html>") is None
    bare = '<div class="left_zw"><p>短</p></div><div id="backtop"></div>'
    assert extract_article(bare) is None and NEWS_CN.extract(bare) is None


def test_news_cn_short_paragraphs_conflated_into_no_container():
    """**刻意锁住的粗糙点**：站点抽取器把「容器缺失」与「段落全被 20 字粗滤」
    合并成 None，所以链条只能记 no_container。

    这是为了"既有语料一字不变"主动接受的代价，不是 bug——写进测试免得日后
    有人以为是疏漏而改掉它，那会静默改变历史落库文本。
    """
    bare = '<div class="left_zw"><p>短</p></div><div id="backtop"></div>'
    art, attempts = ExtractionChain([NEWS_CN]).extract(bare)
    assert art is None and attempts[0].reason == "no_container"


def test_article_text_join_matches_crawl_convention():
    """`ExtractedArticle.text()` 必须与 `crawl` 落库拼法一致，否则换入口就换语料。"""
    art = NEWS_CN.extract(ARTICLE)
    assert art.text() == art.title + "\n\n" + "\n".join(art.paragraphs)
    assert art.n_chars == sum(len(p) for p in art.paragraphs)


def test_extract_html_matches_chain_result():
    art, attempts = extract_html(ARTICLE)
    assert art.extractor == "news_cn"  # tier 0 命中，不必下探
    assert len(attempts) == 1 and attempts[0].ok


# ===================== 5. 协议 fail-fast =====================


def test_extractor_subclass_requires_name():
    with pytest.raises(TypeError, match="必须声明非空的 name"):

        class _NoName(Extractor):
            def extract(self, html):
                return None


def test_extractor_rejects_bad_tier():
    with pytest.raises(TypeError, match="非负整数"):

        class _BadTier(Extractor):
            name = "_bad_tier_case"
            tier = -1

            def extract(self, html):
                return None


def test_register_duplicate_name_raises():
    class _Dup1(Extractor):
        name = "_dup_case"
        tier = 9

        def extract(self, html):
            return None

    class _Dup2(Extractor):
        name = "_dup_case"
        tier = 9

        def extract(self, html):
            return None

    register_extractor(_Dup1)
    try:
        with pytest.raises(ValueError, match="抽取器名冲突"):
            register_extractor(_Dup2)
        assert register_extractor(_Dup1) is _Dup1  # 幂等注册同一个类不算冲突
    finally:
        unregister_extractor("_dup_case")
    assert "_dup_case" not in available_extractors()


def test_get_extractor_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        get_extractor("_never_registered_xyz")
