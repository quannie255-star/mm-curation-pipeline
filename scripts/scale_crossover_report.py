"""κ 块收官：规模拐点汇总报告——聚合多档双运行时基准，画 local/Ray 交叉曲线。

输入：data/reports/scale_crossover/n{100k,300k,1m}.json（ray_funnel_benchmark.py
按档落盘的 verdict）+ 可选的 γ3 基线（data/reports/ray_funnel_benchmark.json，
同为 10 万档，作「历史同条件参照」）。

产物：data/reports/scale_crossover.{json,md,png}——曲线回答「本机多大才该开 Ray」。

用法：python -X utf8 scripts/scale_crossover_report.py
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

ROOT = Path("data/reports")
POINTS = [  # (标签, verdict json 路径)
    ("100k", ROOT / "scale_crossover" / "n100k.json"),
    ("300k", ROOT / "scale_crossover" / "n300k.json"),
    ("1m", ROOT / "scale_crossover" / "n1m.json"),
]

logger = logging.getLogger(__name__)


def load_points() -> list[dict]:
    out = []
    for label, path in POINTS:
        if not path.exists():
            logger.warning("缺档跳过: %s", path)
            continue
        v = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            {
                "label": label,
                "n": v["n"],
                "local_s": v["seconds_local"],
                "ray_s": v["seconds_ray"],
                "ray_init_s": v["seconds_ray_init"],
                "ray_net_s": round(v["seconds_ray"] - v["seconds_ray_init"], 2),
                "kept_local": v["kept_n_local"],
                "kept_ray": v["kept_n_ray"],
                "equivalent": v["kept_ids_equal"] and v["stats_equal"] and v["score_mismatch"] == 0,
                "ops": v.get("ops", []),
            }
        )
    return out


def crossover_text(points: list[dict]) -> str:
    """按现有数据点外推交叉结论（诚实口径：不拟合曲线，只陈述实测区间）。"""
    winners = {p["label"]: ("local" if p["local_s"] <= p["ray_s"] else "ray") for p in points}
    if all(w == "local" for w in winners.values()):
        slowest = max(points, key=lambda p: p["ray_s"] / p["local_s"])
        ratio = slowest["ray_s"] / max(slowest["local_s"], 0.01)
        return (
            f"实测区间内（最大 {points[-1]['n']:,} 篇）local 全胜，Ray 未出现回本点；"
            f"最不利档 {slowest['label']} local/Ray = "
            f"{slowest['local_s']:.0f}s/{slowest['ray_s']:.0f}s（Ray 慢 {ratio:.1f}×）。"
            "单机结论：**该规模域内永远用 local**；Ray 的回本条件在多机水平扩展"
            "（横向加节点摊薄 map_batches 调度与序列化常数），不在单机加大 n。"
        )
    # 出现 ray 更快的档：找交叉区间
    cross = next(p for p in points if winners[p["label"]] == "ray")
    return f"交叉点在 {cross['label']} 档（n={cross['n']:,}）：Ray 开始反超。"


def main() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    points = load_points()
    if len(points) < 2:
        raise SystemExit("至少需要两档数据点")

    summary = {
        "points": points,
        "crossover": crossover_text(points),
        "equivalence_all": all(p["equivalent"] for p in points),
    }

    xs = [p["n"] for p in points]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(xs, [p["local_s"] for p in points], "o-", label="local 串行")
    ax.plot(xs, [p["ray_s"] for p in points], "s-", label="Ray（8 CPU，含 init）")
    ax.plot(xs, [p["ray_net_s"] for p in points], "s--", label="Ray（扣除 init）")
    for p in points:
        ax.annotate(
            f"{p['local_s']:.0f}s / {p['ray_s']:.0f}s",
            (p["n"], p["ray_s"]),
            textcoords="offset points",
            xytext=(0, 8),
            fontsize=8,
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("语料规模（篇，对数轴）")
    ax.set_ylabel("漏斗耗时（秒，对数轴）")
    ax.set_title("文本漏斗 local vs Ray：规模-耗时交叉曲线（7 级 CPU 算子）")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    out_png = ROOT / "scale_crossover.png"
    fig.savefig(out_png, dpi=150)

    (ROOT / "scale_crossover.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rows = "\n".join(
        f"| {p['label']} | {p['n']:,} | {p['local_s']:.1f}s | {p['ray_s']:.1f}s "
        f"（init {p['ray_init_s']:.1f}s） | {p['ray_s'] / max(p['local_s'], 0.01):.2f}× "
        f"| {p['kept_local']:,} / {p['kept_ray']:,} | {'✅' if p['equivalent'] else '❌'} |"
        for p in points
    )
    md = [
        "# 规模拐点：local vs Ray 交叉曲线（V3 κ）",
        "",
        "- 语料：维基 zh（text_corpus_1m.jsonl 独立扩量文件，β 基线 30 万档语料未动）",
        f"- 算子链 7 级 CPU（{' → '.join(points[0]['ops'])}）；perplexity（GPU）不进 Ray",
        "- 等价性：各档 kept 集/StageStat/逐 id 分数三口径全等",
        f"（汇总 = `{summary['equivalence_all']}`）",
        "",
        "| 档 | n | local | Ray | Ray/local | kept (local/ray) | 等价 |",
        "|---|---|---|---|---|---|---|",
        rows,
        "",
        f"**交叉结论**：{summary['crossover']}",
        "",
        "![曲线](scale_crossover.png)",
        "",
        "测量条件注记：单机 Windows / 8 逻辑核 / RTX 4060；梯子各档串行执行，",
        "与 GPU 训练任务分时（不同时运行），local 与 Ray 同条件下先后测量。",
    ]
    (ROOT / "scale_crossover.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    logger.info("报告: scale_crossover.{json,md,png}")


if __name__ == "__main__":
    main()
