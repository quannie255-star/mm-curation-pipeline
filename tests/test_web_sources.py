"""网页获取器单测（纯离线夹具）：链接提取 / 正文解析 / 推荐位剥离 / 幂等。"""

from __future__ import annotations

import json
from pathlib import Path

from mm_curation.data.web_sources import (
    extract_article,
    extract_links,
    load_seen_urls,
)

LISTING = """
<a href="/gn/2026/09-03/10689481.shtml">甲</a>
<a href="/cj/2026/09-03/10689486.shtml">乙</a>
<a href="/aboutus/staff/x.html">员工页</a>
<a href="/tp/2026/09-03/10689488.shtml">图集</a>
<a href="/gn/2026/09-03/10689481.shtml">甲重复</a>
"""

ARTICLE = """
<html><head><title>x</title></head><body>
<h1>标题测试：沪渝蓉高铁铺轨</h1>
<div class="left_zw">
  <p>这是第一段正文内容，长度超过二十个字符的阈值线，讲述铺轨启动的消息。</p>
  <p>这是第二段正文，同样满足长度过滤条件，介绍项目背景与线路走向等。</p>
  <p>顶部</p>
  <a href="/gn/1.shtml"><div class="ydtj_div_right"><p>推荐标题混入测试内容足够长</p></div></a>
</div>
<div id="backtop"></div>
<div class="ydtj"><p>容器之后的推荐区段落内容也应该完全不被采集进来才算对</p></div>
</body></html>
"""


def test_extract_links_absolute_and_ordered():
    links = extract_links(LISTING)
    assert links[0].startswith("https://www.chinanews.com.cn/")
    assert len(links) == 3  # 员工页不匹配文章 pattern；重复项去重


def test_extract_article_strips_recommendation_blocks():
    art = extract_article(ARTICLE)
    assert art["title"] == "标题测试：沪渝蓉高铁铺轨"
    # 只有两条真正文段：「顶部」被长度滤掉；<a> 内推荐段与容器外 ydtj 段不出现
    assert len(art["paragraphs"]) == 2
    assert all("推荐" not in p and "顶部" not in p for p in art["paragraphs"])


def test_extract_article_rejects_non_article():
    assert extract_article("<html><body>没有正文容器</body></html>") is None
    bare = '<div class="left_zw"><p>短</p></div><div id="backtop"></div>'
    assert extract_article(bare) is None  # 全部段落低于长度阈值


def test_load_seen_urls_idempotent(tmp_path: Path):
    out = tmp_path / "corpus.jsonl"
    assert load_seen_urls(out) == set()  # 文件不存在 → 空
    out.write_text(
        json.dumps({"id": "news1", "text": "t", "meta": {"url": "https://a/1"}})
        + "\n"
        + "坏行不是json\n"
        + json.dumps({"id": "news2", "text": "t", "meta": {"url": "https://a/2"}})
        + "\n",
        encoding="utf-8",
    )
    seen = load_seen_urls(out)
    assert seen == {"https://a/1", "https://a/2"}  # 坏行容错跳过
