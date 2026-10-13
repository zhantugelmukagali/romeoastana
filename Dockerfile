FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend PATH=/opt/venv/bin:$PATH
WORKDIR /app
RUN python -m venv /opt/venv
COPY requirements-server.lock ./
RUN pip install --no-cache-dir -r requirements-server.lock && pip check
RUN groupadd --gid 10001 bonuspro && useradd --uid 10001 --gid 10001 --no-create-home bonuspro
COPY backend ./backend
COPY web ./web
COPY migrations ./migrations
COPY alembic.ini ./
COPY deploy ./deploy
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python","deploy/healthcheck.py"]
CMD ["uvicorn","bonuspro.api.app:create_app","--factory","--host","0.0.0.0","--port","8000","--no-access-log","--no-proxy-headers","--limit-concurrency","32","--timeout-keep-alive","5"]
