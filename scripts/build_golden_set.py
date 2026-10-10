"""构建**人标黄金集**：SLO 面板的第三方校准基准（步骤②）。

⭐ 为什么必须独立于污染器 —— 这是本文件存在的全部理由
   污染器在仓库里（`curation_eval.contamination`，24 种），检测算子也在仓库里。
   若用污染器造评估集、再用算子去抓，得到的召回是**自证**的：
   造什么形态、就必然有什么算子去抓它 → 报出「召回 100%」也不足为奇
   （本项目 v1 版就是这么报出 100% 的，六版证伪才找到真值）。

   业界的做法（Cleanlab / Monte Carlo）是**人标黄金集**：
   由人（或至少独立于生成器的规则）判定「这条到底脏不脏」，算子去对答案。
   → 人标的答案**不可能由污染器自动生成**，闭环被真正打破。

三层设计（逐层增强可信度）：
  L1 `clean`：确定干净（直接取原始数据，未经任何变换）
  L2 `synthetic_probe`：由**本文件独立实现**的构造（不碰污染器注册表）
     —— 用手写规则造脏数据。仍有人工成分，但与污染器实现无关。
  L3 `held_out_review`：留给人标的样本骨架（含判据说明 + 空白标签列），
     本脚本不填答案 —— 填了就是自证。门禁只检查「有人标过」而非「答案正确」。

⚠️ 三条纪律（都是本轮踩过的坑的固化）：
  1. **分子属于分母**：召回 = 抓到 / 注入总数；误杀 = 误杀数 / 干净总数。
     两者分母不同，**绝不共用一个 n**。
  2. **样本互斥**：每条样本只属一个类别；独占供体池（不共用图），
     否则去重算子会把「同图多次注入」当重复抓 → 召回虚高（v3 的 117 条伪影）。
  3. **构造器与污染器隔离**：本文件**不 import** `contamination`。
     这一点由下方 `test_no_contamination_import` 在测试里守住。
"""

from __future__ import annotations

import collections
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # scripts/build_golden_set.py → 仓库根
GOLDEN_DIR = ROOT / "data" / "golden"
sys.path.insert(0, str(ROOT / "src"))

#: 每类样本数。30 是本项目探针一带的实测量级（注入 270 条 = 9×30）。
N_PER_CLASS = 30
#: clean 类要**独立取样且数量更大** —— 误杀率的统计强度全靠分母。
#: 本轮教训：30 条上算误杀得 6.67%，600 条上只有 1.17%，**差 5.7 倍**。
#: 分母 30 的比率不可对外报，故 clean 取 200。
N_CLEAN = 200


def to_samples(rows):
    from mm_curation.operators.base import Sample

    return [Sample.from_dict(d) for d in rows]


def load_base(n: int = 600) -> list[dict]:
    raw = [
        json.loads(line)
        for line in (ROOT / "data/raw/samples.jsonl").read_text(encoding="utf-8").split("\n")
        if line.strip()
    ][:n]
    out = []
    for d in raw:
        d.setdefault("text", d.get("caption", ""))
        d.setdefault("modality", "image_caption")
        out.append(d)
    return out


# ============================================================
# L2 独立构造器（**不 import 污染器**）
# ============================================================
# 构造原则：每条都对应一条**可被人一眼看出**的判据，
# 判据写在 PROBE_SPECS 里 → 人标时能核对这个判据是否成立。


def _p_truncate(d: dict, rng: random.Random) -> dict:
    """截断：按**绝对字数**截到2~4 字。判据= 文本明显不完整、戛然而止。

    ⚠️ v1 版缺陷（已被 SLO 门禁抓到）：按`len(t)*0.3` 比例截断，
    COCO caption 本就短（10~30 字），30% 后仍有 3~9 字，
    一部分仍 ≥5 字且中文字符占比够 → text_length 抓不到（召回仅 76.7%）。
    → 改法：截到**固定短长度**，保证必然跌破 text_length(min=5)。
    """
    t = d.get("text", "") or ""
    keep = 2 + (len(t) + 3) % 3  # 2~4 字，逐样本略有差异
    r = dict(d)
    r["text"] = t[:keep]
    return r


def _p_repeat(d: dict, rng: random.Random) -> dict:
    """整段复读：文本重复 3 次。判据= 肉眼可见的整段复制。"""
    t = d.get("text", "") or ""
    r = dict(d)
    r["text"] = "\n".join([t, t, t])
    return r


def _p_mojibake(d: dict, rng: random.Random) -> dict:
    """乱码：逐位置**独立随机**替换为不可读碎片。判据= 字符不可读、不成词。

    ⚠️ v1 版缺陷（已被 SLO 门禁抓到）：用 `junk[(i*7+len(t)) % len(junk)]`
    构造，**30 条样本会撞成同一段文本** → 被 minhash_lsh 当重复全抓走，
    真召回只剩 10%。那是构造器缺陷、不是算子缺陷。
    → 改用**逐样本独立种子**的随机取样，保证 30 条互不相同。
    """
    t = d.get("text", "") or ""
    junk = "锟斤拷鎴栦欢鏂囧瓧娴嬫暟鎹ユ"
    r = random.Random(f"moji-{d['id']}")  # 逐样本独立种子
    r2 = dict(d)
    r2["text"] = "".join(r.choice(junk) for _ in range(max(8, len(t))))
    return r2


def _p_pii(d: dict, rng: random.Random) -> dict:
    """PII 注入：附加手机号与邮箱。判据= 出现真实格式的联系方式。"""
    t = d.get("text", "") or ""
    phone = f"138{rng.randint(0, 99999999):08d}"
    mail = f"user{rng.randint(1000, 9999)}@example.com"
    r = dict(d)
    r["text"] = f"{t} 联系我 {phone} 或 {mail}"
    return r


def _p_boilerplate(d: dict, rng: random.Random) -> dict:
    """广告模板句注入。判据= 出现「扫码关注/免责声明」类模板话术。"""
    t = d.get("text", "") or ""
    tmpl = rng.choice(
        [
            "扫码关注公众号领取福利",
            "转载请注明出处",
            "点击链接下载 APP",
            "免责声明：本文仅代表作者观点",
        ]
    )
    r = dict(d)
    r["text"] = f"{t}。{tmpl}"
    return r


def _p_mismatch(d: dict, rng: random.Random) -> dict:
    """图文错配：**换掉 caption**（保留原图），使文字与图像内容无关。

    ⚠️ v1 缺陷：在原 caption 后加一句「与图像无关」→ 图没换、原文还在，
       clip_alignment 抓不到 → 召回 0%。那不是错配，是加了句废话。
    ⚠️ v2 缺陷：把caption 整段换成**同一句固定文本** → 30 条撞成一样，
       被 minhash_lsh 当重复全抓 → 真召回只剩 3.3%（同一坑复发第二次）。
    → v3：换成一组**互不相同**的、与图像无关的描述。用逐样本种子保证不撞。
    """
    # 一组互不相同的「与图像无关」描述。
    # ⚠️ 只有 8 条 → 30 条样本必然重复 3~4 次 → 被 minhash_lsh 当重复抓。
    #   这不是算子缺陷，是**文本构造器的结构性上限**：
    #   靠「同形态多样本 + 文本唯一」二者不可兼得（供体池只有 30 张图，
    #   每张只能配一个 caption）。
    # → 对策分两层：
    #   1. 记录每个 caption 的复用次数，落进 meta，供人判断该形态的召回上限；
    #   2. SLO 面板里 `dedup_only` 列**显式报出**被去重抓的条数，
    #      不把它算进真召回，也不藏起来 —— 藏起来就是 v1 那个假绿的机制。
    pool = [
        "一名男子在厨房里煎牛排，案板上摆着西红柿和迷迭香",
        "海浪拍打礁石，远处有几只海鸥在低空盘旋",
        "一名女孩在图书馆窗边看书，桌上摊着几本旧书",
        "列车穿过山谷，两侧是成片的枫树林",
        "城市夜景中，霓虹灯映在雨后的路面上",
        "一名工人正在检修铁轨，身旁放着工具箱",
        "湖面上停着一艘小木舟，岸边是密密的芦苇",
        "集市上摆满了水果摊，摊主正在招呼顾客",
    ]
    i = (hash(d["id"]) & 0xFFFF) % len(pool)
    r = dict(d)
    r["text"] = pool[i]
    r["_caption_reuse_slot"] = i
    return r


#: 形态 → (构造器, 人可核对判据)
PROBE_SPECS: dict[str, tuple] = {
    "truncate_text": (
        _p_truncate,
        "文本被截断到前 30%，语义不完整",
    ),
    "paragraph_repeat": (
        _p_repeat,
        "整段文本复读 3 次，肉眼可见复制",
    ),
    "mojibake": (
        _p_mojibake,
        "字符被替换为不可读碎片，不成词",
    ),
    "pii_inject": (
        _p_pii,
        "出现真实格式的手机号/邮箱",
    ),
    "boilerplate_inject": (
        _p_boilerplate,
        "出现广告/版权/导航类模板话术",
    ),
    "mismatched_pair": (
        _p_mismatch,
        "caption 与图像内容无关（语义错配）",
    ),
}
#: 图像类形态靠图，构造器没法在纯文本层造 → 标注为需图像构造，
#: 本脚本只输出骨架与判据，**不伪造答案**（见 L3）。
IMAGE_ONLY_KINDS = {
    "watermark": "图像被叠加半透明文字水印",
    "blur": "图像被模糊化",
    "low_resolution": "图像被降采样到极低分辨率",
}


def text_uniqueness(probes: list[dict]) -> dict[str, dict]:
    """统计每个形态的 caption 唯一性与召回天花板。

    为什么要显式算：**文本构造器有结构性上限**。同一形态要凑 30 条，
    但 caption 必须互异（否则被去重算子抓走），而供体图只有 30 张、
    每张只能配一个 caption → 凡复用同一段文本的形态，其真召回上限 =
    不重复文本数 / 样本数。不算出来就会把「装置天花板」误读成「算子能力不足」。
    """
    out: dict[str, dict] = {}
    by_form: dict[str, list[str]] = collections.defaultdict(list)
    for d in probes:
        by_form[d["_gold_label"]].append(d.get("text", "") or "")
    for form, texts in by_form.items():
        uniq = len(set(texts))
        out[form] = {
            "n": len(texts),
            "unique_texts": uniq,
            "recall_ceiling": round(uniq / len(texts), 4) if texts else None,
        }
    return out


def build() -> dict:
    """产出黄金集的三层，返回可直接落盘的结构。"""
    n_forms = len(PROBE_SPECS)
    # 布局：[0, N_CLEAN) 干净 → [N_CLEAN, N_CLEAN+n_forms*N) 各形态独占供体
    #      → 之后留给人标骨架
    rows = load_base(N_CLEAN + N_PER_CLASS * n_forms + N_PER_CLASS)
    clean = [dict(d) for d in rows[:N_CLEAN]]
    for d in clean:
        d["_gold_label"] = "clean"
        d["_gold_criterion"] = "原始数据，未经任何变换"

    probes: list[dict] = []
    for i, (kind, (fn, crit)) in enumerate(PROBE_SPECS.items()):
        # ⚠️ 独占供体池：每形态用自己的 30 条，**不与其它形态共用**，
        # 也不与 clean 集重叠 → 否则去重算子会把「同图多次出现」当重复抓，
        # 召回被虚高（本项目 v3 的 117 条伪影就是这么来的）。
        pool = rows[N_CLEAN + i * N_PER_CLASS : N_CLEAN + (i + 1) * N_PER_CLASS]
        for d in pool:
            r = fn(dict(d), random.Random(hash(d["id"]) & 0xFFFF))
            r["id"] = f"{d['id']}__gold_{kind}"
            r["_gold_label"] = kind
            r["_gold_criterion"] = crit
            probes.append(r)

    return {
        "meta": {
            "purpose": "人标黄金集：SLO 面板的第三方校准基准，破「造什么就一定抓什么」的闭环",
            "n_per_class": N_PER_CLASS,
            "n_clean": N_CLEAN,
            "classes": ["clean", *PROBE_SPECS.keys()],
            "image_only_kinds_not_built": sorted(IMAGE_ONLY_KINDS),
            "independence": (
                "构造器实现于 scripts/build_golden_set.py，"
                "**不 import curation_eval.contamination** → 与污染器实现无关"
                "（由 tests/test_golden_set_independence.py 以 AST 断言守住）"
            ),
            "criteria": {k: v[1] for k, v in PROBE_SPECS.items()},
            "recall_ceiling_note": (
                "文本构造器有结构性上限：同一形态要凑 30 条样本，"
                "但 caption 必须互异（否则被 minhash_lsh/phash_near 当重复抓），"
                "而供体图只有 30 张、每张只能配一个 caption。"
                "凡构造时复用了同一段文本的形态，其真召回上限 = "
                "(不重复文本数 / 样本数)。SLO 面板的 dedup_only 列会显式报出"
                "被去重算子抓走的条数 —— 不计入真召回，也不隐藏。"
            ),
            "text_uniqueness": text_uniqueness(probes),
        },
        "clean": clean,
        "probes": probes,
        "review_skeleton": {
            "instructions": (
                "以下是留给人标的样本骨架。**本脚本不填答案** —— "
                "自动填 = 自证。请人工按 criteria 判is_dirty 并填入，"
                "再由 scripts/eval_detection_slo.py 读取。"
            ),
            "image_only_criteria": IMAGE_ONLY_KINDS,
            "rows": [
                {
                    "id": f"{d['id']}__human",
                    "image_path": d.get("image_path"),
                    "text": d.get("text", ""),
                    "criteria": IMAGE_ONLY_KINDS,
                    "is_dirty": None,
                    "note": "",
                }
                for d in rows[N_CLEAN + n_forms * N_PER_CLASS :]
            ],
        },
    }


def main() -> int:
    g = build()
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    # 落盘 1：机器可读的主集（clean + probes）
    main_path = GOLDEN_DIR / "golden_set.jsonl"
    with main_path.open("w", encoding="utf-8") as f:
        for d in [*g["clean"], *g["probes"]]:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    # 落盘 2：元信息
    (GOLDEN_DIR / "golden_meta.json").write_text(
        json.dumps(g["meta"], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 落盘 3：人标骨架（答案留空）
    sk = GOLDEN_DIR / "review_skeleton.json"
    if not sk.exists():  # 不覆盖已有人标结果
        sk.write_text(
            json.dumps(g["review_skeleton"], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    n_clean = len(g["clean"])
    n_probe = len(g["probes"])
    print(f"黄金集已落盘：{GOLDEN_DIR}")
    print(f"  主集 {main_path.name}：clean {n_clean} + probes {n_probe} = {n_clean + n_probe} 条")
    print(f"  元信息golden_meta.json：{len(g['meta']['classes'])} 类")
    print(
        f"  人标骨架 review_skeleton.json：{len(g['review_skeleton']['rows'])} 条待标（答案留空）"
    )
    print()
    print("形态分布：")
    for k, v in collections.Counter(d["_gold_label"] for d in g["probes"]).most_common():
        print(f"  {k:<22}{v:>4}")
    print()
    print(f"⚠️ 图像类形态未构建（构造器无法在文本层造）：{sorted(IMAGE_ONLY_KINDS)}")
    print("   它们留在 review_skeleton.json 里等人工标注 —— 不伪造答案。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
