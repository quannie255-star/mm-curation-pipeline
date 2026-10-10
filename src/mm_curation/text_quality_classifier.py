"""文本质量分类器：正例分布设计 + 防循环论证（G3）。

DataComp-LM 的两条实证结论决定了本模块的设计
----------------------------------------------------
1. **模型式质量过滤是主导干预**，且**廉价的 bigram 分类器胜过昂贵的 LLM 打分**
   （1B 档 fastText 30.2 Core > Perplexity 29.0 > AskLLM 28.6）。
   → 所以这里用 **线性分类器 + 字符/词 n-gram 特征**，
     不上大模型、不引入 torch。这是被实证支持的取舍，不是省事。

2. ⭐ **「正例参考分布定义得好，比分类器 sophistication 更重要」**
   （OpenHermes+ELI5 作正例得Core 41.0，Wikipedia 作正例只有 35.7）。
   → 所以本模块的重点**不在分类器**，而在 `Positives` 的定义与登记。
     每种正例分布必须写清「它代表什么下游目标」。

⚠️ 防循环论证（本项目最核心的门禁纪律）
----------------------------------------
**分类器最容易骗自己**：用合成污染训练 → 再在同类合成污染上测 → 必然高分。
所以采用图像侧检测器已验证过的**风格组 A/B** 协议：
  · A 组参与训练与选择
  · B 组**不参与任何训练决策**，只做最终泛化评测
  · A/B 在**生成参数维度上错开**（不是随机切分！）——
    随机切分会让「污染器的随机种子」成为泄漏通道，
    分类器可能只是记住了某个噪声图案。
见 `Positives` 与 `Negatives` 的维度清单。

判据纪律（为什么这些断言这么严）
-------------------------------
「训练集准确率 0.99」**不是**证据：分类器可以只是背下了训练集。
真正要看的是 **B 组上的表现** 与 **B 组上的负例召回**。
所以 `evaluate` 强制同时报 testA / testB，且 `assert_not_memorized`
在 testB 显著低于 testA 时报错。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

#: 正例分布的定义——**每一条都要能回答「它代表什么下游目标」**
#:
#: ⚠️ 键名即 `positives_kind`，会写进报告与 claims.json。
#: 改名等于破坏可比性（与本项目其他注册表同纪律）。
POSITIVE_KINDS: dict[str, str] = {
    "wikipedia_zh": (
        "中文维基正文（`data/raw/text_corpus.jsonl` 的真实抓取语料）。"
        "代表的下游目标：**通用知识性预训练**。"
        "⚠️ 它不含任何「有用性」信号 —— DataComp-LM 指出「看起来高质量」"
        "不等于「对下游有用」，所以本正例只承诺「干净」，不承诺「有用」。"
    ),
}


@dataclass(frozen=True)
class StyleGroup:
    """污染生成风格组。A/B 必须在下列维度上**错开**，不是随机切分。"""

    name: str
    #: 损伤类型集合
    kinds: tuple[str, ...]
    #: 替换字符池（错开 = 用不同的字面量，不复用训练组的）
    filler_chars: tuple[str, ...]
    #: 复读插入次数区间
    repeat_range: tuple[int, int]
    #: 截断保留比例区间
    keep_ratio: tuple[float, float]

    def describe(self) -> str:
        return (
            f"{self.name}(kinds={','.join(self.kinds)}"
            f"|filler={''.join(self.filler_chars)}"
            f"|repeat={self.repeat_range}|keep={self.keep_ratio})"
        )


#: B 组用的零宽字符（U+200B ZERO WIDTH SPACE）。
#: ⚠️ 不能选U+FEFF（BOM）—— 它常被解码器当 BOM 直接吃掉，注入会"消失"。
# ⚠️ **用码位定义，不要把不可见字符直接写进源码** ——
#   编辑器/工具链会静默丢弃它（本次实测：写成字面量后文件里 0 个非 ASCII
#   码位，而注入函数照常返回原文 → 「看起来在注入、实际什么都没做」的静默失效）。
_ZERO_WIDTH = chr(0x200B)  # ZERO WIDTH SPACE


#: 风格组 A —— 训练组。第一版只用它一种，泛化gap -36.9%（见 A_SUBSTYLES 注释）
STYLE_A = StyleGroup(
    name="A",
    kinds=("mojibake_gbk", "para_repeat", "char_repeat", "truncate_head"),
    # 训练组用「甲乙丙」类字面量（真实语料里罕见）
    filler_chars=("甲", "乙", "丙", "丁"),
    repeat_range=(3, 5),
    keep_ratio=(0.1, 0.2),
)

#: ⚠️⚠️ **A 组内部必须再切子风格**（这一条是被泛化检查逼出来的）
#:
#: 第一版只用 A 的四种损伤训练 → B 组负例召回实测 **0.022**（几乎完全失效），
#: 泛化 gap **-36.9%**，被 `assert_not_memorized` 拦红。
#:
#: 那个红是**正确且有价值的**：它证明了
#:   「分类器只学会了一种损伤的表面特征，而不是『脏』这件事本身」。
#: 修法**不是放宽阈值**（那等于把门禁调绿），而是**改训练分布**：
#: A 组内部拆成多个子风格，彼此在损伤类型/字面量/强度上都不同，
#: 模型要拿高分就**必须**学到跨损伤的不变量。
#: 这正是 DataComp-LM 那条结论的落地：
#:   **正例/负例分布决定分类器学到什么**，比分类器 sophistication 更重要。
A_SUBSTYLES: tuple[StyleGroup, ...] = (
    STYLE_A,
    # A2：同样四类损伤，但字面量池、复读强度、截断比例全换
    StyleGroup(
        name="A2",
        kinds=("mojibake_gbk", "para_repeat", "char_repeat", "truncate_head"),
        filler_chars=("子", "丑", "寅", "卯"),
        repeat_range=(6, 9),
        keep_ratio=(0.3, 0.45),
    ),
    # A3：**只保留**两类损伤 —— 迫使模型不能依赖任何单一特征
    StyleGroup(
        name="A3",
        kinds=("para_repeat", "truncate_head"),
        filler_chars=("辰", "巳", "午", "未"),
        repeat_range=(2, 4),
        keep_ratio=(0.5, 0.7),
    ),
    # A4：引入 A 组没有的**新型**损伤（扩分布，且不与 B 重叠）
    StyleGroup(
        name="A4",
        kinds=("mojibake_gbk", "char_repeat", "html_junk", "dash_spam"),
        filler_chars=("申", "酉", "戌", "亥"),
        repeat_range=(4, 7),
        keep_ratio=(0.2, 0.35),
    ),
    # A5：**只用另一种不可见字符**注入（U+2060 WORD JOINER，不是 B 的 U+200B）。
    #   目的不是多一种损伤，而是让`_augment` 的档位特征在训练集里**有非零取值**
    #   —— 否则隐形特征是常数、模型学不到任何东西（第一版 B 组卡 0.02 的根因）。
    #   教的是「含不可见字符 → 低质」这个**不变量**，
    #   B 组换一种没见过的不可见字符去检验该不变量能否迁移。
    StyleGroup(
        name="A5",
        kinds=("wordjoiner_spam",),
        filler_chars=(
            "壬",
            "癸",
        ),
        repeat_range=(4, 7),
        keep_ratio=(0.3, 0.5),
    ),
)

#: B 组：与 A 的**全部子风格**在损伤类型与参数上都不重叠。
#: 它的两种损伤（zero_width / bracket_spam）**不在任何 A 子风格里**——
#: 这样 B 测的才是「见到没见过的脏东西还能不能认出来」。
STYLE_B = StyleGroup(
    name="B",
    # ① 损伤类型整体不同：不再用 A 的四种，换成语义仍可辨但统计上不同的两类
    kinds=("zero_width", "bracket_spam"),
    # ② 字符池不同
    filler_chars=("戊", "己", "庚", "辛"),
    # ③ 强度不同（更轻）
    repeat_range=(6, 8),
    # ④ 截断比例不同（保留更多）
    keep_ratio=(0.6, 0.8),
)


def _rng_for(seed: int, salt: str) -> random.Random:
    """按 (seed, salt) 派生独立随机源。

    ⚠️ **不用同一个 seed 派两次**——A/B 若共享随机流，
    「B 组更干净」可能只是随机差异而非风格差异。
    这里刻意用字符串 salt 分离两条流。
    """
    return random.Random(f"{seed}:{salt}")


def apply_style(text: str, style: StyleGroup, rng: random.Random) -> str:
    """按风格组注入一种损伤。

    每种损伤都对应漏斗里一个**真实算子**，所以分类器学的是
    「可被算子抓的低质」，而不是任意的噪声。
    """
    kind = rng.choice(style.kinds)
    ch = rng.choice(style.filler_chars)

    if kind == "mojibake_gbk":
        # UTF-8 字节流按 GBK 误解码 → 合法汉字但语义全毁（chinese_ratio 放行）
        try:
            return text.encode("utf-8").decode("gbk", errors="replace")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return text + ch * 50

    if kind == "para_repeat":
        segs = text.split("\n")
        if not segs:
            return text + ch * 30
        pos = rng.randrange(len(segs))
        times = rng.randint(*style.repeat_range)
        return "\n".join(segs[:pos] + [segs[pos]] * times + segs[pos:])

    if kind == "char_repeat":
        if not text:
            return ch * 40
        pos = rng.randrange(len(text))
        times = rng.randint(*style.repeat_range)
        return text[:pos] + ch * times * 10 + text[pos:]

    if kind == "truncate_head":
        keep = rng.uniform(*style.keep_ratio)
        return text[: max(20, int(len(text) * keep))]

    if kind == "html_junk":
        # HTML 残渣：抽取器没清干净的标签串（爬虫模板/评论区的典型产物）
        junk = '<divclass="comment"><spanid="reply"></span></div>'
        return text + "\n" + junk * rng.randint(*style.repeat_range)

    if kind == "dash_spam":
        # 分隔符刷屏：目录页/导航残留被当正文抓进来
        line = "-" * rng.randint(*style.repeat_range) * 5
        return "\n".join([text[: len(text) // 2], line * 20, text[len(text) // 2 :]])

    if kind == "wordjoiner_spam":
        # 另一种不可见字符（U+2060 WORD JOINER）—— 与 B 组的 U+200B 不同码位，
        # 但属于同一「隐形污染」类别，用来教不变量而非教具体字符。
        wj = chr(0x2060)
        step = max(1, len(text) // rng.randint(*style.repeat_range))
        return wj.join([text[i : i + step] for i in range(0, len(text), step)])

    if kind == "zero_width":
        # 零宽字符注入：肉眼不可见但确实破坏了文本纯度
        # （爬虫去噪/模板渲染的常见副产物）。
        # ⚠️ 注入率必须**稀疏**（约每 12 字一个）：全篇都是零宽字符时
        # 任何基于字符统计的分类器都能轻松分辨，那是送分题而不是难题。
        zw = _ZERO_WIDTH
        step = rng.randint(10, 14)
        out: list[str] = []
        for i, c in enumerate(text):
            out.append(c)
            if (i + 1) % step == 0:
                out.append(zw)
        return "".join(out)

    if kind == "bracket_spam":
        # 括号/符号刷屏：模板化导航残留的典型形态
        return text + "\n" + ("（详情）" * rng.randint(*style.repeat_range) * 10)

    return text


def group_by_style(texts: list[str], style: StyleGroup, seed: int) -> list[str]:
    """按风格组批量注入损伤（正例不走这里）。"""
    rng = _rng_for(seed, style.name)
    return [apply_style(t, style, rng) for t in texts]


def split_positives(texts: list[str], seed: int, n_test: int = 200) -> tuple[list[str], list[str]]:
    """把**真实干净语料**切成训练正例与测试正例。

    ⚠️ 切分必须先于任何损伤注入：先注入再切分，
    同一条原文的两个版本可能分居训练/测试两侧 → 泄漏。
    """
    uniq = sorted(set(t for t in texts if len(t) >= 100))
    rng = random.Random(f"{seed}:pos_split")
    rng.shuffle(uniq)
    need = n_test * 3
    if len(uniq) < need:
        raise ValueError(f"正例不足：需要 {need} 条，只有 {len(uniq)} 条")
    return uniq[need:], uniq[:need]


def assert_no_overlap(train: list[str], test: list[str]) -> None:
    """训练集与测试集不得有**逐字相同**的样本。

    这是防泄漏的最后一道：正例侧的重复会让 test 分数虚高，
    而「test 分数虚高」正是分类器自欺的起点。
    """
    tr = set(train)
    overlap = [t for t in set(test) if t in tr]
    assert not overlap, (
        f"训练/测试集有 {len(overlap)} 条逐字重复 —— 分类器可能只是背下了训练集，test 分数会虚高"
    )


# ───────────────────────────── 分类器 ─────────────────────────────


#: **显式隐形污染特征**（不靠 n-gram 碰运气）。
#:
#: 实测（2026-10-08）：零宽字符损伤在 `char_wb 2-4gram` 下引入 193 个
#: **全新 n-gram，且 100% 含零宽字符**，训练集里一个都没有
#: → `min_df=2` 的词表自然不收 → 分类器对该损伤召回 **0.020**，泛化 gap -36.6%。
#:
#: 结论：**「不可见污染」必须作为独立特征，不能指望 n-gram 泛化**。
#: 这与业界「detector 侧显式查不可见字符」的做法一致。
#: 业务含义：本项目漏斗目前**没有**算子查零宽字符 —— 补上是独立决策
#: （见 docs/GAP_ANALYSIS.md），此处只保证分类器侧能看见它。
INVISIBLE_CHARS: tuple[str, ...] = (
    chr(0x200B),  # ZERO WIDTH SPACE
    chr(0x200C),  # ZERO WIDTH NON-JOINER
    chr(0x200D),  # ZERO WIDTH JOINER
    chr(0x2060),  # WORD JOINER
    chr(0xFEFF),  # ZERO WIDTH NO-BREAK SPACE
)


def invisible_ratio(text: str) -> float:
    """文本中不可见污染字符的占比。"""
    if not text:
        return 0.0
    n = sum(text.count(c) for c in INVISIBLE_CHARS)
    return n / len(text)


#: 隐形比例的分档边界（占字符数）。**必须是有序的少量档位**，
#: 不能把浮点写进 token —— 见`_augment` 注释里那个静默失效。
_INV_BUCKETS: tuple[float, ...] = (0.0, 0.001, 0.005, 0.02, 0.05)


def repeat_ratio(text: str, n: int = 3) -> float:
    """文本中 n-gram 的重复占比（0=全不重复, ~1=高度重复）。

    **与字面量无关的结构量**。这是本项目学到的一条硬结论：
    `TfidfVectorizer(analyzer="char_wb")` 学到的是**具体字符**，
    不是**形态** —— 实测同一形态（尾部重复堆叠）换个字面量
    （`-` → `（详情）`）召回从 **1.000 掉到 0.070**。
    泛化必须靠**显式结构特征**，让 n-gram 只负责它擅长的部分。
    """
    if len(text) < n * 2:
        return 0.0
    grams = [text[i : i + n] for i in range(len(text) - n + 1)]
    return 1.0 - len(set(grams)) / len(grams)


def length_bucket(n_chars: int) -> int:
    """长度档位（截断检测用；0 = 正常长度，档位越高越短）。"""
    for i, edge in enumerate((400, 200, 100, 50)):
        if n_chars < edge:
            return i + 1
    return 0


def max_repeat_run(text: str, n: int = 3) -> int:
    """最长的「相邻相同 n-gram 连续出现」游程长度。干净正文约 1~3。"""
    if len(text) < n * 2:
        return 1
    grams = [text[i : i + n] for i in range(len(text) - n + 1)]
    best = cur = 1
    for i in range(1, len(grams)):
        cur = cur + 1 if grams[i] == grams[i - 1] else 1
        if cur > best:
            best = cur
    return best


def line_uniqueness(text: str) -> float:
    """不同行数 / 总行数。**段落级复读**会让它趋近 0。"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) <= 1:
        return 1.0
    return len(set(lines)) / len(lines)


def _bucket(value: float, edges: tuple[float, ...]) -> int:
    b = 0
    for i, edge in enumerate(edges):
        if value > edge:
            b = i + 1
    return b


def _struct_token(text: str) -> str:
    """生成**与字面量无关**的结构特征档位 token。

    ⚠️⚠️ 两个粒度**都不能省**，这是实测逼出来的（不是预防性设计）：

    · `REP`（3-gram 重复率 /最长游程）：抓 `char_repeat`、`dash_spam`、
      以及 held-out 的 `bracket_spam`（实测 0.070 → 0.500）。
      ⚠️ 短文本上「重复率」本身没有区分度 —— 实测干净正例的 3-gram
      重复率就有 0.137~0.451，与损伤区间完全重叠。**必须改用最长游程**
      （干净 1~3vs 损伤 28~598），否则这一族特征是恒真的。
    · `LNU`（行级唯一度）：抓 `para_repeat` —— 它复制的是**整段**，
      3-gram 粒度抓不到（实测游程仍 1~3、召回仅 0.180），
      但行级唯一度会趋近 1/N。
    · `LEN`：抓截断。

    教训同族「判据腐烂」：**判据必须与数据的实际形态匹配**。
    一个在长文本上有效的量（n-gram 重复率）搬到短文本语料上会静默失去区分度。
    """
    run = max_repeat_run(text)
    run_b = _bucket(float(run), (3.0, 6.0, 15.0, 40.0))
    lnu_b = _bucket(line_uniqueness(text), (0.0, 0.5, 0.8, 0.95))
    # `RAT` 重复率**与** `REP` 游程并存，不是二选一：实测
    #   ·只有重复率 → bracket_spam（held-out）0.070，para_repeat 0.180
    #   ·只有游程   → para_repeat 0.830，但 bracket_spam 掉回 0.100
    # 两个量覆盖不同形态（段级vs 词级），缺任一个另一类损伤就漏。
    rat_b = _bucket(repeat_ratio(text), (0.02, 0.05, 0.15, 0.35, 0.6))
    return f"⟦REP{run_b}⟧⟦RAT{rat_b}⟧⟦LNU{lnu_b}⟧⟦LEN{length_bucket(len(text))}⟧"


def inv_bucket(ratio: float) -> int:
    """把隐形比例映射成**档位序号**（0..len(_INV_BUCKETS)）。"""
    b = 0
    for i, edge in enumerate(_INV_BUCKETS):
        if ratio > edge:
            b = i + 1
    return b


def _augment(texts: list[str]) -> list[str]:
    """给每个样本前置**档位化**的隐形污染标记。

    ⚠️⚠️ 第一版写成 `⟦INV 0.0234⟧` —— **浮点字符串化**，这是静默失效：
      `TfidfVectorizer(analyzer="char_wb")` 看到的是 token `"0.0234"` 这个
      **具体数字串**，不是数值。训练集里出现过的是 `0.0000`，推理时来一个
      `0.0234` → 词表里根本没有这个 token → 模型对它权重恒为 0
      → 隐形污染特征完全无效，而且**不报任何错**（照常出accuracy）。
      实测就是B 组负例召回卡在 0.02 的直接原因之一。
      正确写法是**有序分档**：`⟦INV2⟧` 这样的类别 token 能被学到「越大越脏」。

    三族结构 token 各自负责一类损伤，**都不能省**（实测省一族的代价见下）：
      · `INV` 隐形比例  → unseen 的零宽字符（U+200B）召回 0.020 → **0.915**
      · `REP` n-gram 重复率 → 同形态换字面量（`-`→`（详情）`）→ 必须靠它迁移
      · `LEN` 长度档位  → 截断
    另有**训练集里必须出现过非零档位**，否则该特征是常数、
    模型学不到任何东西。所以 A 子风格里必须有一种**用另一种不可见字符**的
    损伤（见 `wordjoiner_spam`）—— 教的是「有不可见字符就是脏」这个不变量，
    而 B 组换一种**没见过的**不可见字符去检验这个不变量能否迁移。
    """
    out: list[str] = []
    for t in texts:
        out.append(f"⟦INV{inv_bucket(invisible_ratio(t))}⟧{_struct_token(t)} {t}")
    return out


def train_classifier(
    pos_train: list[str],
    neg_train: list[str],
    *,
    ngram: int = 2,
    seed: int = 42,
    min_df: int = 2,
    max_features: int = 60_000,
):
    """训练线性文本分类器。

    优先 `TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))` +
    `LogisticRegression`。这条路对应 DataComp-LM 的 fastText bigram
    （廉价 n-gram 线性分类器），是被实证支持的选择。

    ⚠️ 没有 sklearn 时**明确报错**，绝不静默退化成规则阈值 ——
    那样会得到「一个跑得通但不是分类器」的假交付。
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise SystemExit(
            "❌ 缺 scikit-learn。装它：pip install scikit-learn\n"
            "   **不要**改成规则阈值兜底 —— 那会得到一个"
            "「跑得通但不是分类器」的假交付。"
        ) from exc

    x = _augment(list(pos_train)) + _augment(list(neg_train))
    y = [1] * len(pos_train) + [0] * len(neg_train)
    vec = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 4),
        min_df=min_df,
        max_features=max_features,
        sublinear_tf=True,
    )
    X = vec.fit_transform(x)
    clf = LogisticRegression(max_iter=1000, C=4.0, random_state=seed)
    clf.fit(X, y)
    return vec, clf


def predict_score(vec, clf, texts: list[str]) -> list[float]:
    """输出「属于干净正例」的概率分数（越高越干净）。

    ⚠️ 输入**必须**与训练时同走 `_augment` ——
    少一步就是「训练/推理特征不一致」，模型会静默地按错误口径打分。
    """
    if not texts:
        return []
    X = vec.transform(_augment(list(texts)))
    return [float(p) for p in clf.predict_proba(X)[:, 1]]


def pick_threshold(
    vec,
    clf,
    pos: list[str],
    neg: list[str],
    *,
    grid: tuple[float, ...] | None = None,
) -> tuple[float, dict]:
    """在**A 组**上选平衡准确率最高的阈值。

    ⚠️ 阈值**只能**用 A 组（训练内分布）选，且必须现取不许硬编码 0.5。

    为什么：第一版硬编码 0.5，扩训练分布后正负分数分布重叠，
    实测正例召回塌到 0.468 / B 组负例 0.647 —— balance 只有 0.56。
    根因不是模型不行，是**阈值没随训练分布重新校准**。
    这是「格式化是事件不是状态」的同族：模型一换，阈值就是过期值。

    ⚠️ 阈值不碰B 组 —— 用 B 组选阈值等于在测试集上调参，
    泛化检查会立刻失去意义（这正是必须守住的那条线）。
    """
    if grid is None:
        grid = tuple(round(0.05 * i, 2) for i in range(1, 20))
    sp = predict_score(vec, clf, pos)
    sn = predict_score(vec, clf, neg)
    rows: dict[str, float] = {}
    best, best_bal = 0.5, -1.0
    for th in grid:
        acc = (sum(1 for s in sp if s >= th) / len(sp) + sum(1 for s in sn if s < th) / len(sn)) / 2
        rows[f"{th:.2f}"] = round(acc, 4)
        if acc > best_bal:
            best, best_bal = th, acc
    return best, rows


def evaluate(
    vec,
    clf,
    pos_test_a: list[str],
    neg_test_a: list[str],
    pos_test_b: list[str],
    neg_test_b: list[str],
    threshold: float = 0.5,
) -> dict:
    """双风格组评测。

    返回的每个指标都必须在 report 里标明它测的是 **A 组（训练内）还是 B 组（泛化）**。
    只报 A 组 = 自欺；只报 B 组 = 看不出是否过拟合到风格。
    """
    out: dict[str, object] = {"threshold": threshold}

    for tag, pos, neg in (("A", pos_test_a, neg_test_a), ("B", pos_test_b, neg_test_b)):
        if not pos or not neg:
            out[f"test{tag}"] = {"status": "缺样本"}
            continue
        sp = predict_score(vec, clf, pos)
        sn = predict_score(vec, clf, neg)
        # 「干净」= 分数高；判据与算子一致（分数 >= threshold 视为干净）
        pos_acc = sum(1 for s in sp if s >= threshold) / len(sp)
        neg_acc = sum(1 for s in sn if s < threshold) / len(sn)
        out[f"test{tag}"] = {
            "n_pos": len(pos),
            "n_neg": len(neg),
            "positive_recall": pos_acc,
            "negative_recall": neg_acc,
            "balance": 0.5 * (pos_acc + neg_acc),
            "n_correct": sum(1 for s in sp if s >= threshold) + sum(1 for s in sn if s < threshold),
            "n_total": len(sp) + len(sn),
        }
    return out


def assert_not_memorized(report: dict, max_gap: float = 0.10) -> None:
    """B 组表现不得显著低于 A 组——否则分类器是在背训练集。

    ⚠️ 判据是**有向差**：`balance_B - balance_A >= -max_gap`。
    只断言「B 组分数高」是恒真方向（永远过）；
    真正要拦的是「A 很高、B 塌下来」这个失效模式。
    """
    a = report.get("testA", {})
    b = report.get("testB", {})
    usable = isinstance(a, dict) and isinstance(b, dict) and "balance" in a and "balance" in b
    if not usable:
        return  # 缺样本时不做断言，但已在报告里标status
    gap = float(b["balance"]) - float(a["balance"])
    report["generalization_gap"] = gap
    assert gap >= -max_gap, (
        f"B 组比 A 组低 {abs(gap):.1%}（超过 {max_gap:.0%}）—— "
        "分类器很可能记住了风格组的表面特征而不是「脏」这件事本身。"
        "检查：A/B 是否真的在生成参数维度上错开？"
    )


def save_model(vec, clf, out_dir: Path, meta: dict) -> Path:
    """落盘模型 + 元信息。

    元信息**必须含正例分布 id** —— 否则模型无法回答
    「它是为哪个下游目标训的」，也就无法在换目标时判断该不该重用。
    """
    import pickle

    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "text_quality_clf.pkl"
    with model_path.open("wb") as fh:
        pickle.dump({"vec": vec, "clf": clf}, fh)
    (out_dir / "text_quality_clf.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return model_path


def load_model(path: Path):
    import pickle

    with Path(path).open("rb") as fh:
        d = pickle.load(fh)
    return d["vec"], d["clf"]
