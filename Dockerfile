# PACE 运行镜像：只用 Python 标准库，没有 pip install
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PACE_HOST=0.0.0.0 \
    PACE_TRUST_PROXY=1 \
    PACE_QUIET=1

WORKDIR /app
COPY run.py ./
COPY pace ./pace
COPY tools ./tools
COPY web ./web
COPY sample-data ./sample-data

RUN useradd -m -u 10001 pace && mkdir -p /data && chown -R pace:pace /app /data
USER pace

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"

CMD ["python", "run.py", "--no-open", "--host", "0.0.0.0", "--port", "8765", "--db", "/data/pace.db"]
