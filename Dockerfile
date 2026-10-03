FROM python:3.13-slim
WORKDIR /app
# 项目无 OpenViking/DeepRead 或宿主机编译工具链依赖。
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && mkdir -p /app/data /opt/tiktoken-cache
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN .venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
EXPOSE 8000
CMD [".venv/bin/plain-memory", "--config", "/app/config.local.yaml", "serve", "--host", "0.0.0.0"]
