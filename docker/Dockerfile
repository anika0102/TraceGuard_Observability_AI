# Build from the repository root:  docker build -f docker/Dockerfile -t traceguard:0.1.0 .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Numeric UID so Kubernetes `runAsNonRoot` can verify it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin traceguard
USER 10001

EXPOSE 8000
# Single worker on purpose: incident-detection window and fault state are in-process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
