"""中国新闻网站点抽取器（V6 W2-4 收编）。

**这个模块是从 `data/web_sources.py` 搬过来的，不是重写的。** 原文的
`extract_article` 只是取了一个函数名，现在它变成 `NewsCnExtractor`（tier 0，
站点专用），`web_sources.extract_article` 退化成一层薄适配。

为什么要搬而不是复制一份：**逐字等价是红线**。W2-4 的验收要求"收编后既有语料
一字不变"，如果留两份实现，等价只能靠测试维持，谁改一边忘了另一边就静默漂移。
搬成单一真相源后，等价是**结构上**成立的，测试只需锁住"语义没变"（见
`tests/test_web_sources.py` 与 `tests/test_extract.py::test_news_cn_verbatim_*`）。

站点的三条结构事实（实测得来，别删注释，换源时会再踩）：

1. 正文容器是 `class="left_zw"`，**找不到就当非文章页**（图集页/栏目页没有它）；
2. 容器结束标记有三个候选（`id="backtop"` / `class="ydtj"` / `<div class="share"`），
   取**最早出现者**——只写一个会在结构变体上截错；都没有才退回固定 80_000 字符窗口；
3. 推荐位会把推荐标题塞进 `<a>` 里的 `<p>`，所以**必须先整体剥掉 `<a>` 块再取 `<p>`**，
   否则推荐标题会混进正文（实测 `/tp/` 图集页就是这么翻车的）。

与 `heuristic` 的**刻意差异**（不是疏漏）：
- 不做 HTML 反转义、不做空白归一——保持历史落库文本逐字不变；
- 段落用长度 20 粗滤（`PARA_MIN_CHARS`），而 heuristic 不过滤。
  因此"所有段落都短于 20"会与"没有正文容器"一样返回 None，链条只能记
  `no_container`。这是为了等价**主动接受**的粗糙归因，测试里锁住了这个行为。
"""

from __future__ import annotations

import re

from .base import ExtractedArticle, Extractor, register_extractor

#: 正文容器标识
CONTAINER_MARKER = "left_zw"

#: 容器结束标记（多候选取最早出现者）
END_MARKERS: tuple[str, ...] = ('id="backtop"', 'class="ydtj"', '<div class="share"')

#: 没有结束标记时的兜底截断窗口
MAX_SEGMENT_CHARS = 80_000

#: 段落粗滤：滤「顶部」/导航/分享按钮（深度清洗归漏斗）
PARA_MIN_CHARS = 20

TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
TAG_RE = re.compile(r"<[^>]+>")

_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_A_BLOCK_RE = re.compile(r"<a\s[^>]*>.*?</a>", re.S)


@register_extractor
class NewsCnExtractor(Extractor):
    """站点专用抽取器：容器 `left_zw` + 多候选结束标记 + 先剥 `<a>` 再取 `<p>`。"""

    name = "news_cn"
    tier = 0
    requires = ()

    def extract(self, html: str) -> ExtractedArticle | None:
        i = html.find(CONTAINER_MARKER)
        if i == -1:
            return None

        ends = [html.find(m, i) for m in END_MARKERS]
        ends = [p for p in ends if p != -1]
        seg = html[i : min(ends) if ends else i + MAX_SEGMENT_CHARS]

        seg = _A_BLOCK_RE.sub("", seg)  # 剥掉链接块（推荐位/导航）
        paras = [TAG_RE.sub("", p).strip() for p in _P_RE.findall(seg)]
        paras = [p for p in paras if len(p) >= PARA_MIN_CHARS]
        if not paras:
            return None

        t = TITLE_RE.search(html)
        title = TAG_RE.sub("", t.group(1)).strip() if t else ""
        return ExtractedArticle(title=title, paragraphs=tuple(paras), extractor=self.name)


#: 单例——无状态，省掉每次调用都构造一个对象
NEWS_CN = NewsCnExtractor()
