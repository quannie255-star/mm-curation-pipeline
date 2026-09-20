"""抽取层协议：`ExtractedArticle` + `Extractor` + 三级链 `ExtractionChain`（V6 W2-2/W2-3）。

**三层链的形状是声明式的**，不是硬编码列表：

| tier | 定位 | 谁 | 可用性 |
|---|---|---|---|
| 0 | 站点专用 | `news_cn` | 永远可用（纯 stdlib 正则，针对已核验站点） |
| 1 | 通用库 | `trafilatura`（W2-3） | 需 `[extract]` extra；**没装就静默跳过** |
| 2 | stdlib 兜底 | `heuristic` | **永远可用**——保证 CI 零新依赖、任何环境都能抽 |

链条按 `tier` 升序尝试，**第一个产出长度达标的赢**。每一次尝试都记进
`ExtractAttempt`——这正是 W2-6 三口径对照实验要的数据：不只是"谁赢了"，
还要"前一级为什么没赢"（`no_container` / `no_paragraphs` / `too_short` / `unavailable`）。
只记赢家的话，阴性结果会退化成一句无法归因的"新抽取器没变好"。
"""

from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Sequence

#: 段落合并后的最小正文长度；与 `web_sources.crawl` 的历史门槛 80 保持一致
MIN_CHARS_DEFAULT = 80


@dataclass(frozen=True)
class ExtractedArticle:
    """抽取结果。`paragraphs` 用 tuple 保序且不可变（下游要靠顺序做对比）。"""

    title: str
    paragraphs: tuple[str, ...]
    extractor: str = ""

    @property
    def n_chars(self) -> int:
        return sum(len(p) for p in self.paragraphs)

    def text(self, *, sep: str = "\n\n") -> str:
        """标题 + 正文——与 `web_sources.crawl` 落库时的拼法一致。"""
        body = "\n".join(self.paragraphs)
        return f"{self.title}{sep}{body}" if self.title else body

    def to_row(self) -> dict:
        return {
            "extractor": self.extractor,
            "title": self.title,
            "n_paragraphs": len(self.paragraphs),
            "n_chars": self.n_chars,
        }


@dataclass(frozen=True)
class ExtractAttempt:
    """一次抽取尝试的记录——**失败也要记，且要记原因**。"""

    extractor: str
    ok: bool
    reason: str = ""
    n_chars: int = 0

    def to_row(self) -> dict:
        return {
            "extractor": self.extractor,
            "ok": self.ok,
            "reason": self.reason,
            "n_chars": self.n_chars,
        }


class Extractor(ABC):
    """抽取器协议。与算子体系**并列**、互不继承——抽取发生在样本存在之前。

    子类只需声明 `name` / `tier` 并实现 `extract`。`name` 为空会在**注册期**
    直接炸（fail-fast），避免出现"注册了但叫不出名字"的匿名抽取器。
    """

    #: 唯一标识（进报告与判决书，改名等于破坏可比性）
    name: ClassVar[str] = ""
    #: 链条层级：0 站点专用 / 1 通用库 / 2 stdlib 兜底
    tier: ClassVar[int] = 1
    #: 依赖的第三方模块名；非空则 `available()` 按 importlib 探测（装上就自动启用）
    requires: ClassVar[tuple[str, ...]] = ()

    def __init_subclass__(cls, **kw) -> None:
        super().__init_subclass__(**kw)
        if not cls.name:
            raise TypeError(f"{cls.__name__} 必须声明非空的 name（注册期 fail-fast）")
        if not isinstance(cls.tier, int) or cls.tier < 0:
            raise TypeError(f"{cls.__name__}.tier 必须是非负整数，得 {cls.tier!r}")

    @classmethod
    def available(cls) -> bool:
        """依赖缺失即不可用——链条会跳过它并记 `unavailable`，而不是抛异常。"""
        return all(importlib.util.find_spec(m) is not None for m in cls.requires)

    @abstractmethod
    def extract(self, html: str) -> ExtractedArticle | None:
        """抽正文；抽不出返回 None（**不要抛异常**，那是链条的失败不是局部失败）。"""


_REGISTRY: dict[str, type[Extractor]] = {}


def register_extractor(cls: type[Extractor]) -> type[Extractor]:
    """注册一个抽取器（可作装饰器用）。重名直接炸——静默覆盖会让报告指向错的东西。"""
    if not (isinstance(cls, type) and issubclass(cls, Extractor)):
        raise TypeError("register_extractor 只接受 Extractor 子类")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"抽取器名冲突: {cls.name!r} 已被 {_REGISTRY[cls.name].__name__} 占用")
    _REGISTRY[cls.name] = cls
    return cls


def unregister_extractor(name: str) -> None:
    _REGISTRY.pop(name, None)


def available_extractors() -> tuple[str, ...]:
    """已注册的抽取器名（按 tier 再按名字排序，保证链条顺序可复现）。"""
    return tuple(sorted(_REGISTRY, key=lambda n: (_REGISTRY[n].tier, n)))


def get_extractor(name: str) -> type[Extractor]:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的抽取器 {name!r}；已有 {available_extractors()}")
    return _REGISTRY[name]


@dataclass
class ExtractionChain:
    """按 tier 依次尝试的抽取链；返回 (结果, 逐级尝试记录)。"""

    extractors: list[Extractor] = field(default_factory=list)
    min_chars: int = MIN_CHARS_DEFAULT

    @classmethod
    def default(
        cls,
        *,
        names: Sequence[str] | None = None,
        min_chars: int = MIN_CHARS_DEFAULT,
    ) -> "ExtractionChain":
        names = names if names is not None else available_extractors()
        return cls([get_extractor(n)() for n in names], min_chars=min_chars)

    def extract(self, html: str) -> tuple[ExtractedArticle | None, list[ExtractAttempt]]:
        attempts: list[ExtractAttempt] = []
        for ex in self.extractors:
            if not type(ex).available():
                missing = ", ".join(type(ex).requires)
                attempts.append(
                    ExtractAttempt(ex.name, False, f"unavailable(缺 {missing})")
                )
                continue
            art = ex.extract(html)
            if art is None:
                attempts.append(ExtractAttempt(ex.name, False, "no_container"))
                continue
            if not art.paragraphs:
                attempts.append(ExtractAttempt(ex.name, False, "no_paragraphs"))
                continue
            if art.n_chars < self.min_chars:
                attempts.append(
                    ExtractAttempt(ex.name, False, "too_short", art.n_chars)
                )
                continue
            attempts.append(ExtractAttempt(ex.name, True, "", art.n_chars))
            return ExtractedArticle(art.title, art.paragraphs, art.extractor or ex.name), attempts
        return None, attempts
