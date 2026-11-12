FROM python:3.13-slim

WORKDIR /app

# ── 系统依赖 ──
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python 依赖 ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 项目源码 ──
COPY . .

# ── 运行时目录结构 ──
RUN mkdir -p /app/.claw/sessions /app/.claw/memory /app/.claw/data

# ── 导出端口 ──
EXPOSE 8080

# ── 入口：Gateway 模式（docker-compose 可覆盖 CMD） ──
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--gateway", "--port", "8080"]
