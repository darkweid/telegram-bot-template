# --- Build stage ---
FROM python:3.13-slim-bookworm AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUSERBASE=/install

# The dev image carries the test and lint tools plus watchfiles.
COPY infra/requirements/dev.txt infra/requirements/base.txt ./requirements/
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --user -r requirements/dev.txt

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
# The dev stack mounts the working tree over /app, hiding anything compiled
# into the image, so no catalogs are compiled here; `make run-dev` does it.
COPY --chown=appuser:appuser . .

USER appuser

CMD ["python", "-m", "src.main"]
