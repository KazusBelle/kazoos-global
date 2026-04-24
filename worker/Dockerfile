FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app:/app/shared

WORKDIR /app

COPY worker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY shared /app/shared
COPY worker /app/worker

WORKDIR /app/worker

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, sqlalchemy as sa; e = sa.create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True); c = e.connect(); c.close()" || exit 1

CMD ["python", "-m", "app.runner"]
