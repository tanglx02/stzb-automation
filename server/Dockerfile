# 后端控制台镜像。整个镜像只有 app/ 与依赖，没有任何采集脚本相关的东西。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STZB_DATA_DIR=/data

WORKDIR /srv

# 依赖单独一层，改代码不会触发重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据目录（数据库 + 上传的报告与截图）。compose 里会挂卷出来。
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 健康检查：/healthz 是公开的，不需要登录
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

# 只监听容器内网卡，对外由 Caddy 反代。workers=1：
# SQLite 单写者，多 worker 反而容易锁等待。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "1"]
