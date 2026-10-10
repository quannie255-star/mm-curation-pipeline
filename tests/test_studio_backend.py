"""Studio 后端端到端测试 —— 真起服务、真造数据、真跑清洗。

⚠️ 这一层最容易被「单元测试全绿但接口根本不通」骗过去。
所以这里做的是**真的**：真的 `ThreadingHTTPServer`、真的HTTP 请求
（`urllib`）、真的跑一遍漏斗，断言的是**响应内容**而不是函数返回值。

每条测试都遵守同一条判据纪律：**不能有恒真的断言**。
所以除了「成功路径」，还有「错误路径必须真的报错」——
例如把 JSONL 换成内容不符的文件，检查接口**必须**拒。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "src"))

from mm_curation.studio import backend  # noqa: E402
from mm_curation.studio.serve import Handler  # noqa: E402

SESSION = "test_studio_e2e"
# 单独命名是为了避开「换行写进字符串字面量」这类编辑事故：
# 一旦它变成真字符，文件会直接语法报错（比默默算错更容易发现）。
_NL = "\n"


@pytest.fixture(autouse=True)
def _stub_perplexity(monkeypatch):
    """把 `perplexity` 的语言模型换成确定性桩（本模块内全部测试生效）。

    ⚠️ 为什么**必须**桩，而不是让它真加载模型：
    `text_article` 配方里含 `perplexity`（MODEL 档），真跑要加载
    `uer/gpt2-chinese-cluecorpussmall` 的本地权重（几百 MB）。而 `models/`
    在 `.gitignore` 里，**CI 干净检出中没有这份权重**，于是这组端到端测试
    会以「未找到 … 的本地缓存」挂掉 —— 那是**环境缺失**，不是代码缺陷。
    （实测：CI #91 上 4 条测试就是这样红的，而本机因为 models/ 在，全绿。
    典型的「本地绿 / CI 红」。）

    本文件要验的是 **HTTP 接口 + 漏斗接线**（真起服务、真发请求、真读响应），
    不是语言模型的困惑度质量 —— 后者由 `tests/test_text_corpus.py::
    test_perplexity_with_fake_scorer` 专门测。所以这里沿用那条测试的同一个
    注入点（`text_corpus.get_scorer`），只换掉模型推理，漏斗/归因/统计全是真的。

    桩的行为刻意对齐真实语义：长文干净（低困惑度→通过），短文/乱码被丢。
    """
    import mm_curation.operators.text_corpus as tc

    def fake_scorer(texts):
        return [20.0 if len(t) > 10 else 800.0 for t in texts]

    monkeypatch.setattr(tc, "get_scorer", lambda: fake_scorer)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def _get(url: str) -> dict:
    """GET 并**容错地**读回JSON。

    ⚠️ 必须容错：接口返回 4xx 时 urllib 会抛 HTTPError，
    而「未知任务返回错误」这类测试**要的正是一屏错误信息**。
    helper 只按 200 读的话，测试会因为「异常」而不是「断言」结束，
    就看不出服务到底返回了什么 —— 那就变成测「有没有抛异常」而不是
    「有没有给出正确原因」。
    """
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def _wait_job(base: str, job_id: str, timeout: float = 300) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = _get(f"{base}/api/job?id={job_id}")["job"]
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.4)
    raise AssertionError(f"任务 {job_id} 超时未完成")


# ── 静态页面 ──────────────────────────────────────────


def test_首页能打开(server):
    with urllib.request.urlopen(server + "/", timeout=15) as r:
        html = r.read().decode("utf-8")
    assert r.status == 200
    assert "多模态数据清洗" in html
    # 三步向导的容器必须在（前端靠它切步骤）
    for i in (1, 2, 3, 4):
        assert f'id="panel{i}"' in html, f"缺少第 {i} 步的面板"


def test_静态资源三个都在(server):
    for path, needle in (("/app.js", "loadScenarios"), ("/style.css", ".card")):
        with urllib.request.urlopen(server + path, timeout=15) as r:
            body = r.read().decode("utf-8")
        assert r.status == 200 and needle in body, f"{path} 内容不对"


def test_未知页面返回404而不是首页(server):
    """静态白名单必须真的拦住 —— 否则能读任意文件。"""
    try:
        urllib.request.urlopen(server + "/../claims.json", timeout=15)
        raise AssertionError("静态白名单没拦住路径穿越")
    except urllib.error.HTTPError as e:
        assert e.code == 404


# ── 场景接口 ──────────────────────────────────────────


def test_场景接口给出四个场景且字段齐全(server):
    j = _get(server + "/api/scenarios")
    assert j["ok"] is True
    scs = j["scenarios"]
    assert len(scs) >= 4
    for sc in scs:
        assert sc["title"] and sc["blurb"] and sc["produces"]
        assert sc["sample_hint"], f"{sc['key']} 没有格式示例"
        assert sc["n_steps"] >= 3, f"{sc['key']} 检查步骤太少"
        for s in sc["steps"]:
            # 代价档必须是**前端认识的字符串**，不能是 Enum 的 str()
            assert s["cost"] in ("rule", "perceptual", "model", "llm"), (
                f"{sc['key']}/{s['op']} 的代价档 {s['cost']!r} 前端认不出"
            )
            assert s["modality_ok"] is True, (
                f"{sc['key']}/{s['op']} 模态不匹配（配方不该含这种算子）"
            )


# ── 真数据端到端（纯文本场景，规则档，不需要 GPU）────────


_NEWS = [
    "今日北京市发布新一轮优化营商环境措施，涉及审批流程精简、税费减免等多个方面。",
    "国务院常务会议决定进一步优化营商环境，重点在于减少审批事项、压缩办理时限。",
    "某地推出新政策支持中小企业发展，涵盖融资担保、设备更新补贴与人才培训。",
    "国家统计局公布最新数据，上季度国内生产总值同比增长百分之五点三。",
    "交通运输部门发布清明假期出行指引，预计高速公路车流量将同比增长一成左右。",
    "教育部门宣布今年扩大义务教育阶段教师招聘规模，计划新增岗位三万个。",
    "医疗保障局推进药品集中带量采购常态化，第四批中选结果已在多地落地。",
    "生态环境部门通报上月空气质量状况，平均优良天数比例达到百分之八十五。",
    "水利部门启动年度防汛检查，要求重点流域提前落实度汛措施。",
    "农业农村部发布春耕技术指导意见，建议各地加强农作物病虫害监测。",
    "科技部公布新一轮重点研发计划立项名单，共支持二百三十余个项目。",
    "商务部表示将推动外贸新业态发展，扩大跨境电商综合试验区范围。",
]


def _make_corpus() -> Path:
    """造一份含**真实脏数据**的语料。

    ⚠️ 造数据踩过的坑（别重犯）：
    · 语料必须**彼此不同** —— 全用同一个模板句的话，`text_minhash`
      会把它们全判成转载重复，测出来就是「几乎全丢」，
      那样既测不出 doc_length 也测不出别的算子（数据被上游吃光了）。
    · 样本**可以不带 `id`** —— Studio 会按行号自动补，
      这正是本文件另一条测试要验的行为。
    """
    d = backend.session_dir(SESSION)
    p = d / "corpus.jsonl"
    rows: list[dict] = [{"text": t, "id": f"news{i}"} for i, t in enumerate(_NEWS)]
    rows.append({"text": _NEWS[0], "id": "dup_a"})  # 转载重复（应被 minhash 删）
    rows.append({"text": _NEWS[3], "id": "dup_b"})  # 转载重复
    rows.append({"text": "短"})  # 太短（无 id，测自动补）
    rows.append({"text": "转载" * 40})  # 超短复读
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + _NL for r in rows), encoding="utf-8")
    return p


def test_检查接口会拒内容不符的文件(server):
    """错误路径必须**真的拒** —— 否则前端会放行一份注定失败的输入。"""
    _make_corpus()
    bad = backend.session_dir(SESSION) / "bad.jsonl"
    bad.write_text(
        "".join(json.dumps({"txt": "x"}, ensure_ascii=False) + "\n" for _ in range(5)),
        encoding="utf-8",
    )
    r = _post(
        server + "/api/check",
        {
            "session": SESSION,
            "filename": "bad.jsonl",
            "scenario": "text_article",
            "images_uploaded": False,
        },
    )
    assert r["ok"] is False
    assert "text" in r["error"], f"报错要说清缺哪个字段，实际：{r['error']}"


def test_检查接口放行合法文件(server):
    _make_corpus()
    r = _post(
        server + "/api/check",
        {
            "session": SESSION,
            "filename": "corpus.jsonl",
            "scenario": "text_article",
            "images_uploaded": False,
        },
    )
    assert r["ok"] is True, r
    assert r["n_rows"] == 16, f"行数算错：{r['n_rows']}（应为 16）"


def test_完整跑通清洗(server):
    """真跑漏斗：脏数据进 → 干净数据出 + 每级统计。"""
    _make_corpus()
    r = _post(
        server + "/api/funnel",
        {"session": SESSION, "filename": "corpus.jsonl", "scenario": "text_article", "limit": 0},
    )
    assert r["ok"] is True and r.get("id"), r
    job = _wait_job(server, r["id"], timeout=600)
    assert job["status"] == "done", f"清洗失败：{job['error']} / {job['hint']}"
    res = job["result"]
    # 断言必须是**真的量**，不能只看 status == done
    assert res["n_input"] == 16, f"读入条数不对：{res['n_input']}（应为 16）"
    assert res["n_kept"] > 0, "全被丢了 —— 配方或输入有问题"
    assert res["n_dropped"] > 0, "一条没丢 —— 说明脏数据没被识别，判据可能恒真"
    assert res["n_kept"] + res["n_dropped"] == res["n_input"], "进出不闭合"
    by_op = {s["op"]: s["dropped"] for s in res["stages"]}
    # 太短的必须被 doc_length 抓到（语料里有 2 条短的）
    assert by_op.get("doc_length", 0) >= 2, f"太短的样本没被拦住：{by_op}"
    # 转载重复必须被 minhash 抓到（语料里有 2 条 dup）
    assert by_op.get("text_minhash", 0) >= 2, f"转载重复没被识别：{by_op}"
    # 产物文件必须在
    assert Path(res["cleaned_path"]).exists()
    assert Path(res["dropped_path"]).exists()
    # 清洗产物每行都必须是合法 JSON（前端预览要读它）
    with open(res["cleaned_path"], encoding="utf-8") as f:
        for line in f:
            if line.strip():
                json.loads(line)


def test_缺id的样本被自动补上而不是被丢掉(server):
    """`id` 是 Sample 的协议必填字段，但用户数据通常没有。

    不补的话症状是「读入条数少于文件行数」，用户完全看不出原因 ——
    实测栽过：24 条被静默跳过，界面显示 96 而不是 120。
    """
    d = backend.session_dir(SESSION)
    p = d / "noid.jsonl"
    texts = list(_NEWS[:6])
    p.write_text(
        "".join(json.dumps({"text": t}, ensure_ascii=False) + _NL for t in texts), encoding="utf-8"
    )
    r = _post(
        server + "/api/funnel",
        {"session": SESSION, "filename": "noid.jsonl", "scenario": "text_article", "limit": 0},
    )
    job = _wait_job(server, r["id"], timeout=600)
    assert job["status"] == "done", job["error"]
    assert job["result"]["n_input"] == len(texts), (
        f"丢了 {len(texts) - job['result']['n_input']} 条 —— 自动补 id 没生效"
    )
    assert any("id" in line for line in job["log"]), f"日志没说明自动补了 id：{job['log']}"


def test_预览接口返回真实内容(server):
    """前端「看几条」用的接口 —— 不能返回空数组糊弄。"""
    j = _get(f"{server}/api/preview?session={SESSION}&limit=5")
    assert j["ok"] is True
    assert j["rows"], "清洗后应该有内容可预览"
    assert all(isinstance(r, dict) and "text" in r for r in j["rows"])


def test_未知任务返回错误而不是挂(server):
    r = _get(server + "/api/job?id=nope")
    assert r["ok"] is False and "找不到" in r["error"]


def test_数据集接口在没清洗结果时拒绝(server):
    """顺序错了也要说清（不是崩）。"""
    r = _post(server + "/api/dataset", {"session": "no_such_session_xyz", "name": "x"})
    assert r["ok"] is False


def test_数据集名非法被拒(server):
    """数据集名会拼进文件系统路径 —— 不校验就是路径穿越。"""
    r = _post(server + "/api/dataset", {"session": SESSION, "name": "../evil"})
    assert r["ok"] is False
    assert "字母" in r["error"] or "-" in r["error"], r["error"]


# ── 工具函数 ─────────────────────────────────────────


def test_数据目录必须落在仓库根的data下():
    """⚠️ 回归：层级别数写错会把用户数据落进 **源码树**。

    本文件在 src/mm_curation/studio/ 下，仓库根是 parents[3]。
    写成 parents[2] 时 UPLOAD_ROOT 变成 `<repo>/src/data/studio` ——
    实测真的在那儿建了目录，要删掉才发现。
    这条断言直接把「不污染源码树」变成可核对的事实。
    """
    repo = ROOT.resolve()
    up = backend.UPLOAD_ROOT.resolve()
    assert up.is_relative_to(repo / "data"), f"数据目录跑到 {up} 去了 —— 必须在 {repo / 'data'} 下"
    assert not up.is_relative_to(repo / "src"), f"数据目录落在源码树里了：{up}"
    assert (repo / "src" / "mm_curation").is_dir()  # 确认层级没算错


def test_静态目录与模块同级():
    """静态资源必须和代码在一起 —— 打包/安装时才不会 404。"""
    from mm_curation.studio import serve

    assert serve.STATIC.is_dir(), "缺 static/ 目录"
    for f in ("index.html", "app.js", "style.css"):
        assert (serve.STATIC / f).exists(), f"缺 {f}"


def test_端口占用检查不能是恒真的():
    """⚠️ 回归：带 SO_REUSEADDR 的 bind 在 Windows 上**永远成功**。

    第一版端口自检就是这样的，结果「端口被占用」这条永远报不出来 ——
    起服务时才崩，而外行看到的是一段 traceback。
    这条测试真的占住一个端口，再让自检去报：
    """
    import socket
    import subprocess
    import sys

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # 故意**不设** SO_REUSEADDR，否则端口算不算占用就测不出来了
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        r = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(ROOT / "scripts" / "run_studio.py"),
                "--no-browser",
                "--port",
                str(busy),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=180,
        )
    out = r.stdout + r.stderr
    assert "已被占用" in out, "端口被占用却没报出来（可能自检恒真）：" + out[-600:]
    assert "换一个端口" in out, "报错没告诉用户怎么办"
    assert r.returncode != 0, "端口冲突时应该非0 退出，别让用户以为起来了"


def test_预检在可用端口上通过():
    """反向对照：没被占用时自检必须放行（否则上一条就是个永远红的假门禁）。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_studio", ROOT / "scripts" / "run_studio.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._preflight(free)  # 不抛异常即通过


def test_会话id拒绝路径穿越():
    for bad in ("../x", "a/b", "..", "a\\b", ""):
        with pytest.raises(backend.StudioError):
            backend.session_dir(bad)


def test_会话id接受正常值():
    d = backend.session_dir("abc-123_XYZ")
    assert d.exists()


def test_上传白名单只放行允许的后缀(tmp_path):
    from mm_curation.studio.backend import ALLOWED_SUFFIX
    from mm_curation.studio.serve import _SAFE_NAME

    assert _SAFE_NAME.match("corpus.jsonl")
    assert not _SAFE_NAME.match("../x.jsonl")
    for good in ("a.jsonl", "b.csv", "c.tsv", "数据.jsonl"):
        assert _SAFE_NAME.match(good) and Path(good).suffix.lower() in ALLOWED_SUFFIX
    # 可执行后缀必须在白名单之外
    for bad in ("x.exe", "x.sh", "x.py"):
        assert Path(bad).suffix.lower() not in ALLOWED_SUFFIX
