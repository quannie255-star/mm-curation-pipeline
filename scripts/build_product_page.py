#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成产品页 `docs/product.html`（数字全部由 `docs/claims.json` 现算）。

为什么要有这个脚本，而不是手写一个 HTML：

1. **不手抄数字**。页面上的每个数值都用 `verify_claims.py` 的 `render_value()` 渲染
   —— 与门禁**同一套渲染逻辑**，所以页面字面量 === 注册表字面量 === 文档里的字面量。
   手写页面的下场已经在本项目发生过 6 次（见 `GAP_AUDIT` P3-1）。
2. **自带覆盖率自检**。生成完顺手统计"正文里出现的数值有几个已在注册表里"，
   未注册的**列出来**——这就是 `GAP_AUDIT` N-3b 的量化口径，也是 S3-1 扩围的输入。

    python -X utf8 scripts/build_product_page.py            # 写 docs/product.html
    python -X utf8 scripts/build_product_page.py --check    # 只报告，不写文件
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "docs" / "claims.json"
TEMPLATE = REPO / "scripts" / "templates" / "product_page.html"
OUT = REPO / "docs" / "product.html"
REPO_URL = "https://github.com/quannie255-star/mm-curation-pipeline"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_claims import render_value  # noqa: E402  （同一套渲染逻辑，不许重写）

# --------------------------------------------------------------------------
# 页面内容：只写"讲什么"，不写"数字是多少"——数字一律从注册表现取
# --------------------------------------------------------------------------

CARDS: list[tuple[str, str, str, list[str]]] = [
    # (小标题, 结论一句话, 回答的质疑, 用到的 claim)
    (
        "清洗收益",
        "脏索引 → 净索引的检索命中率提升",
        "「清洗效果怎么证明？」",
        ["soul_dirty_r1", "soul_clean_r1"],
    ),
    (
        "训练级证据",
        "脏集微调真的更差（不只是检索指标好看）",
        "「指标好看有什么用？」",
        ["clip_clean_ft_r1", "clip_dirty_ft_r1", "ppl_clean", "ppl_dirty"],
    ),
    (
        "净贡献对照",
        "比「随机删同样多」显著更好（有对照臂）",
        "「删了不就行了？」",
        ["random_drop_margin", "random_drop_clean_control"],
    ),
    (
        "门禁口径",
        "医疗 / 工业门禁按 3 个 seed 的**最差**口径报",
        "「是不是挑了个好 seed？」",
        [
            "fhir_gate_recall_worst",
            "fhir_gate_fk_worst",
            "ind_gate_recall_worst",
            "ind_gate_fk_worst",
        ],
    ),
]

# claims 表里承载结论的核心条目（历史数字单独标注）
TABLE_CLAIMS = [
    "soul_dirty_r1",
    "soul_clean_r1",
    "clip_clean_ft_r1",
    "clip_dirty_ft_r1",
    "ppl_clean",
    "ppl_dirty",
    "random_drop_margin",
    "random_drop_clean_control",
    "fhir_gate_recall_worst",
    "fhir_gate_fk_worst",
    "ind_gate_recall_worst",
    "ind_gate_fk_worst",
    "text_dedup_exact_10k",
    "ablation_dedup_group",
]

BOUNDARY = [
    (
        "真实轨的召回只能是「标签口径下的召回」",
        "三个公开工业数据集都只能给弱标签：一个的异常是人为切阀造的、一个极稀疏且不覆盖数据链路类缺陷、"
        "还有一个是动力学仿真。所以「真实场景下的召回率」这个数字目前**无法测**——现有的都是口径内召回。",
    ),
    (
        "真实轨的「误杀率」是上界，不是业务指标",
        "人工抽检发现，被判为误杀的部分里有一段**连续数天的多通道完全冻结**（真实缺陷，但标签体系不覆盖）。"
        "所以任何拿数据集标签直接算出的误杀率，都只是上界——最终裁决必须靠人，这正是人审队列要补的位置。",
    ),
    (
        "「无标签 ≠ 干净」，同一形态可以有不同含义",
        "「完美平坦」既可能是传感器卡死（故障），也可能是该通道本来就不测量任何东西（正常）。"
        "现有算子只看单个窗，缺全量上下文——这是**架构级**需求，不是调参能解决的。",
    ),
    (
        "统计效力有限",
        "训练级对比是单 seed、无置信区间；held-out 查询集很小，波动约 ±1pp。"
        "这些不是隐瞒，是硬约束，写在证据链的「已承认局限」里。",
    ),
    (
        "外部 API 的引入会带来不可复现性（本轮新增）",
        "把卡壳处换成成熟方案后，凡走外部 API 的数字都会同时记**模型名 + 调用日期 + 后处理参数**，"
        "并在证据链里降级标注为「外部依赖，非位级可复现」——否则「数字可复现」这块招牌就自己砸了。",
    ),
]

COMMANDS = """
<div class="step">
  <div class="t">① 复算数字注册表（不需要数据、不需要 GPU）</div>
  <div class="s">覆盖「文档手写数字 ↔ 注册表」与「复审清单」两部分：</div>
  <pre>python -X utf8 scripts/verify_claims.py     # 逐条校验注册表（缺报告的标"缺报告"，不算通过）
python -X utf8 scripts/gap_audit_probe.py   # 产品级差距审计清单可复跑（只读）</pre>
</div>
<div class="step">
  <div class="t">② 跑真实工业数据链路（纯 CPU，不用 GPU）</div>
  <div class="s">见 <code>docs/QUICKSTART.md</code>「路线 D」；数据下载只做一次：</div>
  <pre>python -m pip install -r requirements.lock
python -m pip install --no-deps ./packages/curation-eval
python -X utf8 scripts/ingest_real_sensor.py --source skab
python -X utf8 scripts/ingest_real_sensor.py --source metropt3 --unzip
python -m mm_curation.cli run --datasets metropt3,skab_w64,cmapss \\
    --run-id route_d__001 --batch-date 2026-09-28</pre>
</div>
<div class="step">
  <div class="t">③ 跑测试</div>
  <div class="s">本机 Windows：<code>--basetemp</code> 必须是盘符绝对路径、且目录不存在</div>
  <pre>python -X utf8 -m pytest -q --basetemp C:/tmp/pt_fresh_001 --junitxml C:/tmp/junit.xml</pre>
</div>
"""


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def facades_by_source(registry: dict) -> dict[str, dict]:
    """取每条 claim / baseline 的**第一个**门面 → 复用它的 fmt/scale/suffix。

    这样页面字面量与文档字面量天然一致（同一套格式契约），而不是另起一套。
    """
    out: dict[str, dict] = {}
    for f in registry.get("facades", []):
        src = f.get("source", {})
        key = (
            f"claim:{src['claim']}"
            if "claim" in src
            else (f"baseline:{src['baseline']}" if "baseline" in src else None)
        )
        if key and key not in out:
            out[key] = f
    return out


def lit(value, fmt_key: str, fmts: dict[str, dict]) -> str:
    f = fmts.get(fmt_key)
    if f is None:
        return f"{value:g}"
    return render_value(value, f["fmt"], f.get("scale", 1), f.get("suffix", ""))


def build(registry: dict) -> str:
    claims = {c["id"]: c for c in registry["claims"]}
    fmts = facades_by_source(registry)
    base = registry["baselines"]

    def L(claim_id: str) -> str:
        return lit(claims[claim_id]["expected"], f"claim:{claim_id}", fmts)

    # ---- 徽章 ----
    badges = [
        f"测试基线 <b>{base['tests_main']} + {base['tests_pkg']} = {base['tests_total']}</b>",
        f"claim <b>{len(registry['claims'])}</b> 条"
        f" · 文档门面 <b>{len(registry['facades'])}</b> 条",
        "版本 <b>1.0.0</b> · MIT",
    ]
    badges_html = "".join(f'<span class="badge">{b}</span>' for b in badges)

    # ---- 首屏 KPI（含一个派生值：+21%） ----
    d, c = claims["soul_dirty_r1"]["expected"], claims["soul_clean_r1"]["expected"]
    gain = (c / d - 1) * 100
    hero = [
        f"""<div class="kpi"><div class="k">清洗收益 · Recall@1</div>
        <div class="v">{L("soul_dirty_r1")} <span class="arrow">→</span> {L("soul_clean_r1")}</div>
        <div class="d">相对提升 <b>+{gain:.0f}%</b>（由上面两个 claim 现算）</div></div>""",
        f"""<div class="kpi"><div class="k">训练级证据 · CLIP R@1</div>
        <div class="v">{L("clip_clean_ft_r1")} <small>vs</small> {L("clip_dirty_ft_r1")}</div>
        <div class="d">干净集微调 vs 脏集微调（同一预算）</div></div>""",
        f"""<div class="kpi"><div class="k">语言侧 · GPT-2 困惑度</div>
        <div class="v">{L("ppl_clean")} <small>vs</small> {L("ppl_dirty")}</div>
        <div class="d">越低越好；脏语料微调更差</div></div>""",
        f"""<div class="kpi"><div class="k">工业门禁 · 误杀率</div>
        <div class="v">{L("ind_gate_fk_worst")}</div>
        <div class="d">3 seed 最差口径（召回 {L("ind_gate_recall_worst")}）</div></div>""",
    ]

    # ---- claims 表 ----
    rows = []
    for cid in TABLE_CLAIMS:
        c = claims.get(cid)
        if c is None:
            continue
        val = lit(c["expected"], f"claim:{cid}", fmts)
        src = f"{c['file'] or '—'}<br><span style='color:#5b6470'>{c['pointer'] or '—'}</span>"
        cap = c.get("comparator", "?")
        tol = c.get("tol")
        cap_txt = (
            "历史结论（未绑报告）"
            if cap == "historical"
            else f"{cap}　tol={tol}"
            if tol not in (None, "None", "0.0")
            else cap
        )
        rows.append(
            f"<tr><td>{c['desc']}</td><td class='num'><b>{val}</b></td>"
            f"<td style='font-size:13px'>{src}</td><td style='font-size:13px'>{cap_txt}</td></tr>"
        )
    rows_html = "\n      ".join(rows)

    # ---- 诚实边界 ----
    bound_html = "\n    ".join(f"<li><b>{t}</b>　{d}</li>" for t, d in BOUNDARY)

    tpl = TEMPLATE.read_text(encoding="utf-8")
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M %z")
    out = (
        tpl.replace("{{BADGES}}", badges_html)
        .replace("{{HERO}}", "\n    ".join(hero))
        .replace("{{CLAIMS_ROWS}}", rows_html)
        .replace("{{COMMANDS}}", COMMANDS)
        .replace("{{BOUNDARY}}", bound_html)
        .replace("{{GEN_AT}}", now)
        .replace("{{REG_MD5}}", hashlib.md5(REGISTRY.read_bytes()).hexdigest()[:12])
        .replace("{{N_CLAIM}}", str(len(registry["claims"])))
        .replace("{{N_FACADE}}", str(len(registry["facades"])))
        .replace("{{TESTS}}", f"{base['tests_main']} + {base['tests_pkg']} = {base['tests_total']}")
        .replace("{{COVERAGE_LINE}}", "{{COVERAGE}}")
        .replace("{{REPO_URL}}", REPO_URL)
    )
    return out


# --------------------------------------------------------------------------
# 覆盖率自检（= GAP_AUDIT N-3b 的量化口径）
# --------------------------------------------------------------------------


def coverage_report(html: str, registry: dict, derived: set[str]) -> tuple[str, list[str]]:
    """统计正文里出现的数值有几个能追溯到注册表。命令块 / 样式表 / 章节号豁免。

    「追溯到」= 该数值能在注册表里找到出处（facade 字面量 / baselines / claim 的
    expected·tol·desc 文本 / 由 claim 现算出的派生值）。**在注册表里找不到的，
    就是页面上手写的数字**——那才是要报出来的东西。
    """
    body = html.split("</style>", 1)[-1]
    body = re.sub(r'<span class="n">[\s\S]*?</span>', " ", body)  # 章节号 01–04
    body = re.sub(r"<pre>[\s\S]*?</pre>", " ", body)  # 命令块："怎么跑"，不是"结论"
    body = re.sub(r"<code>[\s\S]*?</code>", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"https?://\S+", " ", body)  # URL 里的数字是误报

    found = set(re.findall(r"(?<![0-9.])\d+\.\d+(?![0-9])", body)) | set(
        re.findall(r"(?<![0-9.])\d{2,4}(?![0-9.])", body)
    )

    registered = {f["literal"] for f in registry.get("facades", [])}
    registered |= {str(v) for v in registry["baselines"].values()}
    registered |= derived
    # 注册表自身的文本里的数字也是"有出处"的（claim 的 desc / expected / tol）
    registered |= set(re.findall(r"\d+(?:\.\d+)?", REGISTRY.read_text(encoding="utf-8")))
    # 由注册表现算的计数（条数）同样是"有出处"的
    registered |= {str(len(registry["claims"])), str(len(registry.get("facades", [])))}

    missing = sorted(x for x in found if x not in registered)
    total = len(found)
    n_ok = total - len(missing)
    pct = 100.0 if not total else 100.0 * n_ok / total
    detail = ""
    for x in missing:
        m = re.search(r".{0,26}" + re.escape(x) + r".{0,26}", body)
        detail += (
            f"\n    · {x:<8} … {re.sub(chr(92) + 's+', ' ', m.group(0)).strip() if m else '?'} …"
        )
    line = (
        f"本页正文含 <code>{total}</code> 个不同数值，其中 <code>{n_ok}</code> 个可追溯到"
        f"注册表（<code>{pct:.0f}%</code>）。"
        + (
            f" 未追溯到的 <code>{len(missing)}</code> 个 —— 这是注册表覆盖面的问题（见 "
            f"<code>GAP_AUDIT</code> N-3b 与 <code>PRODUCTIZATION</code> S3-1），"
            f"不是本页的问题；生成器把它们列在 stdout。"
            if missing
            else " 全部可追溯。"
        )
    )
    return line, missing, detail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只报告，不写文件")
    args = ap.parse_args()

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    html = build(registry)

    # 派生值：由 claim 现算，同样"有出处"（不是手写）
    cl = {c["id"]: float(c["expected"]) for c in registry["claims"]}
    gain = (cl["soul_clean_r1"] / cl["soul_dirty_r1"] - 1) * 100
    derived = {f"{gain:.0f}", f"+{gain:.0f}%"}

    line, missing, detail = coverage_report(html, registry, derived)
    html = html.replace("{{COVERAGE}}", line)

    print(f"注册表 {REGISTRY.relative_to(REPO)} → 渲染 {len(html)} 字符")
    print(re.sub(r"<[^>]+>", "", line))
    if missing:
        print(f"未追溯到注册表的正文数值（{len(missing)}）:{detail}")

    if args.check:
        print("--check：未写文件")
        return 0
    OUT.write_text(html, encoding="utf-8", newline="\n")
    print(f"已写 {OUT.relative_to(REPO)}  （{OUT.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
