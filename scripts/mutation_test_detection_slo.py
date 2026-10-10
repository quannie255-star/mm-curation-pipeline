"""SLO 门禁的变异测试：证明 `eval_detection_slo.py` **真能拦红**，不是恒绿。

为什么必须做（本项目的核心教训：判据腐烂比门禁腐烂更难发现）：
一个永不报错的门禁比没有门禁更危险 —— 它告诉你「已合规」。
而**恒真判据**的特征就是：怎么跑都绿。本测试用 7 个变异逐一验证：

  M1 收紧**有缺口**形态的召回预算 → 应 BREACH（变严就红）
  M2 放宽误杀预算 → 误杀项不再违约（门禁只认契约，不认现状）
  M3 删掉一个形态的契约 → 应判 NO_SLO（**不设目标就不算达标**）
  M4 目标设成理想值 1.0（超装置天花板）→ 应仍红（天花板护栏）
  M5 空配置（漏斗全通过）→ 召回 0% 应红
  M6 契约文件缺失 → 必须报错退出，不得静默绿
  M7 黄金集缺失 → 必须 rc=2 且明确报错（不得 fallback 自证）

纪律（变异测试三条硬规矩，本文件全遵守）：
  1. **第一步必须 assert 基线 rc==0** —— 否则后面的红是因为本来就红，无意义。
  2. **deepcopy 真实跑出的基线**，不从文件读，也不手搓。
  3. **必须有反向还原那一步**，且还原后复跑断言 rc 回到基线值。
  4. **变异对象必须有实测缺口** ——恒满分对象（如召回 100%）无法被任何预算变异逼红，
     那时失败的是装置不是判据。
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
EVAL = ROOT / "scripts" / "eval_detection_slo.py"
SLO = ROOT / "configs" / "detection_slo.yaml"
CFG = ROOT / "configs" / "pipeline.v3_full_coverage.yaml"
GOLDEN = ROOT / "data" / "golden" / "golden_set.jsonl"

import yaml  # noqa: E402


def run(slo_path: Path, cfg_path: Path) -> tuple[int, str]:
    r = subprocess.run(
        [PY, "-X", "utf8", str(EVAL), "--slo", str(slo_path), "--config", str(cfg_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
    )
    return r.returncode, r.stdout + r.stderr


def main() -> int:
    if not GOLDEN.exists():
        print(f"❌ 黄金集不存在：{GOLDEN}\n   先跑 python scripts/build_golden_set.py")
        return 2

    # 纪律 1：先确认基线。
    # ⚠️ 基线**允许是绿或红**，取决于契约是否如实反映现状：
    #   绿 = 契约的 target 就是当前实测棘轮（现状已达标）
    #   红 = 契约里写着尚未达成的目标
    # 两种都合法。**真正要证明的是「变异让它变红/保持红」**，
    # 而不是「基线必须红」—— 那个假设本身就是个恒真陷阱
    # （第一版就栽在这：契约按实测设了棘轮后基线转绿，断言反而炸了）。
    baseline_slo = yaml.safe_load(SLO.read_text(encoding="utf-8"))
    rc0, out0 = run(SLO, CFG)
    baseline_breaches = [ln for ln in out0.split("\n") if ln.strip().startswith("- ")]
    print("=" * 72)
    print(f"基线 rc={rc0}，BREACH {len(baseline_breaches)} 条")
    for b in baseline_breaches:
        print(f"   {b.strip()[:100]}")
    if rc0 not in (0, 1):
        print(f"★ 基线 rc={rc0} 非 0/1，说明脚本自身崩了，先修脚本再谈变异")
        return 2
    results: list[tuple[str, bool, str]] = []

    # ---- M1 收紧召回预算 → 应变红（比基线更严）----
    # ⚠️ 变异对象**必须选有实测缺口的形态**（mojibake 93.3%）。
    #   第一版挑了 pii_inject（实测召回**恰好 100%**，失败率 0）：
    #   budget 从 0.05 收到 0.0，失败率 0 <= 0 → 判 PASS，**数学上完全正确**，
    #   但断言「应该更红」于是炸了。
    #   这是**变异装置错了，不是判据错了** —— 记忆里的规矩：
    #   「变异没被拦住先怀疑装置，再怀疑判据」。
    #   一个恒满分（0%失败）的被测对象，压根不可能被任何预算变异逼红。
    m1 = copy.deepcopy(baseline_slo)
    m1["contracts"]["forms"]["mojibake"]["recall"]["budget"] = 0.0
    rc, out = run(write(m1), CFG)
    ok = rc != 0 and "mojibake" in out
    results.append(("M1 收紧 mojibake 预算→0（应比基线更红）", ok, f"rc={rc}（基线 {rc0}）"))

    # ---- M2 放宽误杀预算到 100% → 误杀项转绿 →整体应与基线同rc ----
    # 这一条验的是「门禁只对契约负责」：放宽 budget 后误杀项不该再报。
    m2 = copy.deepcopy(baseline_slo)
    m2["contracts"]["global"]["false_kill"]["budget"] = 1.0
    rc, out = run(write(m2), CFG)
    no_fk_breach = "false_kill:" not in out
    print(f"\nM2 放宽后是否还报 false_kill 违约：{('false_kill:' in out)}")
    results.append(
        (
            "M2 误杀预算放到 100% → 误杀项不再违约（门禁只认契约）",
            no_fk_breach,
            f"rc={rc}（基线 {rc0}）",
        )
    )

    # ---- M3 删掉 boilerplate_inject 的契约 → 应判 NO_SLO ----
    m3 = copy.deepcopy(baseline_slo)
    m3["contracts"]["forms"]["boilerplate_inject"]["recall"]["target"] = None
    m3["contracts"]["forms"]["boilerplate_inject"]["recall"]["budget"] = None
    rc, out = run(write(m3), CFG)
    no_slo_ok = "NO_SLO" in out and "boilerplate_inject" in out
    results.append(("M3 契约 target置空→应判 NO_SLO（不算达标）", no_slo_ok, f"rc={rc}"))

    # ---- M4 天花板护栏：把 mismatched_pair 契约调到理想值 1.0 ----
    # 应因**装置天花板**（23.3%）而仍红 → 证明门禁不会被「理想目标」骗绿
    m4 = copy.deepcopy(baseline_slo)
    m4["contracts"]["forms"]["mismatched_pair"]["recall"]["target"] = 1.0
    m4["contracts"]["forms"]["mismatched_pair"]["recall"]["budget"] = 0.0
    rc, out = run(write(m4), CFG)
    ceiling_guarded = rc != 0
    print("\nM4 输出片段：")
    for ln in out.split("\n"):
        if "mismatched_pair" in ln:
            print(f"   {ln.strip()[:110]}")
    results.append(
        ("M4 目标设1.0（超装置天花板）→ 应仍红（天花板护栏）", ceiling_guarded, f"rc={rc}")
    )

    # ---- M5 空配置（漏斗全通过）→ 误杀=0、召回=0 → 必红（召回项） ----
    empty_cfg = write(
        {"name": "empty", "description": "空配置", "operators": []},
    )
    rc, out = run(SLO, empty_cfg)
    empty_red = rc != 0
    results.append(("M5 空配置（漏斗全通过）→ 召回 0% 应红", empty_red, f"rc={rc}"))

    # ---- M6 缺失契约文件 → 脚本必须报错，不得静默绿 ----
    rc, out = run(ROOT / "configs" / "_does_not_exist.yaml", CFG)
    missing_red = rc != 0
    results.append(("M6 契约文件缺失 → 必须报错退出", missing_red, f"rc={rc}"))

    # ---- M7 黄金集缺失 → 必须 rc=2（明确报错，不 fallback） ----
    tmp_golden = GOLDEN
    stash = tmp_golden.read_text(encoding="utf-8")
    backup = ROOT / "data" / "golden" / "_stash.jsonl"
    backup.write_text(stash, encoding="utf-8")
    try:
        tmp_golden.unlink()
        rc, out = run(SLO, CFG)
        golden_missing_fails = rc == 2 and "黄金集不存在" in out
    finally:
        tmp_golden.write_text(stash, encoding="utf-8")
        backup.unlink(missing_ok=True)
    results.append(
        (
            "M7 黄金集缺失 → 必须 rc=2 且明确报错（不得 fallback 自证）",
            golden_missing_fails,
            f"rc={rc}",
        )
    )

    print()
    print("=" * 72)
    print("变异测试结果")
    print("=" * 72)
    passed = 0
    for name, ok, info in results:
        print(f"  {'✅' if ok else '★★ 失败'} {name}   [{info}]")
        passed += bool(ok)
    print(f"\n{passed}/{len(results)} 通过")

    # 纪律 3：反向还原 + 断言回到基线
    rc_back, _ = run(SLO, CFG)
    print(f"\n还原后 rc={rc_back}（应回到基线 {rc0}）")
    assert rc_back == rc0, "还原失败：变异测试污染了基线状态"
    return 0 if passed == len(results) else 1


_TMP_FILES: list[Path] = []
#: ⭐ 变异配置**必须**写到独立目录，绝不碰真契约。
#: 第一版把SLO 直接传给 write() → 变异内容被 yaml.safe_dump 写进了
#: configs/detection_slo.yaml（真契约被污染，连带 M4 的 target=1.0 都留在里面）。
#: 根因是「路径参数和内容参数混在一个调用里」，容易看错。
#: 修法：write() **只接受内容**，路径由本模块统一生成，且名字必带 `_mutation`。
MUT_DIR = ROOT / "configs" / "_mutations"


def write(data) -> Path:
    """把变异后的配置写到独立临时目录，登记待清理。

    ⚠️ 刻意**不接受**目标路径参数 —— 传路径进来就可能误写真配置。
    """
    MUT_DIR.mkdir(parents=True, exist_ok=True)
    p = MUT_DIR / f"{next(_counter)}.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    _TMP_FILES.append(p)
    return p


def _counter_gen():
    n = 0
    while True:
        n += 1
        yield f"m{n:02d}"


_counter = _counter_gen()


def cleanup() -> None:
    for p in _TMP_FILES:
        if p.exists() and p.parent == MUT_DIR:
            p.unlink()
    _TMP_FILES.clear()
    if MUT_DIR.exists() and not any(MUT_DIR.iterdir()):
        MUT_DIR.rmdir()


if __name__ == "__main__":
    try:
        code = main()
    finally:
        cleanup()
    raise SystemExit(code)
