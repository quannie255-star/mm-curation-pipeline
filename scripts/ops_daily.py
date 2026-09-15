r"""OPS 日常运维单入口壳（R0/R3）：步骤顺序执行、非零即停、必出日报。

设计：薄壳只编排不实现——外部项目走 subprocess 入口命令，审计/日报在本壳内
用纯函数完成（可离线单测）。任一步失败 → 跳过其余步骤，但日报必渲染
（顶部「今日异常」），exit 1——管道吞错 exit 0 不可信（η-b 在案教训）。

步骤（对应 OPS_PRD R3 六步，①③合并为 findata_daily）：
    findata_daily  复用 findata scripts/daily_pipeline.py（采集→巡检→推送→归档）
    fetch_text     akshare 个股新闻 → Sample JSONL（增量，fetch_finance_news.py）
    funnel         金融文本漏斗全量重跑（configs/text_funnel_finance.yaml）
    audit          dropped.jsonl 按 dropped_by 聚合 + 每类抽 3 条
    report         日报 + 磁盘水位 + 台账追加（data/ops/stats.jsonl）

用法（Windows 加 -X utf8）：
    python -X utf8 scripts/ops_daily.py                 # 标准日更
    python -X utf8 scripts/ops_daily.py --skip-findata  # 未配 findata / 调试
    python -X utf8 scripts/ops_daily.py --dry-run       # 只打印步骤与产物检查

退出码：0 全过；1 任一步失败。调度安装见 scripts/ops_install_schedule.py。
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FINDATA_PATH = Path(
    __import__("os").environ.get("FINDATA_PATH", str(Path.home() / "Desktop" / "FinData-Agent"))
)
NEWS_CORPUS = REPO / "data/raw/finance_news/news_corpus.jsonl"
FUNNEL_DIR = REPO / "data/processed/finance_news_funnel"
FUNNEL_CONFIG = REPO / "configs/text_funnel_finance.yaml"
STATS_PATH = REPO / "data/ops/stats.jsonl"
REPORT_DIR = REPO / "data/reports/daily"
LOG_DIR = REPO / "data/ops/logs"

DISK_ALERT_PCT = 80.0
BAND_MIN_HISTORY = 3
BAND_LOOKBACK = 7
BAND_DROP_RATIO = 0.5
SAMPLE_PER_CLASS = 3
SAMPLE_TEXT_CHARS = 80


@dataclass
class Step:
    name: str
    cmd: list[str]
    cwd: Path
    check_path: Path | None = None  # 步骤成功后必须存在的产物
    pre_fail_reason: str | None = None  # 非空则直接判失败（如 findata venv 缺失）


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str
    log_tail: str = ""
    stats: dict = field(default_factory=dict)


def findata_python_path() -> Path | None:
    rel = ".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python"
    py = FINDATA_PATH / rel
    return py if py.exists() else None


def build_steps(skip_findata: bool, findata_py: Path | None) -> list[Step]:
    steps: list[Step] = []
    if not skip_findata:
        if findata_py is None:
            steps.append(
                Step(
                    name="findata_daily",
                    cmd=[],
                    cwd=FINDATA_PATH,
                    pre_fail_reason=(
                        f"找不到 findata venv 解释器（{FINDATA_PATH}）。"
                        "请在 findata 仓库创建环境或设 FINDATA_PATH；当日可 --skip-findata。"
                    ),
                )
            )
        else:
            steps.append(
                Step(
                    name="findata_daily",
                    cmd=[str(findata_py), "scripts/daily_pipeline.py"],
                    cwd=FINDATA_PATH,
                )
            )
    steps.extend(
        [
            Step(
                name="fetch_text",
                cmd=[sys.executable, "-X", "utf8", str(REPO / "scripts/fetch_finance_news.py")],
                cwd=REPO,
                check_path=NEWS_CORPUS,
            ),
            Step(
                name="funnel",
                cmd=[
                    sys.executable,
                    "-X",
                    "utf8",
                    str(REPO / "scripts/run_pipeline.py"),
                    "--config",
                    str(FUNNEL_CONFIG),
                ],
                cwd=REPO,
                check_path=FUNNEL_DIR / "cleaned.jsonl",
            ),
        ]
    )
    return steps


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").split("\n") if line.strip())


def run_step(step: Step, log_dir: Path, date_tag: str) -> StepResult:
    if step.pre_fail_reason:
        return StepResult(step.name, ok=False, detail=step.pre_fail_reason)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{date_tag}_{step.name}.log"
    proc = subprocess.run(  # noqa: S603
        step.cmd,
        cwd=str(step.cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log_path.write_text(proc.stdout or "", encoding="utf-8")
    tail = "\n".join((proc.stdout or "").split("\n")[-6:])
    if proc.returncode != 0:
        return StepResult(step.name, ok=False, detail=f"exit={proc.returncode}", log_tail=tail)
    if step.check_path is not None and not step.check_path.exists():
        return StepResult(
            step.name, ok=False, detail=f"产物缺失 {step.check_path}", log_tail=tail
        )
    return StepResult(step.name, ok=True, detail="ok", log_tail=tail)


def evaluate_band(history: list[int], today: int) -> str | None:
    """行数预期带告警（R1）：近 7 天中位数的 50%。历史不足 BAND_MIN_HISTORY 天跳过。"""
    recent = [v for v in history[-BAND_LOOKBACK:] if v > 0]
    if len(recent) < BAND_MIN_HISTORY:
        return None
    median = statistics.median(recent)
    if median <= 0:
        return None
    if today < BAND_DROP_RATIO * median:
        return (
            f"text_new={today} 低于近{len(recent)}天中位数 {median:g} 的 "
            f"{BAND_DROP_RATIO:.0%}，疑似断采/接口异常"
        )
    return None


def aggregate_drops(lines: list[str]) -> dict[str, dict]:
    """dropped_by 聚合：{算子: {count, samples[]}}；坏行静默跳过（审计不容错崩）。"""
    result: dict[str, dict] = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = str(row.get("dropped_by", "unknown"))
        text = str(row.get("text", ""))[:SAMPLE_TEXT_CHARS]
        slot = result.setdefault(op, {"count": 0, "samples": []})
        slot["count"] += 1
        if len(slot["samples"]) < SAMPLE_PER_CLASS:
            slot["samples"].append(text)
    return result


def render_report(
    date_str: str,
    abnormal: list[str],
    findata_detail: str | None,
    text_total: int,
    text_new: int,
    band_alert: str | None,
    funnel_in: int,
    funnel_kept: int,
    audit: dict[str, dict],
    disk_used_pct: float,
) -> str:
    lines = [f"# 运维日报 {date_str}", "", "## 今日异常"]
    if abnormal or band_alert or disk_used_pct > DISK_ALERT_PCT:
        for item in abnormal:
            lines.append(f"- 🔴 [失败] {item}")
        if band_alert:
            lines.append(f"- 🔴 [告警] {band_alert}")
        if disk_used_pct > DISK_ALERT_PCT:
            lines.append(f"- 🔴 [磁盘] 已用 {disk_used_pct:.1f}% 超过 {DISK_ALERT_PCT:.0f}% 警戒线")
    else:
        lines.append("- 无")
    lines += ["", "## 采集"]
    if findata_detail is not None:
        lines.append(f"- 结构化（findata daily_pipeline）：{findata_detail}")
    lines.append(f"- 文本新闻：库内 {text_total} 条（今日新增 {text_new}）")
    lines += ["", "## 漏斗"]
    if funnel_kept > 0 or funnel_in > 0:
        rate = funnel_kept / funnel_in * 100 if funnel_in else 0.0
        lines.append(f"- 输入 {funnel_in} → 保留 {funnel_kept}（保留率 {rate:.1f}%）")
    else:
        lines.append("- 今日无漏斗产物")
    lines += ["", "## 丢弃审计"]
    if audit:
        for op, slot in sorted(audit.items(), key=lambda kv: -kv[1]["count"]):
            lines.append(f"- **{op}**：{slot['count']} 条")
            for sample in slot["samples"]:
                lines.append(f"  - `{sample}`")
    else:
        lines.append("- 无丢弃产物（漏斗未运行或零丢弃）")
    lines += ["", "## 磁盘", f"- 已用 {disk_used_pct:.1f}%（警戒线 {DISK_ALERT_PCT:.0f}%）", ""]
    return "\n".join(lines)


def append_stats(stats_path: Path, record: dict) -> None:
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with stats_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_history(stats_path: Path, today: str) -> list[int]:
    if not stats_path.exists():
        return []
    history = []
    for line in stats_path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("date") != today and isinstance(row.get("text_new"), int):
            history.append(row["text_new"])
    return history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-findata", action="store_true", help="跳过 findata daily_pipeline")
    parser.add_argument("--dry-run", action="store_true", help="只打印步骤与产物检查，不执行")
    args = parser.parse_args()

    now = datetime.now()
    date_str = f"{now:%Y-%m-%d}"
    date_tag = f"{now:%Y%m%d_%H%M%S}"
    steps = build_steps(args.skip_findata, findata_python_path())

    if args.dry_run:
        for step in steps:
            reason = f"  [预失败] {step.pre_fail_reason}" if step.pre_fail_reason else ""
            check = f"  产物检查: {step.check_path}" if step.check_path else ""
            print(f"- {step.name}: ({step.cwd})\n  {' '.join(step.cmd)}{check}{reason}")
        return 0

    text_total_before = count_lines(NEWS_CORPUS)
    results: list[StepResult] = []
    failed = False
    for step in steps:
        if failed:
            results.append(StepResult(step.name, ok=False, detail="前序步骤失败，跳过"))
            continue
        result = run_step(step, LOG_DIR, date_tag)
        results.append(result)
        if not result.ok:
            failed = True

    # audit：不依赖 subprocess，漏斗没跑成功时如实显示无产物
    dropped_path = FUNNEL_DIR / "dropped.jsonl"
    audit = aggregate_drops(
        dropped_path.read_text(encoding="utf-8").split("\n") if dropped_path.exists() else []
    )

    text_total_after = count_lines(NEWS_CORPUS)
    text_new = max(0, text_total_after - text_total_before)
    cleaned_path = FUNNEL_DIR / "cleaned.jsonl"
    funnel_kept = count_lines(cleaned_path) if cleaned_path.exists() else 0
    band_alert = evaluate_band(load_history(STATS_PATH, date_str), text_new)
    disk = shutil.disk_usage(REPO)
    disk_used_pct = disk.used / disk.total * 100

    findata_detail = next(
        (f"{r.detail}；日志尾部：{r.log_tail}" for r in results if r.name == "findata_daily"), None
    )
    abnormal = [
        f"{r.name}: {r.detail}" + (f"\n  日志尾部：\n{r.log_tail}" if r.log_tail else "")
        for r in results
        if not r.ok
    ]
    report = render_report(
        date_str, abnormal, findata_detail, text_total_after, text_new, band_alert,
        text_total_after, funnel_kept, audit, disk_used_pct,
    )
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"{date_str}.md"
    report_path.write_text(report, encoding="utf-8")

    append_stats(
        STATS_PATH,
        {
            "date": date_str,
            "ts": now.isoformat(timespec="seconds"),
            "text_total": text_total_after,
            "text_new": text_new,
            "funnel_in": text_total_after,
            "funnel_kept": funnel_kept,
            "disk_used_pct": round(disk_used_pct, 1),
            "failures": [r.name for r in results if not r.ok],
        },
    )

    print(f"日报：{report_path}")
    ok = all(r.ok for r in results)
    print("全部步骤通过" if ok else "存在失败步骤（见日报「今日异常」）")
    return 0 if ok else 1


if __name__ == "__main__":
    t0 = time.perf_counter()
    code = main()
    print(f"耗时 {time.perf_counter() - t0:.1f}s")
    sys.exit(code)
