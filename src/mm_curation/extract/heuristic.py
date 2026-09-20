"""stdlib 零依赖兜底抽取器（V6 W2-2）。

**为什么需要一个"明知很笨"的兜底器**：抽取链必须保证**任何环境都能抽**——
CI 里没装 `trafilatura`、换一个没做站点适配的新源、页面结构临时改版，
这条链都不能断。所以 tier=2 的兜底器 `requires=()`，只用标准库。

它只有两条规则，且刻意保持笨：

1. **整块丢弃 <script>/<style>/<nav>/<header>/<footer>/<aside> 等**——这些标签里的
   文字是代码或导航，不是正文；
2. **取全部 <p>**（没有 <p> 时退化为按 <br> 切行）。

**它不做长度过滤**——长度策略统一归 `ExtractionChain.min_chars` 管。
否则"这篇文章到底是被兜底器滤空了，还是本来就短"会变成一个查不清的问题，
`ExtractAttempt` 的 `no_paragraphs` / `too_short` 也就分不开了。

`strip_noise` 把「剥掉了什么」作为**显式返回值**给出（而不是悄悄丢掉）：
单测如果只断言"输出里没有 alert 字样"，那是测不出剥离有没有发生的——
`<script>alert(1)</script>` 即使不剥离，`alert(1)` 也可能被后续去标签顺手带走，
测试会"绿得没有信息量"。返回计数才能锁住"确实剥了一块 script"。
"""

from __future__ import annotations

import html as html_mod
import re

from .base import ExtractedArticle, Extractor, register_extractor

#: 连内容一起丢掉的块级标签（里面的文本是代码/交互控件，不是正文）
DROP_BLOCKS: tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "template",
    "select",
    "button",
    "form",
)

#: 结构性容器：内容多为导航/页脚，通篇丢弃比逐段判别更稳
BOILERPLATE_BLOCKS: tuple[str, ...] = ("nav", "header", "footer", "aside")

#: 段落最小长度（**只用来滤掉空段**，长度政策归链条）
MIN_PARA_CHARS = 1

_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_WS_RE = re.compile(r"[\s\u00a0\u3000]+")


def _block_re(tag: str) -> re.Pattern[str]:
    return re.compile(rf"<{tag}\b[^>]*>.*?</{tag}\s*>", re.S | re.I)


def strip_noise(html: str) -> tuple[str, dict[str, int]]:
    """剥离噪声块，返回 `(清洗后 HTML, {标签: 剥离块数})`。

    非贪婪匹配对嵌套同名标签（`<div><div>...`）会提前收尾——这里**故意接受**：
    兜底器的目标是"永远能给出点什么"，不是"解析得对"。真正要求准确的场景
    应该上 tier 1 的通用库或 tier 0 的站点适配器。
    """
    removed: dict[str, int] = {}
    for tag in DROP_BLOCKS + BOILERPLATE_BLOCKS:
        html, n = _block_re(tag).subn("", html)
        if n:
            removed[tag] = n
    html, n = _COMMENT_RE.subn("", html)
    if n:
        removed["comment"] = n
    return html, removed


def _clean(fragment: str) -> str:
    """去标签 → 反转义 → 空白归一。

    **顺序不能换**：先反转义会把正文里的 `&lt;p&gt;` 变成真标签，再被去标签规则吃掉。
    """
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub("", fragment))).strip()


def _text_paragraphs(cleaned: str) -> list[str]:
    """无 <p> 时的退化路径：按 <br> 切行。很多老站点正文就是一堆 <br> 拼的。"""
    return [_clean(chunk) for chunk in _BR_RE.split(cleaned)]


@register_extractor
class HeuristicExtractor(Extractor):
    """零依赖兜底：剥噪声 → 取 <p>（退化 <br> 切行）→ 标题取 <h1>，其次 <title>。"""

    name = "heuristic"
    tier = 2
    requires = ()

    def extract(self, html: str) -> ExtractedArticle | None:
        if not html or not html.strip():
            return None
        cleaned, _ = strip_noise(html)

        has_paragraph_tags = bool(_P_RE.search(cleaned))
        has_br = bool(_BR_RE.search(cleaned))
        if not has_paragraph_tags and not has_br:
            # 既无 <p> 也无 <br>：这份 HTML 没有可识别的正文结构（→ no_container）
            return None

        if has_paragraph_tags:
            raw = _P_RE.findall(cleaned)
        else:
            raw = _text_paragraphs(cleaned)
        paras = [_clean(p) for p in raw]
        paras = [p for p in paras if len(p) >= MIN_PARA_CHARS]
        # paras 为空时返回空 tuple 而不是 None：让链条记 `no_paragraphs`
        # （"有段落标签但滤空了"）而不是 `no_container`（"根本没有结构"）——
        # 这两种失败原因对应完全不同的修法。
        return ExtractedArticle(
            title=self._title(html, cleaned),
            paragraphs=tuple(paras),
            extractor=self.name,
        )

    @staticmethod
    def _title(html: str, cleaned: str) -> str:
        m = _H1_RE.search(cleaned) or _H1_RE.search(html) or _TITLE_RE.search(html)
        return _clean(m.group(1)) if m else ""

    @classmethod
    def diagnose(cls, html: str) -> dict:
        """自检用：把"剥了什么 / 抽了几段"一次说清，供对照实验与排障取数。"""
        cleaned, removed = strip_noise(html)
        art = cls().extract(html)
        return {
            "removed": removed,
            "n_paragraphs": len(art.paragraphs) if art else 0,
            "n_chars": art.n_chars if art else 0,
            "title": art.title if art else "",
        }
