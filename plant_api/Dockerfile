FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY plant_api/requirements.txt /app/plant_api/requirements.txt
RUN pip install --no-cache-dir -r /app/plant_api/requirements.txt

COPY plant_api /app/plant_api
COPY data/sync_baseline.json /app/data/sync_baseline.json
COPY data/plants.json data/photos.json /app/data/

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["sh", "-c", "uvicorn plant_api.app:app --host 0.0.0.0 --port ${PORT}"]
