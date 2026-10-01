"""服务层测试（S4）：鉴权、行级授权、列级脱敏、令牌桶限流、契约闸门、指标暴露。

**全部走 `ServiceCore.dispatch`**，不起 HTTP 服务器：
权限/限流/脱敏的逻辑与框架无关，能被直接单测；FastAPI 适配层只是薄壳。
"""

from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
yaml = pytest.importorskip("yaml")

from mm_curation.platform import jobs  # noqa: E402
from mm_curation.platform.service import (  # noqa: E402
    AccessDenied,
    Principal,
    RateLimited,
    ServiceCore,
    TokenBucket,
    hash_token,
    load_rbac,
)

ADMIN = "mmc-admin-demo"
ANALYST = "mmc-analyst-demo"
FINANCE = "mmc-finance-demo"


@pytest.fixture()
def rbac_file(tmp_path):
    cfg = {
        "roles": {
            "admin": {"hidden_columns": [], "datasets": ["*"], "qps": 100, "burst": 200},
            "analyst": {
                "hidden_columns": ["device_id"],
                "datasets": ["*"],
                "qps": 100,
                "burst": 200,
            },
            "finance_reader": {
                "hidden_columns": ["device_id", "channel"],
                "datasets": ["news_corpus", "text_funnel"],
                "qps": 100,
                "burst": 200,
            },
            "tiny": {"hidden_columns": [], "datasets": ["*"], "qps": 0.01, "burst": 3},
        },
        "tokens": [
            {"name": "a", "role": "admin", "token_sha256": hash_token(ADMIN)},
            {"name": "b", "role": "analyst", "token_sha256": hash_token(ANALYST)},
            {"name": "c", "role": "finance_reader", "token_sha256": hash_token(FINANCE)},
            {"name": "d", "role": "tiny", "token_sha256": hash_token("mmc-tiny-demo")},
        ],
    }
    p = tmp_path / "rbac.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


@pytest.fixture()
def served_root(lake_root):
    """跑一遍平台链路（7 行样本），让视图就绪，再交给服务层查。"""
    res = jobs.run_platform(
        lake_root, datasets=("metropt3", "news_corpus"), verbose=False, batch_date="2026-09-22"
    )
    assert res["status"] == "SUCCESS", res
    return lake_root


@pytest.fixture()
def core(served_root, rbac_file):
    c = ServiceCore(served_root, rbac_path=rbac_file)
    c.startup()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_authorize_returns_principal_with_role_policy(core):
    p = core.authorize(ADMIN)
    assert p.role == "admin" and p.name == "a"
    assert p.can_read("anything")
    assert p.hidden_columns == ()


def test_authorize_rejects_bad_and_empty_token(core):
    with pytest.raises(AccessDenied):
        core.authorize("not-a-token")
    with pytest.raises(AccessDenied):
        core.authorize("")


def test_authorize_rejects_token_bound_to_undefined_role(served_root, tmp_path):
    p = tmp_path / "rbac.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "roles": {},
                "tokens": [{"name": "x", "role": "ghost", "token_sha256": hash_token("t")}],
            }
        ),
        encoding="utf-8",
    )
    c = ServiceCore(served_root, rbac_path=p)
    try:
        with pytest.raises(AccessDenied, match="未在 roles 中定义"):
            c.authorize("t")
    finally:
        c.close()


def test_missing_rbac_file_grants_nothing(served_root, tmp_path):
    """配置缺失 → 空策略（谁都不授权），**不静默放开**。"""
    c = ServiceCore(served_root, rbac_path=tmp_path / "nope.yaml")
    try:
        assert c.rbac == {"roles": {}, "tokens": {}}
        with pytest.raises(AccessDenied):
            c.authorize("any")
    finally:
        c.close()


def test_hash_token_is_sha256_of_token():
    assert hash_token("abc") == ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_load_rbac_hashes_are_lowercased(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        yaml.safe_dump({"roles": {"r": {}}, "tokens": [{"role": "r", "token_sha256": "ABCDEF"}]}),
        encoding="utf-8",
    )
    assert "abcdef" in load_rbac(p)["tokens"]


# ---------------------------------------------------------------------------
# 行级授权 / 列级脱敏
# ---------------------------------------------------------------------------


def test_row_level_deny_returns_403(core):
    st, body = core.dispatch("/api/datasets/metropt3/health", token=FINANCE)
    assert st == 403 and body["error"] == "forbidden"
    # 有权限的数据集照常返回
    st2, _ = core.dispatch("/api/datasets/news_corpus/health", token=FINANCE)
    assert st2 == 200


def test_missing_token_is_401_bad_token_is_403(core):
    assert core.dispatch("/api/datasets")[0] == 401
    assert core.dispatch("/api/datasets", token="wrong")[0] == 403


def test_list_datasets_is_row_filtered(core):
    st, body = core.dispatch("/api/datasets", token=FINANCE)
    assert st == 200
    assert {d["dataset"] for d in body["datasets"]} == {"news_corpus"}


def test_column_masking_hides_natural_key_but_keeps_surrogate(core):
    _, admin = core.dispatch("/api/datasets/metropt3/dims", token=ADMIN, query={"limit": 5})
    _, analyst = core.dispatch("/api/datasets/metropt3/dims", token=ANALYST, query={"limit": 5})
    assert admin["columns_masked"] == []
    assert analyst["columns_masked"] == ["device_id"]
    assert admin["rows"][0]["device_id"]
    for r in analyst["rows"]:
        # 业务自然键不给，但 SCD-2 代理键在 —— 分析不必知道设备叫什么
        assert "device_id" not in r
        assert r["device_sk"] is not None


def test_column_masking_is_explicit_not_silent(core):
    """脱敏必须写在响应里：静默少给一列会被理解成"这列不存在"。

    注意脱敏只对**存在的列**生效：`ads_dataset_health` 里根本没有 device_id/channel，
    所以它的 `columns_masked` 是空的——这是对的，不是漏了。
    用 dims 端点验证（那里才有这两列）；news_corpus 没有设备通道 → 结果集为空，
    走的是"空结果集仍要能算出脱敏清单"的兜底列清单。
    """
    _, body = core.dispatch("/api/datasets/news_corpus/dims", token=FINANCE)
    assert body["rows"] == []
    assert body["columns_masked"] == ["device_id", "channel"]

    # 有实际行的场景：metropt3 的维表（analyst 只脱敏 device_id）
    _, real = core.dispatch("/api/datasets/metropt3/dims", token=ANALYST)
    assert real["columns_masked"] == ["device_id"]
    assert real["rows"] and all("device_id" not in r for r in real["rows"])


def test_visible_and_masked_helpers():
    p = Principal(name="x", role="analyst", hidden_columns=("device_id",))
    cols = ["dataset", "device_id", "device_sk"]
    assert p.visible_columns(cols) == ["dataset", "device_sk"]
    assert p.masked(cols) == ["device_id"]


# ---------------------------------------------------------------------------
# 令牌桶
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float) -> None:
        self.t += dt


def test_token_bucket_allows_burst_then_throttles():
    clk = _Clock()
    b = TokenBucket(qps=10, burst=5, now=clk)
    assert [b.try_acquire()[0] for _ in range(5)] == [True] * 5
    ok, wait = b.try_acquire()
    assert ok is False
    # 稳态 10/s → 补 1 个令牌要 0.1s
    assert wait == pytest.approx(0.1, abs=1e-6)


def test_token_bucket_refills_over_time_and_caps_at_burst():
    clk = _Clock()
    b = TokenBucket(qps=10, burst=5, now=clk)
    for _ in range(5):
        b.try_acquire()
    clk.tick(0.1)
    assert b.try_acquire()[0] is True
    clk.tick(10.0)  # 远超容量 → 只能补到 burst，不能攒出无限额度
    assert [b.try_acquire()[0] for _ in range(5)] == [True] * 5
    assert b.try_acquire()[0] is False


def test_rate_limit_returns_429_with_retry_after(core):
    # role=tiny：burst=3
    codes = [core.dispatch("/api/datasets", token="mmc-tiny-demo")[0] for _ in range(6)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429, 429]
    st, body = core.dispatch("/api/datasets", token="mmc-tiny-demo")
    assert st == 429 and body["retry_after_s"] > 0


def test_rate_limit_is_per_role_not_global(core):
    for _ in range(4):
        core.dispatch("/api/datasets", token="mmc-tiny-demo")
    assert core.dispatch("/api/datasets", token="mmc-tiny-demo")[0] == 429
    # 别的角色不受影响
    assert core.dispatch("/api/datasets", token=ADMIN)[0] == 200


def test_check_rate_raises_rate_limited(core):
    p = core.authorize("mmc-tiny-demo")
    assert p.burst == 3.0
    for _ in range(3):
        core.check_rate(p)
    with pytest.raises(RateLimited):
        core.check_rate(p)


# ---------------------------------------------------------------------------
# 契约闸门
# ---------------------------------------------------------------------------


def _failing_contract(tmp_path, severity="error"):
    d = tmp_path / "configs" / "contracts_platform"
    d.mkdir(parents=True, exist_ok=True)
    (d / "core.yaml").write_text(
        yaml.safe_dump(
            {
                "dataset": "text_funnel",
                "version": 1,
                "owner": "test",
                "table": "ods_samples",
                "checks": [
                    {"name": "always_fails", "sql": "SELECT 1", "expect": 0, "severity": severity}
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return d


def test_startup_gate_blocks_service_when_contract_fails(served_root, rbac_file, tmp_path):
    _failing_contract(tmp_path)
    c = ServiceCore(served_root, rbac_path=rbac_file)
    try:
        rep = c.startup()
        assert rep["n_fail"] == 1
        assert rep["blocking"] == ["text_funnel@1::always_fails"]
        assert c.ready is False
        # 不就绪 → 拒绝供数（而不是"打个日志继续"）
        st, body = c.dispatch("/api/datasets", token=ADMIN)
        assert st == 503 and body["error"] == "not_ready"
        assert body["blocking"] == ["text_funnel@1::always_fails"]
        # health 自身仍可访问（要能诊断"为什么不就绪"）
        st_h, h = c.dispatch("/api/health")
        assert st_h == 200 and h["ready"] is False
    finally:
        c.close()


def test_contract_gate_can_be_disabled_for_debug(served_root, rbac_file, tmp_path):
    _failing_contract(tmp_path)
    c = ServiceCore(served_root, rbac_path=rbac_file, contract_blocking=False)
    try:
        assert c.startup()["n_fail"] == 1
        assert c.ready is True
        assert c.dispatch("/api/datasets", token=ADMIN)[0] == 200
    finally:
        c.close()


def test_warn_severity_does_not_block(served_root, rbac_file, tmp_path):
    _failing_contract(tmp_path, severity="warn")
    c = ServiceCore(served_root, rbac_path=rbac_file)
    try:
        rep = c.startup()
        assert rep["n_fail"] == 1 and rep["blocking"] == []
        assert c.ready is True
    finally:
        c.close()


def test_missing_contract_dir_means_ready(served_root, rbac_file):
    c = ServiceCore(served_root, rbac_path=rbac_file, contract_dir="configs/nope")
    try:
        rep = c.startup()
        assert rep["n_contracts"] == 0 and c.ready is True
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 端点 / 错误映射 / 指标
# ---------------------------------------------------------------------------


def test_endpoints_return_expected_shapes(core):
    st, d = core.dispatch("/api/datasets/metropt3/health", token=ADMIN)
    assert st == 200 and d["data"]["dataset"] == "metropt3"
    st, dy = core.dispatch("/api/datasets/metropt3/daily", token=ADMIN, query={"limit": 5})
    assert st == 200 and isinstance(dy["rows"], list) and len(dy["rows"]) <= 5
    st, dop = core.dispatch("/api/datasets/metropt3/ops", token=ADMIN, query={"limit": 5})
    assert st == 200
    st, runs = core.dispatch("/api/runs", token=ADMIN)
    assert st == 200 and runs["runs"]


def test_unknown_path_is_404_and_lists_endpoints(core):
    st, body = core.dispatch("/api/nope", token=ADMIN)
    assert st == 404 and body["error"] == "not_found" and body["endpoints"]


def test_unknown_dataset_is_404(core):
    st, body = core.dispatch("/api/datasets/nosuch/health", token=ADMIN)
    assert st == 404 and body["error"] == "not_found"


def test_limit_is_clamped(core):
    _, body = core.dispatch("/api/datasets/metropt3/daily", token=ADMIN, query={"limit": "999999"})
    assert len(body["rows"]) <= 2000


def test_metrics_text_contains_service_series(core):
    core.dispatch("/api/datasets", token=ADMIN)
    core.dispatch("/api/datasets/nosuch/health", token=ADMIN)
    txt = core.metrics_text()
    assert "mm_service_up 1" in txt
    for name in (
        "mm_service_requests_total",
        "mm_service_latency_p95_seconds",
        "mm_service_denied_total",
        "mm_service_rate_limited_total",
        "mm_service_columns_masked_total",
    ):
        assert f"# HELP {name}" in txt and f"# TYPE {name}" in txt
    # 数据侧指标（S5）拼在同一份暴露里，两边口径不重复实现
    assert "mm_dataset_rows" in txt
    assert 'role="admin"' in txt


def test_metrics_endpoint_is_not_self_observed(core):
    core.dispatch("/metrics")
    txt = core.metrics_text()
    assert 'path="/metrics"' not in txt


def test_p95_is_computed_from_recent_window(core):
    for _ in range(5):
        core.dispatch("/api/datasets", token=ADMIN)
    snap = core.metrics.snapshot()
    assert snap["p95"]["/api/datasets"] is not None
    assert snap["n_latency_samples"]["/api/datasets"] == 5


# ---------------------------------------------------------------------------
# 环境与容器探针（S6）
# ---------------------------------------------------------------------------


def test_health_reports_which_environment_the_service_reads(core):
    """dev/prod 双环境之后，"这个容器读的是哪个湖"是排障的第一个问题，
    而它**无法从响应内容推断**（两个环境的表名完全一样）。"""
    status, body = core.dispatch("/api/health")
    assert status == 200
    assert body["env"]["env"] == "dev"
    assert body["env"]["isolated"] is False
    assert body["env"]["lake_dir"].replace("\\", "/").endswith("data/lake")


def test_healthz_returns_503_when_the_contract_gate_blocks(served_root, rbac_file, tmp_path):
    """容器探针的语义是"**能供数**才算健康"，与 `/api/health`（永远 200、状态在 body）
    刻意分开。混用的坏结果二选一：探针在闸门没过时报 healthy，
    或者老诊断入口 `/api/health` 开始返回 503。"""
    _failing_contract(tmp_path)
    c = ServiceCore(served_root, rbac_path=rbac_file)
    try:
        c.startup()
        assert c.ready is False
        assert c.dispatch("/healthz")[0] == 503
        assert c.dispatch("/api/health")[0] == 200  # 诊断入口语义不变
    finally:
        c.close()


def test_healthz_returns_200_when_ready(core):
    assert core.ready is True
    status, body = core.dispatch("/healthz")
    assert status == 200 and body["ready"] is True


def test_service_core_env_prod_reads_the_prod_store_and_leaves_dev_untouched(lake_root, rbac_file):
    """`--env prod` 的服务必须读 prod 的产物，且**不允许**顺手改动 dev 的产物。"""
    from mm_curation.platform import envs
    from mm_curation.platform.envs import ENV_DEV, ENV_PROD
    from mm_curation.platform.runs import RunLedger

    jobs.run_platform(lake_root, datasets=("metropt3", "news_corpus"), verbose=False)
    dev_fp = envs.tree_fingerprint(envs.resolve(lake_root, ENV_DEV).lake_dir)

    prod = ServiceCore(lake_root, rbac_path=rbac_file, env=ENV_PROD)
    try:
        assert prod.spec.name == ENV_PROD
        assert prod.spec.warehouse_db == envs.resolve(lake_root, ENV_PROD).warehouse_db
        # prod 的台账里**没有** dev 那次运行 → 服务读的确实是另一份产物。
        # 这条断言比"ready 是 False"更直接：空 prod 也可能因为没配契约而 ready。
        assert prod.ledger.list_runs(limit=5) == []
    finally:
        prod.close()

    dev_led = RunLedger(envs.resolve(lake_root, ENV_DEV).warehouse_db)
    assert len(dev_led.list_runs(limit=5)) == 1
    assert envs.tree_fingerprint(envs.resolve(lake_root, ENV_DEV).lake_dir) == dev_fp


def test_endpoints_list_contains_healthz(core):
    """端点是公开契约，新增了必须出现在自描述里。"""
    assert "/healthz" in core.dispatch("/api/health")[1]["endpoints"]


# ---------------------------------------------------------------------------
# HTTP 适配层（真实 ASGI 调用）——**这一节的存在原因值得说明**
# ---------------------------------------------------------------------------
# 上面所有测试都是直接调 `ServiceCore.dispatch(path, token=...)` 的。那是刻意的
# 设计（权限/限流/脱敏与框架无关，不该为了单测去起服务器）。但它留下了一个
# **整层大小的盲区**：HTTP 适配层从来没被跑过。
#
# 结果是它整层是坏的（ENGINEERING_NOTES #85）：`from __future__ import annotations`
# 让 `request: Request` 在运行时成了字符串，而 `Request` 当时只在 `create_app`
# 内部导入，FastAPI 用**模块全局**解析注解时找不到它，于是把它当成必填 query 参数
# ——**除 `/metrics` 外所有路径一律 422**。单元测试全绿，docker 里服务不可用。
#
# 所以这一节不测业务逻辑，只测一件事：**接线接对了**。
# 用 TestClient（进程内 ASGI，不需要 socket），因此本地与 CI 都能跑。
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_client(served_root, rbac_file):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    pytest.importorskip("httpx")
    from mm_curation.platform.service import create_app

    app = create_app(served_root, rbac_path=rbac_file)
    with fastapi_testclient.TestClient(app) as c:
        yield c


def test_http_layer_is_wired_up_at_all(http_client):
    """回归测试：**每个端点都不许是 422**。

    422 是"框架把参数校验挂了"的信号，不是业务拒绝。它曾经是**全部**端点的返回值，
    而这条断言会立刻抓住它。
    """
    checks = [
        ("/api/health", None, 200),
        ("/healthz", None, 200),
        ("/metrics", None, 200),
        ("/api/datasets", ADMIN, 200),
        # 无 token → 401（"没带凭据"）；带错 token 是 403（"凭据不对"）。
        # 两者都拒绝，但状态码不同，口径与 `test_missing_token_is_401_bad_token_is_403` 一致。
        ("/api/datasets", None, 401),
        # 未知路径**也要先过鉴权** → 无 token 是 401 而不是 404。
        # 这是刻意的：404 会告诉未鉴权的调用方"这个路径不存在"，
        # 等于把路由表当公开信息。带上合法凭据才看得到 404。
        ("/nope", None, 401),
        ("/nope", ADMIN, 404),
    ]
    for path, token, expect in checks:
        headers = {"X-API-Token": token} if token else {}
        r = http_client.get(path, headers=headers)
        assert r.status_code != 422, f"{path} 返回 422 —— HTTP 适配层的注解解析又坏了"
        assert r.status_code == expect, f"{path} 期望 {expect}，实际 {r.status_code}"


def test_http_healthz_503_when_contract_gate_blocks(served_root, rbac_file, tmp_path):
    """就绪语义必须穿过 HTTP 层：闸门不过 → 探针 503（容器才会被判定不健康）。"""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    pytest.importorskip("httpx")
    from mm_curation.platform.service import create_app

    _failing_contract(tmp_path)
    app = create_app(served_root, rbac_path=rbac_file)
    with fastapi_testclient.TestClient(app) as c:
        assert c.get("/healthz").status_code == 503
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/health").json()["ready"] is False


def test_http_row_level_auth_survives_the_adapter(http_client):
    """行级授权必须在**穿过 HTTP 适配层之后**仍然生效。"""
    r = http_client.get("/api/datasets", headers={"X-API-Token": FINANCE})
    assert r.status_code == 200
    names = {d["dataset"] for d in r.json()["datasets"]}
    assert not (names & {"metropt3", "cmapss", "skab_w64"})


def test_http_and_dispatch_agree_on_status_codes(http_client, core):
    """两层对同一请求必须给同样的状态码。

    这条是防"以后有人只改一层"的：适配层薄，但它会漂移，
    而漂移的表现是"单测绿、线上错"。
    """
    for path, token in [
        ("/api/health", ""),
        ("/healthz", ""),
        ("/api/datasets", ADMIN),
        ("/api/datasets", ""),
        ("/api/datasets/news_corpus/health", ADMIN),
        ("/nope", ADMIN),
    ]:
        via_http = http_client.get(path, headers={"X-API-Token": token} if token else {})
        via_dispatch, _ = core.dispatch(path, token=token)
        assert via_http.status_code == via_dispatch, f"{path} 两层不一致"


def test_http_metrics_text_is_prometheus_parseable_shape(http_client):
    r = http_client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# HELP mm_service_up" in body and "# TYPE mm_service_up gauge" in body


def test_views_survive_store_relocation(served_root, rbac_file, tmp_path_factory):
    """回归（容器探针首跑事故）：视图不得被绝对路径绑死。

    事故机理：promote 建视图时把当时的绝对路径烧进 duckdb
    （read_parquet('C:/…' 或 '/home/runner/…')）；库文件随 store 搬家后
    （CI runner → 容器 /app 挂载）路径失效，契约查询整体 error →
    服务永久 503。修复 = serve 启动时按**当前** root/store 重建视图。
    本测试把整个 store 搬到新路径后启动，断言视图已重指新家且查询可用。
    """
    import shutil

    # served_root 基于 lake_root（tmp_path 根），副本必须放到它**外面**，
    # 否则 copytree 复制自身子树会无限递归（首版实测）
    new_root = tmp_path_factory.mktemp("reloc") / "repo"
    shutil.copytree(served_root, new_root)

    c = ServiceCore(new_root, rbac_path=rbac_file)
    rep = c.startup()
    try:
        # 视图已指向新家（不再引用旧绝对路径）
        v = c.con.sql("select sql from duckdb_views() where view_name='ods_all'").fetchone()[0]
        fwd = lambda x: str(x).replace("\\", "/")  # noqa: E731
        assert fwd(new_root) in fwd(v), "视图未按当前环境重建"
        assert fwd(served_root) not in fwd(v)
        # 查询层真的能用（事故现场是 10 条全 error）
        n = c.con.sql("select count(*) from ods_samples").fetchone()[0]
        assert n > 0
        assert rep["n_error"] == 0
    finally:
        c.close()
