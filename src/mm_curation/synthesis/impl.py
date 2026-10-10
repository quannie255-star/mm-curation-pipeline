"""增强器实现：挑的是**污染器真实注入过、且可逆**的形态。

这个对应关系是刻意的，不是巧合——它让「合成」在本项目里有一个可检验的含义：

    污染器把干净样本弄脏（叠字 / 重复 8-gram / 缺标点 / 裁剪 / 模糊…）
    增强器把轻度受损样本修回干净（压叠字 / 去重复 / 补标点 / 重构图…）

## ★ 可逆性对照表（本模块最重要的边界，先说破）

读了 `contamination/impl.py` 的真实实现后可以确认：
**污染分成「可逆」与「不可逆」两类，只有可逆的才有增强器。**

读 `contamination/impl.py` 的真实实现后可以确认，污染分两类：

**可逆 / 半可逆（有对应增强器）**

- `low_quality_text`(repeat)：`text[:8] + "哈"*30`
  → 半可逆。叠字可压，但**前 8 字之后的原文在注入时已丢弃**。
  对应 `text_deredup`（只压叠字）。
- `near_duplicate_text`：删 3% 字符 + 复制一个 8-gram
  → 半可逆。重复段可去，**删掉的字回不来**。不去重，交给算子。
- `semantic_duplicate`：裁剪 + tags 重组
  → 分句顺序那部分可逆。对应 `text_paraphrase`。

**无污染对应，但真实存在（也做增强）**

- 爬取导致缺句末标点 → `text_truncate_repair`
- 真实数据里的叠字 / 叠词 → `text_deredup`
- 构图裁剪 → `image_crop_reframe`

**不可逆（明确写「无对应增强器」）**

- `low_quality_text`(truncate)：`text[:3]`，信息已丢
- `low_quality_text`(noise)：`'的地得和与及或在是被有了一个'` 虚词整串替换
- `low_quality_text`(mojibake)：GBK → UTF-8 错解
- `nsfw_placeholder`：换成生成的广告图
- `exact_duplicate`：靠去重算子处理，不靠增强
- `blur` / `low_resolution`：PIL 没有 un-blur / 超分逆运算，
  `image_sharpen` / `image_upscale` 只能锐化 / 插值，**不能还原**

**结论（比「可逆/不可逆」二分法更重要的那条）**：
**增强 ≠ 逆向还原**。哪怕是完全可逆的污染，
「增强后的样本」与「原始样本」也不是同一个东西——
它只是**同一分布下的另一个形态**。
所以本模块的定位是**分布增强**，不是「修脏数据」；
真正要「修」的样本，应该交给漏斗里的算子（那是它们的职责）。

那几行 ❌ 是本模块的诚实边界：
声称能修它们，就等于在编造内容——那样产出的不是「同一条样本的另一个形态」，
而是**一条凭空生成的新样本**，它的 ground truth 是假的。

第一版我写过两个错的声称，都留着记录：
1. `text_typo_fix` 造了一张「`0→o` 噪声还原表」，而项目里的噪声注入
   用的是`'的地得和与及或在是被有了一个'` 这类常用虚词整串替换——
   **凭空设计一张「反向表」等于自己跟自己玩**。已删除。
2. `text_deredup` 声称能复原 `repeat` 变体，实测**不能**（前半段已丢失）。
   见该类的 docstring。

## 为什么不复用 contamination 的注入函数
反向操作**不是**简单地「把污染器倒过来跑」：
污染器是单向的（加模糊），增强器要的是「减模糊」，
PIL 没有 un-blur 这种逆运算，只能自己实现（锐化滤波 / 插值上采样）。
真正能复用的是**同一套 PIL / 随机数 / 文件写出约定**，所以这里保持同样的写法风格。
"""

from __future__ import annotations

from ..operators.base import Sample
from .base import SynthesisContext, Synthesizer, register_synthesizer

# ---------------------------------------------------------------------------
# 文本类增强（不碰图片，纯文本操作）
# ---------------------------------------------------------------------------


@register_synthesizer("text_truncate_repair")
class TruncateRepair(Synthesizer):
    """补全**残缺标点**的文本。

    ## 边界说清：这不是 `low_quality_text`(truncate 变体) 的反向
    污染器的 truncate 是 `text[:3]`——直接切前 3 字，
    **信息已经丢了，无法还原**（不知道原文多长、写了什么）。
    任何声称能「修复」它的增强器都是在编内容。

    这里做的是另一件真实存在、且可逆的事：
    爬取/截断常让文本**停在半个标点上**（"链路可追" / "指标口径统一，"），
    补一个句末标点让它成为完整句子。

    约束：只补**结构性收尾**，不追加任何字词——
    追加字词就是编内容，会让「合成样本仍然干净」失效
    （它变成了一条新样本，而不是「同一条样本的另一个形态」）。
    """

    _TRAILING = "。．.!?！？；;，,、：:"

    def apply(self, source: Sample, index: int, ctx: SynthesisContext) -> Sample:
        t = source.text.rstrip()
        if t and t[-1] not in self._TRAILING:
            source.text = t + "。"
        source.meta["augment"] = {
            "kind": self.kind,
            "changed": bool(t) and t[-1] not in self._TRAILING,
        }
        return source


@register_synthesizer("text_deredup")
class DeRedup(Synthesizer):
    """压掉叠字与叠词 —— 面向**真实存在的**叠字形态。

    ## ★它不能「复原」污染注入的文本（2026-10-05 实测）
    第一版 docstring 声称它是 `low_quality_text`(repeat) 的反向。
    实测闭环（注入 → 增强 → 与原文比）**不成立**：

        原文      '数据平台建设需要统一指标口径，并且链路可追溯…'（32 字）
        注入      '数据平台建设需要' + '哈'*30← text[:8] + "哈"*30
        增强后'数据平台建设需要哈哈'
        →前8 字之外的原文**在注入时就被丢弃了**，压掉"哈"也回不来。

    结论：`repeat` 变体是**「截断 + 叠字」的复合**，
    只有叠字那一半可逆。所以本增强器不声称能复原注入样本，
    它处理的是**真实数据里本来就有**的叠字（刷评、叠词、口水话）。

    这个修正值得留着：它是「**增强 ≠ 逆向还原**」的第一个实例，
    而我最初把两者当成了一回事——那会让整个「合成 = 修复污染」的叙事变成空话。

    ## 那为什么还保留它
    因为「去叠字」本身是真实需求（叠字会污染 n-gram 特征、
    让 MinHash 召回虚高），它属于**分布增强**而非**损伤修复**。
    """

    #: 叠字数超过这个值就判定为刷字（正文里连着同一个字出现这么多次不合理）
    _MAX_RUN = 4

    def apply(self, source: Sample, index: int, ctx: SynthesisContext) -> Sample:
        text = source.text
        out: list[str] = []
        run = 1
        for ch in text:
            if out and ch == out[-1]:
                run += 1
                # 超过阈值就压回阈值：保留正常叠词（"重要""看看"），
                # 只压掉明显的刷字（"哈"*30）
                if run <= self._MAX_RUN:
                    out.append(ch)
                continue
            run = 1
            out.append(ch)
        fixed = "".join(out)
        source.text = fixed
        source.meta["augment"] = {
            "kind": self.kind,
            "changed": fixed != text,
            "len_before": len(text),
            "len_after": len(fixed),
        }
        return source


@register_synthesizer("text_paraphrase")
class Paraphrase(Synthesizer):
    """同义改写 —— `semantic_duplicate` 的反向。

    做法：把 tags 重组（逗号内换序）+ 在句首加一个连接词。
    目的是让**基于哈希的算子失效**（字节变了、词袋变了），
    但语义不变——所以它检验的是 embedding 类算子的能力。
    """

    _CONNECTORS = ("该条目：", "具体而言，", "概述：")

    def apply(self, source: Sample, index: int, ctx: SynthesisContext) -> Sample:
        rng = ctx.rng
        t = source.text.strip()
        # 先把句末标点摘下来再旋转：直接对整串 split/join 会把句号
        # 夹到句子中间，产出「可追溯。，数据平台建设」这种病句。
        # （2026-10-05 实跑抓到的，不是推演出来的。）
        tail = ""
        while t and t[-1] in "。．.!?！？：:;":
            tail = t[-1] + tail
            t = t[:-1]
        # ★ 连接词要先剥掉再判断，否则旋转后它会跑到句子中间，
        #   第二次调用 `startswith` 就检测不到，于是又加一个 →
        #   「该条目：丙，甲，具体而言，该条目：乙。」（2026-10-05 测试抓到）。
        #   剥离必须在**剥句末标点之后、旋转之前**做，两步都要。
        had_connector = ""
        for c in self._CONNECTORS:
            if t.startswith(c):
                had_connector = c
                t = t[len(c) :]
                break
        parts = [p.strip() for p in t.replace("，", ",").split(",") if p.strip()]
        if len(parts) >= 2:
            # 旋转一位：词袋不变、顺序变了 → 哈希变、embedding 不变
            parts = parts[1:] + parts[:1]
            # 已经加过连接词就**沿用原来那个**（而不是再随机加一个）：
            # 多轮增强时连接词会随旋转跑到句子中间，
            # 只靠 startswith 检测不到，必须先剥后重建。
            lead = had_connector or self._CONNECTORS[rng.randrange(len(self._CONNECTORS))]
            source.text = lead + "，".join(parts) + tail
        else:
            # 只有一个分句就没什么可旋转的：不动它，而不是硬造变化
            source.text = t + tail
        source.meta["augment"] = {"kind": self.kind, "changed": len(parts) >= 2}
        return source


# ---------------------------------------------------------------------------
# 图像类增强（需要写图片文件）
# ---------------------------------------------------------------------------


@register_synthesizer("image_sharpen", needs_image=True)
class Sharpen(Synthesizer):
    """锐化 —— `blur` 的反向。

    PIL 的 ImageFilter.UnsharpMask 就是为这件事准备的。
    注意：锐化**不能把模糊完全还原**（信息已经丢了），
    它的意义是给出「轻度失焦」的另一个形态，
    用于检验算子是不是只认「原图 vs 糊图」这种二元差异。
    """

    def apply(self, source: Sample, index: int, ctx: SynthesisContext) -> Sample:
        from PIL import Image, ImageFilter

        if not source.image_path:
            return source
        src = ctx.images_out / f"synth_{index}_{source.id.replace(':', '_')}.png"
        with Image.open(source.image_path) as im:
            im.convert("RGB").filter(ImageFilter.UnsharpMask(radius=2, percent=150)).save(src)
        source.image_path = str(src)
        source.meta["augment"] = {"kind": self.kind, "changed": True}
        return source


@register_synthesizer("image_upscale", needs_image=True)
class Upscale(Synthesizer):
    """提分辨率 —— `low_resolution` 的反向。

    用 LANCZOS 上采样回原尺寸。**必须记下原尺寸**，
    否则这条样本「本该是低分辨率」的ground truth 就丢了——
    下游按图判断质量时需要这个信息。
    """

    def apply(self, source: Sample, index: int, ctx: SynthesisContext) -> Sample:
        from PIL import Image

        if not source.image_path:
            return source
        src = ctx.images_out / f"synth_{index}_{source.id.replace(':', '_')}.png"
        with Image.open(source.image_path) as im:
            rgb = im.convert("RGB")
            orig = rgb.size
            # 放大回**元数据里记的原尺寸**；没有就按 2x
            target = tuple(source.meta.get("orig_size") or (orig[0] * 2, orig[1] * 2))
            rgb.resize(target, Image.LANCZOS).save(src)
        source.meta.setdefault("orig_size", orig)
        source.image_path = str(src)
        source.meta["augment"] = {
            "kind": self.kind,
            "from_size": list(orig),
            "to_size": list(target),
            "changed": True,
        }
        return source


@register_synthesizer("image_crop_reframe", needs_image=True)
class CropReframe(Synthesizer):
    """裁剪重构图 —— `mismatched_pair` 的反向。

    裁掉边缘 5%~12% 再贴回原尺寸：主体还在，但**构图变了**。
    它的检验价值在于：如果判官只靠「整体布局」判断图文相关，
    裁剪就会破坏它——这正是过拟合的信号。
    """

    def apply(self, source: Sample, index: int, ctx: SynthesisContext) -> Sample:
        from PIL import Image

        if not source.image_path:
            return source
        rng = ctx.rng
        src = ctx.images_out / f"synth_{index}_{source.id.replace(':', '_')}.png"
        with Image.open(source.image_path) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            fx = rng.uniform(0.05, 0.12)
            fy = rng.uniform(0.05, 0.12)
            box = (int(w * fx), int(h * fy), int(w * (1 - fx)), int(h * (1 - fy)))
            if box[2] <= box[0] or box[3] <= box[1]:
                return source  # 图太小，裁不动就**不改**而不是产出坏样本
            rgb.crop(box).resize((w, h), Image.LANCZOS).save(src)
        source.image_path = str(src)
        source.meta["augment"] = {"kind": self.kind, "crop_frac": [round(fx, 4), round(fy, 4)]}
        return source
