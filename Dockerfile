# syntax=docker/dockerfile:1.7

# ==================== 构建阶段：安装依赖 ====================
# 不要用 ghcr.io/astral-sh/uv 作为构建基底：uv 镜像从某个版本起不再发布
# "版本号-python3.13-..." 形式的 tag（实测 0.12.9-python3.13-bookworm-slim 返回 404），
# 带 python 的滚动 tag 又会随时间漂移。这里改为官方 python 镜像 + pip 安装固定版本的 uv。
FROM python:3.13-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# UV_VERSION 要与 uv.lock 兼容（本地可以用 uv lock --check 验证）
ARG UV_VERSION=0.11.16
# PyPI 镜像：装 uv 自身和装项目依赖都走它，否则从 pypi.org 拉会非常慢
# 换别的源可以构建时覆盖：--build-arg PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_INDEX=https://mirrors.aliyun.com/pypi/simple/

RUN pip install --no-cache-dir -i ${PYPI_INDEX} "uv==${UV_VERSION}"

WORKDIR /app

# 先复制依赖清单，利用镜像层缓存
# 这里故意不复制 src：安装依赖用不到源码（源码在运行阶段从构建上下文复制），
# 这样以后只改代码不会让依赖层缓存失效，重新构建会快很多
COPY pyproject.toml uv.lock .python-version ./

# 这里不能用 --frozen：一旦把索引源换成镜像，uv 会认为 uv.lock 需要更新，
# 加 --frozen 会直接报 "The lockfile at uv.lock needs to be updated"。
# 不带 --frozen 时 uv 仍优先使用锁文件里已固定的版本，只是把下载源换成镜像。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project --default-index ${PYPI_INDEX}


# ==================== 运行阶段：只保留运行时需要的内容 ====================
FROM python:3.13-slim-bookworm

# PYTHONPATH 必须包含 /app/src：项目里存在 `import app.xxx` 形式的绝对导入，
# 而构建时用了 --no-install-project，不设置的话容器启动就会 ImportError。
# WEB_CONCURRENCY 是工作进程数，PORT 是监听端口，两者都可以在 docker-compose.yml 中覆盖。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:/app/src" \
    WEB_CONCURRENCY=2 \
    PORT=8000

WORKDIR /app

# 以非 root 用户运行，并提前建好日志与上传目录，避免运行时权限不足
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/storage/uploads /app/storage/documents /app/src/app/logs \
    && chown -R appuser:appuser /app

COPY --chown=appuser:appuser --from=builder /app/.venv /app/.venv

# alembic.ini 必须一起复制，否则容器内执行不了 alembic 迁移
COPY --chown=appuser:appuser pyproject.toml uv.lock .python-version alembic.ini ./
COPY --chown=appuser:appuser src ./src

USER appuser

EXPOSE 8000

# 健康检查用标准库实现，镜像里不需要额外装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${PORT:-8000}/api/v1/health',timeout=3).status==200 else 1)"

# 用 uvicorn 自带的多进程模式启动，原因：
# 1) 不依赖已标记废弃的 uvicorn.workers.UvicornWorker
# 2) 原生支持 --proxy-headers，配合 nginx 才能拿到真实客户端 IP
# 3) 没有 gunicorn 默认 30 秒的 worker 超时，/api/v1/qweather/chat 的 SSE 长连接不会被掐断
CMD ["sh", "-c", "exec uvicorn src.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='*'"]
