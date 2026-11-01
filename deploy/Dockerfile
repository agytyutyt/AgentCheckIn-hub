# syntax=docker/dockerfile:1
FROM python:3.12-slim

# 国内服务器拉 pip 包慢，默认走腾讯云镜像；可用 --build-arg 覆盖
ARG PIP_INDEX=https://mirrors.cloud.tencent.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.cloud.tencent.com

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    TZ=Asia/Shanghai \
    HOME=/home/appuser \
    CHECKIN_DATA_DIR=/app/data

WORKDIR /app

# 不跑 apt：国内机器访问 deb.debian.org 经常超时（首次构建实测失败过）。
# 时区由 PyPI 的 tzdata 提供；降权用基础镜像自带的 setpriv。
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX} --trusted-host ${PIP_TRUSTED_HOST} -r requirements.txt

COPY app ./app
COPY tools ./tools
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd -m -u 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app /home/appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.serve"]
