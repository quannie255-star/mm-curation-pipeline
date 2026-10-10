"""一次性变异测试：验证 agent 门禁的新判据**真的会红**，不是恒真。

## 为什么必须做
本轮已在同一个文件里抓到**三次恒真判据**：
1. `only_in_agent` / `only_in_baseline`恒为 0（两臂跑同一批算子）
2. `agent_missed = rule_ids - agent_ids` 恒为空（plan 就建在rule_ids 上）
3. 覆盖率棘轮 `coverage_ceiling` 被误删 → 「0 篇越界」＝没在扫

三条都有一个共同外观：**报出来是 0 / 空 / 通过，看起来完美。**
所以每加一条判据，都要问「它在正确实现下会不会误判」**以及**
「它在错误实现下会不会真的变红」。

## 做法
只改**门禁读到的数据**，不改判据代码：
若把 `agent_missed` 从 0 改成 1，门禁必须 rc=1。
这不是「测代码」而是「测判据的有效性」——恒真的判据在这一步必然露馅。
"""

from __future__ import annotations

import copy
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import agent_routing_gate as G  # noqa: E402


def _load_real() -> dict:
    """跑一次真门禁，拿到基线结果（不判定）。"""
    import tempfile

    with tempfile.TemporaryDirectory(dir="C:/Users/10393/AppData/Local/Temp") as td:
        return G.run_experiment(n=G.DEFAULT_N, scan_limit=G.DEFAULT_SCAN, workdir=pathlib.Path(td))


def main() -> int:
    print("=" * 74)
    print("变异测试：agent 门禁的判据有效性")
    print("=" * 74)

    base = _load_real()
    ok, probs = G.judge(copy.deepcopy(base))
    print(f"\n[基线] judge -> {ok}，problems={len(probs)}")
    assert ok, f"基线就不绿，无法测变异：{probs}"
    dirty = base["arms"]["dirty"]
    print(
        f"  基线实测：agent_skipped_expensive={dirty['agent_skipped_expensive']} "
        f"costly_only={dirty['costly_only']} agent_missed={dirty['agent_missed']}"
    )

    assert dirty["agent_skipped_expensive"] > 0, (
        "★分母为 0——「没漏检」只是因为没跳过任何贵档，"
        "这条判据在当前配置下不构成证据（judge 应当已报红）"
    )
    print("  ✓ 分母非零：Agent 确实跳了贵档，「没漏检」是真省出来的")

    def _red(res: tuple[bool, list[str]]) -> bool:
        return res[0] is False and len(res[1]) > 0

    # ── 变异 1：脏数据上漏检 1 条 → 必须红
    m = copy.deepcopy(base)
    m["arms"]["dirty"]["agent_missed"] = 1
    r1 = G.judge(m)
    print(f"\n变异 1（脏臂漏检 1 条）：judge={r1[0]} problems={r1[1]}")
    assert _red(r1), f"变异 1 没被拦住：{r1[1]}"

    # ── 变异 2：Agent 偷看贵档分数（路由输入集不等于规则档放行集）
    m = copy.deepcopy(base)
    m["arms"]["agent"]["routing_input_ok"] = False
    r2 = G.judge(m)
    print(f"变异 2（Agent 偷看贵档分数）：judge={r2[0]} problems={r2[1]}")
    assert _red(r2), f"变异 2 没被拦住：{r2[1]}"

    # ── 变异 3：Agent 一条贵档都没跳 → 红线不构成证据，必须红
    m = copy.deepcopy(base)
    m["arms"]["dirty"]["agent_skipped_expensive"] = 0
    r3 = G.judge(m)
    print(f"变异 3（一条贵档都没跳）：judge={r3[0]} problems={r3[1]}")
    assert _red(r3), f"变异 3 没被拦住：{r3[1]}"

    # ── 变异 4：保留率不再逐位一致（放宽口径）→ 必须红
    m = copy.deepcopy(base)
    m["arms"]["agent"]["kept_rate"] = base["arms"]["agent"]["kept_rate"] - 0.02
    r4 = G.judge(m)
    print(f"变异 4（保留率差 2%）：judge={r4[0]} problems={r4[1]}")
    assert _red(r4), f"变异 4 没被拦住：{r4[1]}"

    # ── 变异 5：成本没省（saved_ratio 归零）→ 必须红
    m = copy.deepcopy(base)
    m["cost"]["saved_ratio"] = 0.0
    r5 = G.judge(m)
    print(f"变异 5（省 0%）：judge={r5[0]} problems={r5[1]}")
    assert _red(r5), f"变异 5 没被拦住：{r5[1]}"

    # ── 变异 6：污染臂整体缺失 → 红线未验证，必须红
    m = copy.deepcopy(base)
    m["arms"]["dirty"] = {"n": 0}
    r6 = G.judge(m)
    print(f"变异 6（污染臂缺失）：judge={r6[0]} problems={r6[1]}")
    assert _red(r6), f"变异 6 没被拦住：{r6[1]}"

    # ── 反向：还原后必须回到绿（否则上面6 条可能是「永远红」的假通过）
    ok_back, probs_back = G.judge(copy.deepcopy(base))
    print(f"\n[反向] 还原后 judge={ok_back} problems={len(probs_back)}")
    assert ok_back, f"还原后仍不绿，说明判据本身有问题：{probs_back}"

    print("\n" + "=" * 74)
    print("6/6 变异全部被正确拦住——判据具备区分能力，不是恒真")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
