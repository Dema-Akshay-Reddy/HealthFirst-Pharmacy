# Smart Pharmacy Inventory Management System
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + frontend
COPY app.py .
COPY pharmacy/ ./pharmacy/
COPY static/ ./static/

# Raw dataset feed so a fresh container can self-seed. Kept OUTSIDE /app/data:
# a volume mounted on /app/data would otherwise shadow the dataset.
COPY data/zenith/ ./dataset/
ENV PHARMACY_DATASET_DIR=/app/dataset

# SQLite state lives in the volume so data survives container upgrades
RUN useradd --uid 10001 --create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data
USER appuser
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "app.py"]
