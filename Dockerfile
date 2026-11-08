# 시뮬레이터와 수집 서버가 같은 이미지를 쓴다. 어느 프로세스를 띄울지는 docker-compose 의 command 로 정한다.
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.29 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY common ./common
COPY simulator ./simulator
COPY collector ./collector
COPY config ./config
ENV PATH="/app/.venv/bin:$PATH"
