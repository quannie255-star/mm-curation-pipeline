"""判官成本核算（V3 η+ 业务层）：本机小模型 vs 云 API vs 人工抽检。

业务主张：判官选型是「质量 × 成本」二维决策——质量由能力矩阵（冻结
benchmark 测定）给出，成本由本模块给出。两侧合成选型矩阵。

成本口径（全部可改，assumptions 即协议）：
- 本机推理：实测吞吐（runs/experiments.jsonl 的 eval 秒数/条数）×
  （GPU+主机功耗 × 电价）；设备折旧不计入（沉没成本口径，注明）
- 云 API：输入/输出 token 牌价 × 每条 token 用量（实测 prompt 均长）
- 人工抽检：时薪 × 单条耗时
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostAssumptions:
    """成本假设（币种：CNY）。牌价为 2026-09 国内主流 API 档位示例。"""

    electricity_per_kwh: float = 0.6
    device_power_w: float = 120.0  # RTX 4060 Laptop 推理功耗（GPU+摊派主机）
    annotator_hourly: float = 30.0
    annotator_seconds_per_item: float = 30.0
    # 云 API 牌价（¥ / 百万 tokens）
    api_input_per_m: float = 2.0
    api_output_per_m: float = 4.0
    # 每条判定的 token 用量（prompt+协议+候选；实测均值可覆盖）
    tokens_in_per_item: float = 1200.0
    tokens_out_per_item: float = 60.0


def local_cost_per_item(seconds_per_item: float, a: CostAssumptions) -> float:
    """本机单条成本 = 墙钟时间 × 设备功耗电费。"""
    kwh_per_item = (a.device_power_w / 1000.0) * (seconds_per_item / 3600.0)
    return kwh_per_item * a.electricity_per_kwh


def api_cost_per_item(a: CostAssumptions) -> float:
    return (a.tokens_in_per_item / 1e6 * a.api_input_per_m) + (
        a.tokens_out_per_item / 1e6 * a.api_output_per_m
    )


def human_cost_per_item(a: CostAssumptions) -> float:
    return a.annotator_hourly / 3600.0 * a.annotator_seconds_per_item


@dataclass
class JudgeCostRow:
    judge: str
    quality: str  # 能力矩阵结论（人读）
    seconds_per_item: float
    cost_per_item: float
    cost_per_10k: float
    items_per_hour: float
    note: str = ""


def cost_table(
    local_seconds_per_item: dict[str, float],
    a: CostAssumptions | None = None,
    quality: dict[str, str] | None = None,
) -> list[JudgeCostRow]:
    """合成选型表。local_seconds_per_item: {"0.5B 本机": x, ...}。"""
    a = a or CostAssumptions()
    quality = quality or {}
    rows = []
    for name, spi in local_seconds_per_item.items():
        c = local_cost_per_item(spi, a)
        rows.append(
            JudgeCostRow(
                judge=f"{name}（本机）",
                quality=quality.get(name, "见能力矩阵"),
                seconds_per_item=round(spi, 2),
                cost_per_item=c,
                cost_per_10k=c * 10_000,
                items_per_hour=3600.0 / spi,
                note="电费口径，设备折旧不计",
            )
        )
    api_c = api_cost_per_item(a)
    rows.append(
        JudgeCostRow(
            judge="云 API（通用大模型）",
            quality=quality.get("API", "通用域外能力，未经你的 benchmark 验收"),
            seconds_per_item=2.0,
            cost_per_item=api_c,
            cost_per_10k=api_c * 10_000,
            items_per_hour=1800.0,
            note=f"按 {a.tokens_in_per_item:.0f} 入/{a.tokens_out_per_item:.0f} 出 tokens/条",
        )
    )
    h_c = human_cost_per_item(a)
    rows.append(
        JudgeCostRow(
            judge="人工抽检",
            quality=quality.get("人工", "金标准，但不可全量"),
            seconds_per_item=a.annotator_seconds_per_item,
            cost_per_item=h_c,
            cost_per_10k=h_c * 10_000,
            items_per_hour=3600.0 / a.annotator_seconds_per_item,
        )
    )
    return rows


def render_markdown(rows: list[JudgeCostRow], assumptions: CostAssumptions) -> str:
    lines = [
        "# 判官成本核算（质量 × 成本选型表）",
        "",
        f"- 口径：本机=电费（{assumptions.device_power_w:.0f}W × "
        f"¥{assumptions.electricity_per_kwh}/kWh，设备折旧不计）；"
        f"API=¥{assumptions.api_input_per_m}/¥{assumptions.api_output_per_m} 每百万"
        f" tokens（入/出，示例牌价可改）；人工=¥{assumptions.annotator_hourly}/h × "
        f"{assumptions.annotator_seconds_per_item:.0f}s/条",
        "",
        "| 判官 | ¥/条 | ¥/万条 | 吞吐（条/时） | 质量口径 | 备注 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        ten_k = f"{r.cost_per_10k:,.2f}" if r.cost_per_10k < 1 else f"{r.cost_per_10k:,.0f}"
        lines.append(
            f"| {r.judge} | {r.cost_per_item:.5f} | {ten_k} "
            f"| {r.items_per_hour:,.0f} | {r.quality} | {r.note} |"
        )
    lines += [
        "",
        "> 质量列的数字结论见判官能力矩阵（runs/experiments.jsonl + 各冻结 benchmark）；",
        "> 本表只回答「每一条数据，不同判官各要多少钱」——两表合读即选型。",
    ]
    return "\n".join(lines) + "\n"
