FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app:/app/shared \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# System libs chromium needs at runtime. We pin the list explicitly instead
# of relying on `playwright install --with-deps`, which in 1.47 tries to
# install ttf-unifont/ttf-ubuntu-font-family — packages that no longer
# exist in Debian 12 slim, breaking the build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
        libxfixes3 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
        fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY worker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && \
    playwright install chromium

COPY shared /app/shared
COPY worker /app/worker

WORKDIR /app/worker

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, sqlalchemy as sa; e = sa.create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True); c = e.connect(); c.close()" || exit 1

CMD ["python", "-m", "app.runner"]
