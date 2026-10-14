# --- Build stage ---
FROM python:3.13-slim-bookworm AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUSERBASE=/install

COPY infra/requirements/prod.txt infra/requirements/base.txt ./requirements/
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --user -r requirements/prod.txt

# Compiled catalogs are build output (.gitignore, .dockerignore): the image
# compiles its own, with the same flags as `make i18n-compile`.
COPY locales ./locales
RUN /install/bin/pybabel compile -d locales -D messages --statistics

# --- Final stage ---
FROM python:3.13-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUSERBASE=/install \
    PATH="/install/bin:$PATH" \
    PYTHONPATH="/install/lib/python3.13/site-packages"

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && groupadd --system appuser \
    && useradd --system --gid appuser --create-home --home-dir /home/appuser appuser \
    && chown appuser:appuser /app

COPY --from=builder --chown=appuser:appuser /install /install
COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser loggers ./loggers
COPY --chown=appuser:appuser models ./models
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser alembic.ini ./
COPY --from=builder --chown=appuser:appuser /app/locales ./locales

USER appuser

CMD ["python", "-m", "src.main"]
