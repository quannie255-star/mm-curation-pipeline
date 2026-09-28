"""容器冒烟断言：对着一个**已经在跑的服务**打 HTTP，验交付面是否真的可用。

## 为什么把它做成脚本，而不是写进 workflow 的 shell

因为"能不能本地跑"决定了它是**判据**还是**赌注**。写在 YAML 里的话，
它只能在 CI 上第一次运行、失败信息是一串 curl 输出；做成脚本后，
本机可以这样先验一遍（**不起 docker**）：

    python -m mm_curation.cli run --run-id smoke__0001
    python -m mm_curation.cli promote
    python -m mm_curation.cli serve --env prod --port 18080 &
    python scripts/smoke_container.py --base-url http://127.0.0.1:18080

于是 CI 里剩下的唯一未知量就是"镜像能不能构建、容器能不能起"——
而那两件事本机确实做不到（拉不到 registry-1.docker.io，已记在诚实边界里）。

只用标准库 `urllib`：镜像与 runner 都不该为了一个冒烟脚本多装依赖。

## 断言的取舍

每条都对应一个**会真的坏掉**的东西，而不是"访问一下 200 就算过"：

- `/healthz` 必须 **ready=true** —— 只验 200 会让"契约闸门没过"也算通过；
- `env.name` 必须是 **prod** —— 容器默认就该服务 prod store，写成 dev 是静默的错配；
- 行级授权必须在容器里仍然生效（finance 角色看不到传感器数据集）——
  把 RBAC 验成"容器里能查数"是不完整的，权限才是服务层的主要风险面；
- `/metrics` 必须存在 —— 没有它，容器里出问题就没有任何可观测入口。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

ADMIN = "mmc-admin-demo"
FINANCE = "mmc-finance-demo"


class SmokeError(AssertionError):
    pass


def _get(base: str, path: str, token: str = "") -> tuple[int, str]:
    req = urllib.request.Request(base.rstrip("/") + path)
    if token:
        req.add_header("X-API-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # 4xx/5xx 也要能读 body
        return e.code, e.read().decode("utf-8", "replace")


def _check(cond: bool, msg: str, extra: str = "") -> None:
    if not cond:
        raise SmokeError(f"{msg}{(' :: ' + extra) if extra else ''}")


def run(base: str, expect_env: str = "prod") -> list[str]:
    lines: list[str] = []

    st, body = _get(base, "/healthz")
    _check(st == 200, f"/healthz 期望 200，实际 {st}", body[:300])
    health = json.loads(body)
    _check(health.get("ready") is True, "/healthz 返回 200 但 ready 不是 true", body[:300])
    _check(
        health.get("env", {}).get("env") == expect_env,
        f"容器服务的环境应是 {expect_env}（dev/prod 表名相同，只能靠这里区分）",
        json.dumps(health.get("env"), ensure_ascii=False),
    )
    lines.append(f"/healthz 200 · ready · env={expect_env} · store={health['env']['store']}")

    st, _ = _get(base, "/api/datasets")
    _check(st in (401, 403), f"无 token 访问 /api/datasets 应被拒，实际 {st}")
    lines.append(f"无 token → {st}（鉴权生效）")

    st, body = _get(base, "/api/datasets", ADMIN)
    _check(st == 200, f"admin 取数期望 200，实际 {st}", body[:300])
    admin_ds = json.loads(body)["datasets"]
    _check(len(admin_ds) >= 1, "admin 应至少看到一个数据集（晋升过来的产物是空的？）")
    lines.append(f"admin → {st}，可见 {len(admin_ds)} 个数据集")

    st, body = _get(base, "/api/datasets", FINANCE)
    _check(st == 200, f"finance 角色期望 200（不是 403：它有合法身份），实际 {st}", body[:300])
    fin_ds = json.loads(body)["datasets"]
    names = {d.get("dataset") for d in fin_ds}
    _check(
        not (names & {"metropt3", "cmapss", "skab_w64"}),
        f"finance 角色不该看到工业传感器数据集（行级授权在容器里失效了）：{names}",
    )
    lines.append(f"finance → {st}，可见 {sorted(names)}（行级授权生效）")

    st, body = _get(base, "/metrics")
    _check(st == 200, f"/metrics 期望 200，实际 {st}")
    _check("mm_service_up" in body, "/metrics 里没有 mm_service_up，可观测入口不完整")
    lines.append("/metrics 200（含 mm_service_up）")

    st, _ = _get(base, "/api/datasets/no_such_dataset", ADMIN)
    _check(st == 404, f"不存在的 dataset 期望 404，实际 {st}")
    lines.append(f"不存在的 dataset → {st}")

    return lines


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="容器/服务冒烟断言")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--expect-env", default="prod")
    a = ap.parse_args(argv[1:])
    try:
        for line in run(a.base_url, a.expect_env):
            print("  [ok]", line)
    except SmokeError as e:
        print(f"  [FAIL] {e}", file=sys.stderr)
        return 1
    print("\n冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
