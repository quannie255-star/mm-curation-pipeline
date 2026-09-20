"""真实工业传感器数据 → Sample 协议 前置脚本（V5 β 真实数据轨第一步）。

把公开真实数据集切成一窗一样本（`SensorSample` 约定），并生成 labels：
- SKAB      逐点 anomaly 标签 → 窗级标签（任一异常点 → 窗脏）；changepoint → 工况窗
- MetroPT-3 官方失败表 → 故障窗口标签；检修记录 → maintenance_event 样本
- C-MAPSS   运行到失效轨迹 → 末段（RUL<阈值）标 degraded

设计约束（与 DOMAIN_PACKS 一致）：**窗口化 + 标签聚合是「漏斗外前置阶段」**，
算子本身不碰原始 CSV、不猜业务。本脚本只做转换与标签，不做任何清洗或改写。

用法：
    python -X utf8 scripts/ingest_real_sensor.py --source skab
    python -X utf8 scripts/ingest_real_sensor.py --source metropt3 --unzip
    python -X utf8 scripts/ingest_real_sensor.py --source cmapss --window 30 --stride 10

产物：data/raw/real/<source>/windows.jsonl（不进库，data/ 已 gitignore）
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "curation-eval" / "src"))

from curation_eval import SensorSample  # noqa: E402

REAL = REPO / "data" / "raw" / "real"

# --- SKAB：8 传感器 + 单位 ---
# 注意：列名与分隔符以**实际文件为准**（官方 README 写 `RateRMS`、逗号分隔，
# 实测文件是 `Volume Flow RateRMS` + **分号分隔**——照 README 写会整列 KeyError）。
SKAB_CHANNELS: dict[str, str] = {
    "Accelerometer1RMS": "g",
    "Accelerometer2RMS": "g",
    "Current": "A",
    "Pressure": "bar",
    "Temperature": "degC",
    "Thermocouple": "degC",
    "Voltage": "V",
    "Volume Flow RateRMS": "L/min",
}
SKAB_DELIMITER = ";"

# --- MetroPT-3：7 个模拟量 + 单位（来源：UCI 数据集 791 变量表） ---
METROPT_CHANNELS: dict[str, str] = {
    "TP2": "bar",
    "TP3": "bar",
    "H1": "bar",
    "DV_pressure": "bar",
    "Reservoirs": "bar",
    "Oil_temperature": "degC",
    "Motor_current": "A",
}

# MetroPT-3 官方失败报告表（公司提供，见 UCI 数据集 791 页面）。
# 数据本身 unlabeled——故障窗口与检修时刻**都从这张表推导**，属窗口级弱标签。
# 注意：末列 Maintenance 是**计划检修时刻**（合法静默的来源，非故障）。
METROPT_FAILURES: list[tuple[str, str, str]] = [
    ("2020-04-18T00:00", "2020-04-18T23:59", "air_leak"),
    ("2020-05-29T23:30", "2020-05-30T06:00", "air_leak"),
    ("2020-06-05T10:00", "2020-06-07T14:30", "air_leak"),
    ("2020-07-15T14:30", "2020-07-15T19:00", "air_leak"),
]
METROPT_MAINTENANCE: list[str] = [
    "2020-04-30T12:00",
    "2020-06-08T16:00",
    "2020-07-16T00:00",
]
# 检修持续时长未知（表里只给时刻）→ 假设 6 小时窗；属显式假设，报告需声明。
METROPT_MAINTENANCE_HOURS = 6


def _sampling_hz(times: list[datetime], fallback: float = 1.0) -> float:
    """采样率**从时间戳实测**，不照数据集文档填。

    踩过的坑：UCI 数据集 791 页面自身对 MetroPT-3 有两处矛盾口径（0.1Hz / 1Hz），
    实测时间戳间隔是 **12 秒**（≈0.083 Hz）——按 1Hz 写会把「256 读数窗」的时长
    算错 12 倍（4.3 分钟 vs 51.2 分钟），窗口语义整个偏掉。
    """
    if len(times) < 2:
        return fallback
    deltas = sorted(
        (times[i + 1] - times[i]).total_seconds() for i in range(min(len(times) - 1, 5000))
    )
    dt = deltas[len(deltas) // 2]
    if dt <= 0:
        return fallback
    return round(1.0 / dt, 6)


def _emit(payload: dict, label: str | None) -> dict:
    """经 SensorSample 适配器校验 + 规范化，返回可直接落盘的 dict。"""
    sample = SensorSample.from_payload(payload)
    row = sample.to_dict()
    if label:
        row["labels"] = {"dirty": label}
    return row


def _windows_from_series(
    *,
    device_id: str,
    device_type: str,
    channel: str,
    unit: str,
    values: list[float],
    times: list[datetime],
    sampling_hz: float,
    modes: list[str] | None = None,
    labels: list[str | None] | None = None,
    window: int,
    stride: int,
) -> list[dict]:
    """把一条数值序列切成不重叠/半重叠窗，逐窗产 Sample payload。"""
    rows: list[dict] = []
    for wi, start in enumerate(range(0, max(0, len(values) - window + 1), stride)):
        chunk = values[start : start + window]
        if len(chunk) < window:
            break
        # 注意：modes/labels 是**窗序号**索引的紧凑列表，不是序列位置索引——
        # 用 start 直接索引会在 start>=len(modes) 处 IndexError（已踩过一次）。
        mode = modes[wi] if modes else "steady"
        payload = {
            "record_type": "reading_window",
            "device_id": device_id,
            "device_type": device_type,
            "channel": channel,
            "unit": unit,
            "sampling_hz": sampling_hz,
            "operating_mode": mode,
            "window_start": times[start].isoformat(),
            "window_end": times[min(start + window - 1, len(times) - 1)].isoformat(),
            "readings": [round(v, 6) for v in chunk],
        }
        rows.append(_emit(payload, labels[wi] if labels else None))
    return rows


def _num(raw: str | None, default: float = 0.0) -> float:
    """真实 CSV 单元格转数：容忍空白/空串/千分位外的脏写法。"""
    if raw is None:
        return default
    text = raw.strip().strip('"')
    if not text:
        return default
    return float(text)


def _read_csv(path: Path, delimiter: str = ",") -> tuple[list[dict], list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        return list(reader), list(reader.fieldnames or [])


def ingest_skab(window: int, stride: int) -> list[dict]:
    files = sorted(glob.glob(str(REAL / "skab" / "SKAB" / "**" / "*.csv"), recursive=True))
    if not files:
        raise SystemExit(
            "未找到 SKAB CSV。先执行：\n"
            "  git clone --depth 1 https://github.com/waico/SKAB data/raw/real/skab/SKAB"
        )
    rows: list[dict] = []
    for path in map(Path, files):
        records, fields = _read_csv(path, delimiter=SKAB_DELIMITER)
        if not records or "anomaly" not in fields:
            continue
        if not records[0].get("datetime"):
            continue
        # 文件名即实验名（SKAB 每个文件 = 一次独立实验，含一段异常）
        rel = Path(path).relative_to(REAL / "skab" / "SKAB")
        device_id = "skab_" + "_".join(rel.with_suffix("").parts)
        times = [datetime.fromisoformat(r["datetime"].strip()) for r in records]
        anomaly = [int(_num(r.get("anomaly"))) for r in records]
        changepoint = [int(_num(r.get("changepoint"))) for r in records]
        for channel, unit in SKAB_CHANNELS.items():
            if channel not in fields:
                continue
            values = [_num(r.get(channel)) for r in records]
            # 工况窗：窗内出现 changepoint → changeover（工况切换），否则 steady。
            # 这是**从标签推导**的工况，不是算子猜测业务。
            labels: list[str | None] = []
            modes: list[str] = []
            for start in range(0, max(0, len(values) - window + 1), stride):
                win_anom = any(anomaly[start : start + window])
                win_cp = any(changepoint[start : start + window])
                labels.append("sensor_anomaly" if win_anom else None)
                modes.append("changeover" if win_cp else "steady")
            rows += _windows_from_series(
                device_id=device_id,
                device_type="skab_testbed",
                channel=channel,
                unit=unit,
                values=values,
                times=times,
                sampling_hz=_sampling_hz(times),
                modes=modes,
                labels=labels,
                window=window,
                stride=stride,
            )
    return rows


def ingest_metropt3(
    window: int, stride: int, unzip: bool, csv_path: str | None = None
) -> list[dict]:
    base = REAL / "metropt3"
    if unzip:
        for zp in base.glob("*.zip"):
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(base)
    if csv_path:
        files = [Path(csv_path)]
    else:
        # 优先选官方全量口径的文件（1,516,948 行）；同目录下的分组/子集文件
        # （如 Group_14_Clean_Data.csv，59k 行）**不能混进来**，否则重复计数。
        prefer = ["labelled_df.csv", "MetroPT3(AirCompressor).csv", "MetroPT3.csv"]
        files = [base / name for name in prefer if (base / name).exists()]
        if not files:
            candidates = sorted(
                (p for p in base.glob("**/*.csv") if "group" not in p.stem.lower()),
                key=lambda p: p.stat().st_size,
                reverse=True,
            )
            files = candidates[:1]
    if not files:
        raise SystemExit(
            "未找到 MetroPT-3 CSV。两个可用来源（都是社区镜像，原始出处为 UCI 数据集 791）：\n"
            "  ① 全量 1,516,948 行（推荐）：Masa-Tantawy/MetroPT-3-Predictive-Maintenance\n"
            "     的 labelled_df.csv.zip（`--unzip` 会解开）\n"
            "  ② harveyphm/MetroPT-3-Anomaly-Detection 的 data/Group_14_Clean_Data.csv\n"
            "     （**只有 59k 行的分组子集，别当全量用**）\n"
            "  GitHub raw 不通时用 https://ghfast.top/<raw 完整 URL> 代理。"
        )
    failures = [
        (datetime.fromisoformat(a), datetime.fromisoformat(b), kind)
        for a, b, kind in METROPT_FAILURES
    ]
    rows: list[dict] = []
    for path in map(Path, files):
        records, fields = _read_csv(path)
        if not records or "timestamp" not in fields:
            continue
        device_id = "metropt3_" + path.stem.lower()
        times = [datetime.fromisoformat(r["timestamp"].strip()) for r in records]
        fault_labels: list[str | None] = []
        for start in range(0, max(0, len(times) - window + 1), stride):
            t0 = times[start]
            hit = next((k for a, b, k in failures if a <= t0 <= b), None)
            fault_labels.append(f"fault_window_{hit}" if hit else None)
        for channel, unit in METROPT_CHANNELS.items():
            if channel not in fields:
                continue
            values = [_num(r.get(channel)) for r in records]
            rows += _windows_from_series(
                device_id=device_id,
                device_type="metropt3_apu",
                channel=channel,
                unit=unit,
                values=values,
                times=times,
                # 实测 12 秒/行（UCI 页面口径自相矛盾，以时间戳为准）
                sampling_hz=_sampling_hz(times),
                modes=None,  # 真实数据无工况标签 → 单一工况；见报告「已知限制」
                labels=fault_labels,
                window=window,
                stride=stride,
            )
    # 计划检修事件：进同模态样本流（fault_vs_maintenance 的计划索引来源）
    for stamp in METROPT_MAINTENANCE:
        start = datetime.fromisoformat(stamp)
        rows.append(
            _emit(
                {
                    "record_type": "maintenance_event",
                    "device_id": "metropt3_" + Path(files[0]).stem.lower(),
                    "device_type": "metropt3_apu",
                    "window_start": start.isoformat(),
                    "window_end": (start + timedelta(hours=METROPT_MAINTENANCE_HOURS)).isoformat(),
                },
                None,
            )
        )
    return rows


def ingest_cmapss(window: int, stride: int) -> list[dict]:
    paths = sorted(glob.glob(str(REAL / "cmapss" / "train_FD*.txt")))
    if not paths:
        raise SystemExit(
            "未找到 C-MAPSS。先执行：\n"
            "  curl -LO https://raw.githubusercontent.com/Ekkohng/CMAPSSData"
            "/master/train_FD001.txt （放到 data/raw/real/cmapss/）"
        )
    rows: list[dict] = []
    for path in map(Path, paths):
        set_name = path.stem.replace("train_", "").lower()
        per_unit: dict[int, list[list[float]]] = {}
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    per_unit.setdefault(int(line.split()[0]), []).append(
                        [float(x) for x in line.split()[2:]]
                    )
        for unit, cycles in per_unit.items():
            n = len(cycles)
            times = [datetime(2000, 1, 1) + timedelta(hours=i) for i in range(n)]
            # 21 个传感器（列 3..23，索引 0 起为设置 1..3 之后的测量）
            sensor_idx = list(range(3, 24))
            for si in sensor_idx:
                if si >= len(cycles[0]):
                    continue
                values = [c[si] for c in cycles]
                labels: list[str | None] = []
                for start in range(0, max(0, n - window + 1), stride):
                    rul = n - (start + window)  # 窗末的剩余寿命（周期数）
                    labels.append("degraded" if rul < window else None)
                rows += _windows_from_series(
                    device_id=f"cmapss_{set_name}_unit{unit:03d}",
                    device_type=f"cmapss_{set_name}",
                    channel=f"s{si - 3 + 1}",
                    unit="au",  # NASA 未给各传感器物理单位 → 任意单位
                    values=values,
                    times=times,
                    sampling_hz=1.0 / 3600.0,  # 一周期=一小时，仅为占位口径
                    modes=None,
                    labels=labels,
                    window=window,
                    stride=stride,
                )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=["skab", "metropt3", "cmapss"])
    parser.add_argument("--window", type=int, default=256, help="窗内读数（SKAB/MetroPT 默认 256）")
    parser.add_argument("--stride", type=int, default=None, help="窗步长，默认=window（不重叠）")
    parser.add_argument("--out", default=None)
    parser.add_argument("--unzip", action="store_true", help="MetroPT-3：先解压 zip")
    parser.add_argument("--csv", default=None, help="MetroPT-3：显式指定 CSV 路径")
    args = parser.parse_args()

    window = args.window
    if args.source == "cmapss" and args.window == 256:
        window = 30  # C-MAPSS 一周期一行，30 周期/窗更合理
    stride = args.stride or window

    if args.source == "skab":
        rows = ingest_skab(window, stride)
    elif args.source == "metropt3":
        rows = ingest_metropt3(window, stride, args.unzip, args.csv)
    else:
        rows = ingest_cmapss(window, stride)

    out = Path(args.out) if args.out else REAL / args.source / "windows.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    n_dirty = sum(1 for r in rows if r.get("labels"))
    n_clean = len(rows) - n_dirty
    kinds: dict[str, int] = {}
    for r in rows:
        kind = (r.get("labels") or {}).get("dirty")
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
    print(f"[{args.source}] 窗口 {window} 步长 {stride}：{len(rows)} 条样本")
    print(f"  干净 {n_clean} / 脏 {n_dirty}  脏类型: {kinds or '（无标签）'}")
    print(f"  产出: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
