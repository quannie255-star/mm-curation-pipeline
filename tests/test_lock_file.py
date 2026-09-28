"""交付物结构门（S6）+ 仓库卫生：`requirements.lock`、`docker/Dockerfile.app`、
以及 `.gitignore` 对 `data/raw/` 的覆盖面的**可核对性**。

这组测试**不联网、不跑 docker**。它拦的是那类"看起来做了交付、其实对不上"的
问题——它们的共同特征是**没有任何命令会失败**：

- 锁文件里混进一条 `>=`（于是它不再是锁，而是第二份 requirements）
- 新增了直接依赖却忘了重新生成锁（于是镜像里少了那个包，构建期才发现）
- Dockerfile 里 `COPY` 了一个不存在的路径（构建总是失败，但那要等 CI 跑完）
- Dockerfile 用的是 `requirements.txt`（把训练栈拖进服务镜像）
- `.gitignore` 用逐条列举覆盖 `data/raw/`，于是新出现的子目录漏网
  （`git status` 里多一个 `??`，没人会发现，直到有人 `git add -A`）

容器的**构建与运行**只能在 CI 的 runner 上验（本机拉不到 `registry-1.docker.io`，
这一条已记在 docs/DATA_SYSTEM_TRACK.md 的诚实边界里）。所以这里只做
"结构上对得上"这一层，并明确写清它不验什么。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

packaging_requirements = pytest.importorskip("packaging.requirements")

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "requirements.lock"
APP_REQ = REPO / "requirements-app.txt"
DOCKERFILE = REPO / "docker" / "Dockerfile.app"
COMPOSE = REPO / "docker-compose.yaml"

PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s=<>!~;]+)$")


def _lock_entries() -> list[dict]:
    out = []
    for i, line in enumerate(LOCK.read_text(encoding="utf-8").splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        m = PIN_RE.match(raw)
        out.append({"raw": raw, "line": line, "lineno": i, "name": m.group("name") if m else ""})
    return out


def _direct_requirements() -> list[str]:
    names = []
    for line in APP_REQ.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        names.append(packaging_requirements.Requirement(s).name.lower())
    return names


def test_lock_every_line_is_an_exact_pin():
    """锁文件里不许出现 `>=` / `~=` / `!=` / 环境标记。

    允许范围表达式等于允许"锁"在别人机器上解析出别的版本——
    那它就不再是锁，而是第二份 requirements，而且更难看出这件事。
    """
    entries = _lock_entries()
    assert entries, "锁文件不该是空的"
    for e in entries:
        assert e["name"], f"requirements.lock:{e['lineno']} 不是精确 pin：{e['line']!r}"
        spec = packaging_requirements.Requirement(e["raw"]).specifier
        ops = {s.operator for s in spec}
        assert ops == {"=="}, f"{e['raw']!r} 的约束不是纯 ==，而是 {ops}"


def test_lock_covers_every_direct_requirement():
    """改了 `requirements-app.txt` 就必须重新生成锁。

    这条是"忘记重新生成"的唯一自动发现手段：漏了包，镜像里就没有它，
    而失败会发生在容器启动那一刻（甚至更晚，取决于 import 是否惰性）。
    """
    pinned = {e["name"].lower() for e in _lock_entries()}
    missing = [n for n in _direct_requirements() if n not in pinned]
    assert not missing, f"这些直接依赖没进锁：{missing}（重跑 scripts/gen_lock.py）"


def test_lock_carries_the_transitive_closure_not_just_direct_deps():
    """闭包必须真的在：`uvicorn[standard]` 的 extras 一个都不能少。

    extras 是最容易漏的一类——`pip install uvicorn` 和 `uvicorn[standard]`
    解析出的包集不同，而漏掉 websockets/watchfiles 只会在**运行时**、
    且只在用到对应协议时才炸。
    """
    pinned = {e["name"].lower() for e in _lock_entries()}
    assert {"websockets", "watchfiles", "httptools", "python-dotenv"} <= pinned
    assert {"starlette", "pydantic", "anyio", "h11", "click"} <= pinned
    assert len(pinned) > len(_direct_requirements()), "闭包应当**严格多于**直接依赖"


def test_lock_is_lf_only_so_it_can_be_diffed_across_platforms():
    """生成物要跨平台比对，写入侧就必须钉死 LF（同 Airflow DAG 那条坑）。"""
    assert b"\r\n" not in LOCK.read_bytes()


# ---------------------------------------------------------------------------
# Dockerfile：只做结构核对，**不构建**
# ---------------------------------------------------------------------------


def test_dockerfile_exists_and_is_multi_stage():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count("FROM ") >= 2, "应用镜像要求多阶段构建（builder / runtime 分离）"
    assert "requirements.lock" in text, "镜像必须装锁文件，而不是 requirements-app.txt"
    assert "requirements.txt" not in text, (
        "不许把 requirements.txt 拖进服务镜像——它含 transformers/peft/trl/streamlit，"
        "会把训练栈带进一个只需要出数的容器"
    )


def test_dockerfile_runs_the_service_entrypoint_and_probes_healthz():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "mm_curation.cli" in text, "容器入口必须是平台轨 CLI"
    assert "serve" in text and "--env" in text, "入口应当显式指定环境（默认 prod）"
    assert "HEALTHCHECK" in text and "/healthz" in text, (
        "探针必须打 /healthz（能供数才算健康），而不是 /api/health（永远 200）"
    )
    assert "USER " in text, "不许用 root 跑服务"
    assert "0.0.0.0" in text, "容器内必须绑定 0.0.0.0，否则端口映射不通"


def test_dockerfile_copy_sources_exist_in_the_repo():
    """`COPY` 的**构建上下文源路径**必须真实存在。

    （`--from=<stage>` 与容器内绝对路径不是构建上下文路径，跳过。）
    否则构建必失败，而那要等到 CI 才知道——本机预演不了 `docker build`。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    srcs = re.findall(r"^COPY\s+(?:--from=\S+\s+)?(\S+)", text, re.M)
    assert srcs, "Dockerfile 里应当有 COPY"
    checked = 0
    for s in srcs:
        if s.startswith("/") or s.startswith("--from"):
            continue
        assert (REPO / s).exists(), f"Dockerfile COPY 的构建上下文源不存在：{s}"
        checked += 1
    assert checked >= 3, f"应当核对到至少 3 个源（锁文件 + src + configs），实际 {checked}"


def test_compose_exposes_the_app_service_with_ports_and_data_volume():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "docker/Dockerfile.app" in text, "compose 应当把应用容器加进去"
    assert "./data:/app/data" in text, "应用容器必须挂到 data（prod store 在里面）"
    assert "8081:8080" in text, "应用端口不该和 Airflow webserver 抢 8080"


# ---------------------------------------------------------------------------
# 仓库卫生：`data/raw/` 下的产物不许漏进索引（**不联网、不跑 docker**）
# ---------------------------------------------------------------------------


def _ignored_by_repo_gitignore(rels: list[str]) -> set[str]:
    """把本仓库的 `.gitignore` **单独**放进一个全新临时仓库再判定。

    为什么不直接在仓库里跑 `git check-ignore`——那会引入两个与规则无关的变量，
    而它们都能让断言在"规则已经坏掉"时依然通过（门禁失明）：

    1. `core.excludesFile` / `.git/info/exclude` 会额外忽略一些路径；
    2. **git 的 untracked cache 会缓存历史判定**。2026-09-27 实测：把
       `data/raw/*/` 换回旧式逐条列举后，**盘上已存在**的 `data/raw/fhir_synth/`
       仍被判 IGNORED，而**不存在**的目录名判 NOT-IGNORED —— 变量是
       "这个目录在不在盘上"，不是规则。这一条差点让本节的门禁变成假绿。

    临时仓库里没有 index、没有全局 exclude，判定就只可能来自这一份文件。
    """
    try:
        gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - 文件在仓库里，正常不会发生
        pytest.skip(".gitignore 读不到")
    if shutil.which("git") is None:
        pytest.skip("git 不可用")

    with tempfile.TemporaryDirectory(prefix="mmc-gitignore-") as td:
        root = Path(td)
        init = subprocess.run(["git", "init", "-q", "."], cwd=root, capture_output=True)
        if init.returncode != 0:
            pytest.skip("git init 失败")
        (root / ".gitignore").write_text(gitignore, encoding="utf-8", newline="\n")
        ignored: set[str] = set()
        for rel in rels:
            # 逐个 -q 而不是 --stdin：Windows 上 subprocess 的文本模式会把 `\n`
            # 翻成 `\r\n`，git 于是把路径连 `\r` 一起读进去再带引号回显，
            # 解析出来的名字就永远匹配不上（本次实现里真的踩到过）。
            proc = subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.excludesFile={root / 'NO_SUCH_GLOBAL_IGNORE'}",
                    "check-ignore",
                    "-q",
                    "--no-index",
                    rel,
                ],
                cwd=root,
                capture_output=True,
            )
            if proc.returncode == 0:
                ignored.add(rel)
            elif proc.returncode not in (0, 1):
                pytest.skip(f"git check-ignore 意外退出码 {proc.returncode}")
    return ignored


def test_raw_data_tree_is_ignored_regardless_of_depth_or_directory_name():
    """`data/raw/` 下**除 .gitkeep 外全是产物** → 必须整棵被忽略。

    2026-09-27 实点：旧 `.gitignore` 逐条列举 images/html/real/text_cache/.cache，
    而 `data/raw/*.jsonl` 只匹配**一层**，于是当时新出现的 `fhir_synth/`（44 KB）
    与 `finance_news/`（136 KB）谁都没覆盖 → `git status` 里以 `??` 出现 →
    任何人 `git add -A` 都会把它们暂存。这类缺陷**没有任何命令会失败**，
    "名单漏了新目录"是唯一征兆。所以除了已知目录，这里还断言一个**当前不存在**
    的目录名——那一条才是对"名单必然腐烂"这个根因的回归锁。
    """
    rels = [
        "data/raw/real/probe.bin",
        "data/raw/images/probe.bin",
        "data/raw/html/probe.bin",
        "data/raw/text_cache/probe.bin",
        "data/raw/.cache/probe.bin",
        "data/raw/fhir_synth/probe.bin",
        "data/raw/finance_news/probe.bin",
        "data/raw/news_corpus.jsonl",
        "data/raw/manifest.json",
        "data/raw/deep/nested/probe.parquet",
    ]
    ignored = _ignored_by_repo_gitignore(rels)
    missed = [r for r in rels if r not in ignored]
    assert not missed, f"这些原始数据/产物路径没被 .gitignore 覆盖：{missed}"


def test_new_source_subdirectory_is_ignored_without_touching_gitignore():
    """**根因锁**：往 `data/raw/` 加一个新子目录，不需要动 `.gitignore` 就该被忽略。

    与上一条分开写，是因为这条才是"逐条列举的名单必然会被新目录追上"的
    直接回归锁——它必须独立失败、独立可读。
    """
    probe = "data/raw/some_future_source_2030/deep/probe.jsonl"
    ignored = _ignored_by_repo_gitignore([probe])
    assert probe in ignored, (
        "data/raw/ 下的新子目录必须**自动**被忽略——否则新采集的语料会以 "
        "`??` 出现在 git status 里，直到有人 git add -A 把它提交上去"
    )


def test_gitkeep_stays_trackable_so_data_raw_survives_a_clone():
    """`data/raw/.gitkeep` 必须**不被**忽略。

    它是"空目录在 clone 后仍然存在"的唯一依据；把 `data/raw/` 整棵吃掉会连它
    一起吃掉，而症状是**在别人的机器上**才出现的（目录不存在 → 脚本 mkdir 才补）。
    """
    rel = "data/raw/.gitkeep"
    ignored = _ignored_by_repo_gitignore([rel])
    assert rel not in ignored, "覆盖 data/raw/ 子目录的模式不该把 .gitkeep 一起吃掉"
