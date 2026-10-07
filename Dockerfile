FROM python:3.11.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=Europe/Bucharest

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl libexpat1 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock.txt /app/
RUN python -m pip install --no-cache-dir --upgrade pip==24.3.1 \
    && python -m pip install --no-cache-dir -r /app/requirements.lock.txt

COPY src /app/src
COPY data /app/data
COPY README.md /app/README.md

RUN mkdir -p /app/outputs /app/cache \
    && useradd --create-home --uid 10001 csi \
    && chown -R csi:csi /app

USER csi

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
