# syntax=docker/dockerfile:1
# =============================================================================
# mm-curation 应用镜像（服务 / 湖仓侧，CPU-only，多阶段构建）
#
# 这个容器只做三件事：**读湖上的 Parquet、查数仓、响应 HTTP**。
# 因此它不装 transformers / peft / trl / datasets / streamlit / faiss ——
# 那些是宿主机 venv 的事（算力分级是本项目的既有设计，见 docs/ROADMAP.md）。
# 依赖范围见 requirements-app.txt，精确版本见 requirements.lock。
#
# 两阶段的收益是具体的：编译工具链、wheel 缓存、pip 自身的下载产物
# 都留在 builder，runtime 只拿到一个 /opt/venv。
#
# ⚠️ 诚实边界：**本机无法验证这个构建**。本机 Docker 拉不到
# `registry-1.docker.io`（"Docker Desktop has no HTTPS proxy"），
# 所以 `docker build` 只能在 CI 的 runner 上跑（.github/workflows/container-ci.yml）。
# 这条限制记在 docs/DATA_SYSTEM_TRACK.md 的诚实边界一节，不藏着。
# 也正因为它不能本地预演，"构建与运行"这一步在 CI 里是**真门禁**，
# 不允许 continue-on-error —— 那种写法等于把未验证当成通过。
# =============================================================================

FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# 装的是**锁文件**而不是 requirements-app.txt：
# 范围表达式会让"同一个 Dockerfile 在不同日期构建出不同内容的镜像"，
# 而"镜像可复现"正是引入 lock 的唯一目的。
COPY requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir -r /build/requirements.lock

# 本地协议包 `curation-eval`。**必须装**：平台链路真的依赖它
# （`mm_curation.operators/**` 建立在它的协议之上；实测把它的 import 挡掉后
#   跑 `mm_curation.cli run` 会直接 ImportError）。
# `--no-deps`：它的依赖（pillow）已经由上面的锁文件钉住，这里不再让它自己解析一遍
# ——两个解析器各自决定版本，正是锁存在的意义要消灭的东西。
# 用**普通安装**而不是 `-e`：editable 会在 venv 里留下指向 /build 的路径，
# 而 /build 不进 runtime 阶段，结果是"构建成功、运行时报找不到模块"。
COPY packages/curation-eval/ /build/curation-eval/
RUN pip install --no-cache-dir --no-deps /build/curation-eval


FROM python:3.11-slim AS runtime

# PYTHONUNBUFFERED：不设的话容器崩溃前最后几行日志会随缓冲丢掉，
# 而那几行往往正是原因。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# 只搬运行时真正要用的三样：源码、配置、人类入口。
# 不搬 tests/（进镜像只会变大，还会让人以为容器里能跑测试）。
COPY src/ /app/src/
COPY configs/ /app/configs/
COPY scripts/ /app/scripts/

# 非 root 运行。目录先建好并交给 appuser —— 否则第一次写 /app/data 会在
# **运行**阶段（而不是构建阶段）权限失败，报错点离原因很远。
#
# ⚠️ 挂宿主机卷时要注意 uid：本机用户通常是 uid 1000，而这里是 10001。
# 服务只读数据，但 DuckDB 以读写方式打开库文件（台账要 CREATE TABLE IF NOT EXISTS）。
# 若挂载后报 "Could not set lock on file"，把宿主机的 data/envs 目录
# chown 给 10001，或者按 docs/PLATFORM.md 的说明用 rootless 挂载方案。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/runs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# 探针打 `/healthz` 而不是 `/api/health`：
# `/api/health` 的语义是"进程活着、把状态如实报出来"（永远 200）；
# 探针要的是"**契约闸门过了、真能供数**"（不就绪返回 503）。
# 用 python 标准库而不是 curl —— slim 镜像里没有 curl，
# 为一个探针去装它，是在给攻击面做加法。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

# 环境显式写死：容器服务的是 **prod store**（data/envs/prod/），
# 而源与配置读的是仓库根（/app）。这两者刻意分开，见 platform/envs 模块文档。
CMD ["python", "-m", "mm_curation.cli", "serve", \
     "--host", "0.0.0.0", "--port", "8080", "--env", "prod"]
