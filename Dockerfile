FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --system --uid 10001 --no-create-home app && chown -R app /app
USER app

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=4).status == 200 else 1)"

# WEB_CONCURRENCY sets the worker count (default 2). Behind Caddy, which sets X-Forwarded-For.
CMD ["sh", "-c", "exec gunicorn server:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:8001 --timeout 120 --graceful-timeout 30 --keep-alive 5 --access-logfile - --forwarded-allow-ips='*'"]
