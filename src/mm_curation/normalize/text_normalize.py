"""文本归一化层（V6 决策点 2）：把「改写」从算子里分出来，一次改写全链受益。

## 为什么这一层必须存在（笔记 #65 的正面答案）

真实新闻语料上 `chinese_ratio` 一级拦 778/2066（37.7%），抽样审计发现被拦篇目
**空白占比中位数 77.2%**（空格 3000+、`\r` 残留）——「汉字/全文长度」的分母
被爬虫抽取缺陷撑爆。当时的修复把去空白写进了 `chinese_ratio` 的语义。
**补丁正确，但只救了这一个算子**：`text_length`（`len(text.strip())` 含内部空白）、
`char_repetition`（最长单字符游程占比——3000 个连续空格会被判成「水文本」）、
`text_minhash`（shingle 集被空白污染）、`boilerplate`/`pii_detect`（正则被空白切断）
都还在被污染的文本上打分。本模块是那条修复的语义正确位置。

## 规则（七条，逐条可开关，逐条留痕）

| 规则 | 动作 | 依据 |
|---|---|---|
| `nfc` | Unicode NFC 规范化 | 兼容字符（全角拉丁等）污染字符统计 |
| `line_separators` | U+2028/U+2029 → `\n` | 笔记 #44 陷阱：`splitlines()` 会在此错切 JSONL |
| `newline` | `\r\n` / `\r` → `\n` | #65 现场实测的 `\r` 残留 |
| `zero_width` | 剥 U+200B/200C/200D/2060/FEFF | 网页复制的隐形字符让「长度」失真 |
| `control_chars` | 剥 Cc/Cf（保留 `\n` `\t`） | 见下方「规则正交性」 |
| `whitespace_collapse` | 行内空白段→单空格；trim 两端；`\n{3,}`→`\n\n` | #65 的 3000+ 空格 |
| `strip` | 整体首尾 trim | 抽取拼接的残留 |

**规则正交性（首跑教训）**：`whitespace_collapse` 最初用 `str.strip()` 收尾，
顺带把行尾 `\r` 也吃掉了 —— 于是报告写「`whitespace_collapse` 命中 976 篇」时，
其中一部分功劳其实属于换行归一化。**规则必须各管一段，逐规则归因才可信**：
`newline` 独占 `\r`，`control_chars` 不删 `\r`（删了会合并相邻行），
`whitespace_collapse` 只 trim 行内空白、不碰 `\r\n`。
同理 `control_chars` 刻意留着 `\t`：让空白折叠把它变成**空格**而不是直接删掉，
`a\tb` 就不该塌成 `ab` —— 删除与替换是两种语义，不能混。

## 默认 opt-in（红线）

归一化会改写**所有下游算子的输入**，因此会改变既有四个模态的全部数字。
V4 设计表有「既有指标逐项相等」的硬约束 → **既有 config 一字不动**，
归一化由调用方显式开启（`pre_stages=`）。这也让「同批数据开/关」成为天然 A/B 对照组。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable, Sequence

RULES: tuple[str, ...] = (
    "nfc",
    "line_separators",
    "newline",
    "zero_width",
    "control_chars",
    "whitespace_collapse",
    "strip",
)

_LINE_SEP_TABLE = {0x2028: "\n", 0x2029: "\n"}
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff")
# 行内空白连续段（含全角空格 U+3000 与制表符）。
# `\n` 与 `\r` **刻意排除**：换行归一化是 `newline` 规则的职责，
# 两条规则必须正交——否则报告里「whitespace_collapse 命中 976 篇」会把
# 换行归一化的功劳算进来，逐规则归因就不纯了（首跑被这个骗过）。
_INLINE_WS_RUN = re.compile(r"[^\S\n\r]+")
_BLANK_RUN = re.compile(r"\n{3,}")
# 控制字符剥除的保留集：`\r` 也保留——删掉它会**合并相邻行**，
# 而合并段落是改写语义，必须留给 `newline` 规则显式完成。
_CONTROL_KEEP = "\n\t\r"


def _r_nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _r_line_separators(text: str) -> str:
    return text.translate(_LINE_SEP_TABLE)


def _r_newline(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _r_zero_width(text: str) -> str:
    return "".join(ch for ch in text if ch not in _ZERO_WIDTH) if any(
        ch in _ZERO_WIDTH for ch in text
    ) else text


def _r_control_chars(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if ch in _CONTROL_KEEP or unicodedata.category(ch) not in ("Cc", "Cf")
    )


def _r_whitespace_collapse(text: str) -> str:
    """行内空白连续段压成单空格 + 行首尾 trim + 连续空行压成一个空行。

    保留单个 `\\n\\n`（段落边界）——段落结构是语言证据，其余空白不是（#65 的判据）。

    只 trim **行内空白**（空格/制表符/全角空格），不 trim `\\r\\n`——
    换行由 `newline` 规则独占（规则正交，归因才纯）。
    """
    lines = [_INLINE_WS_RUN.sub(" ", line).strip(" ") for line in text.split("\n")]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines))


def _r_strip(text: str) -> str:
    return text.strip()


_RULE_FNS = {
    "nfc": _r_nfc,
    "line_separators": _r_line_separators,
    "newline": _r_newline,
    "zero_width": _r_zero_width,
    "control_chars": _r_control_chars,
    "whitespace_collapse": _r_whitespace_collapse,
    "strip": _r_strip,
}


def whitespace_ratio(text: str) -> float:
    """空白字符占比（#65 的口径：空白字符数 / 全文长度）。空串 → 0.0。"""
    if not text:
        return 0.0
    return sum(ch.isspace() for ch in text) / len(text)


@dataclass(frozen=True)
class NormalizeOutcome:
    """单次归一化的结果与留痕（可审计的最小单元）。"""

    text: str
    rules_applied: tuple[str, ...]
    orig_len: int
    new_len: int
    chars_removed: int
    whitespace_ratio_before: float
    whitespace_ratio_after: float

    @property
    def changed(self) -> bool:
        return bool(self.rules_applied)

    @property
    def is_empty(self) -> bool:
        return self.new_len == 0

    def to_log(self) -> dict[str, Any]:
        """判决书 / meta 用的留痕（字段名与 V6 设计表一致）。"""
        return {
            "rules_applied": list(self.rules_applied),
            "orig_len": self.orig_len,
            "new_len": self.new_len,
            "chars_removed": self.chars_removed,
            "whitespace_ratio_before": round(self.whitespace_ratio_before, 6),
            "whitespace_ratio_after": round(self.whitespace_ratio_after, 6),
        }


def normalize_text(text: str, *, rules: Iterable[str] | None = None) -> NormalizeOutcome:
    """按序施加规则；`rules_applied` 只记**实际改变了文本**的规则（诚实留痕）。

    `rules=None` → 全部七条；传子集可做逐规则消融。
    """
    active = tuple(RULES if rules is None else rules)
    unknown = sorted(set(active) - set(RULES))
    if unknown:
        raise ValueError(f"未知归一化规则 {unknown}，可用: {list(RULES)}")

    ws_before = whitespace_ratio(text)
    cur = text
    applied: list[str] = []
    for name in active:
        nxt = _RULE_FNS[name](cur)
        if nxt != cur:
            applied.append(name)
        cur = nxt
    return NormalizeOutcome(
        text=cur,
        rules_applied=tuple(applied),
        orig_len=len(text),
        new_len=len(cur),
        chars_removed=len(text) - len(cur),
        whitespace_ratio_before=ws_before,
        whitespace_ratio_after=whitespace_ratio(cur),
    )


@dataclass
class NormalizeAggregate:
    """语料级聚合（对照实验与报告用；纯函数，可单测）。"""

    n: int = 0
    n_changed: int = 0
    n_emptied: int = 0
    chars_removed_total: int = 0
    orig_len_total: int = 0
    rule_counts: dict[str, int] = field(default_factory=dict)
    _ws_before: list[float] = field(default_factory=list, repr=False)
    _ws_after: list[float] = field(default_factory=list, repr=False)

    def add(self, outcome: NormalizeOutcome) -> None:
        self.n += 1
        if outcome.changed:
            self.n_changed += 1
        if outcome.is_empty:
            self.n_emptied += 1
        self.chars_removed_total += outcome.chars_removed
        self.orig_len_total += outcome.orig_len
        for rule in outcome.rules_applied:
            self.rule_counts[rule] = self.rule_counts.get(rule, 0) + 1
        self._ws_before.append(outcome.whitespace_ratio_before)
        self._ws_after.append(outcome.whitespace_ratio_after)

    @property
    def changed_ratio(self) -> float:
        return self.n_changed / self.n if self.n else 0.0

    @property
    def chars_removed_ratio(self) -> float:
        return self.chars_removed_total / self.orig_len_total if self.orig_len_total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "n_changed": self.n_changed,
            "changed_ratio": round(self.changed_ratio, 6),
            "n_emptied": self.n_emptied,
            "chars_removed_total": self.chars_removed_total,
            "chars_removed_ratio": round(self.chars_removed_ratio, 6),
            "rule_counts": dict(sorted(self.rule_counts.items())),
            "whitespace_ratio_before_p50": round(median(self._ws_before), 6)
            if self._ws_before
            else 0.0,
            "whitespace_ratio_after_p50": round(median(self._ws_after), 6)
            if self._ws_after
            else 0.0,
        }


def aggregate(outcomes: Sequence[NormalizeOutcome]) -> NormalizeAggregate:
    agg = NormalizeAggregate()
    for o in outcomes:
        agg.add(o)
    return agg
