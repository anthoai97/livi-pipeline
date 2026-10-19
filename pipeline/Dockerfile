# Build from the repository root: the service imports product-data/src.
FROM python:3.13-slim
WORKDIR /app
COPY pipeline/requirements.txt pipeline/requirements.txt
RUN pip install --no-cache-dir -r pipeline/requirements.txt
COPY product-data/src product-data/src
COPY pipeline pipeline
WORKDIR /app/pipeline
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
