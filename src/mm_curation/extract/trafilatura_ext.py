"""通用库抽取器（V6 W2-3）：`trafilatura`，带缺依赖降级。

**这是"可插拔"的样板**：站点专用器（tier 0）覆盖已核验的源，通用库（tier 1）
补未适配的新源。`requires = ("trafilatura",)` 让链条用 `importlib` 探测安装情况：

- 装了 → 自动参与竞争，无需改配置；
- 没装 → 链条记一条 `ExtractAttempt(ok=False, reason="unavailable(缺 trafilatura)")`
  然后**跳到下一级**，而不是抛 ImportError 把整批抓取带崩。

**"没装"必须留痕**，这是 W2-3 的验收重点：如果缺依赖时链条静默少一级，
那对照实验里"trafilatura 这一档没赢"就有两种可能（抽不好 / 压根没跑），
数字会变得不可解释。留下 `unavailable` 记录，才能一眼区分。

导入是**方法内懒加载**：模块级 import 会让"没装"变成 import 期崩溃，
把降级能力本身毁掉。
"""

from __future__ import annotations

from .base import ExtractedArticle, Extractor, register_extractor

#: 传参优先用它；老版本不认这些 kwargs 时退回零参调用（见 `extract`）
_OPTIONS: dict[str, object] = {
    "include_comments": False,
    "include_tables": False,
}


@register_extractor
class TrafilaturaExtractor(Extractor):
    """通用正文抽取。抽不出返回 None（交给下一级），绝不抛异常。"""

    name = "trafilatura"
    tier = 1
    requires = ("trafilatura",)

    def extract(self, html: str) -> ExtractedArticle | None:
        if not html or not html.strip():
            return None
        try:
            import trafilatura
        except ImportError:  # 理论上不会走到：链条已按 available() 过滤
            return None

        text = self._run(trafilatura, html)
        if not text:
            return None

        paras = tuple(line.strip() for line in text.split("\n") if line.strip())
        if not paras:
            return None
        return ExtractedArticle(
            title=self._title(trafilatura, html),
            paragraphs=paras,
            extractor=self.name,
        )

    @staticmethod
    def _run(trafilatura, html: str) -> str | None:
        """带 kwargs 调一次；版本不认 kwargs 就退回零参——**两种失败都不外抛**。"""
        try:
            return trafilatura.extract(html, **_OPTIONS)
        except TypeError:
            try:
                return trafilatura.extract(html)
            except Exception:
                return None
        except Exception:
            return None

    @staticmethod
    def _title(trafilatura, html: str) -> str:
        """标题是加分项不是必需项：取不到就空串，不影响正文判定。"""
        try:
            meta = trafilatura.extract_metadata(html)
        except Exception:
            return ""
        return str(getattr(meta, "title", "") or "").strip()
